#!/usr/bin/env python3
"""knowledge_pack.py - turn agy-slide-analyzer (or agy-pdf-analyzer) results for a whole course into a layered
knowledge package for a tutoring agent: verified text first, slide images and region crops as linked evidence.

  init --root DIR --course-id ID --title T [--write]   scaffold course.json from DIR/<lecture>/{slides,slide_analysis}
                                or agy-pdf-analyzer folders (source.json; slides = <analysis>/ref); validated first
  preflight                     shared code, Pillow, OCR engines, analyzer outputs (counts, statuses, order, sha)
  ingest [--accept-renumber]    normalised slide records + trust inputs -> .kpack/ingest/<lecture>.json
  struct [options]              structure check (code, tables, diagram edges, formulas, [?], unchecked digits):
                                blind subagent reads of crops, deterministic 2-of-3 vote  (advance; exit 2 = batches pending)
  integrate                     per-lecture digests -> deck.json written by a subagent, validated (advance)
  merge                         concept ids unified across lectures + registry (advance; exit 2 = merge_queue.md)
  build [--allow-pending] [--strict]   render to package.staging, run the gate, publish (package.prev kept)
  gate [--package DIR]          re-run the gate on a package
  status                        one JSON: per-stage state and the next command (re-checks analyzer files, digests,
                                struct coverage and the decks behind the merge)

Every command takes --course course.json (default ./course.json), --lectures db01,db03 (subset; not for build)
and --work DIR (default <course>/.kpack). Analyzer folders are read-only inputs: corrections live in .kpack only.

Exit codes: 0 ok, 2 pending work / not published / blocked, 4 missing dependency (shared code, Pillow),
5 bad arguments or course.json. (3 is agy quota in the sibling skills; this skill never calls agy.)
JSON result on stdout, logs on stderr. Python 3.9+. Requires the agy-slide-analyzer skill and Pillow.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kp_common as kc  # noqa: E402


def emit(obj) -> None:
    print(kc.dumps(obj))


def need_shared():
    if kc.SHARED_ERR:
        emit({"ok": False, "error": f"shared code unavailable: {kc.SHARED_ERR}"})
        sys.exit(kc.EXIT_DEPS)


# --------------------------------------------------------------------------- init
def init_layouts(root: Path) -> list:
    """(name, slides, analysis) per lecture folder: DIR/<lecture>/{slides, slide_analysis} (agy-slide-analyzer), or
    agy-pdf-analyzer output (source.json; page images in its ref/) as DIR/<lecture>/slide_analysis or DIR/<x>_analysis."""
    found = []
    for d in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: kc.natural_key(p.name)):
        sa = d / "slide_analysis"
        if (sa / "source.json").is_file():
            found.append((d.name, f"{d.name}/slide_analysis/ref", f"{d.name}/slide_analysis"))
        elif sa.is_dir() and (d / "slides").is_dir():
            found.append((d.name, f"{d.name}/slides", f"{d.name}/slide_analysis"))
        elif (d / "source.json").is_file() and (d / "ref").is_dir():
            found.append((re.sub(r"_analysis$", "", d.name), f"{d.name}/ref", d.name))
    return found


def lecture_id(name: str, n: int, used: set) -> str:
    """Folder name -> id; a name that sanitises to nothing usable or to a taken id falls back to l01, l02 ..."""
    lid = re.sub(r"[^a-z0-9]", "", name.lower())
    lid = lid if lid[:1].isalpha() else f"l{lid}"
    if len(lid) > 16:  # keep a trailing number: databasesystems01 -> databasesystem01
        tail = re.search(r"\d*$", lid).group()[-8:]
        lid = lid[:16 - len(tail)] + tail
    k = n
    while not kc.ID_RE.match(lid) or lid in used:
        lid, k = f"l{k:02d}", k + 1
    return lid


def course_problem(root: Path, course: dict):
    """load_course's own rules applied to a draft (through a temporary file next to where course.json goes)."""
    fd, tmp = tempfile.mkstemp(prefix=".course.", suffix=".json", dir=root)
    os.close(fd)
    try:
        kc.write_json(tmp, course)
        kc.load_course(tmp)
        return None
    except kc.CourseError as e:
        return str(e)
    finally:
        Path(tmp).unlink(missing_ok=True)


def cmd_init(a) -> int:
    root = Path(a.root).resolve()
    if not root.is_dir():
        emit({"ok": False, "error": f"{root} is not a folder"})
        return kc.EXIT_ARGS
    if not kc.ID_RE.match(a.course_id or ""):
        emit({"ok": False, "error": f"--course-id '{a.course_id}' must match {kc.ID_RE.pattern}"})
        return kc.EXIT_ARGS
    lectures, used = [], set()
    for n, (name, slides, analysis) in enumerate(init_layouts(root), start=1):
        lid = lecture_id(name, n, used)
        used.add(lid)
        lectures.append({"id": lid, "title": name, "slides": slides, "analysis": analysis})
    course = {"schema": kc.COURSE_SCHEMA, "course_id": a.course_id, "title": a.title or a.course_id,
              "subject": a.subject or "", "lectures": lectures}
    if not lectures:
        emit({"ok": False, "written": None, "course": course,
              "hint": "expected DIR/<lecture>/slides and DIR/<lecture>/slide_analysis, or agy-pdf-analyzer output "
                      "folders (source.json, ref/page_XXXX.png)"})
        return kc.EXIT_ARGS
    problem = course_problem(root, course)
    if problem:
        emit({"ok": False, "error": f"generated course.json would not load: {problem}", "course": course})
        return kc.EXIT_ARGS
    out = root / "course.json"
    if a.write:
        if out.exists() and not a.force:
            emit({"ok": False, "error": f"{out} exists (use --force to overwrite)"})
            return kc.EXIT_ARGS
        kc.write_json(out, course)
    emit({"ok": True, "written": str(out) if a.write else None, "course": course, "hint": None})
    return kc.EXIT_OK


# --------------------------------------------------------------------------- preflight
def cmd_preflight(a, course) -> int:
    import ocr_ref
    checks, warnings = {}, []
    try:
        import PIL  # noqa: F401
        checks["pillow"] = "ok"
    except ImportError:
        emit({"ok": False, "error": "Pillow is required (pip install pillow)"})
        return kc.EXIT_DEPS
    checks["shared"] = str(kc.SHARED_DIR)
    try:
        engines = [e.name for e in ocr_ref.available_engines()]
    except Exception as e:  # engine probing must not block the report
        engines, checks["ocr_probe_error"] = [], repr(e)
    checks["ocr_engines_here"] = engines
    if not engines:
        warnings.append("no OCR engine on this machine (pip install winsdk; Tesseract kor recommended as a 2nd engine): "
                        "new analyses will end `unverified`")
    elif len(engines) == 1:
        warnings.append("one OCR engine only: the analyzer checks digits/identifiers only on words two engines agree on")
    lecs = []
    for lec in kc.select_lectures(course, a.lectures):
        imgs = kc.list_images(lec["slides"])
        res_dir, ref_dir = lec["analysis"] / "results", lec["analysis"] / "ref"
        source = kc.read_json(lec["analysis"] / "source.json")
        tool = "pdf_analyze.py" if isinstance(source, dict) else "slide_analyze.py"
        statuses, missing, stale, high, ref_engines = Counter(), [], [], 0, Counter()
        for img in imgs:
            rec = kc.read_json(res_dir / f"{img.stem}.json")
            statuses[(rec or {}).get("status") or "missing"] += 1
            if not rec:
                missing.append(img.name)
            ref = kc.read_json(ref_dir / f"{img.stem}.json")
            if ref:
                ref_engines[",".join(sorted(e["engine"] for e in ref.get("engines") or [] if e.get("ok")))] += 1
                if ref.get("sha256") and ref["sha256"] != kc.sha256_file(img):
                    stale.append(img.name)
                if any(w.get("weight") == "high" for ln in ref.get("lines") or [] for w in ln.get("words") or []):
                    high += 1
        stems = {p.stem for p in imgs}
        orphans = sorted(p.name for p in res_dir.glob("*.json") if p.stem not in stems) if res_dir.is_dir() else []
        info = {"id": lec["id"], "images": len(imgs), "statuses": dict(statuses), "ref_engines": dict(ref_engines),
                "digits_checked_slides": high, "missing_results": missing[:20], "stale": stale[:20],
                "results_without_image": orphans[:20]}
        if isinstance(source, dict):
            info["source_pdf"] = source.get("file")
            if os.path.normcase(str(lec["slides"])) != os.path.normcase(str(ref_dir)):
                warnings.append(f"{lec['id']}: agy-pdf-analyzer output: set this lecture's \"slides\" to its analysis "
                                "folder's ref/ (the page images page_XXXX.png)")
        if sorted(p.name for p in imgs) != [p.name for p in imgs]:
            warnings.append(f"{lec['id']}: file names sort differently in natural and plain order; ids follow natural order")
        if missing:
            warnings.append(f"{lec['id']}: {len(missing)} slide(s) without analyzer results (run {tool} run)")
        if orphans:
            warnings.append(f"{lec['id']}: {len(orphans)} analyzer result(s) have no image in the slides folder and are "
                            "left out" + (" (agy-pdf-analyzer saves ref/page_XXXX.png only when it can extract the page "
                                          "image: install pymupdf, then run pdf_analyze.py reference)"
                                          if isinstance(source, dict) else ""))
        if stale:
            warnings.append(f"{lec['id']}: {len(stale)} slide image(s) changed after analysis (re-run with --force --pages)")
        if statuses.get("failed"):
            warnings.append(f"{lec['id']}: {statuses['failed']} slide(s) failed in the analyzer (re-run {tool} run; "
                            "until then they are image_only)")
        if statuses.get("escalated") or statuses.get("needs_attention"):
            warnings.append(f"{lec['id']}: {statuses['escalated'] + statuses['needs_attention']} slide(s) escalated or "
                            f"needing attention upstream (work the analyzer's review_queue.md, then {tool} resolve; "
                            "until then trust low)")
        if statuses.get("ok") or statuses.get("unreviewed"):
            warnings.append(f"{lec['id']}: {statuses['ok'] + statuses['unreviewed']} slide(s) carry a legacy reviewer-mode "
                            f"status (run {tool} rescore; until then trust low)")
        if (lec["analysis"] / "review_queue.md").is_file():
            warnings.append(f"{lec['id']}: analyzer review queue is pending (work review_queue.md, then {tool} resolve)")
        if statuses.get("unverified"):
            warnings.append(f"{lec['id']}: {statuses['unverified']} slide(s) unverified (no OCR reference; after installing "
                            f"an engine run {tool} rescore)")
        if imgs and high < len(imgs) and ref_engines:
            warnings.append(f"{lec['id']}: {len(imgs) - high} slide(s) without digit/identifier checks upstream "
                            "(the struct stage adds line checks for them)")
        lecs.append(info)
    emit({"ok": True, "checks": checks, "lectures": lecs, "warnings": warnings})
    return kc.EXIT_OK


# --------------------------------------------------------------------------- ingest
def cmd_ingest(a, course) -> int:
    import kp_ingest
    prev = kc.read_json(course["package"] / "manifest.json")
    out, blocked, renum_map = [], [], kc.read_json(course["work"] / "renumber_map.json", {}) or {}
    for lec in kc.select_lectures(course, a.lectures):
        ing = kp_ingest.ingest_lecture(lec, prev)
        if ing["renumber"] and not a.accept_renumber:
            blocked.append({"lecture": lec["id"], "renumber": ing["renumber"]})
            continue
        if ing["renumber"]:
            renum_map[lec["id"]] = ing["renumber"]
        kc.write_json(course["work"] / "ingest" / f"{lec['id']}.json", ing)
        out.append({"lecture": lec["id"], "slides": len(ing["records"]),
                    "text_check": dict(Counter(r["text_check"] for r in ing["records"])),
                    "digits_checked": sum(1 for r in ing["records"] if r["digits_checked"]),
                    "flags": dict(Counter(f for r in ing["records"] for f in r["flags"])),
                    "build_sequences": sum(1 for r in ing["records"] if r["build_of"]),
                    "warnings": ing["warnings"]})
    if renum_map:
        kc.write_json(course["work"] / "renumber_map.json", renum_map)
    emit({"ok": not blocked, "lectures": out, "blocked": blocked,
          "hint": "slide positions changed since the last build: slide ids would point at different slides. "
                  "Re-run with --accept-renumber (recorded in .kpack/renumber_map.json) to accept." if blocked else None})
    return kc.EXIT_PENDING if blocked else kc.EXIT_OK


# --------------------------------------------------------------------------- build / gate
def write_report(course, prep, gate, published, extra=None):
    lines = [f"# knowledge pack report: {course['course_id']}", ""]
    c = Counter(v["trust"] for v in prep["views"])
    lines += [f"Trust: high={c['high']}, medium={c['medium']}, low={c['low']}, image_only={c['image_only']}"]
    st = Counter(g["state"] for v in prep["views"] for g in v["regions"])
    lines += [f"Struct regions: " + (", ".join(f"{k}={v}" for k, v in sorted(st.items())) or "none")]
    lines += [f"Gate: {'PASSED' if gate['passed'] else 'FAILED'}; published: {published}", ""]
    for ch in gate["checks"]:
        lines.append(f"- {ch['id']} {ch['name']}: {'ok' if ch['ok'] else 'FAIL (' + str(ch['n_failures']) + ')'}")
        lines += [f"  - {x}" for x in ch["failures"]]
    if extra:
        lines += ["", extra]
    kc.write_text(course["work"] / "report.md", "\n".join(lines) + "\n")


def cmd_build(a, course) -> int:
    import kp_gate
    import kp_render
    opts = {"allow_pending": a.allow_pending, "view_max": a.view_max, "no_claude_md": a.no_claude_md}
    try:
        ctx = kp_render.build_context(course, course["lectures"], opts)
    except FileNotFoundError as e:
        emit({"ok": False, "error": str(e)})
        return kc.EXIT_PENDING
    prep = kp_render.prepare(ctx)
    staging = course["package"].with_name(course["package"].name + ".staging")
    manifest = kp_render.render(ctx, prep, staging)
    gate = kp_gate.run_gate(staging, ctx, prep)
    if a.strict:
        bad = [v["id"] for v in prep["views"] if v["trust"] in ("low", "image_only")]
        gate["checks"].append(kp_gate._check("S1", "strict: no low/image_only slides", [f"{s}: low trust" for s in bad]))
        gate["passed"] = gate["passed"] and not bad
    manifest["gate"] = {c["id"]: c["ok"] for c in gate["checks"]}
    kc.write_json(staging / "manifest.json", manifest)
    published, reg_note, error = False, None, None
    if gate["passed"]:
        import kp_integrate
        if ctx.get("concepts"):  # a registry update that would fail must fail before anything is published
            kp_integrate.registry_after_publish(course, ctx["concepts"], manifest["build_id"], dry_run=True)
        try:
            kp_gate.publish(staging, course["package"])
            published = True
        except OSError as e:  # a folder held by another process: package/ and package.prev/ are unchanged
            error = str(e)
        if published and ctx.get("concepts"):
            reg_note = kp_integrate.registry_after_publish(course, ctx["concepts"], manifest["build_id"])
    write_report(course, prep, gate, published, extra=error and f"Not published: {error}")
    emit({"ok": published, "published": published, "package": str(course["package"]) if published else str(staging),
          "build_id": manifest["build_id"], "counts": manifest["counts"],
          "gate": [{k: c[k] for k in ("id", "name", "ok", "n_failures")} for c in gate["checks"]],
          "registry": reg_note, "error": error, "report": str(course["work"] / "report.md")})
    return kc.EXIT_OK if published else kc.EXIT_PENDING


def cmd_gate(a, course) -> int:
    import kp_gate
    import kp_render
    pkg = Path(a.package).resolve() if a.package else course["package"]
    try:
        ctx = kp_render.build_context(course, course["lectures"], {"allow_pending": a.allow_pending})
    except FileNotFoundError as e:
        emit({"ok": False, "error": str(e)})
        return kc.EXIT_PENDING
    prep = kp_render.prepare(ctx)
    gate = kp_gate.run_gate(pkg, ctx, prep)
    emit({"ok": gate["passed"], "package": str(pkg), "checks": gate["checks"]})
    return kc.EXIT_OK if gate["passed"] else kc.EXIT_PENDING


# --------------------------------------------------------------------------- status
def reads_ok(path: Path) -> bool:
    """A struct reads file the struct stage can take in: a JSON object, optionally fenced (as kp_verify reads it)."""
    try:
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", path.read_text(encoding="utf-8-sig").strip())
        return isinstance(json.loads(text), dict)
    except (OSError, ValueError):
        return False


def struct_open(ings: list, st: dict) -> list:
    """Current regions of the ingested slides that state.json lacks, holds for older inputs, or has not settled."""
    import kp_regions
    import kp_verify
    have = st.get("regions") or {}
    out = []
    for ing in ings:
        for rec in ing["records"]:
            if not rec.get("analysis"):
                continue
            ref = kc.read_json(rec["ref_path"]) if rec.get("ref_path") else None
            for reg in kp_regions.select_regions(rec, ref):
                old = have.get(reg["rid"]) or {}
                if old.get("state") not in kp_regions.FINAL or old.get("sha") != kp_verify.region_sha(reg, rec, ref):
                    out.append(reg["rid"])
    return out


def cmd_status(a, course) -> int:
    import kp_ingest
    import kp_integrate
    work = course["work"]
    lecs, ings, nxt = [], [], None
    for lec in kc.select_lectures(course, a.lectures):
        ing = kc.read_json(work / "ingest" / f"{lec['id']}.json")
        info = {"id": lec["id"], "ingest": bool(ing)}
        if ing:
            ings.append(ing)
            shas = [kc.sha256_file(p) for p in kc.list_images(lec["slides"])]
            changed = [r["id"] for r in ing["records"] if kp_ingest.analyzer_changed(r)]
            info["ingest_fresh"] = shas == ing["image_seq"] and not changed
            if changed:
                info["analyzer_changed"] = changed[:20]
        if not info.get("ingest_fresh"):
            nxt = nxt or "ingest"
        idir = work / "integrate" / lec["id"]
        check = kc.read_json(idir / "check.json") or {}
        if not check or not check.get("valid"):
            info["deck"] = "invalid" if check else "missing"
        elif ing and (check.get("digest_sha") != kp_integrate.current_digest_sha(course, lec)
                      or check.get("deck_sha") != kc.sha256_json(kc.read_json(idir / "deck.json"))):
            info["deck"] = "stale"  # the slides or deck.json changed since integrate validated it
        else:
            info["deck"] = "valid"
        lecs.append(info)
    sdir = work / "struct"
    struct = kc.read_json(sdir / "state.json") or {}
    sc = Counter(r.get("state") for r in (struct.get("regions") or {}).values())
    waiting = sorted(b for b, v in (struct.get("batches") or {}).items() if v.get("status") == "pending")
    unreadable = [str(sdir / "reads" / f"{b}.json") for b in waiting
                  if (sdir / "reads" / f"{b}.json").is_file() and not reads_ok(sdir / "reads" / f"{b}.json")]
    pending_batches = [f"{b}.md" for b in waiting if not reads_ok(sdir / "reads" / f"{b}.json")]
    open_regions = struct_open(ings, struct) if not nxt else []
    resolved = kc.read_json(work / "concepts.resolved.json") or {}
    merge_queue = (work / "merge_queue.md").is_file() and not (work / "merge_reads.json").is_file()
    man = kc.read_json(course["package"] / "manifest.json") or {}
    if not nxt:
        if unreadable:
            nxt = ("struct (the reads listed in unreadable_reads are not a JSON object: have a fresh subagent answer "
                   "those batches again, then re-run)")
        elif pending_batches:
            nxt = "struct (answer pending batches with subagents, then re-run)"
        elif not struct or open_regions:
            nxt = "struct"
        elif any(l["deck"] != "valid" for l in lecs):
            nxt = "integrate"
        elif resolved.get("decks_sha") != kp_integrate.decks_sha(course):
            nxt = "merge (answer .kpack/merge_queue.md, then re-run)" if merge_queue else "merge"
        else:
            nxt = "build"
    emit({"ok": True, "course": course["course_id"], "lectures": lecs, "struct": dict(sc),
          "struct_open": len(open_regions), "pending_batches": pending_batches, "unreadable_reads": unreadable,
          "merge_queue": merge_queue, "package_build": man.get("build_id"), "next": nxt})
    return kc.EXIT_OK


# --------------------------------------------------------------------------- main
def parser():
    p = argparse.ArgumentParser(prog="knowledge_pack.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init")
    s.add_argument("--root", required=True)
    s.add_argument("--course-id", required=True)
    s.add_argument("--title")
    s.add_argument("--subject")
    s.add_argument("--write", action="store_true")
    s.add_argument("--force", action="store_true")
    for name in ("preflight", "ingest", "struct", "integrate", "merge", "build", "gate", "status"):
        s = sub.add_parser(name)
        s.add_argument("--course", default="course.json")
        s.add_argument("--lectures", help="comma-separated lecture ids (default: all)")
        s.add_argument("--work", help="work folder (default <course dir>/.kpack)")
        if name == "ingest":
            s.add_argument("--accept-renumber", action="store_true")
        if name == "struct":
            s.add_argument("--budget-tokens", type=int, default=0, help="image-token budget for new reads (0 = no limit)")
            s.add_argument("--kinds", help="limit to kinds: code,table,diagram,formula,uncertain,line,visual")
            s.add_argument("--batch-items", type=int, default=15)
            s.add_argument("--batch-tokens", type=int, default=12000)
            s.add_argument("--skip", action="store_true", help="mark every open region skipped (no reads)")
            s.add_argument("--force", action="store_true", help="re-select regions; reads with unchanged inputs are kept")
            s.add_argument("--redecide", action="store_true",
                           help="re-apply the decision rules to the stored reads (after an upgrade; regions that now "
                                "need a second read are batched)")
        if name in ("build", "gate"):
            s.add_argument("--allow-pending", action="store_true")
        if name == "build":
            s.add_argument("--strict", action="store_true")
            s.add_argument("--no-claude-md", action="store_true")
            s.add_argument("--view-max", type=int, default=1568)
        if name == "gate":
            s.add_argument("--package")
        if name == "integrate":
            s.add_argument("--digest-max-tokens", type=int, default=25000)
    return p


def main(argv=None) -> int:
    kc.stdout_utf8()
    a = parser().parse_args(argv)
    if a.cmd == "init":
        return cmd_init(a)
    need_shared()
    try:
        course = kc.load_course(a.course)
        if a.work:
            course["work"] = Path(a.work).resolve()
        if a.lectures:
            kc.select_lectures(course, a.lectures)
    except kc.CourseError as e:
        emit({"ok": False, "error": str(e)})
        return kc.EXIT_ARGS
    if a.cmd in ("struct", "build", "gate"):  # crops and views; preflight reports Pillow itself
        try:
            import PIL.Image  # noqa: F401
        except ImportError:
            emit({"ok": False, "error": "Pillow is required (pip install pillow)"})
            return kc.EXIT_DEPS
    handlers = {"preflight": cmd_preflight, "ingest": cmd_ingest, "build": cmd_build, "gate": cmd_gate,
                "status": cmd_status}
    if a.cmd in ("struct", "integrate", "merge"):
        import kp_stages
        handlers.update(kp_stages.HANDLERS)
    return handlers[a.cmd](a, course)


if __name__ == "__main__":
    sys.exit(main())
