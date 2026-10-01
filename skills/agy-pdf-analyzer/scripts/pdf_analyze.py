#!/usr/bin/env python3
"""pdf_analyze.py - analyse every page of a lecture PDF with agy, a few pages per session.

The PDF is never converted to images: agy opens the PDF itself. The page count is read from the PDF at
run time, the pages are grouped into chunks (--chunk, default 5) and each chunk goes through:
  1. analyst  : agy + prompts/analyst.md views the chunk PDF and writes page_XXXX.json per page
  2. validator: pdf_validate.py, deterministic schema / grounding / leak checks per page
  3. reviewer : a second agy session + prompts/reviewer.md compares the pages with the analyses
  4. retry    : pages with validator errors or a `major_issues` verdict are re-analysed once with feedback

  preflight                   check agy + the agy-delegate skill + pypdf
  run --pdf FILE [options]    analyse (resumable; finished pages are skipped)
  report --pdf FILE           rebuild all_pages.json + report.md from existing results

Outputs in --out (default <pdf dir>/<pdf stem>_analysis):
  results/page_XXXX.json  {page, file, status, attempts, analysis, validation, review}
  all_pages.json          list of analyses with a `_qa` block each
  report.md               one row per page: status, type, confidence, verdict/recall, flags
  source.json             name, sha256 and page count of the analysed PDF

With pypdf installed each session only gets its own pages (a small PDF cut from the original). Without
pypdf (or with --no-split) every session is handed the whole PDF and told which pages to do; the page
count must then be given with --total.

Resume: re-running skips ok/needs_attention pages, re-analyses `failed` ones and only re-reviews
`unreviewed` ones. On agy RESOURCE_EXHAUSTED the whole run stops and exits 3; queued chunks stay pending.

Exit codes: 0 all ok, 2 some pages need attention/failed, 3 agy quota exhausted, 4 preflight failure,
5 bad arguments.
Python 3.9+. Requires the agy-delegate skill (scripts/agy_task.py); pypdf is recommended.
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
from pdf_validate import cross_check, validate_analysis  # noqa: E402

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
_lock = threading.Lock()
STOP = threading.Event()  # set on quota exhaustion: running chunks finish their call, queued ones are skipped

# staged: file name agy sees; base: page number (in the whole PDF) of the staged file's first page; count: pages in it
Chunk = namedtuple("Chunk", "label first last path staged base count")


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
    """Review the analyses of `pages` in one agy session; sets recs[p]['review'].
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


def feedback_for(rec: dict) -> str:
    lines = [f"- [검증기] {i['code']}: {i['msg']}" for i in rec.get("validation") or [] if i["level"] == "error"]
    review = rec.get("review") or {}
    if review.get("verdict") == "major_issues":
        lines.append(f"- [검수자] {review.get('feedback_for_analyst', '')}")
        lines += [f"- [검수자 누락 지적] {m}" for m in review.get("missing", [])]
        lines += [f"- [검수자 환각 지적] {h}" for h in review.get("hallucinated", [])]
    return "\n".join(lines)


def process_chunk(agy_task, ch: Chunk, targets: list, total: int, out: Path, work: Path, a):
    """Analyse the pending pages of one chunk (or, for `unreviewed` records, only re-review them).
    Pages whose work was cut off by the agy quota are not written, so they stay pending."""
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
            state = {p: "clean" for p in carried}  # analyses that only lack a review
            carried = []
            if analyse:
                run_analyst(agy_task, ch, analyse, attempt, feedback, recs, state, total, wd, a)
            clean = sorted(p for p, s in state.items() if s == "clean")
            if clean and not a.skip_review:
                run_reviewer(agy_task, ch, clean, attempt, recs, total, wd, a)
            analyse = []
            for p, s in sorted(state.items()):
                rec = recs[p]
                verdict = (rec.get("review") or {}).get("verdict") if s == "clean" else None
                errors = sum(i["level"] == "error" for i in rec.get("validation") or []) if s != "failed" else "-"
                log(f"[{p:03d}] {ch.label}#{attempt}: {s}, {errors} errors, review={verdict}")
                if s == "clean" and verdict != "major_issues":
                    rec["status"] = "ok" if verdict in ("pass", "minor_issues", None) else "unreviewed"
                    save(out, rec)
                    continue
                if s != "failed":
                    rec["status"] = "needs_attention"
                feedback[p] = "" if s == "failed" else feedback_for(rec)
                analyse.append(p)
            state = {}
            if not analyse:
                break
        for p in analyse:  # out of attempts
            save(out, recs[p])
    except QuotaExhausted as e:
        if not STOP.is_set():
            STOP.set()
            log(f"[{ch.label}] agy quota exhausted ({e}); stopping - queued pages are left pending")
        for p, s in state.items():  # keep finished analyses; a re-run only reviews them
            if s == "clean":
                verdict = (recs[p].get("review") or {}).get("verdict")
                recs[p]["status"] = "ok" if verdict in ("pass", "minor_issues") else "unreviewed"
                save(out, recs[p])


def build_report(pages: list, out: Path) -> dict:
    recs = {p: r for p in pages if (r := load_rec(out, p))}
    dup = cross_check({p: r.get("analysis") for p, r in recs.items()})
    rows, all_pages, counts = [], [], {}
    for p in pages:
        r = recs.get(p)
        status = r["status"] if r else "missing"
        counts[status] = counts.get(status, 0) + 1
        if not r:
            rows.append(f"| {p} | missing | | | | |")
            continue
        an, rv = r.get("analysis") or {}, r.get("review") or {}
        if not isinstance(an, dict):
            an = {}
        issues = (r.get("validation") or []) + dup.get(p, [])
        flags = ", ".join(sorted({i["code"] for i in issues})) or "-"
        rows.append(f"| {p} | {status} | {an.get('slide_type', '')} | {an.get('confidence', '')} | "
                    f"{rv.get('verdict', '')} / {rv.get('text_recall', '')} | {flags} |")
        if an:
            all_pages.append({**an, "_qa": {"status": status, "attempts": len(r["attempts"]),
                                            "review_verdict": rv.get("verdict"), "text_recall": rv.get("text_recall"),
                                            "review_errors": rv.get("errors", []), "review_missing": rv.get("missing", []),
                                            "review_hallucinated": rv.get("hallucinated", []),
                                            "validation": [i["code"] for i in issues]}})
    (out / "all_pages.json").write_text(json.dumps(all_pages, ensure_ascii=False, indent=2), encoding="utf-8")
    head = ["# PDF analysis QA report", "",
            "Status: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), "",
            "| page | status | type | confidence | review verdict / recall | validator flags |",
            "| --- | --- | --- | --- | --- | --- |"]
    (out / "report.md").write_text("\n".join(head + rows) + "\n", encoding="utf-8")
    return counts


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


def check_source(out: Path, pdf: Path, total: int, force: bool):
    """Refuse to mix results of two different PDFs in one output folder."""
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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pdf_analyze.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="check agy + agy-delegate + pypdf")
    for name in ("run", "report"):
        s = sub.add_parser(name)
        s.add_argument("--pdf", required=True, help="the PDF file to analyse (all pages unless --pages)")
        s.add_argument("--out", help="output folder (default <pdf dir>/<pdf stem>_analysis)")
        s.add_argument("--pages", help="1-based page selection, e.g. 1-10,15")
        s.add_argument("--total", type=int, help="page count of the PDF; only needed when pypdf is not installed")
        if name == "run":
            s.add_argument("--subject", default=DEFAULT_SUBJECT, help="course subject for the personas, e.g. 데이터베이스")
            s.add_argument("--context", default=DEFAULT_CONTEXT, help="one-paragraph description of the PDF")
            s.add_argument("--chunk", type=int, default=5, help="pages per agy session (0 = the whole PDF in one session)")
            s.add_argument("--no-split", action="store_true",
                           help="hand every session the whole PDF instead of a cut-out of its pages")
            s.add_argument("-j", "--jobs", type=int, default=3, help="parallel agy sessions")
            s.add_argument("--timeout", type=int, default=20, help="minutes per agy call")
            s.add_argument("--model", default=os.environ.get("AGY_TASK_MODEL"))
            s.add_argument("--work", help="scratch dir for agy workdirs (default <out>/.work)")
            s.add_argument("--force", action="store_true", help="redo pages that already have results")
            s.add_argument("--skip-review", action="store_true", help="skip the agy reviewer pass")
    a = p.parse_args(argv)

    try:
        agy_task = find_agy_task()
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 4
    if a.cmd == "preflight":
        log(f"pypdf: {pypdf.__version__}" if pypdf else f"pypdf: not installed - {PYPDF_HINT}")
        return subprocess.call([sys.executable, str(agy_task), "preflight"])

    pdf, reader, total = open_pdf(a)
    a.pdf_name = pdf.name
    out = Path(a.out).resolve() if a.out else pdf.parent / f"{pdf.stem}_analysis"
    (out / "results").mkdir(parents=True, exist_ok=True)
    pages = parse_pages(a.pages, total)
    if a.cmd == "run":
        check_source(out, pdf, total, a.force)
        work = Path(a.work).resolve() if a.work else out / ".work"
        work.mkdir(parents=True, exist_ok=True)
        redo = {"failed"} if a.skip_review else {"failed", "unreviewed"}  # statuses a plain re-run picks up again

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
            f"split={split}, jobs={a.jobs}, out={out}")
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(process_chunk, agy_task, ch, targets, total, out, work, a): ch for ch, targets in jobs}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:  # keep the batch going
                    log(f"[{futs[fut].label}] crashed: {e!r}")
    counts = build_report(pages, out)
    print(json.dumps({"pdf": str(pdf), "pages": total, "out": str(out), "counts": counts,
                      "quota_exhausted": STOP.is_set(), "report": str(out / "report.md"),
                      "all_pages": str(out / "all_pages.json")}, ensure_ascii=False, indent=2))
    if STOP.is_set():
        log("agy quota exhausted: re-run the same command after the reset to continue")
        return 3
    return 0 if set(counts) <= {"ok"} else 2


if __name__ == "__main__":
    sys.exit(main())
