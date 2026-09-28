#!/usr/bin/env python3
"""slide_analyze.py - analyse a folder of lecture slide images with agy, one slide per session.

Pipeline per slide (the images are only ever opened by agy):
  1. analyst  : agy + prompts/analyst.md (persona, deck context, CoT, few-shot) -> slide_analysis.json
  2. validator: slide_validate.py, deterministic schema / grounding / leak checks
  3. reviewer : a second agy session + prompts/reviewer.md compares image vs analysis -> review.json
  4. retry    : on validator errors or a `major_issues` verdict, re-run the analyst once with feedback

  preflight                     check agy + the agy-delegate skill
  run --slides DIR [options]    analyse (resumable; finished slides are skipped)
  report --slides DIR           rebuild all_slides.json + report.md from existing results

Outputs in --out (default <slides>/../slide_analysis):
  results/<stem>.json   {page, file, status, attempts, analysis, validation, review}
  all_slides.json       list of analyses with a `_qa` block each
  report.md             one row per slide: status, type, confidence, verdict/recall, flags

Resume: re-running skips ok/needs_attention slides, re-analyses `failed` ones and only re-reviews
`unreviewed` ones. On agy RESOURCE_EXHAUSTED the whole run stops and exits 3; queued slides stay pending.

Exit codes: 0 all ok, 2 some slides need attention/failed, 3 agy quota exhausted, 4 preflight failure,
5 bad arguments.
Python 3.9+, stdlib only. Requires the agy-delegate skill (scripts/agy_task.py).
"""
from __future__ import annotations

import argparse
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
from slide_validate import cross_check, validate_analysis  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
PROMPTS = SKILL_DIR / "prompts"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
MAX_ATTEMPTS = 2
DEFAULT_SUBJECT = "해당"
DEFAULT_CONTEXT = "강의 슬라이드 묶음을 페이지별 이미지로 변환한 것입니다."
QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|quota reached|\(code 429\)", re.I)
PENDING = {"failed", "unreviewed"}  # statuses a plain re-run picks up again
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


def process(agy_task, img: Path, page: int, total: int, out: Path, work: Path, a):
    """Analyse (or, for an `unreviewed` record, only re-review) one slide. Returns the record, or None when
    skipped because agy ran out of quota (nothing is written, so the slide stays pending)."""
    if STOP.is_set():
        return None
    file = img.name
    ctx = dict(page=page, file=file, total=total, subject=a.subject, deck_context=a.context)
    prev_f = out / "results" / f"{img.stem}.json"
    prev = json.loads(prev_f.read_text(encoding="utf-8")) if prev_f.exists() and not a.force else None
    try:
        if prev and prev.get("status") == "unreviewed" and prev.get("analysis") and not a.skip_review:
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
            prev["status"] = "needs_attention"  # major_issues: fall through to a fresh analysis with feedback
        return analyse(agy_task, img, page, out, work, ctx, a)
    except QuotaExhausted as e:
        if not STOP.is_set():
            STOP.set()
            log(f"[{page:03d}] agy quota exhausted ({e}); stopping - queued slides are left pending")
        return None


def analyse(agy_task, img: Path, page: int, out: Path, work: Path, ctx: dict, a) -> dict:
    file = img.name
    analyst_t = (PROMPTS / "analyst.md").read_text(encoding="utf-8")
    rec = {"page": page, "file": file, "status": "failed", "attempts": []}
    feedback = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wd = work / img.stem / f"analyze_{attempt}"
        data, err = call_agy(agy_task, wd, analyst_t.format(feedback=feedback, **ctx),
                             "slide_analysis.json", [(img, file)], a)
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
        if not errors and not a.skip_review:
            try:
                review = run_review(agy_task, img, data, work / img.stem, attempt, ctx, a)
            except QuotaExhausted:
                rec["status"] = "unreviewed"  # keep the analysis; a re-run only reviews it
                save(out, img, rec)
                raise
        att["review"] = review
        rec["review"] = review
        verdict = (review or {}).get("verdict")
        log(f"[{page:03d}] analyze#{attempt}: {len(errors)} errors, {len(issues) - len(errors)} warns, review={verdict}")
        if not errors and verdict != "major_issues":
            rec["status"] = "ok" if verdict in ("pass", "minor_issues", None) else "unreviewed"
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


def build_report(slides: list, pages: list, out: Path) -> dict:
    recs = {}
    for p in pages:
        f = out / "results" / f"{slides[p - 1].stem}.json"
        if f.exists():
            recs[p] = json.loads(f.read_text(encoding="utf-8"))
    dup = cross_check({p: r.get("analysis") for p, r in recs.items()})
    rows, all_slides, counts = [], [], {}
    for p in pages:
        r = recs.get(p)
        status = r["status"] if r else "missing"
        counts[status] = counts.get(status, 0) + 1
        if not r:
            rows.append(f"| {p} | {slides[p - 1].name} | missing | | | | |")
            continue
        an, rv = r.get("analysis") or {}, r.get("review") or {}
        issues = (r.get("validation") or []) + dup.get(p, [])
        flags = ", ".join(sorted({i["code"] for i in issues})) or "-"
        rows.append(f"| {p} | {r['file']} | {status} | {an.get('slide_type', '')} | {an.get('confidence', '')} | "
                    f"{rv.get('verdict', '')} / {rv.get('text_recall', '')} | {flags} |")
        if an:
            all_slides.append({**an, "_qa": {"status": status, "attempts": len(r["attempts"]),
                                             "review_verdict": rv.get("verdict"), "text_recall": rv.get("text_recall"),
                                             "review_errors": rv.get("errors", []), "review_missing": rv.get("missing", []),
                                             "review_hallucinated": rv.get("hallucinated", []),
                                             "validation": [i["code"] for i in issues]}})
    (out / "all_slides.json").write_text(json.dumps(all_slides, ensure_ascii=False, indent=2), encoding="utf-8")
    head = ["# Slide analysis QA report", "",
            "Status: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), "",
            "| page | file | status | type | confidence | review verdict / recall | validator flags |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    (out / "report.md").write_text("\n".join(head + rows) + "\n", encoding="utf-8")
    return counts


def resolve(a):
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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="slide_analyze.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="check agy + agy-delegate")
    for name in ("run", "report"):
        s = sub.add_parser(name)
        s.add_argument("--slides", required=True, help="folder of slide images (sorted by name = page order)")
        s.add_argument("--out", help="output folder (default <slides>/../slide_analysis)")
        s.add_argument("--pages", help="1-based page selection, e.g. 1-10,15")
        if name == "run":
            s.add_argument("--subject", default=DEFAULT_SUBJECT, help="course subject for the personas, e.g. 데이터베이스")
            s.add_argument("--context", default=DEFAULT_CONTEXT, help="one-paragraph description of the deck")
            s.add_argument("-j", "--jobs", type=int, default=4, help="parallel agy sessions")
            s.add_argument("--timeout", type=int, default=10, help="minutes per agy call")
            s.add_argument("--model", default=os.environ.get("AGY_TASK_MODEL"))
            s.add_argument("--work", help="scratch dir for agy workdirs (default <out>/.work)")
            s.add_argument("--force", action="store_true", help="redo slides that already have results")
            s.add_argument("--skip-review", action="store_true", help="skip the agy reviewer pass")
    a = p.parse_args(argv)

    try:
        agy_task = find_agy_task()
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        return 4
    if a.cmd == "preflight":
        return subprocess.call([sys.executable, str(agy_task), "preflight"])

    slides, out = resolve(a)
    pages = parse_pages(a.pages, len(slides))
    if a.cmd == "run":
        work = Path(a.work).resolve() if a.work else out / ".work"
        work.mkdir(parents=True, exist_ok=True)

        def pending(pg):
            f = out / "results" / f"{slides[pg - 1].stem}.json"
            return a.force or not f.exists() or json.loads(f.read_text(encoding="utf-8"))["status"] in PENDING

        todo = [pg for pg in pages if pending(pg)]
        log(f"{len(todo)} slides to process (of {len(pages)}), jobs={a.jobs}, out={out}")
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(process, agy_task, slides[pg - 1], pg, len(slides), out, work, a): pg for pg in todo}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:  # keep the batch going
                    log(f"[{futs[fut]:03d}] crashed: {e!r}")
    counts = build_report(slides, pages, out)
    print(json.dumps({"out": str(out), "counts": counts, "quota_exhausted": STOP.is_set(),
                      "report": str(out / "report.md"), "all_slides": str(out / "all_slides.json")},
                     ensure_ascii=False, indent=2))
    if STOP.is_set():
        log("agy quota exhausted: re-run the same command after the reset to continue")
        return 3
    return 0 if set(counts) <= {"ok"} else 2


if __name__ == "__main__":
    sys.exit(main())
