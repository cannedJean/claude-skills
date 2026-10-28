#!/usr/bin/env python3
"""slide_analyze.py - analyse a folder of lecture slide images with agy, one slide per session, and verify
the result WITHOUT a second look at the image by the same vendor.

Pipeline per slide (the image is opened by agy and by local, non-LLM tools only):
  0. reference : ocr_ref.py  - local OCR (winocr / tesseract) + layout stats, sealed with the image sha256
                 BEFORE the analysis exists (-> <out>/ref/<stem>.json). `run` builds the reference of every
                 slide of the deck before its first analyst call; lines that recur at the same place on 5+
                 slides (the deck template) are left out of the comparison.
  1. analyst   : agy + prompts/analyst.md (persona, deck context, CoT, few-shot) -> slide_analysis.json
  2. validator : slide_validate.py, deterministic schema / grounding / leak checks
  3. scorer    : ocr_score.py compares the analysis with the sealed reference -> decision
                   accept    reference agrees: done, no reviewer tokens
                   retry     clear omission or validator error: analyst re-run once, the feedback names
                             the OCR lines that are missing
                   escalate  disagreement the text cannot settle: crops of the disputed lines (and/or a
                             640px thumbnail) are queued for Claude, blind
  4. audit     : a deterministic sample of OCR-accepted slides gets an independent blind thumbnail read by
                 Claude so the run can state its residual error rate. Shadow mode: every record also carries
                 `score_legacy` (the first-release scorer's decision, never used for the status), and a share
                 (--shadow-audit-rate, default 0.3; 0 turns it off) of the slides only the new rules accept is
                 audited as a stratum of its own
  5. resolve   : Claude's answers (crop_reads*.json) are compared with the analysis -> accepted / needs_attention

  preflight                       check agy, the agy-delegate skill and the OCR engines
  reference --slides DIR          build/refresh the sealed references only (no agy); reports built / kept / unchanged
  run --slides DIR [options]      analyse (resumable; finished slides are skipped)
  resolve --slides DIR            apply crop_reads*.json / grounding_reads.json, rebuild the report
  queue --slides DIR --shards N   rebuild the report and split the review queue into N disjoint files
  rescore --slides DIR            re-run validator + scorer on stored analyses (no agy), e.g. after calibrate
  report --slides DIR             rebuild all_slides.json + report.md + the review queue from results
  calibrate --slides DIR --gold DIR   fit thresholds on human-corrected gold JSONs -> calibration.json
  bench --slides DIR --gold DIR       score a run against the gold set (prompt / model A/B)

Outputs in --out (default <slides>/../slide_analysis):
  results/<stem>.json   {page, file, sha256, status, decision, attempts, analysis, validation, score, queue, judgments}
  ref/<stem>.json       sealed OCR reference      crops/*.png  what Claude may look at
  review_queue.md/json  Claude's blind work list  (review_queue_<k>.md after `queue --shards`)
  crop_reads.json       Claude's answers {id: answer} (shards: crop_reads_<k>.json; {id: {"answer", "fp"}} also
                        accepted); applied_reads.json records which queue entry each answer was judged against,
                        and `run` / `rescore` record answers already on disk as belonging to the entries they replace
  all_slides.json       list of analyses with a `_qa` block each (always the whole deck)
  report.md             one row per slide + budget / audit summary
  slides_source.json    the deck manifest (folder, sha256 per image); an --out holding another deck is refused

Units that recur in the header/footer items of 3+ slides of the deck (the logo, slogan, section band) are left out
of the unsupported ratio. `run` scores each slide against the analyses on disk at that moment, then settles: once the
analyst pool ends, every slide of the deck that holds a scored analysis and no judgments is re-scored (no agy)
against the whole deck, so `run` (also a resumed or partial one) and `rescore` decide alike.

Statuses: accepted, escalated (waiting for resolve), needs_attention, failed, unverified (no reference and
nothing to show), plus ok / unreviewed in the legacy `--reviewer agy` mode.
Resume: re-running skips everything except `failed` (and `unreviewed`) and slides whose image changed;
`--force` redoes the selection. On agy RESOURCE_EXHAUSTED the whole run stops and exits 3; queued slides stay
pending (an analysis already scored before the cut-off is kept).
With a single OCR engine, a second upscaled pass of it is added (OCR_SCALES, default "1,2").

Exit codes: 0 all accepted, 2 some slides escalated / need attention / failed, 3 agy quota exhausted,
4 preflight failure, 5 bad arguments (also: --out holds another deck, conflicting answers).
Python 3.9+. Requires the agy-delegate skill (scripts/agy_task.py) and Pillow; numpy recommended.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crops as cropmod  # noqa: E402
import ocr_ref  # noqa: E402
import ocr_score  # noqa: E402
from ocr_score import corpus_text, feedback_text, gold_metrics, load_thresholds, score, suggest_thresholds  # noqa: E402
from slide_validate import cross_check, validate_analysis  # noqa: E402
from textnorm import units  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
PROMPTS = SKILL_DIR / "prompts"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
MAX_ATTEMPTS = 2
DEFAULT_SUBJECT = "해당"
DEFAULT_CONTEXT = "강의 슬라이드 묶음을 페이지별 이미지로 변환한 것입니다."
QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|quota reached|\(code 429\)", re.I)
PENDING = {"failed", "unreviewed"}  # statuses a plain re-run picks up again
FINAL_OK = {"accepted", "ok"}
DECK_MANIFEST = "slides_source.json"  # not source.json: lecture-knowledge-pack reads that name as an agy-pdf-analyzer folder
LEDGER = "applied_reads.json"  # {id: {"fp", "sha"}}: the queue entry each answer was judged against
READS_RE = re.compile(r"crop_reads(?:_[\w-]+)?\.json")
TOTAL_TOKENS_PER_ITEM = 2000  # measured: whole reviewer-agent context per queue item, not image tokens only
SCORER_VERSION = 2  # 2: deck-template / variant-OCR aware score(); written into calibration.json
SHADOW_AUDIT_RATE = 0.3  # rollout check: audit share of the slides only the new rules accept
_lock = threading.Lock()
STOP = threading.Event()  # set on quota exhaustion: running slides finish their call, queued ones are skipped


class QuotaExhausted(Exception):
    """agy reported RESOURCE_EXHAUSTED; every further call would fail the same way."""


def log(msg):
    with _lock:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()


def find_agy_task() -> Path:
    cands = [os.environ.get("AGY_TASK"),
             SKILL_DIR.parent / "agy-delegate" / "scripts" / "agy_task.py",
             Path.home() / ".claude" / "skills" / "agy-delegate" / "scripts" / "agy_task.py"]
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    raise FileNotFoundError("agy-delegate skill not found; install it next to this skill or set AGY_TASK")


def list_slides(slides: Path) -> list:
    return sorted(p for p in slides.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT)


def parse_pages(spec, total):
    if not spec:
        return list(range(1, total + 1))
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return [p for p in out if 1 <= p <= total]


def load_json(path: Path):
    text = path.read_text(encoding="utf-8-sig").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def read_json(path: Path, default=None):
    """Parsed JSON, or default when the file is missing or unreadable (truncated, not UTF-8)."""
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (ValueError, OSError):
        return default


def quota_message(workdir: Path, cp) -> str | None:
    """Return agy's quota error line if this run hit RESOURCE_EXHAUSTED (checked in agy_task output and agy stderr)."""
    texts = [cp.stdout.decode("utf-8", "replace"), cp.stderr.decode("utf-8", "replace")]
    texts += [f.read_text(encoding="utf-8", errors="replace") for f in workdir.glob(".agy_task/*/stderr.txt")]
    for t in texts:
        for line in t.splitlines():
            if QUOTA_RE.search(line):
                m = re.search(r"Resets in [^.\"]+", line)
                return m.group(0) if m else line.strip()[:200]
    return None


def call_agy(agy_task: Path, workdir: Path, prompt: str, out_name: str, stage: list, a):
    """One agy session in a fresh workdir. Returns (parsed_json | None, error | None).
    Raises QuotaExhausted when agy is out of quota.
    agy's stdout is not trusted for text (console code page on Windows); only the UTF-8 file it writes is read."""
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    for src, name in stage:
        shutil.copy2(src, workdir / name)
    task_file = workdir.parent / f"{workdir.name}.prompt.md"
    task_file.write_text(prompt, encoding="utf-8")
    cmd = [sys.executable, str(agy_task)]
    if a.model:
        cmd += ["--model", a.model]
    cmd += ["run", "-w", str(workdir), "-f", str(task_file), "--timeout", f"{a.timeout}m", "--no-schema"]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        cp = subprocess.run(cmd, capture_output=True, env=env, timeout=a.timeout * 60 + 120)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    out = workdir / out_name
    if not out.exists():
        quota = quota_message(workdir, cp)
        if quota:
            raise QuotaExhausted(quota)
        return None, f"agy rc={cp.returncode}, {out_name} not written: {cp.stderr.decode('utf-8', 'replace')[-400:]}"
    try:
        return load_json(out), None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"invalid JSON in {out_name}: {e}"


# --------------------------------------------------------------------------- reference / deck helpers
def ref_path(out: Path, img: Path) -> Path:
    return out / "ref" / f"{img.stem}.json"


def get_reference(img: Path, out: Path, engines: list, rebuild: bool = False):
    """Sealed reference for the image: reused when the image bytes and engine set are unchanged. None when no
    OCR engine is available (the scorer then falls back to intrinsic checks and more Claude sampling).
    An outdated reference of a slide whose record holds Claude's judgments is kept (they name its line ids)
    unless `rebuild` (rescore, --force and a changed image drop those judgments)."""
    if not engines:
        return None
    return ocr_ref.load_or_build(img, ref_path(out, img), engines, rebuild=rebuild)


def build_refs(slides: list, pages: list, out: Path, engines: list, jobs: int, rebuild=(), log_each=False,
               states=None) -> dict:
    """Sealed references for `pages`, in parallel: {page: ref | None}. Failures are logged, never raised.
    states: a dict that receives {page: built | kept | unchanged | failed} (kept = outdated, but the slide's
    judgments or queued line crops name its line ids; see ocr_ref.ref_state)."""
    res = {}
    if not pages or not engines:
        return res

    def one(pg):
        img = slides[pg - 1]
        st = ocr_ref.ref_state(img, ref_path(out, img), engines, pg in rebuild)[0]
        return get_reference(img, out, engines, pg in rebuild), {"current": "unchanged", "kept": "kept"}.get(st, "built")

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futs = {ex.submit(one, pg): pg for pg in pages}
        for fut in as_completed(futs):
            pg = futs[fut]
            try:
                res[pg], st = fut.result()
                q = (res[pg] or {}).get("quality") or {}
                if log_each:
                    log(f"[{pg:03d}] ref {st}: {q.get('n_words')} words, usable={q.get('usable')}")
            except Exception as e:  # OCR trouble must not block the analysis
                res[pg], st = None, "failed"
                log(f"[{pg:03d}] reference failed: {e!r}")
            if states is not None:
                states[pg] = st if res[pg] else "failed"
    return res


def deck_template_keys(slides: list, out: Path, th: dict):
    """Template-line keys (ocr_score.template_keys) from every reference on disk, not only the selection.
    None with fewer than 5 references or when the thresholds switch the exclusion off."""
    if not th.get("template_exclusion", True):
        return None
    refs = {}
    for img in slides:
        r = read_json(ref_path(out, img))
        if isinstance(r, dict):
            refs[img.stem] = r
    if len(refs) < 5:
        return None
    try:
        return ocr_score.template_keys(refs, min_slides=5) or None
    except Exception as e:  # a damaged reference must not stop the run; the slides are then scored without it
        log(f"template keys failed: {e!r}")
        return None


def deck_hf_boilerplate(slides: list, out: Path, th: dict, current=None):
    """ocr_score.hf_boilerplate over the analysis of every slide of the deck on disk (not only --pages; results of
    stems outside `slides`, left over from a longer deck, never count); current: {stem: analysis} in place of what
    is on disk for those slides (an analysis not saved yet). None when the thresholds switch it off."""
    if not th.get("hf_boilerplate", True):
        return None
    stems, ans = {img.stem for img in slides}, {}
    for img in slides:
        r = read_json(out / "results" / f"{img.stem}.json")
        if isinstance(r, dict) and isinstance(r.get("analysis"), dict):
            ans[img.stem] = r["analysis"]
    ans.update({k: v for k, v in (current or {}).items() if k in stems})
    return ocr_score.hf_boilerplate(ans)


def score_pair(analysis, ref, th: dict, errors=(), tkeys=None, boilerplate=None) -> tuple:
    """-> (score, score_legacy). score_legacy = {decision, reasons} of the first-release scorer (every switch off,
    ocr_score.legacy_thresholds): shadow mode's view, never used for a status."""
    lg = score(analysis, ref, ocr_score.legacy_thresholds(th), validator_errors=errors, tkeys=tkeys)
    sc = score(analysis, ref, th, validator_errors=errors, tkeys=tkeys, boilerplate=boilerplate)
    return sc, {"decision": lg["decision"], "reasons": lg["reasons"]}


def deck_boilerplate(recs: dict, min_slides: int = 3) -> set:
    """Units found in the analyses of min_slides or more slides (logos, slogans, section bands): never treated
    as hallucination suspects of one slide."""
    c = Counter()
    for r in recs.values():
        if isinstance(r.get("analysis"), dict):
            c.update(set(units(corpus_text(r["analysis"]))))
    return {u for u, n in c.items() if n >= min_slides}


def pending_reason(out: Path, img: Path, page: int, force: bool = False):
    """Why `run` must (re)analyse a slide: force | new | unreadable | failed | unreviewed | stale (the image
    changed since its analysis); None when it is done."""
    if force:
        return "force"
    f = out / "results" / f"{img.stem}.json"
    if not f.exists():
        return "new"
    rec = read_json(f)
    if not isinstance(rec, dict):
        log(f"[{page:03d}] unreadable result {f.name}; redoing it")
        return "unreadable"
    if rec.get("status") in PENDING:
        return rec["status"]
    sha = rec.get("sha256") or (read_json(ref_path(out, img)) or {}).get("sha256")
    if sha and sha != ocr_ref.sha256_file(img):
        log(f"[{page:03d}] {img.name} changed since it was analysed; redoing it")
        return "stale"
    return None


def check_deck(out: Path, slides: list, force: bool = False, write: bool = True) -> int:
    """Refuse (5) an --out that holds results of another deck: the images shared with its manifest are fewer than
    half of the smaller of the two decks (a deck that grew or had a few pages re-captured still matches).
    Writes the manifest (folder + sha256 per image) when `write`. Returns 0 when the deck matches."""
    files = {p.name: ocr_ref.sha256_file(p) for p in slides}
    f = out / DECK_MANIFEST
    old = read_json(f)
    if isinstance(old, dict) and not force and any((out / "results").glob("*.json")):
        known = set((old.get("files") or {}).values())
        hits = len(known & set(files.values()))
        if known and hits < min(len(known), len(set(files.values()))) / 2:
            log(f"ERROR: {out} holds results for another deck ({old.get('slides')}; {hits} of {len(files)} images "
                "match); use another --out, or `run --force` to overwrite")
            return 5
    if write:
        cropmod.write_atomic(f, json.dumps({"slides": str(slides[0].parent), "files": files}, ensure_ascii=False, indent=1))
    return 0


def audit_hash(stem: str) -> float:
    return int(hashlib.sha1(stem.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF


def grounding_claims(an: dict) -> dict:
    claims = {}
    if an.get("summary"):
        claims["summary"] = an["summary"]
    for i, kc in enumerate(an.get("key_concepts") or []):
        if isinstance(kc, dict):
            claims[f"key_concepts[{i}]"] = f"{kc.get('term')}: {kc.get('explanation')}"
    for i, e in enumerate(an.get("exam_points") or []):
        claims[f"exam_points[{i}]"] = str(e)
    if an.get("lecture_role"):
        claims["lecture_role"] = an["lecture_role"]
    return claims


# --------------------------------------------------------------------------- legacy agy reviewer
def run_review(agy_task, img: Path, data: dict, wd: Path, attempt: int, ctx: dict, a):
    tmp = wd / f"analysis_{attempt}.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    review, rerr = call_agy(agy_task, wd / f"review_{attempt}", (PROMPTS / "reviewer.md").read_text(encoding="utf-8").format(**ctx),
                            "review.json", [(img, img.name), (tmp, "analysis.json")], a)
    return {"verdict": "review_failed", "error": rerr} if rerr else review


def save(out: Path, img: Path, rec: dict) -> dict:
    cropmod.write_atomic(out / "results" / f"{img.stem}.json", json.dumps(rec, ensure_ascii=False, indent=2))
    return rec


# --------------------------------------------------------------------------- per-slide pipeline
def process(agy_task, img: Path, page: int, total: int, out: Path, work: Path, a, engines, th, tkeys=None, refs=None,
            deck=None):
    """Analyse one slide (legacy mode: or only re-review an `unreviewed` record). Returns the record written (also
    the scored attempt kept when agy ran out of quota on its retry), or None when agy ran out of quota before
    anything was written (the slide stays pending).
    refs: {page: reference} built before any analyst call; tkeys: deck-template keys for the scorer; deck: every
    slide image of the deck (default: the images next to img)."""
    if STOP.is_set():
        return None
    file = img.name
    ctx = dict(page=page, file=file, total=total, subject=a.subject, deck_context=a.context)
    prev = None if a.force else read_json(out / "results" / f"{img.stem}.json")
    try:
        if a.reviewer == "agy":
            if isinstance(prev, dict) and prev.get("status") == "unreviewed" and prev.get("analysis"):
                attempt = len(prev["attempts"])
                review = run_review(agy_task, img, prev["analysis"], work / img.stem, attempt, ctx, a)
                prev["attempts"][-1]["review"] = review
                prev["review"] = review
                verdict = review.get("verdict")
                log(f"[{page:03d}] re-review: {verdict}")
                if verdict in ("pass", "minor_issues"):
                    prev["status"] = "ok"
                    return save(out, img, prev)
                if verdict == "review_failed":
                    return save(out, img, prev)
            return analyse_legacy(agy_task, img, page, out, work, ctx, a)
        return analyse(agy_task, img, page, out, work, ctx, a, engines, th, tkeys, refs, deck)
    except QuotaExhausted as e:
        if not STOP.is_set():
            STOP.set()
            log(f"[{page:03d}] agy quota exhausted ({e}); stopping - queued slides are left pending")
        return getattr(e, "saved", None)


def analyse(agy_task, img: Path, page: int, out: Path, work: Path, ctx: dict, a, engines, th, tkeys=None, refs=None,
            deck=None) -> dict:
    file = img.name
    deck = list_slides(img.parent) if deck is None else deck
    analyst_t = (PROMPTS / "analyst.md").read_text(encoding="utf-8")
    # 0. sealed reference, built before the analyst runs so it cannot be influenced by the analysis
    if refs is not None and page in refs:
        ref = refs[page]
    else:
        try:
            ref = get_reference(img, out, engines, rebuild=bool(getattr(a, "force", False)))
        except Exception as e:  # OCR trouble must not block the analysis
            log(f"[{page:03d}] reference failed: {e!r}")
            ref = None
    rec = {"page": page, "file": file, "sha256": ocr_ref.sha256_file(img), "status": "failed", "attempts": [],
           "reference": None, "queue": [], "judgments": []}
    if ref:
        rec["reference"] = {"path": str(ref_path(out, img)), "engines": [e["engine"] for e in ref["engines"] if e.get("ok")],
                            "quality": ref["quality"]}
    feedback, sc = "", None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wd = work / img.stem / f"analyze_{attempt}"
        try:
            data, err = call_agy(agy_task, wd, analyst_t.format(feedback=feedback, **ctx), "slide_analysis.json", [(img, file)], a)
        except QuotaExhausted as e:
            if sc is not None:  # an earlier attempt was scored: keep that paid-for analysis instead of dropping it
                rec["attempts"].append({"attempt": attempt, "error": f"agy quota exhausted: {e}"})
                e.saved = save(out, img, decide(rec, sc, ref, img, out, page))
                log(f"[{page:03d}] quota exhausted on attempt {attempt}; attempt {attempt - 1} kept as {rec['status']}")
            raise
        att = {"attempt": attempt, "error": err}
        if err:
            rec["attempts"].append(att)
            feedback = ""
            log(f"[{page:03d}] analyze#{attempt} failed: {err[:160]}")
            continue
        issues = validate_analysis(data, page, file)
        errors = [i for i in issues if i["level"] == "error"]
        # the deck boilerplate of the analyses on disk now; settle() re-scores once every slide is in
        sc, legacy = score_pair(data, ref, th, errors, tkeys, deck_hf_boilerplate(deck, out, th, {img.stem: data}))
        att.update(validation=issues, decision=sc["decision"], reasons=sc["reasons"], metrics=sc["metrics"])
        rec["attempts"].append(att)
        rec.update(analysis=data, validation=issues, score=sc, decision=sc["decision"], score_legacy=legacy)
        log(f"[{page:03d}] analyze#{attempt}: {len(errors)} errors, recall={sc['metrics'].get('recall')}, "
            f"unsupported={sc['metrics'].get('unsupported_ratio')} -> {sc['decision']} {sc['reasons']}")
        if sc["decision"] != "retry":
            break
        feedback = ("# 이전 시도에 대한 피드백 (반드시 반영하되, 이미지를 직접 다시 보고 사실인지 확인하세요)\n"
                    + feedback_text(sc, errors) + "\n")
    if not rec.get("analysis"):
        return save(out, img, rec)
    return save(out, img, decide(rec, sc, ref, img, out, page))


def analyse_legacy(agy_task, img: Path, page: int, out: Path, work: Path, ctx: dict, a) -> dict:
    """The original flow: analyst -> validator -> agy reviewer -> retry on major_issues. Kept for comparison runs."""
    file = img.name
    analyst_t = (PROMPTS / "analyst.md").read_text(encoding="utf-8")
    rec = {"page": page, "file": file, "status": "failed", "attempts": []}
    feedback = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wd = work / img.stem / f"analyze_{attempt}"
        data, err = call_agy(agy_task, wd, analyst_t.format(feedback=feedback, **ctx), "slide_analysis.json", [(img, file)], a)
        att = {"attempt": attempt, "error": err}
        if err:
            rec["attempts"].append(att)
            feedback = ""
            log(f"[{page:03d}] analyze#{attempt} failed: {err[:160]}")
            continue
        issues = validate_analysis(data, page, file)
        errors = [i for i in issues if i["level"] == "error"]
        att["validation"] = issues
        rec["attempts"].append(att)
        rec.update(analysis=data, validation=issues, review=None)
        review = None
        if not errors:
            try:
                review = run_review(agy_task, img, data, work / img.stem, attempt, ctx, a)
            except QuotaExhausted:
                rec["status"] = "unreviewed"
                save(out, img, rec)
                raise
        att["review"] = review
        rec["review"] = review
        verdict = (review or {}).get("verdict")
        log(f"[{page:03d}] analyze#{attempt}: {len(errors)} errors, review={verdict}")
        if not errors and verdict != "major_issues":
            rec["status"] = "ok" if verdict in ("pass", "minor_issues") else "unreviewed"
            break
        lines = [f"- [검증기] {i['code']}: {i['msg']}" for i in errors]
        if review and verdict == "major_issues":
            lines.append(f"- [검수자] {review.get('feedback_for_analyst', '')}")
            lines += [f"- [검수자 누락 지적] {m}" for m in review.get("missing", [])]
            lines += [f"- [검수자 환각 지적] {h}" for h in review.get("hallucinated", [])]
        feedback = ("# 이전 시도에 대한 피드백 (반드시 반영하되, 이미지를 직접 다시 보고 사실인지 확인하세요)\n"
                    + "\n".join(lines) + "\n")
        rec["status"] = "needs_attention"
    return save(out, img, rec)


def decide(rec: dict, sc: dict, ref, img: Path, out: Path, page: int):
    """Turn a score into the record's status (and the reviewer queue for escalations)."""
    rec.update(score=sc, decision=sc["decision"])
    d = sc["decision"]
    if d == "accept":
        rec["status"] = "accepted"
    elif d == "unverified":
        rec["status"] = "unverified"
    elif d == "escalate":
        try:
            rec["queue"] = cropmod.prepare(img, ref, sc, rec["analysis"], out / "crops", img.stem, page)
        except Exception as e:
            log(f"[{page:03d}] crop preparation failed: {e!r}")
            rec["queue"] = []
        rec["status"] = "escalated" if rec["queue"] else "needs_attention"
    else:
        rec["status"] = "needs_attention"
    return rec


def rescore(slides: list, pages: list, out: Path, a, th, ref_fn, validate_fn=None) -> dict:
    """Re-run validator + scorer on stored analyses (no agy): after a threshold / scorer / reference change.
    Escalations are re-queued from scratch, previous judgments are dropped, and so are their answers: the ledger
    records the entry each answer was judged against, so an answer is reused only for an identical entry (a rebuilt
    reference renumbers line ids). The references of the selection are built first; the deck-template keys then
    come from every reference on disk, the header/footer boilerplate from every analysis of the deck on disk.
    ref_fn(img, page) -> reference; validate_fn(analysis, page, file) -> issues (default: slide_validate)."""
    validate_fn = validate_fn or validate_analysis
    todo = []
    for p in pages:
        img = slides[p - 1]
        rec = read_json(out / "results" / f"{img.stem}.json")
        if not isinstance(rec, dict) or not rec.get("analysis") or (rec.get("review") is not None and not rec.get("score")):
            continue  # nothing analysed, or a legacy-mode record
        try:
            ref = ref_fn(img, p)
        except Exception as e:
            log(f"[{p:03d}] reference failed: {e!r}")
            ref = None
        todo.append((p, img, rec, ref))
    tkeys, boiler = deck_template_keys(slides, out, th), deck_hf_boilerplate(slides, out, th)
    # every answer on disk for these records belongs to the entries being dropped; recorded before the first
    # record is saved, so a rescore killed half-way cannot leave answers that look current
    seed_ledger(out, {img.stem: rec for _p, img, rec, _r in todo})
    for p, img, rec, ref in todo:
        issues = validate_fn(rec["analysis"], p, rec.get("file") or img.name)
        errors = [i for i in issues if i["level"] == "error"]
        sc, legacy = score_pair(rec["analysis"], ref, th, errors, tkeys, boiler)
        rec.update(validation=issues, queue=[], judgments=[], audit=False, grounding=None, score_legacy=legacy)
        rec.pop("audit_stratum", None)
        if ref:
            rec["reference"] = {"path": str(ref_path(out, img)), "engines": [e["engine"] for e in ref["engines"] if e.get("ok")],
                                "quality": ref["quality"]}
        decide(rec, sc, ref, img, out, p)
        log(f"[{p:03d}] rescore: recall={sc['metrics'].get('recall')} unsupported={sc['metrics'].get('unsupported_ratio')} "
            f"-> {sc['decision']} {sc['reasons']}")
        save(out, img, rec)
    return build_report(slides, pages, out, a)


def settle(slides: list, out: Path, th: dict, decide_fn=None) -> list:
    """`run` scores each slide against the deck boilerplate of the analyses on disk at that moment. Once the
    analyst pool has ended, re-score (no agy; the stored validator issues) every record of the deck that holds a
    scored analysis and no judgments yet - not only the slides of this run: a resumed or partial run moves the
    boilerplate of the others too - against the template keys and boilerplate of the whole deck on disk, so `run`
    ends with the records `rescore` makes. A record whose score changed (decision, reasons, needs, metrics) is
    decided again (decide_fn(rec, sc, ref, img, page); default decide()): its old queue entries are dropped and the
    answers on disk for them seeded as stale. A settle-time retry would need agy, so that record stays as stored.
    -> [(page, old decision, new decision)] for every record decided again."""
    decide_fn = decide_fn or (lambda rec, sc, ref, img, p: decide(rec, sc, ref, img, out, p))
    tkeys, boiler, changed = deck_template_keys(slides, out, th), deck_hf_boilerplate(slides, out, th), []
    for p, img in enumerate(slides, 1):
        rec = read_json(out / "results" / f"{img.stem}.json")
        if not isinstance(rec, dict) or not rec.get("analysis") or not rec.get("score") or rec.get("judgments"):
            continue
        ref = read_json(ref_path(out, img))
        errors = [i for i in rec.get("validation") or [] if i.get("level") == "error"]
        sc, legacy = score_pair(rec["analysis"], ref, th, errors, tkeys, boiler)
        old = rec.get("decision")
        if json.loads(json.dumps([sc, legacy], ensure_ascii=False)) == [rec["score"], rec.get("score_legacy")]:
            continue
        if sc["decision"] == "retry" and old != "retry":
            log(f"[{p:03d}] settle: re-scored as a retry, which would need agy; the stored decision ({old}) stays")
            continue
        seed_ledger(out, {img.stem: rec}, keep=True)
        rec.update(queue=[], audit=False, score=sc, decision=sc["decision"], score_legacy=legacy)
        rec.pop("audit_stratum", None)
        decide_fn(rec, sc, ref, img, p)
        save(out, img, rec)
        log(f"[{p:03d}] settle: {old} -> {sc['decision']} {sc['reasons']} (deck boilerplate of every analysis)")
        changed.append((p, old, sc["decision"]))
    return changed


# --------------------------------------------------------------------------- audit sampling
def new_only(rec: dict) -> bool:
    """Accepted by the scorer, not by its first-release view (score_legacy): shadow mode's stratum."""
    return rec.get("decision") == "accept" and (rec.get("score_legacy") or {}).get("decision") not in (None, "accept")


def pick_audits(slides: list, recs: dict, out: Path, rate: float, min_n: int = 2, boilerplate=None,
                shadow_rate: float = 0.0) -> int:
    """Mark a deterministic sample of OCR-accepted slides (decision accept, nothing judged yet) for an independent
    blind thumbnail read (<stem>__thumb_audit). The stem hash decides, so the sample is stable across re-runs; at
    least min_n slides are audited, 10 once 50 or more are OCR-accepted. Slides Claude accepted after a review are
    not sampled: the read that accepted them is not an independent check of them. Shadow mode adds a second
    stratum: the stem hash also takes a share `shadow_rate` of the new-only accepts (new_only). A slide is audited
    once; rec['audit_stratum'] names the stratum that took it (ocr | new_only)."""
    pool = [(p, r) for p, r in sorted(recs.items()) if r.get("decision") == "accept" and r.get("analysis")]
    if not pool or (rate <= 0 and shadow_rate <= 0):
        return 0
    need = min(len(pool), max(min_n, 10 if len(pool) >= 50 else 0))
    have = sum(1 for _, r in pool if r.get("audit") and r.get("audit_stratum") != "new_only")  # the OCR stratum's own
    fresh = sorted(((p, r) for p, r in pool if not r.get("audit") and not r.get("judgments") and r.get("status") == "accepted"),
                   key=lambda pr: audit_hash(slides[pr[0] - 1].stem))
    k = max(0, sum(audit_hash(slides[p - 1].stem) < rate for p, _ in fresh), need - have) if rate > 0 else 0
    picks = [(p, r, "ocr") for p, r in fresh[:k]]
    picks += [(p, r, "new_only") for p, r in fresh[k:] if new_only(r) and audit_hash(slides[p - 1].stem) < shadow_rate]
    n = 0
    for p, r, stratum in picks:
        img = slides[p - 1]
        try:
            entries = cropmod.prepare(img, read_json(ref_path(out, img)), r.get("score") or {}, r["analysis"],
                                      out / "crops", img.stem, p, audit=True, boilerplate=boilerplate)
        except Exception as e:
            log(f"[{p:03d}] audit thumbnail failed: {e!r}")
            continue
        ids = {e["id"] for e in r.get("queue") or []}
        r.update(audit=True, audit_stratum=stratum)
        r["queue"] = (r.get("queue") or []) + [e for e in entries if e["id"] not in ids]
        save(out, img, r)
        n += 1
    return n


def _audit_line(label: str, pool: list, mine=None) -> str:
    """mine(rec): whether the slide's audit belongs to this stratum's sample (default: every audit counts)."""
    use = [r for r in pool if mine is None or mine(r)]
    aj = [[j for j in r.get("judgments") or [] if j.get("kind") == "thumb_audit"] for r in use]
    n = sum(1 for js in aj if js)
    bad = sum(1 for js in aj if any(j.get("verdict") == "issue" for j in js))
    pend = sum(1 for r in use for e in pending_entries(r) if e["kind"] == "thumb_audit")
    pend_s = f", {pend} audit item(s) pending" if pend else ""
    if not n:
        return f"{label}: no audit judged yet ({len(pool)} accepted{pend_s})"
    rate = bad / n
    upper = min(1.0, 3 / n) if bad == 0 else min(1.0, rate + 1.96 * (rate * (1 - rate) / n) ** 0.5)
    bound = f"upper ≈ {upper:.0%}" if upper < 0.30 else "too few audits for a useful upper bound"
    return (f"{label}: audited {n} of {len(pool)} accepted, {bad} with issues → estimated residual error "
            f"{rate:.0%} ({bound}){pend_s}")


def audit_lines(recs: dict) -> list:
    """OCR-accepted slides with their independent audits, slides Claude accepted after review and, when records
    carry score_legacy (shadow mode), the new-only accepts as a stratum of their own plus a new-vs-legacy count."""
    ocr = [r for r in recs.values() if r.get("decision") == "accept" and r.get("analysis")]
    claude = sum(1 for r in recs.values() if r.get("status") == "accepted" and r.get("decision") == "escalate")
    # the OCR stratum is a uniform hash sample; the shadow stratum's extra audits would tilt it towards new-only accepts
    out = [_audit_line("Audit (OCR-accepted)", ocr, lambda r: r.get("audit_stratum") != "new_only"),
           f"Claude-accepted: {claude} slide(s) accepted after Claude's blind answers to their disputed items "
           "(not audited; only as strong as the questions asked)"]
    shadow = [r for r in recs.values() if r.get("analysis") and r.get("score_legacy")]
    if shadow:
        only = [r for r in ocr if new_only(r)]
        acc = sum(1 for r in shadow if r.get("decision") == "accept")
        old = sum(1 for r in shadow if r["score_legacy"].get("decision") == "accept")
        old_only = sum(1 for r in shadow if r["score_legacy"].get("decision") == "accept" and r.get("decision") != "accept")
        out += [_audit_line("Audit (new-rule-only accepts)", only),
                f"Shadow mode (score_legacy): the scorer accepts {acc} of {len(shadow)} scored slide(s), its first-release "
                f"rules {old}; {len(only)} accepted by the new rules only, {old_only} by the old rules only"]
    return out


# --------------------------------------------------------------------------- report + queue
def pending_entries(rec: dict) -> list:
    done = {j["id"] for j in rec.get("judgments") or []}
    out, seen = [], set()
    for e in rec.get("queue") or []:
        if e["id"] not in done and e["id"] not in seen:
            seen.add(e["id"])
            out.append(e)
    return out


def load_results(slides: list, out: Path) -> dict:
    """{page: record} for every slide with a readable results/<stem>.json."""
    recs = {}
    for p, img in enumerate(slides, 1):
        f = out / "results" / f"{img.stem}.json"
        if not f.exists():
            continue
        r = read_json(f)
        if isinstance(r, dict):
            recs[p] = r
        else:
            log(f"[{p:03d}] unreadable result {f.name}; reported as missing")
    return recs


def _drop(out: Path, *names):
    for n in names:
        if (out / n).exists():
            (out / n).unlink()


def build_report(slides: list, pages: list, out: Path, a=None) -> dict:
    """Deck files (all_slides.json, report.md, review / grounding queues, audit picks) always cover every slide
    that has a result, so a --pages run never truncates them; the returned counts (and so the exit code) cover
    the `pages` selection only."""
    recs = load_results(slides, out)
    if a is not None and getattr(a, "reviewer", "none") != "agy":
        pick_audits(slides, recs, out, getattr(a, "audit_rate", 0.1), boilerplate=deck_boilerplate(recs),
                    shadow_rate=getattr(a, "shadow_audit_rate", SHADOW_AUDIT_RATE))
    dup = cross_check({p: r.get("analysis") for p, r in recs.items()})
    want = getattr(a, "grounding_check", "flagged") if a is not None else "flagged"
    rows, all_slides, counts, queue, grounding, seen = [], [], {}, [], [], set()
    for p in sorted(set(recs) | set(pages)):
        r = recs.get(p)
        status = (r.get("status") or "failed") if r else "missing"
        counts[status] = counts.get(status, 0) + 1
        if not r:
            rows.append(f"| {p} | {slides[p - 1].name} | missing | | | | | | |")
            continue
        an, sc = r.get("analysis") or {}, r.get("score") or {}
        m = sc.get("metrics") or {}
        issues_v = (r.get("validation") or []) + dup.get(p, [])
        flags = sorted({i["code"] for i in issues_v} | set(sc.get("triggers") or []))
        if r.get("grounding") and r["grounding"].get("unsupported"):
            flags.append("grounding_issue")
        pend = [e for e in pending_entries(r) if e["id"] not in seen]
        seen.update(e["id"] for e in pend)
        queue += pend
        q_col = f"{len(pend)} pending" if pend else (f"{len(r.get('judgments') or [])} judged" if r.get("judgments") else "-")
        rv = r.get("review") or {}
        legacy = f" {rv.get('verdict')}/{rv.get('text_recall')}" if rv else ""
        rows.append(f"| {p} | {r.get('file', slides[p - 1].name)} | {status} | {r.get('decision', '')}{legacy} | "
                    f"{an.get('slide_type', '')} | {m.get('recall', '')} | {m.get('unsupported_ratio', '')} | "
                    f"{m.get('digit_recall', '')} | {', '.join(flags) or '-'} | {q_col} |")
        if an:
            all_slides.append({**an, "_qa": {"status": status, "decision": r.get("decision"), "attempts": len(r.get("attempts") or []),
                                             "metrics": m, "reasons": sc.get("reasons"), "triggers": sc.get("triggers"),
                                             "validation": [i["code"] for i in issues_v],
                                             "judgments": [{k: v for k, v in j.items() if k in ("id", "kind", "verdict", "claude_text", "note")}
                                                           for j in r.get("judgments") or []],
                                             "grounding": r.get("grounding"), "review": rv or None}})
            # text-only grounding check: slides whose interpretation may outrun the transcription
            ungrounded = any(i["code"] in ("concept_ungrounded", "title_ungrounded", "interpretation_on_title_only")
                             for i in issues_v)
            if r.get("grounding") is None and status not in ("failed",) and (want == "all" or (want == "flagged" and ungrounded)):
                grounding.append({"page": p, "stem": slides[p - 1].stem, "verbatim": corpus_text(an), "claims": grounding_claims(an)})
    cropmod.write_atomic(out / "all_slides.json", json.dumps(all_slides, ensure_ascii=False, indent=2))

    est = cropmod.token_estimate(queue)
    if queue:
        cropmod.write_queue(out, queue, note=f"슬라이드 폴더: `{slides[0].parent}`")
    else:
        _drop(out, "review_queue.md", "review_queue.json")
        cropmod.remove_shards(out)
    if grounding:
        cropmod.write_grounding_queue(out, grounding)
    else:
        _drop(out, "grounding_queue.md", "grounding_queue.json")
    sel = Counter((recs[p].get("status") or "failed") if p in recs else "missing" for p in pages)
    head = ["# Analysis QA report", "",
            "Status: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))]
    if len(set(pages)) < len(slides):
        head.append("This selection (--pages): " + ", ".join(f"{k}={v}" for k, v in sorted(sel.items())))
    head += ["",
             f"Review queue: {len(queue)} pending item(s) in `review_queue.md` (≈{est} image tokens; ≈{TOTAL_TOKENS_PER_ITEM // 1000}k "
             f"total tokens per item measured, ≈{len(queue) * TOTAL_TOKENS_PER_ITEM // 1000}k in all)" if queue else "Review queue: empty",
             *audit_lines(recs),
             f"Grounding queue: {len(grounding)} slide(s) in `grounding_queue.md` (text only)" if grounding else "Grounding queue: empty", "",
             "| page | file | status | decision | type | recall | unsupported | digits | flags | claude |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    cropmod.write_atomic(out / "report.md", "\n".join(head + rows) + "\n")
    return dict(sel)


# --------------------------------------------------------------------------- resolve (Claude's answers)
def _reads_files(out: Path) -> list:
    """[(file name, {id: answer} | None)] for crop_reads.json and the shard files crop_reads_<k>.json."""
    return [(f.name, read_json(f)) for f in sorted(out.glob("crop_reads*.json")) if READS_RE.fullmatch(f.name)]


def load_reads(out: Path) -> tuple:
    """Merge crop_reads.json and the shard files crop_reads_<k>.json -> (reads, problems, conflicts). An
    unreadable file is a problem; conflicts = {id: [file, file]} for an id answered differently in two files."""
    reads, src, problems, conflicts = {}, {}, [], {}
    for name, d in _reads_files(out):
        if not isinstance(d, dict):
            problems.append(f"{name} is not a readable JSON object")
            continue
        for k, v in d.items():
            if k in reads and reads[k] != v:
                conflicts[k] = [src[k], name]
            elif k not in reads:
                reads[k], src[k] = v, name
    return reads, problems, conflicts


def _ans_sha(ans) -> str:
    return hashlib.sha256(json.dumps(ans, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def stale_seeds(records: dict, reads: dict) -> dict:
    """Ledger entries for a record about to be replaced ({stem: record | None}; run --force / a changed image / a
    retried slide, rescore): every answer already written for one of its ids was written for the outgoing entry,
    so it is recorded as judged against that entry's fp ('before-rerun' when the entry had none or is gone). A new
    entry with the same fp (an identical crop) keeps the answer; any other sees it as stale."""
    seeds = {}
    for stem, rec in records.items():
        rec = rec if isinstance(rec, dict) else {}
        shown = {e.get("id"): e.get("fp") for e in rec.get("queue") or [] if isinstance(e, dict)}
        shown.update({j.get("id"): j.get("fp") for j in rec.get("judgments") or [] if isinstance(j, dict) and j.get("fp")})
        for rid, ans in reads.items():
            if rid.startswith(f"{stem}__"):
                seeds[rid] = {"fp": shown.get(rid) or "before-rerun", "sha": _ans_sha(ans)}
    return seeds


def seed_ledger(out: Path, records: dict, keep: bool = False) -> int:
    """Write stale_seeds(records, <every answer on disk>) into applied_reads.json (atomically, before the records
    are replaced, so an interrupted run or rescore cannot leave answers that look current). keep: answers the ledger
    already records for the same answer keep their entry (settle: the run's start recorded them against the entries
    they were written for, and the record settle replaces was never shown; a slide outside the run is recorded
    against the entry it shows now). -> seeds written"""
    reads = {}
    for _name, d in _reads_files(out):
        if isinstance(d, dict):
            for k, v in d.items():
                reads.setdefault(k, v)
    ledger = read_json(out / LEDGER, {}) or {}
    seeds = {k: v for k, v in stale_seeds(records, reads).items()
             if not (keep and (ledger.get(k) or {}).get("sha") == v["sha"])}
    if seeds:
        ledger.update(seeds)
        cropmod.write_atomic(out / LEDGER, json.dumps(ledger, ensure_ascii=False, indent=1))
    return len(seeds)


def _is_stale(e: dict, ans, ledger: dict) -> bool:
    fp = e.get("fp")
    if isinstance(ans, dict) and set(ans) == {"answer", "fp"}:
        return bool(fp) and ans["fp"] != fp
    led = ledger.get(e["id"]) or {}
    return bool(fp and led.get("fp") and led["fp"] != fp and led.get("sha") == _ans_sha(ans))


def usable_reads(entries: list, reads: dict, ledger: dict) -> tuple:
    """-> ({id: answer} for the current entries, [stale ids]). An answer is stale when its explicit fp differs
    from the entry's, or when the same answer was already judged against an entry with another fingerprint (a
    re-run changed what Claude is shown under that id). Plain {id: answer} answers keep working."""
    use, stale = {}, []
    for e in entries:
        if e["id"] not in reads:
            continue
        ans = reads[e["id"]]
        if _is_stale(e, ans, ledger):
            stale.append(e["id"])
            continue
        use[e["id"]] = ans["answer"] if isinstance(ans, dict) and set(ans) == {"answer", "fp"} else ans
    return use, stale


def _settle_conflicts(out: Path, reads: dict, conflicts: dict, pending: dict, ledger: dict) -> list:
    """Answers to one pending id that differ between files: those written for an older entry (stale) drop out, and
    a single remaining answer is used (put into `reads`). -> the ids that still have two different answers."""
    alts = {}
    for _name, d in _reads_files(out):
        if isinstance(d, dict):
            for k in conflicts:
                if k in d and d[k] not in alts.setdefault(k, []):
                    alts[k].append(d[k])
    left = []
    for k in conflicts:
        if k not in pending:
            continue
        fresh = [v for v in alts.get(k, []) if not _is_stale(pending[k], v, ledger)]
        if len(fresh) == 1:
            reads[k] = fresh[0]
        elif fresh:
            left.append(k)
    return left


def cmd_resolve(slides: list, pages: list, out: Path, a) -> int:
    reads, problems, conflicts = load_reads(out)
    recs = load_results(slides, out)
    pending = {e["id"]: e for p in pages if p in recs for e in pending_entries(recs[p])}
    ledger = read_json(out / LEDGER, {}) or {}
    problems += [f"{k}: different answers in {' and '.join(conflicts[k])}"
                 for k in _settle_conflicts(out, reads, conflicts, pending, ledger)]
    if problems:  # answers to items that are already judged may differ: they are not used
        for m in problems:
            log(f"ERROR: {m}")
        log("fix crop_reads*.json (one answer per id) and run resolve again; nothing was changed")
        return 5
    greads = read_json(out / "grounding_reads.json", {}) or {}
    if not reads and not greads:
        log("nothing to resolve: write crop_reads.json (and/or grounding_reads.json) first; see review_queue.md")
    boiler = deck_boilerplate(recs)
    changed, stale_all, n_ledger = 0, [], len(ledger)
    for p in pages:
        img = slides[p - 1]
        rec = recs.get(p)
        if not rec or not rec.get("analysis"):
            continue
        pend = pending_entries(rec)
        use, stale = usable_reads(pend, reads, ledger)
        stale_all += [f"{e['id']} (fp {e.get('fp')})" for e in pend if e["id"] in stale]
        new = cropmod.judge(pend, use, rec["analysis"], boilerplate=boiler) if pend else []
        for j in new:
            ledger[j["id"]] = {"fp": j.get("fp"), "sha": _ans_sha(reads[j["id"]])}
        g = greads.get(str(p)) or greads.get(img.stem)
        if g is not None and rec.get("grounding") is None:
            rec["grounding"] = {"unsupported": g.get("unsupported", []) if isinstance(g, dict) else list(g),
                                "note": g.get("note", "") if isinstance(g, dict) else ""}
            changed += 1
        if not new:
            if g is not None:
                save(out, img, rec)
            continue
        rec["judgments"] = (rec.get("judgments") or []) + new
        still = pending_entries(rec)
        if not still:  # every question of this slide answered -> final status
            verdict = cropmod.page_verdict(rec["judgments"])
            if rec["status"] == "escalated":
                rec["status"] = verdict
            elif rec["status"] == "accepted" and verdict == "needs_attention":
                rec["status"] = "needs_attention"  # an audited acceptance that did not hold
        bad = [j for j in new if j.get("verdict") in ("agy_wrong", "issue")]
        log(f"[{p:03d}] {len(new)} judged ({len(bad)} against the analysis), {len(still)} pending -> {rec['status']}")
        save(out, img, rec)
        changed += 1
    if stale_all:
        log(f"{len(stale_all)} answer(s) were written for an older version of their queue item and were ignored. Look "
            "at the item again and write the answer as {\"answer\": ..., \"fp\": \"<fp>\"} with the fp shown in "
            f"review_queue.md (the same plain answer stays ignored): {', '.join(stale_all[:20])}")
    if len(ledger) != n_ledger or changed:
        cropmod.write_atomic(out / LEDGER, json.dumps(ledger, ensure_ascii=False, indent=1))
    counts = build_report(slides, pages, out, a)
    print(json.dumps({"out": str(out), "resolved_slides": changed, "counts": counts, "stale_answers": len(stale_all),
                      "report": str(out / "report.md")}, ensure_ascii=False, indent=2))
    return 0 if set(counts) <= FINAL_OK else 2


def cmd_queue(slides: list, pages: list, out: Path, a) -> int:
    """Rebuild the report, then split the pending review queue into disjoint shards for parallel reviewers."""
    counts = build_report(slides, pages, out, a)
    queue = read_json(out / "review_queue.json", []) or []
    shards = cropmod.write_shards(out, queue, a.shards, note=f"슬라이드 폴더: `{slides[0].parent}`") if queue else []
    print(json.dumps({"out": str(out), "queue_items": len(queue), "shards": shards,
                      "next": "each reviewer answers its shard into crop_reads_<k>.json; then run resolve once"},
                     ensure_ascii=False, indent=2))
    return 0 if set(counts) <= FINAL_OK else 2


# --------------------------------------------------------------------------- calibrate / bench
def load_gold(gold_dir: Path, stem: str):
    for cand in (gold_dir / f"{stem}.json", gold_dir / f"{stem}.gold.json"):
        if cand.exists():
            try:
                g = load_json(cand)
                return g.get("analysis", g) if isinstance(g, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def cmd_calibrate(slides: list, pages: list, out: Path, a, th) -> int:
    gold_dir = Path(a.gold).resolve()
    rows, ocr_quality = [], {}
    tkeys, boiler = deck_template_keys(slides, out, th), deck_hf_boilerplate(slides, out, th)
    for p in pages:
        img = slides[p - 1]
        rec = read_json(out / "results" / f"{img.stem}.json")
        gold = load_gold(gold_dir, img.stem)
        if not isinstance(rec, dict) or not rec.get("analysis") or not gold:
            continue
        ref = read_json(ref_path(out, img))
        proxy = score(rec["analysis"], ref, th, tkeys=tkeys, boilerplate=boiler)
        truth = gold_metrics(gold, rec["analysis"])
        rows.append({"page": p, "file": img.name, "proxy": proxy, "truth": truth})
        if ref:  # how much of the human-verified text each OCR engine actually read
            from textnorm import bigrams, fuzzy_in
            gu = units(corpus_text(gold))
            for eng, text in (ref.get("text") or {}).items():
                bg = bigrams(text)
                q = sum(fuzzy_in(u, text, bg, 0.75) for u in gu) / len(gu) if gu else None
                if q is not None:
                    ocr_quality.setdefault(eng, []).append(q)
    if not rows:
        log(f"no slide has both a result and a gold file in {gold_dir} (expected <stem>.json with the analysis schema)")
        return 5
    sugg = suggest_thresholds(rows, a.target_false_accept)
    engines = {e: round(sum(v) / len(v), 3) for e, v in ocr_quality.items()}
    lines = ["# Calibration report", "", f"gold set: {len(rows)} slide(s) from `{gold_dir}`", "",
             "| page | proxy recall | proxy unsupported | proxy decision | true recall | true precision | char sim | digits | cells |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        m, t = r["proxy"]["metrics"], r["truth"]
        lines.append(f"| {r['page']} | {m.get('recall')} | {m.get('unsupported_ratio')} | {r['proxy']['decision']} | "
                     f"{t['true_recall']} | {t['true_precision']} | {t['char_similarity']} | {t['digit_accuracy']} | {t['cell_accuracy']} |")
    lines += ["", f"OCR engine quality (share of gold text each engine read): {json.dumps(engines, ensure_ascii=False)}", "",
              f"Suggested thresholds (false-accept target {a.target_false_accept:.0%}): {json.dumps(sugg, ensure_ascii=False)}", ""]
    cropmod.write_atomic(out / "calibration_report.md", "\n".join(lines))
    cal = {"thresholds": {k: v for k, v in sugg.items() if k in ("recall_accept", "unsupported_accept")},
           "stats": sugg, "ocr_quality": engines, "n_gold": len(rows), "source": str(gold_dir),
           "scorer_version": SCORER_VERSION}
    target = Path(a.write) if a.write else out / "calibration.json"
    if "error" not in sugg:
        cropmod.write_atomic(target, json.dumps(cal, ensure_ascii=False, indent=2))
    print(json.dumps({**cal, "report": str(out / "calibration_report.md"),
                      "written": str(target) if "error" not in sugg else None}, ensure_ascii=False, indent=2))
    return 0


def cmd_bench(slides: list, pages: list, out: Path, a) -> int:
    gold_dir = Path(a.gold).resolve()
    per, agg = [], {}
    for p in pages:
        img = slides[p - 1]
        rec = read_json(out / "results" / f"{img.stem}.json")
        gold = load_gold(gold_dir, img.stem)
        if not isinstance(rec, dict) or not rec.get("analysis") or not gold:
            continue
        t = gold_metrics(gold, rec["analysis"])
        per.append({"page": p, "file": img.name, "status": rec.get("status"), "attempts": len(rec.get("attempts") or []), **t})
        for k, v in t.items():
            if v is not None:
                agg.setdefault(k, []).append(v)
    if not per:
        log(f"no slide has both a result and a gold file in {gold_dir}")
        return 5
    summary = {k: round(sum(v) / len(v), 3) for k, v in agg.items()}
    res = {"label": a.label, "n": len(per), "summary": summary, "per_slide": per}
    bf = out / "bench.json"
    hist = read_json(bf, []) or []
    hist.append(res)
    cropmod.write_atomic(bf, json.dumps(hist, ensure_ascii=False, indent=2))
    print(json.dumps({"label": a.label, "n": len(per), "summary": summary, "history": str(bf)}, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- CLI
def resolve_paths(a):
    slides_dir = Path(a.slides).resolve()
    if not slides_dir.is_dir():
        log(f"ERROR: --slides {slides_dir} is not a directory")
        sys.exit(5)
    slides = list_slides(slides_dir)
    if not slides:
        log(f"ERROR: no images ({', '.join(sorted(IMAGE_EXT))}) in {slides_dir}")
        sys.exit(5)
    out = Path(a.out).resolve() if a.out else slides_dir.parent / "slide_analysis"
    (out / "results").mkdir(parents=True, exist_ok=True)
    return slides, out


def pick_engines(a) -> list:
    names = None if (a.engine or "auto") == "auto" else a.engine.split(",")
    engines = ocr_ref.available_engines(names)
    if names and len(engines) < len(names):
        missing = set(names) - {e.name for e in engines}
        log(f"WARNING: OCR engine(s) not available: {', '.join(sorted(missing))}")
    if not engines:
        log("WARNING: no OCR engine available; slides cannot be verified locally (install Tesseract or `pip install winsdk`)")
        return engines
    if len(engines) == 1:
        # one recognizer cannot confirm its own words; an upscaled second pass of it can ('variant' agreement).
        # Scales come from OCR_SCALES (default 1,2); OCR_SCALES=1 turns the variants off
        engines = ocr_ref.with_variants(engines)
    log("OCR engines: " + ", ".join(e.name for e in engines))
    return engines


def utf8_stdio():
    """Piped output on Korean Windows defaults to cp949: Korean reaches the caller as mojibake, and a path with
    '–' or '•' kills the final JSON print (turning exit 2/3 into 1)."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def warn_old_calibration(th: dict):
    src = th.get("_source")
    data = read_json(Path(src)) if src else None
    if isinstance(data, dict) and "thresholds" in data and (data.get("scorer_version") or 1) < SCORER_VERSION:
        log(f"WARNING: {src} was fitted under an older scorer (scorer_version {data.get('scorer_version', 1)}); "
            "run calibrate again before trusting its thresholds")


def main(argv=None) -> int:
    utf8_stdio()
    p = argparse.ArgumentParser(prog="slide_analyze.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="check agy + agy-delegate + OCR engines")
    for name in ("run", "report", "reference", "resolve", "rescore", "calibrate", "bench", "queue"):
        s = sub.add_parser(name)
        s.add_argument("--slides", required=True, help="folder of slide images (sorted by name = page order)")
        s.add_argument("--out", help="output folder (default <slides>/../slide_analysis)")
        s.add_argument("--pages", help="1-based page selection, e.g. 1-10,15")
        s.add_argument("--thresholds", help="thresholds/calibration JSON (default: <out>/calibration.json, then the skill's)")
        if name in ("run", "reference", "rescore"):
            s.add_argument("--engine", default=os.environ.get("OCR_ENGINE", "auto"),
                           help="OCR engines, comma list of winocr,tesseract (default auto = all available)")
            s.add_argument("-j", "--jobs", type=int, default=4, help="parallel sessions")
        if name in ("run", "report", "resolve", "rescore", "queue"):
            s.add_argument("--audit-rate", type=float, default=0.1, help="share of OCR-accepted slides Claude audits blind (min 2)")
            s.add_argument("--shadow-audit-rate", type=float, default=SHADOW_AUDIT_RATE,
                           help="share of the slides only the new scorer rules accept (score_legacy is not accept) "
                                "that Claude also audits blind; 0 turns shadow audits off")
            s.add_argument("--grounding-check", choices=["flagged", "all", "none"], default="flagged",
                           help="which slides go to the text-only grounding queue")
        if name == "queue":
            s.add_argument("--shards", type=int, default=2, help="number of disjoint review_queue_<k>.md files")
        if name == "run":
            s.add_argument("--subject", default=DEFAULT_SUBJECT, help="course subject for the personas, e.g. 데이터베이스")
            s.add_argument("--context", default=DEFAULT_CONTEXT, help="one-paragraph description of the deck")
            s.add_argument("--timeout", type=int, default=10, help="minutes per agy call")
            s.add_argument("--model", default=os.environ.get("AGY_TASK_MODEL"))
            s.add_argument("--work", help="scratch dir for agy workdirs (default <out>/.work)")
            s.add_argument("--force", action="store_true", help="redo slides that already have results (and accept an --out of another deck)")
            s.add_argument("--reviewer", choices=["none", "agy"], default="none",
                           help="none = OCR reference + Claude (default); agy = legacy same-vendor reviewer session")
        if name in ("calibrate", "bench"):
            s.add_argument("--gold", required=True, help="folder of human-corrected analyses, <stem>.json")
        if name == "calibrate":
            s.add_argument("--target-false-accept", type=float, default=0.05)
            s.add_argument("--write", help="where to write calibration.json (default <out>/calibration.json)")
        if name == "bench":
            s.add_argument("--label", default="run", help="name of this prompt/model variant")
    a = p.parse_args(argv)

    try:
        agy_task = find_agy_task()
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 4
    if a.cmd == "preflight":
        for e in ocr_ref.make_engines():
            log(f"OCR {e.name}: {'available' if e.available() else 'unavailable'} {json.dumps(e.info(), ensure_ascii=False)}")
        return subprocess.call([sys.executable, str(agy_task), "preflight"])

    slides, out = resolve_paths(a)
    rc = check_deck(out, slides, force=getattr(a, "force", False), write=a.cmd in ("run", "reference", "rescore"))
    if rc:
        return rc
    pages = parse_pages(a.pages, len(slides))
    th = load_thresholds(a.thresholds, out / "calibration.json", SKILL_DIR / "calibration.json")
    if th.get("_source"):
        log(f"thresholds: {th['_source']}")
        warn_old_calibration(th)

    if a.cmd == "reference":
        engines = pick_engines(a)
        if not engines:
            return 4
        states = {}
        build_refs(slides, pages, out, engines, a.jobs, log_each=True, states=states)
        n = Counter(states.values())
        if n["kept"]:
            log(f"{n['kept']} outdated reference(s) kept: their slides hold Claude's judgments or queued line crops, whose "
                "ids name the reference's lines; `rescore` rebuilds them (and drops those judgments)")
        print(json.dumps({"out": str(out / "ref"), "built": n["built"], "kept": n["kept"], "unchanged": n["unchanged"],
                          "failed": n["failed"], "of": len(pages)}, ensure_ascii=False))
        return 0
    if a.cmd == "resolve":
        return cmd_resolve(slides, pages, out, a)
    if a.cmd == "queue":
        a.reviewer = "none"
        return cmd_queue(slides, pages, out, a)
    if a.cmd == "rescore":
        engines = pick_engines(a)
        a.reviewer = "none"
        counts = rescore(slides, pages, out, a, th, lambda img, p: get_reference(img, out, engines, rebuild=True))
        rc = 0 if set(counts) <= FINAL_OK else 2
        print(json.dumps({"out": str(out), "counts": counts, "report": str(out / "report.md")}, ensure_ascii=False, indent=2))
        return rc
    if a.cmd == "calibrate":
        return cmd_calibrate(slides, pages, out, a, th)
    if a.cmd == "bench":
        return cmd_bench(slides, pages, out, a)

    if a.cmd == "run":
        work = Path(a.work).resolve() if a.work else out / ".work"
        work.mkdir(parents=True, exist_ok=True)
        engines = pick_engines(a) if a.reviewer != "agy" else []
        why = {pg: pending_reason(out, slides[pg - 1], pg, a.force) for pg in pages}
        todo = [pg for pg in pages if why[pg]]
        # answers already written for these slides were written for the entries their new records replace
        seed_ledger(out, {slides[pg - 1].stem: read_json(out / "results" / f"{slides[pg - 1].stem}.json") for pg in todo})
        refs, tkeys = None, None
        if engines and todo:
            # every slide of the deck gets its sealed reference before the first analyst call; the deck-template
            # lines (same text at the same place on 5+ slides) are then known from all of them
            build = [pg for pg in range(1, len(slides) + 1) if pg in todo or not ref_path(out, slides[pg - 1]).exists()]
            if build:
                log(f"building {len(build)} sealed reference(s) before any analysis")
            built = build_refs(slides, build, out, engines, a.jobs, rebuild={pg for pg in todo if why[pg] in ("force", "stale")})
            refs = {pg: built.get(pg) for pg in todo}
            tkeys = deck_template_keys(slides, out, th)
        log(f"{len(todo)} slides to process (of {len(pages)}), jobs={a.jobs}, reviewer={a.reviewer}, out={out}")
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(process, agy_task, slides[pg - 1], pg, len(slides), out, work, a, engines, th, tkeys, refs,
                              slides): pg for pg in todo}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:  # keep the batch going
                    log(f"[{futs[fut]:03d}] crashed: {e!r}")
        if a.reviewer != "agy":  # the whole deck: a resumed or partial run moves the boilerplate of the other slides
            settle(slides, out, th)
    counts = build_report(slides, pages, out, a)
    queue = read_json(out / "review_queue.json", []) or []
    rc = 3 if STOP.is_set() else (0 if set(counts) <= FINAL_OK else 2)  # decided before any print can fail
    print(json.dumps({"out": str(out), "counts": counts, "quota_exhausted": STOP.is_set(),
                      "review_queue": str(out / "review_queue.md") if queue else None, "queue_items": len(queue),
                      "claude_image_tokens_estimate": cropmod.token_estimate(queue),
                      "claude_total_tokens_estimate": len(queue) * TOTAL_TOKENS_PER_ITEM,
                      "report": str(out / "report.md"), "all_slides": str(out / "all_slides.json")},
                     ensure_ascii=False, indent=2))
    if STOP.is_set():
        log("agy quota exhausted: re-run the same command after the reset to continue")
    return rc


if __name__ == "__main__":
    sys.exit(main())
