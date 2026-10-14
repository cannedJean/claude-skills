#!/usr/bin/env python3
"""slide_analyze.py - analyse a folder of lecture slide images with agy, one slide per session, and verify
the result WITHOUT a second look at the image by the same vendor.

Pipeline per slide (the image is opened by agy and by local, non-LLM tools only):
  0. reference : ocr_ref.py  - local OCR (winocr / tesseract) + layout stats, sealed with the image sha256
                 BEFORE the analysis exists (-> <out>/ref/<stem>.json)
  1. analyst   : agy + prompts/analyst.md (persona, deck context, CoT, few-shot) -> slide_analysis.json
  2. validator : slide_validate.py, deterministic schema / grounding / leak checks
  3. scorer    : ocr_score.py compares the analysis with the sealed reference -> decision
                   accept    reference agrees: done, no reviewer tokens
                   retry     clear omission or validator error: analyst re-run once, the feedback names
                             the OCR lines that are missing
                   escalate  disagreement the text cannot settle: crops of the disputed lines (and/or a
                             640px thumbnail) are queued for Claude, blind
  4. audit     : a deterministic sample of accepted slides gets a blind thumbnail read by Claude so the
                 run can state its residual error rate
  5. resolve   : Claude's answers (crop_reads.json) are compared with the analysis -> accepted / needs_attention

  preflight                       check agy, the agy-delegate skill and the OCR engines
  reference --slides DIR          build/refresh the sealed references only (no agy)
  run --slides DIR [options]      analyse (resumable; finished slides are skipped)
  resolve --slides DIR            apply crop_reads.json / grounding_reads.json, rebuild the report
  rescore --slides DIR            re-run validator + scorer on stored analyses (no agy), e.g. after calibrate
  report --slides DIR             rebuild all_slides.json + report.md + the review queue from results
  calibrate --slides DIR --gold DIR   fit thresholds on human-corrected gold JSONs -> calibration.json
  bench --slides DIR --gold DIR       score a run against the gold set (prompt / model A/B)

Outputs in --out (default <slides>/../slide_analysis):
  results/<stem>.json   {page, file, status, decision, attempts, analysis, validation, score, queue, judgments}
  ref/<stem>.json       sealed OCR reference      crops/*.png  what Claude may look at
  review_queue.md/json  Claude's blind work list   crop_reads.json  Claude's answers (written by Claude)
  all_slides.json       list of analyses with a `_qa` block each
  report.md             one row per slide + budget / audit summary

Statuses: accepted, escalated (waiting for resolve), needs_attention, failed, unverified (no reference and
nothing to show), plus ok / unreviewed in the legacy `--reviewer agy` mode.
Resume: re-running skips everything except `failed` (and `unreviewed`); `--force` redoes the selection.
On agy RESOURCE_EXHAUSTED the whole run stops and exits 3; queued slides stay pending.

Exit codes: 0 all accepted, 2 some slides escalated / need attention / failed, 3 agy quota exhausted,
4 preflight failure, 5 bad arguments.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crops as cropmod  # noqa: E402
import ocr_ref  # noqa: E402
from ocr_score import feedback_text, gold_metrics, load_thresholds, score, suggest_thresholds  # noqa: E402
from slide_validate import cross_check, validate_analysis  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
PROMPTS = SKILL_DIR / "prompts"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
MAX_ATTEMPTS = 2
DEFAULT_SUBJECT = "해당"
DEFAULT_CONTEXT = "강의 슬라이드 묶음을 페이지별 이미지로 변환한 것입니다."
QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|quota reached|\(code 429\)", re.I)
PENDING = {"failed", "unreviewed"}  # statuses a plain re-run picks up again
FINAL_OK = {"accepted", "ok"}
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
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except json.JSONDecodeError:
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


# --------------------------------------------------------------------------- reference / audit helpers
def ref_path(out: Path, img: Path) -> Path:
    return out / "ref" / f"{img.stem}.json"


def get_reference(img: Path, out: Path, engines: list):
    """Sealed reference for the image: reused when the image bytes and engine set are unchanged. None when no
    OCR engine is available (the scorer then falls back to intrinsic checks and more Claude sampling)."""
    if not engines:
        return None
    return ocr_ref.load_or_build(img, ref_path(out, img), engines)


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
    (out / "results" / f"{img.stem}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


# --------------------------------------------------------------------------- per-slide pipeline
def process(agy_task, img: Path, page: int, total: int, out: Path, work: Path, a, engines, th):
    """Analyse one slide (legacy mode: or only re-review an `unreviewed` record). Returns the record, or None
    when skipped because agy ran out of quota (nothing is written, so the slide stays pending)."""
    if STOP.is_set():
        return None
    file = img.name
    ctx = dict(page=page, file=file, total=total, subject=a.subject, deck_context=a.context)
    prev_f = out / "results" / f"{img.stem}.json"
    prev = json.loads(prev_f.read_text(encoding="utf-8")) if prev_f.exists() and not a.force else None
    try:
        if a.reviewer == "agy":
            if prev and prev.get("status") == "unreviewed" and prev.get("analysis"):
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
        return analyse(agy_task, img, page, out, work, ctx, a, engines, th)
    except QuotaExhausted as e:
        if not STOP.is_set():
            STOP.set()
            log(f"[{page:03d}] agy quota exhausted ({e}); stopping - queued slides are left pending")
        return None


def analyse(agy_task, img: Path, page: int, out: Path, work: Path, ctx: dict, a, engines, th) -> dict:
    file = img.name
    analyst_t = (PROMPTS / "analyst.md").read_text(encoding="utf-8")
    # 0. sealed reference, built before the analyst runs so it cannot be influenced by the analysis
    try:
        ref = get_reference(img, out, engines)
    except Exception as e:  # OCR trouble must not block the analysis
        log(f"[{page:03d}] reference failed: {e!r}")
        ref = None
    rec = {"page": page, "file": file, "status": "failed", "attempts": [], "reference": None, "queue": [], "judgments": []}
    if ref:
        rec["reference"] = {"path": str(ref_path(out, img)), "engines": [e["engine"] for e in ref["engines"] if e.get("ok")],
                            "quality": ref["quality"]}
    feedback, sc = "", None
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
        sc = score(data, ref, th, validator_errors=errors)
        att.update(validation=issues, decision=sc["decision"], reasons=sc["reasons"], metrics=sc["metrics"])
        rec["attempts"].append(att)
        rec.update(analysis=data, validation=issues, score=sc, decision=sc["decision"])
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
    Escalations are re-queued from scratch, previous judgments are dropped.
    ref_fn(img, page) -> reference; validate_fn(analysis, page, file) -> issues (default: slide_validate)."""
    validate_fn = validate_fn or validate_analysis
    for p in pages:
        img = slides[p - 1]
        f = out / "results" / f"{img.stem}.json"
        rec = read_json(f)
        if not rec or not rec.get("analysis") or (rec.get("review") is not None and not rec.get("score")):
            continue  # nothing analysed, or a legacy-mode record
        try:
            ref = ref_fn(img, p)
        except Exception as e:
            log(f"[{p:03d}] reference failed: {e!r}")
            ref = None
        issues = validate_fn(rec["analysis"], p, rec.get("file") or img.name)
        errors = [i for i in issues if i["level"] == "error"]
        sc = score(rec["analysis"], ref, th, validator_errors=errors)
        rec.update(validation=issues, queue=[], judgments=[], audit=False, grounding=None)
        if ref:
            rec["reference"] = {"path": str(ref_path(out, img)), "engines": [e["engine"] for e in ref["engines"] if e.get("ok")],
                                "quality": ref["quality"]}
        decide(rec, sc, ref, img, out, p)
        log(f"[{p:03d}] rescore: recall={sc['metrics'].get('recall')} unsupported={sc['metrics'].get('unsupported_ratio')} "
            f"-> {sc['decision']} {sc['reasons']}")
        save(out, img, rec)
    return build_report(slides, pages, out, a)


# --------------------------------------------------------------------------- audit sampling
def pick_audits(slides: list, recs: dict, out: Path, rate: float, min_n: int = 2) -> int:
    """Mark a deterministic sample of accepted slides for a blind thumbnail read. The hash of the file stem
    decides, so the sample is stable across re-runs; at least min_n accepted slides are audited."""
    accepted = [(p, r) for p, r in recs.items() if r.get("status") == "accepted" and r.get("analysis")]
    if not accepted or rate <= 0:
        return 0
    chosen = [(p, r) for p, r in accepted if r.get("audit") or audit_hash(slides[p - 1].stem) < rate]
    if len(chosen) < min(min_n, len(accepted)):
        chosen = sorted(accepted, key=lambda pr: audit_hash(slides[pr[0] - 1].stem))[:min(min_n, len(accepted))]
    n = 0
    for p, r in chosen:
        if r.get("audit"):
            continue
        img = slides[p - 1]
        try:
            entries = cropmod.prepare(img, read_json(ref_path(out, img)), r.get("score") or {}, r["analysis"],
                                      out / "crops", img.stem, p, audit=True)
        except Exception as e:
            log(f"[{p:03d}] audit thumbnail failed: {e!r}")
            continue
        r["audit"] = True
        r["queue"] = (r.get("queue") or []) + entries
        save(out, img, r)
        n += 1
    return n


# --------------------------------------------------------------------------- report + queue
def pending_entries(rec: dict) -> list:
    done = {j["id"] for j in rec.get("judgments") or []}
    return [e for e in rec.get("queue") or [] if e["id"] not in done]


def build_report(slides: list, pages: list, out: Path, a=None) -> dict:
    recs = {}
    for p in pages:
        f = out / "results" / f"{slides[p - 1].stem}.json"
        if f.exists():
            recs[p] = json.loads(f.read_text(encoding="utf-8"))
    if a is not None and getattr(a, "reviewer", "none") != "agy":
        pick_audits(slides, recs, out, getattr(a, "audit_rate", 0.1))
    dup = cross_check({p: r.get("analysis") for p, r in recs.items()})
    rows, all_slides, counts, queue, grounding = [], [], {}, [], []
    audited = issues = 0
    for p in pages:
        r = recs.get(p)
        status = r["status"] if r else "missing"
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
        pend = pending_entries(r)
        queue += pend
        if r.get("audit"):
            aj = [j for j in r.get("judgments") or [] if j["kind"] == "thumb_audit"]
            if aj:
                audited += 1
                issues += any(j.get("verdict") == "issue" for j in aj)
        q_col = f"{len(pend)} pending" if pend else (f"{len(r.get('judgments') or [])} judged" if r.get("judgments") else "-")
        rv = r.get("review") or {}
        legacy = f" {rv.get('verdict')}/{rv.get('text_recall')}" if rv else ""
        rows.append(f"| {p} | {r['file']} | {status} | {r.get('decision', '')}{legacy} | {an.get('slide_type', '')} | "
                    f"{m.get('recall', '')} | {m.get('unsupported_ratio', '')} | {m.get('digit_recall', '')} | "
                    f"{', '.join(flags) or '-'} | {q_col} |")
        if an:
            all_slides.append({**an, "_qa": {"status": status, "decision": r.get("decision"), "attempts": len(r["attempts"]),
                                             "metrics": m, "reasons": sc.get("reasons"), "triggers": sc.get("triggers"),
                                             "validation": [i["code"] for i in issues_v],
                                             "judgments": [{k: v for k, v in j.items() if k in ("id", "kind", "verdict", "claude_text", "note")}
                                                           for j in r.get("judgments") or []],
                                             "grounding": r.get("grounding"), "review": rv or None}})
            # text-only grounding check: slides whose interpretation may outrun the transcription
            want = getattr(a, "grounding_check", "flagged") if a is not None else "flagged"
            ungrounded = any(c in ("concept_ungrounded", "title_ungrounded") for c in (i["code"] for i in issues_v))
            if r.get("grounding") is None and status not in ("failed",) and (want == "all" or (want == "flagged" and ungrounded)):
                from ocr_score import corpus_text
                grounding.append({"page": p, "stem": slides[p - 1].stem, "verbatim": corpus_text(an), "claims": grounding_claims(an)})
    (out / "all_slides.json").write_text(json.dumps(all_slides, ensure_ascii=False, indent=2), encoding="utf-8")

    est = cropmod.token_estimate(queue)
    if queue:
        cropmod.write_queue(out, queue, note=f"슬라이드 폴더: `{slides[0].parent}`")
    else:
        for n in ("review_queue.md", "review_queue.json"):
            if (out / n).exists():
                (out / n).unlink()
    if grounding:
        cropmod.write_grounding_queue(out, grounding)
    n_acc = counts.get("accepted", 0)
    resid = ""
    if audited:
        rate = issues / audited
        upper = min(1.0, (issues + 3) / audited) if issues == 0 else min(1.0, rate + 1.0 / audited ** 0.5 * (rate * (1 - rate)) ** 0.5 * 1.96)
        resid = f"audited {audited} of {n_acc} accepted, {issues} with issues → estimated residual error {rate:.0%} (upper ≈ {upper:.0%})"
    head = ["# Analysis QA report", "",
            "Status: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), "",
            f"Review queue: {len(queue)} pending item(s) in `review_queue.md` (≈{est} image tokens for Claude)" if queue
            else "Review queue: empty",
            f"Audit: {resid}" if resid else f"Audit: no audited slide judged yet ({n_acc} accepted)",
            f"Grounding queue: {len(grounding)} slide(s) in `grounding_queue.md` (text only)" if grounding else "Grounding queue: empty", "",
            "| page | file | status | decision | type | recall | unsupported | digits | flags | claude |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    (out / "report.md").write_text("\n".join(head + rows) + "\n", encoding="utf-8")
    return counts


# --------------------------------------------------------------------------- resolve (Claude's answers)
def cmd_resolve(slides: list, pages: list, out: Path, a) -> int:
    reads = read_json(out / "crop_reads.json", {}) or {}
    greads = read_json(out / "grounding_reads.json", {}) or {}
    if not reads and not greads:
        log("nothing to resolve: write crop_reads.json (and/or grounding_reads.json) first; see review_queue.md")
    changed = 0
    for p in pages:
        img = slides[p - 1]
        f = out / "results" / f"{img.stem}.json"
        rec = read_json(f)
        if not rec or not rec.get("analysis"):
            continue
        pend = pending_entries(rec)
        new = cropmod.judge(pend, reads, rec["analysis"]) if pend else []
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
    counts = build_report(slides, pages, out, a)
    print(json.dumps({"out": str(out), "resolved_slides": changed, "counts": counts,
                      "report": str(out / "report.md")}, ensure_ascii=False, indent=2))
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
    for p in pages:
        img = slides[p - 1]
        rec = read_json(out / "results" / f"{img.stem}.json")
        gold = load_gold(gold_dir, img.stem)
        if not rec or not rec.get("analysis") or not gold:
            continue
        ref = read_json(ref_path(out, img))
        proxy = score(rec["analysis"], ref, th)
        truth = gold_metrics(gold, rec["analysis"])
        rows.append({"page": p, "file": img.name, "proxy": proxy, "truth": truth})
        if ref:  # how much of the human-verified text each OCR engine actually read
            from textnorm import bigrams, fuzzy_in, units
            from ocr_score import corpus_text
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
    (out / "calibration_report.md").write_text("\n".join(lines), encoding="utf-8")
    cal = {"thresholds": {k: v for k, v in sugg.items() if k in ("recall_accept", "unsupported_accept")},
           "stats": sugg, "ocr_quality": engines, "n_gold": len(rows), "source": str(gold_dir)}
    target = Path(a.write) if a.write else out / "calibration.json"
    if "error" not in sugg:
        target.write_text(json.dumps(cal, ensure_ascii=False, indent=2), encoding="utf-8")
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
        if not rec or not rec.get("analysis") or not gold:
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
    bf.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
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
    else:
        log("OCR engines: " + ", ".join(e.name for e in engines))
    return engines


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="slide_analyze.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="check agy + agy-delegate + OCR engines")
    for name in ("run", "report", "reference", "resolve", "rescore", "calibrate", "bench"):
        s = sub.add_parser(name)
        s.add_argument("--slides", required=True, help="folder of slide images (sorted by name = page order)")
        s.add_argument("--out", help="output folder (default <slides>/../slide_analysis)")
        s.add_argument("--pages", help="1-based page selection, e.g. 1-10,15")
        s.add_argument("--thresholds", help="thresholds/calibration JSON (default: <out>/calibration.json, then the skill's)")
        if name in ("run", "reference", "rescore"):
            s.add_argument("--engine", default=os.environ.get("OCR_ENGINE", "auto"),
                           help="OCR engines, comma list of winocr,tesseract (default auto = all available)")
            s.add_argument("-j", "--jobs", type=int, default=4, help="parallel sessions")
        if name in ("run", "report", "resolve", "rescore"):
            s.add_argument("--audit-rate", type=float, default=0.1, help="share of accepted slides Claude audits blind (min 2)")
            s.add_argument("--grounding-check", choices=["flagged", "all", "none"], default="flagged",
                           help="which slides go to the text-only grounding queue")
        if name == "run":
            s.add_argument("--subject", default=DEFAULT_SUBJECT, help="course subject for the personas, e.g. 데이터베이스")
            s.add_argument("--context", default=DEFAULT_CONTEXT, help="one-paragraph description of the deck")
            s.add_argument("--timeout", type=int, default=10, help="minutes per agy call")
            s.add_argument("--model", default=os.environ.get("AGY_TASK_MODEL"))
            s.add_argument("--work", help="scratch dir for agy workdirs (default <out>/.work)")
            s.add_argument("--force", action="store_true", help="redo slides that already have results")
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
    pages = parse_pages(a.pages, len(slides))
    th = load_thresholds(a.thresholds, out / "calibration.json", SKILL_DIR / "calibration.json")
    if th.get("_source"):
        log(f"thresholds: {th['_source']}")

    if a.cmd == "reference":
        engines = pick_engines(a)
        if not engines:
            return 4
        done = 0
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(get_reference, slides[pg - 1], out, engines): pg for pg in pages}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                    done += 1
                    log(f"[{futs[fut]:03d}] ref: {r['quality']['n_words']} words, usable={r['quality']['usable']}")
                except Exception as e:
                    log(f"[{futs[fut]:03d}] ref failed: {e!r}")
        print(json.dumps({"out": str(out / "ref"), "built": done, "of": len(pages)}, ensure_ascii=False))
        return 0
    if a.cmd == "resolve":
        return cmd_resolve(slides, pages, out, a)
    if a.cmd == "rescore":
        engines = pick_engines(a)
        a.reviewer = "none"
        counts = rescore(slides, pages, out, a, th, lambda img, p: get_reference(img, out, engines))
        print(json.dumps({"out": str(out), "counts": counts, "report": str(out / "report.md")}, ensure_ascii=False, indent=2))
        return 0 if set(counts) <= FINAL_OK else 2
    if a.cmd == "calibrate":
        return cmd_calibrate(slides, pages, out, a, th)
    if a.cmd == "bench":
        return cmd_bench(slides, pages, out, a)

    if a.cmd == "run":
        work = Path(a.work).resolve() if a.work else out / ".work"
        work.mkdir(parents=True, exist_ok=True)
        engines = pick_engines(a) if a.reviewer != "agy" else []

        def pending(pg):
            f = out / "results" / f"{slides[pg - 1].stem}.json"
            return a.force or not f.exists() or json.loads(f.read_text(encoding="utf-8"))["status"] in PENDING

        todo = [pg for pg in pages if pending(pg)]
        log(f"{len(todo)} slides to process (of {len(pages)}), jobs={a.jobs}, reviewer={a.reviewer}, out={out}")
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(process, agy_task, slides[pg - 1], pg, len(slides), out, work, a, engines, th): pg for pg in todo}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:  # keep the batch going
                    log(f"[{futs[fut]:03d}] crashed: {e!r}")
    counts = build_report(slides, pages, out, a)
    queue = read_json(out / "review_queue.json", []) or []
    print(json.dumps({"out": str(out), "counts": counts, "quota_exhausted": STOP.is_set(),
                      "review_queue": str(out / "review_queue.md") if queue else None, "queue_items": len(queue),
                      "claude_image_tokens_estimate": cropmod.token_estimate(queue),
                      "report": str(out / "report.md"), "all_slides": str(out / "all_slides.json")},
                     ensure_ascii=False, indent=2))
    if STOP.is_set():
        log("agy quota exhausted: re-run the same command after the reset to continue")
        return 3
    return 0 if set(counts) <= FINAL_OK else 2


if __name__ == "__main__":
    sys.exit(main())
