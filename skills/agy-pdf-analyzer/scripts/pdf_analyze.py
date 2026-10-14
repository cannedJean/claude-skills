#!/usr/bin/env python3
"""pdf_analyze.py - analyse every page of a lecture PDF with agy, a few pages per session, and verify each
page against a sealed local reference instead of a second agy look.

The PDF is never converted to images for agy: agy opens the PDF itself. The page count is read from the PDF
at run time, the pages are grouped into chunks (--chunk, default 5) and each chunk goes through:
  0. reference : per page, BEFORE the analyst runs - the page's text layer when it has one (born-digital
                 pages: a trusted reference), otherwise the embedded page image (scanned PDFs, via pypdf,
                 or a pymupdf render) run through local OCR (ocr_ref.py: winocr / tesseract + layout stats)
  1. analyst   : agy + prompts/analyst.md views the chunk PDF and writes page_XXXX.json per page
  2. validator : pdf_validate.py, deterministic schema / grounding / leak checks per page
  3. scorer    : ocr_score.py compares each page with its reference -> accept | retry | escalate
                 (retry feedback names the reference lines the analysis is missing; escalations queue
                 crops / a thumbnail of the page image for Claude, blind)
  4. audit + resolve: as in agy-slide-analyzer (shared code), see its SKILL.md

  preflight                       check agy + the agy-delegate skill + pypdf + OCR engines
  reference --pdf FILE            build/refresh the sealed page references only (no agy)
  run --pdf FILE [options]        analyse (resumable; finished pages are skipped)
  resolve --pdf FILE              apply crop_reads.json / grounding_reads.json, rebuild the report
  rescore --pdf FILE              re-run validator + scorer on stored analyses (no agy)
  report --pdf FILE               rebuild all_pages.json + report.md + the review queue from results
  calibrate --pdf FILE --gold DIR fit thresholds on human-corrected gold JSONs (page_XXXX.json)
  bench --pdf FILE --gold DIR     score a run against the gold set

Outputs in --out (default <pdf dir>/<pdf stem>_analysis):
  results/page_XXXX.json  {page, file, status, decision, attempts, analysis, validation, score, queue, judgments}
  ref/page_XXXX.json|png  sealed reference and the page image (when one could be extracted)
  crops/*.png             what Claude may look at      review_queue.md/json, crop_reads.json
  all_pages.json          list of analyses with a `_qa` block each
  report.md               one row per page + budget / audit summary
  source.json             name, sha256 and page count of the analysed PDF

Resume: re-running skips everything except `failed` (and `unreviewed`); `--force` redoes the selection.
On agy RESOURCE_EXHAUSTED the whole run stops and exits 3; queued chunks stay pending.

Exit codes: 0 all accepted, 2 some pages escalated / need attention / failed, 3 agy quota exhausted,
4 preflight / dependency failure, 5 bad arguments.
Python 3.9+. Requires the agy-delegate and agy-slide-analyzer skills (shared evaluation code), Pillow;
pypdf is recommended (page count, chunk cutting, page image extraction).
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
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf_validate import validate_analysis  # noqa: E402

try:
    import pypdf
except ImportError:
    pypdf = None

SKILL_DIR = Path(__file__).resolve().parent.parent
PROMPTS = SKILL_DIR / "prompts"
MAX_ATTEMPTS = 2
DEFAULT_SUBJECT = "해당"
DEFAULT_CONTEXT = "강의 자료 PDF입니다."
PYPDF_HINT = "install pypdf (`python -m pip install pypdf`) or pass --total N (every session then loads the whole PDF)"
QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|quota reached|\(code 429\)", re.I)
VERDICTS = {"pass", "minor_issues", "major_issues"}
PENDING = {"failed", "unreviewed"}
FINAL_OK = {"accepted", "ok"}
_lock = threading.Lock()
STOP = threading.Event()  # set on quota exhaustion: running chunks finish their call, queued ones are skipped

# staged: file name agy sees; base: page number (in the whole PDF) of the staged file's first page; count: pages in it
Chunk = namedtuple("Chunk", "label first last path staged base count")


def find_shared() -> Path:
    """The evaluation code lives in agy-slide-analyzer/scripts (ocr_ref, ocr_score, crops, slide_analyze)."""
    cands = [os.environ.get("OCR_EVAL_DIR"),
             SKILL_DIR.parent / "agy-slide-analyzer" / "scripts",
             Path.home() / ".claude" / "skills" / "agy-slide-analyzer" / "scripts"]
    for c in cands:
        if c and (Path(c) / "ocr_score.py").is_file():
            return Path(c)
    raise FileNotFoundError("agy-slide-analyzer skill not found (its scripts/ holds the shared evaluation code); "
                            "install it next to this skill or set OCR_EVAL_DIR")


try:
    SHARED = find_shared()
    sys.path.insert(0, str(SHARED))
    import crops as cropmod  # noqa: E402
    import ocr_ref  # noqa: E402
    import slide_analyze as sa  # noqa: E402
    from ocr_score import feedback_text, load_thresholds, score  # noqa: E402
    from textnorm import norm  # noqa: E402
    SHARED_ERR = None
except Exception as _e:  # reported in main()
    SHARED_ERR = _e


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


def parse_pages(spec, total):
    if not spec:
        return list(range(1, total + 1))
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return [p for p in out if 1 <= p <= total]


def plan_chunks(total: int, size: int) -> list:
    """[(first, last)] on a fixed grid, so a page always lands in the same chunk whatever --pages says."""
    if size <= 0:
        return [(1, total)]
    return [(s, min(s + size - 1, total)) for s in range(1, total + 1, size)]


def make_chunk(pdf: Path, reader, first: int, last: int, total: int, work: Path, split: bool) -> Chunk:
    label = f"p{first:04d}-{last:04d}"
    if not split or (first, last) == (1, total):
        return Chunk(label, first, last, pdf, "lecture.pdf", 1, total)
    dest = work / "chunks" / f"lecture_{label}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    w = pypdf.PdfWriter()
    for i in range(first - 1, last):
        w.add_page(reader.pages[i])
    with open(dest, "wb") as f:
        w.write(f)
    return Chunk(label, first, last, dest, dest.name, first, last - first + 1)


def load_json(path: Path):
    text = path.read_text(encoding="utf-8-sig").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def rec_path(out: Path, page: int) -> Path:
    return out / "results" / f"page_{page:04d}.json"


def load_rec(out: Path, page: int):
    f = rec_path(out, page)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def save(out: Path, rec: dict) -> dict:
    rec_path(out, rec["page"]).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def page_image_path(out: Path, page: int) -> Path:
    return out / "ref" / f"page_{page:04d}.png"


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


def call_agy(agy_task: Path, workdir: Path, prompt: str, stage: list, expect: list, a):
    """One agy session in a fresh workdir. Returns (error | None, quota_message | None); the caller reads
    whichever of the `expect` files exist, so a session that died half-way still yields its finished pages.
    agy's stdout is not trusted for text (console code page on Windows); only the UTF-8 files it writes are read."""
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
        return "timeout", None
    missing = [n for n in expect if not (workdir / n).exists()]
    if not missing:
        return None, None
    return (f"agy rc={cp.returncode}, not written: {', '.join(missing)}: {cp.stderr.decode('utf-8', 'replace')[-400:]}",
            quota_message(workdir, cp))


def prompt_ctx(ch: Chunk, pages: list, total: int, a) -> dict:
    desc = "원본 PDF 전체" if ch.count == total else f"원본 PDF의 {ch.base}~{ch.base + ch.count - 1}쪽만 잘라낸 파일"
    return dict(subject=a.subject, deck_context=a.context, total=total, pdf_name=a.pdf_name,
                chunk_file=ch.staged, chunk_desc=desc, chunk_pages=ch.count, n_targets=len(pages))


# --------------------------------------------------------------------------- sealed page references
def page_reference(pdf: Path, page: int, out: Path, engines: list, src_sha: str):
    """Reference for one page, built before the analyst sees it. A page with a real text layer gets a trusted
    text reference (plus the page image for thumbnails when one can be extracted); a scanned page gets the
    embedded image run through local OCR. Returns None when neither is possible."""
    img = page_image_path(out, page)
    rp = out / "ref" / f"page_{page:04d}.json"
    rp.parent.mkdir(parents=True, exist_ok=True)
    if not img.exists():
        pil = ocr_ref.pdf_page_image(pdf, page)
        if pil is not None:
            pil.save(img)
    text = ocr_ref.pdf_page_text(pdf, page)
    if len(norm(text)) >= 20:
        ref = sa.read_json(rp)
        if ref and ref.get("source_sha") == src_sha and ref.get("primary") == "pdf_text":
            return ref
        ref = ocr_ref.text_reference(text, pdf.name)
        ref["source_sha"] = src_sha
        if img.exists() and ocr_ref.Image is not None:
            with ocr_ref.Image.open(img) as im:
                ref["size"] = list(im.size)
                ref["layout"] = ocr_ref.layout_stats(im, [])
        rp.write_text(json.dumps(ref, ensure_ascii=False, indent=1), encoding="utf-8")
        return ref
    if img.exists() and engines:
        return ocr_ref.load_or_build(img, rp, engines)
    return None


# --------------------------------------------------------------------------- analyst / reviewer sessions
def run_analyst(agy_task, ch: Chunk, pages: list, attempt: int, feedback: dict, recs: dict, state: dict,
                total: int, wd: Path, a):
    """Analyse `pages` of the chunk in one agy session; fills recs and state[p] = clean | invalid | failed.
    Raises QuotaExhausted after recording whatever pages were written."""
    names = {p: f"page_{p:04d}.json" for p in pages}
    table = "\n".join(f"| {p - ch.base + 1}번째 | {p} | `{names[p]}` |" for p in pages)
    fb = "\n".join(f"## {p}쪽 (`{names[p]}`)\n{feedback[p]}" for p in pages if feedback.get(p))
    if fb:
        fb = "# 이전 시도에 대한 피드백 (반드시 반영하되, PDF 페이지를 직접 다시 보고 사실인지 확인하세요)\n" + fb + "\n"
    prompt = (PROMPTS / "analyst.md").read_text(encoding="utf-8").format(
        page_table=table, feedback=fb, **prompt_ctx(ch, pages, total, a))
    awd = wd / f"analyze_{attempt}"
    err, quota = call_agy(agy_task, awd, prompt, [(ch.path, ch.staged)], list(names.values()), a)
    for p in pages:
        rec, f = recs[p], awd / names[p]
        if quota and not f.exists():
            continue  # no attempt was made; the page stays pending
        att = {"attempt": len(rec["attempts"]) + 1, "error": None}
        rec["attempts"].append(att)
        data = None
        if not f.exists():
            att["error"] = err or f"{names[p]} not written"
        else:
            try:
                data = load_json(f)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                att["error"] = f"invalid JSON in {names[p]}: {e}"
        if att["error"]:
            state[p] = "failed"
            log(f"[{p:03d}] analyze#{attempt} failed: {att['error'][:160]}")
            continue
        if isinstance(data, dict):
            data = {"page": data.get("page"), "file": a.pdf_name,
                    **{k: v for k, v in data.items() if k not in ("page", "file")}}
        issues = validate_analysis(data, p)
        att["validation"] = issues
        rec.update(analysis=data, validation=issues, review=None)
        state[p] = "invalid" if any(i["level"] == "error" for i in issues) else "clean"
    if quota:
        raise QuotaExhausted(quota)


def run_reviewer(agy_task, ch: Chunk, pages: list, attempt: int, recs: dict, total: int, wd: Path, a):
    """Legacy same-vendor review of `pages` in one agy session; sets recs[p]['review'].
    Raises QuotaExhausted after recording whatever reviews were written."""
    src = wd / f"review_{attempt}_in"
    src.mkdir(parents=True, exist_ok=True)
    stage, names, rows = [(ch.path, ch.staged)], {}, []
    for p in pages:
        f = src / f"analysis_page_{p:04d}.json"
        f.write_text(json.dumps(recs[p]["analysis"], ensure_ascii=False, indent=2), encoding="utf-8")
        stage.append((f, f.name))
        names[p] = f"review_page_{p:04d}.json"
        rows.append(f"| {p - ch.base + 1}번째 | {p} | `{f.name}` | `{names[p]}` |")
    prompt = (PROMPTS / "reviewer.md").read_text(encoding="utf-8").format(
        page_table="\n".join(rows), **prompt_ctx(ch, pages, total, a))
    rwd = wd / f"review_{attempt}"
    err, quota = call_agy(agy_task, rwd, prompt, stage, list(names.values()), a)
    for p in pages:
        f = rwd / names[p]
        if not f.exists():
            review = {"verdict": "review_failed", "error": err or f"{names[p]} not written"}
        else:
            try:
                review = load_json(f)
                if not isinstance(review, dict) or review.get("verdict") not in VERDICTS:
                    review = {"verdict": "review_failed", "error": f"no valid verdict in {names[p]}"}
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                review = {"verdict": "review_failed", "error": f"invalid JSON in {names[p]}: {e}"}
        recs[p]["review"] = review
        if recs[p]["attempts"]:
            recs[p]["attempts"][-1]["review"] = review
    if quota:
        raise QuotaExhausted(quota)


# --------------------------------------------------------------------------- chunk pipeline (reference + scorer)
def finalize(rec: dict, sc: dict, ref, img: Path, out: Path):
    p = rec["page"]
    d = sc["decision"]
    if d == "accept":
        rec["status"] = "accepted"
    elif d == "unverified":
        rec["status"] = "unverified"
    elif d == "escalate":
        if img.exists():
            try:
                rec["queue"] = cropmod.prepare(img, ref, sc, rec["analysis"], out / "crops", f"page_{p:04d}", p)
            except Exception as e:
                log(f"[{p:03d}] crop preparation failed: {e!r}")
                rec["queue"] = []
            rec["status"] = "escalated" if rec["queue"] else "needs_attention"
        else:
            rec["status"] = "unverified"  # nothing to show the reviewer
    else:
        rec["status"] = "needs_attention"
    save(out, rec)


def process_chunk(agy_task, ch: Chunk, targets: list, total: int, out: Path, work: Path, a, engines, th, src_sha):
    """Analyse the pending pages of one chunk against their sealed references."""
    if STOP.is_set():
        return
    wd = work / ch.label
    recs, refs = {}, {}
    for p in targets:
        recs[p] = {"page": p, "file": a.pdf_name, "status": "failed", "attempts": [], "queue": [], "judgments": []}
        try:
            refs[p] = page_reference(a.pdf_path, p, out, engines, src_sha)
        except Exception as e:
            log(f"[{p:03d}] reference failed: {e!r}")
            refs[p] = None
        if refs[p]:
            recs[p]["reference"] = {"primary": refs[p].get("primary"), "quality": refs[p].get("quality"),
                                    "image": str(page_image_path(out, p)) if page_image_path(out, p).exists() else None}
    analyse, feedback, scored = list(targets), {}, set()
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            state = {}
            run_analyst(agy_task, ch, analyse, attempt, feedback, recs, state, total, wd, a)
            analyse = []
            for p, s in sorted(state.items()):
                rec = recs[p]
                if s == "failed":
                    feedback[p] = ""
                    analyse.append(p)
                    continue
                errors = [i for i in rec["validation"] if i["level"] == "error"]
                sc = score(rec["analysis"], refs[p], th, validator_errors=errors)
                rec["attempts"][-1].update(decision=sc["decision"], reasons=sc["reasons"], metrics=sc["metrics"])
                rec.update(score=sc, decision=sc["decision"])
                scored.add(p)
                log(f"[{p:03d}] {ch.label}#{attempt}: {len(errors)} errors, recall={sc['metrics'].get('recall')}, "
                    f"unsupported={sc['metrics'].get('unsupported_ratio')} -> {sc['decision']} {sc['reasons']}")
                if sc["decision"] == "retry":
                    feedback[p] = feedback_text(sc, errors)
                    rec["status"] = "needs_attention"
                    analyse.append(p)
                else:
                    finalize(rec, sc, refs[p], page_image_path(out, p), out)
            if not analyse:
                break
        for p in analyse:  # out of attempts
            save(out, recs[p])
    except QuotaExhausted as e:
        if not STOP.is_set():
            STOP.set()
            log(f"[{ch.label}] agy quota exhausted ({e}); stopping - queued pages are left pending")
        for p, rec in recs.items():  # pages whose analysis was written before the cut-off are scored anyway
            if rec.get("analysis") and p not in scored:
                errors = [i for i in rec.get("validation") or [] if i["level"] == "error"]
                sc = score(rec["analysis"], refs.get(p), th, validator_errors=errors)
                rec.update(score=sc, decision=sc["decision"])
                if sc["decision"] == "retry":
                    rec["status"] = "needs_attention"
                    save(out, rec)
                else:
                    finalize(rec, sc, refs.get(p), page_image_path(out, p), out)


def feedback_for_legacy(rec: dict) -> str:
    lines = [f"- [검증기] {i['code']}: {i['msg']}" for i in rec.get("validation") or [] if i["level"] == "error"]
    review = rec.get("review") or {}
    if review.get("verdict") == "major_issues":
        lines.append(f"- [검수자] {review.get('feedback_for_analyst', '')}")
        lines += [f"- [검수자 누락 지적] {m}" for m in review.get("missing", [])]
        lines += [f"- [검수자 환각 지적] {h}" for h in review.get("hallucinated", [])]
    return "\n".join(lines)


def process_chunk_legacy(agy_task, ch: Chunk, targets: list, total: int, out: Path, work: Path, a):
    """The original flow (analyst -> validator -> agy reviewer -> retry), kept for comparison runs."""
    if STOP.is_set():
        return
    wd = work / ch.label
    recs, analyse, carried = {}, [], []
    for p in targets:
        prev = None if a.force else load_rec(out, p)
        if prev and prev.get("status") == "unreviewed" and prev.get("analysis"):
            recs[p] = prev
            carried.append(p)
        else:
            recs[p] = {"page": p, "file": a.pdf_name, "status": "failed", "attempts": []}
            analyse.append(p)
    feedback, state = {}, {}
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            state = {p: "clean" for p in carried}
            carried = []
            if analyse:
                run_analyst(agy_task, ch, analyse, attempt, feedback, recs, state, total, wd, a)
            clean = sorted(p for p, s in state.items() if s == "clean")
            if clean:
                run_reviewer(agy_task, ch, clean, attempt, recs, total, wd, a)
            analyse = []
            for p, s in sorted(state.items()):
                rec = recs[p]
                verdict = (rec.get("review") or {}).get("verdict") if s == "clean" else None
                log(f"[{p:03d}] {ch.label}#{attempt}: {s}, review={verdict}")
                if s == "clean" and verdict != "major_issues":
                    rec["status"] = "ok" if verdict in ("pass", "minor_issues", None) else "unreviewed"
                    save(out, rec)
                    continue
                if s != "failed":
                    rec["status"] = "needs_attention"
                feedback[p] = "" if s == "failed" else feedback_for_legacy(rec)
                analyse.append(p)
            state = {}
            if not analyse:
                break
        for p in analyse:
            save(out, recs[p])
    except QuotaExhausted as e:
        if not STOP.is_set():
            STOP.set()
            log(f"[{ch.label}] agy quota exhausted ({e}); stopping - queued pages are left pending")
        for p, s in state.items():
            if s == "clean":
                verdict = (recs[p].get("review") or {}).get("verdict")
                recs[p]["status"] = "ok" if verdict in ("pass", "minor_issues") else "unreviewed"
                save(out, recs[p])


# --------------------------------------------------------------------------- PDF handling
def open_pdf(a):
    """Returns (pdf path, pypdf reader | None, total pages). The page count comes from the PDF itself."""
    pdf = Path(a.pdf).resolve()
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        log(f"ERROR: --pdf {pdf} is not a PDF file")
        sys.exit(5)
    if pypdf is None:
        if not a.total:
            log(f"ERROR: pypdf is not installed, so the page count is unknown; {PYPDF_HINT}")
            sys.exit(4)
        return pdf, None, a.total
    try:
        reader = pypdf.PdfReader(str(pdf))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("the PDF is password-protected")
        total = len(reader.pages)
    except Exception as e:  # pypdf raises several unrelated types on damaged files
        log(f"ERROR: cannot read {pdf.name}: {e}")
        sys.exit(5)
    if not total:
        log(f"ERROR: {pdf.name} has no pages")
        sys.exit(5)
    return pdf, reader, total


def check_source(out: Path, pdf: Path, total: int, force: bool) -> str:
    """Refuse to mix results of two different PDFs in one output folder. Returns the PDF sha256."""
    h = hashlib.sha256()
    with open(pdf, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    sig = {"file": pdf.name, "sha256": h.hexdigest(), "pages": total}
    f = out / "source.json"
    if f.exists() and not force and any((out / "results").glob("page_*.json")):
        old = json.loads(f.read_text(encoding="utf-8"))
        if old.get("sha256") != sig["sha256"]:
            log(f"ERROR: {out} holds results for a different PDF ({old.get('file')}, {old.get('pages')} pages); "
                "use another --out, or --force to overwrite")
            sys.exit(5)
    f.write_text(json.dumps(sig, ensure_ascii=False, indent=2), encoding="utf-8")
    return sig["sha256"]


def rename_outputs(out: Path):
    """The shared report writer names the deck file all_slides.json; this skill documents all_pages.json."""
    src = out / "all_slides.json"
    if src.exists():
        shutil.copyfile(src, out / "all_pages.json")
        src.unlink()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pdf_analyze.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="check agy + agy-delegate + pypdf + OCR engines")
    for name in ("run", "report", "reference", "resolve", "rescore", "calibrate", "bench"):
        s = sub.add_parser(name)
        s.add_argument("--pdf", required=True, help="the PDF file to analyse (all pages unless --pages)")
        s.add_argument("--out", help="output folder (default <pdf dir>/<pdf stem>_analysis)")
        s.add_argument("--pages", help="1-based page selection, e.g. 1-10,15")
        s.add_argument("--total", type=int, help="page count of the PDF; only needed when pypdf is not installed")
        s.add_argument("--thresholds", help="thresholds/calibration JSON (default: <out>/calibration.json, then the skill's)")
        if name in ("run", "reference", "rescore"):
            s.add_argument("--engine", default=os.environ.get("OCR_ENGINE", "auto"),
                           help="OCR engines, comma list of winocr,tesseract (default auto = all available)")
            s.add_argument("-j", "--jobs", type=int, default=3, help="parallel sessions")
        if name in ("run", "report", "resolve", "rescore"):
            s.add_argument("--audit-rate", type=float, default=0.1, help="share of accepted pages Claude audits blind (min 2)")
            s.add_argument("--grounding-check", choices=["flagged", "all", "none"], default="flagged")
        if name == "run":
            s.add_argument("--subject", default=DEFAULT_SUBJECT, help="course subject for the personas, e.g. 데이터베이스")
            s.add_argument("--context", default=DEFAULT_CONTEXT, help="one-paragraph description of the PDF")
            s.add_argument("--chunk", type=int, default=5, help="pages per agy session (0 = the whole PDF in one session)")
            s.add_argument("--no-split", action="store_true",
                           help="hand every session the whole PDF instead of a cut-out of its pages")
            s.add_argument("--timeout", type=int, default=20, help="minutes per agy call")
            s.add_argument("--model", default=os.environ.get("AGY_TASK_MODEL"))
            s.add_argument("--work", help="scratch dir for agy workdirs (default <out>/.work)")
            s.add_argument("--force", action="store_true", help="redo pages that already have results")
            s.add_argument("--reviewer", choices=["none", "agy"], default="none",
                           help="none = sealed reference + Claude (default); agy = legacy same-vendor reviewer session")
        if name in ("calibrate", "bench"):
            s.add_argument("--gold", required=True, help="folder of human-corrected analyses, page_XXXX.json")
        if name == "calibrate":
            s.add_argument("--target-false-accept", type=float, default=0.05)
            s.add_argument("--write", help="where to write calibration.json (default <out>/calibration.json)")
        if name == "bench":
            s.add_argument("--label", default="run", help="name of this prompt/model variant")
    a = p.parse_args(argv)

    if SHARED_ERR:
        log(f"ERROR: {SHARED_ERR}")
        return 4
    try:
        agy_task = find_agy_task()
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 4
    if a.cmd == "preflight":
        log(f"pypdf: {pypdf.__version__}" if pypdf else f"pypdf: not installed - {PYPDF_HINT}")
        log(f"shared evaluation code: {SHARED}")
        for e in ocr_ref.make_engines():
            log(f"OCR {e.name}: {'available' if e.available() else 'unavailable'} {json.dumps(e.info(), ensure_ascii=False)}")
        return subprocess.call([sys.executable, str(agy_task), "preflight"])

    pdf, reader, total = open_pdf(a)
    a.pdf_name, a.pdf_path = pdf.name, pdf
    out = Path(a.out).resolve() if a.out else pdf.parent / f"{pdf.stem}_analysis"
    (out / "results").mkdir(parents=True, exist_ok=True)
    pages = parse_pages(a.pages, total)
    th = load_thresholds(a.thresholds, out / "calibration.json", SKILL_DIR / "calibration.json")
    if th.get("_source"):
        log(f"thresholds: {th['_source']}")
    # the shared report/resolve/calibrate code addresses pages through "slide" paths: ref/page_XXXX.png
    pseudo = [page_image_path(out, pg) for pg in range(1, total + 1)]
    if not getattr(a, "reviewer", None):
        a.reviewer = "none"

    if a.cmd == "reference":
        src_sha = check_source(out, pdf, total, True)
        engines = sa.pick_engines(a)
        built = 0
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(page_reference, pdf, pg, out, engines, src_sha): pg for pg in pages}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                    built += bool(r)
                    log(f"[{futs[fut]:03d}] ref: {r['primary'] if r else 'none'} "
                        f"{(r or {}).get('quality', {}).get('n_words', 0)} words")
                except Exception as e:
                    log(f"[{futs[fut]:03d}] ref failed: {e!r}")
        print(json.dumps({"out": str(out / "ref"), "built": built, "of": len(pages)}, ensure_ascii=False))
        return 0
    if a.cmd == "resolve":
        rc = sa.cmd_resolve(pseudo, pages, out, a)
        rename_outputs(out)
        return rc
    if a.cmd == "rescore":
        src_sha = check_source(out, pdf, total, True)
        engines = sa.pick_engines(a)
        counts = sa.rescore(pseudo, pages, out, a, th, lambda img, pg: page_reference(pdf, pg, out, engines, src_sha),
                            lambda an, pg, f: validate_analysis(an, pg))
        rename_outputs(out)
        print(json.dumps({"out": str(out), "counts": counts, "report": str(out / "report.md")}, ensure_ascii=False, indent=2))
        return 0 if set(counts) <= FINAL_OK else 2
    if a.cmd == "calibrate":
        return sa.cmd_calibrate(pseudo, pages, out, a, th)
    if a.cmd == "bench":
        return sa.cmd_bench(pseudo, pages, out, a)

    if a.cmd == "run":
        src_sha = check_source(out, pdf, total, a.force)
        work = Path(a.work).resolve() if a.work else out / ".work"
        work.mkdir(parents=True, exist_ok=True)
        engines = sa.pick_engines(a) if a.reviewer != "agy" else []
        redo = {"failed"} if a.reviewer != "agy" else PENDING

        def pending(pg):
            rec = None if a.force else load_rec(out, pg)
            return not rec or rec["status"] in redo

        todo = {pg for pg in pages if pending(pg)}
        split = reader is not None and not a.no_split
        jobs = []
        for first, last in plan_chunks(total, a.chunk):
            targets = [pg for pg in range(first, last + 1) if pg in todo]
            if targets:
                jobs.append((make_chunk(pdf, reader, first, last, total, work, split), targets))
        log(f"{pdf.name}: {total} pages; {len(todo)} to process (of {len(pages)} selected) in {len(jobs)} chunks, "
            f"split={split}, jobs={a.jobs}, reviewer={a.reviewer}, out={out}")
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            if a.reviewer == "agy":
                futs = {ex.submit(process_chunk_legacy, agy_task, ch, targets, total, out, work, a): ch for ch, targets in jobs}
            else:
                futs = {ex.submit(process_chunk, agy_task, ch, targets, total, out, work, a, engines, th, src_sha): ch
                        for ch, targets in jobs}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:  # keep the batch going
                    log(f"[{futs[fut].label}] crashed: {e!r}")
    counts = sa.build_report(pseudo, pages, out, a)
    rename_outputs(out)
    queue = sa.read_json(out / "review_queue.json", []) or []
    print(json.dumps({"pdf": str(pdf), "pages": total, "out": str(out), "counts": counts,
                      "quota_exhausted": STOP.is_set(), "review_queue": str(out / "review_queue.md") if queue else None,
                      "queue_items": len(queue), "claude_image_tokens_estimate": cropmod.token_estimate(queue),
                      "report": str(out / "report.md"), "all_pages": str(out / "all_pages.json")}, ensure_ascii=False, indent=2))
    if STOP.is_set():
        log("agy quota exhausted: re-run the same command after the reset to continue")
        return 3
    return 0 if set(counts) <= FINAL_OK else 2


if __name__ == "__main__":
    sys.exit(main())
