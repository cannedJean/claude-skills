"""kp_ingest.py - turn one lecture's agy-slide-analyzer output into normalised slide records (read-only input).

    ingest_lecture(lec, prev_manifest=None) -> {"schema", "lecture", "records", "warnings", "image_seq"}
    classify_text_check(rec, ref) -> str      what the analyzer actually verified (table in references/trust.md)
    slide_trust(rec, regions, struct_info) -> high | medium | low | image_only
    analyzer_changed(rec) -> bool             results/<stem>.json or ref/<stem>.json changed since ingest
    detect_build_sequences(records), detect_boilerplate(records), renumber_diff(old_seq, new_seq)

results/<stem>.json is read directly (all_slides.json is rebuilt from the last --pages selection only). Slides
are ordered naturally (slide_2 before slide_10) and joined to analyzer records by file stem; the analyzer's own
page numbers follow plain sorted() order, so a differing order is reported, never silently trusted.
agy-pdf-analyzer output works the same way with <analysis>/ref as the slides folder (page_XXXX.png next to
page_XXXX.json): its results name the PDF from source.json as their file, and text-layer references carry no
image sha (provenance unverifiable, not stale).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import kp_common as kc
import kp_regions
from kp_safety import scan_injection
from textnorm import bigrams, norm, overlap

INGEST_SCHEMA = "kpack.ingest/1"
THIN_REASONS = {"little text on both sides", "no_reference_little_text"}
LOW_TEXT_CHECKS = {"needs_attention", "unverified", "review_pending", "legacy_unverified"}
VERIFIED_TEXT_CHECKS = {"ocr_verified", "claude_verified"}
BOILERPLATE_SHARE = 0.5
META_REGIONS = {"title", "header/footer", "page_number"}


def classify_text_check(rec, ref) -> str:
    if not rec or not rec.get("analysis"):
        return "image_only"
    st = rec.get("status")
    if st == "accepted":
        judgments = rec.get("judgments") or []
        if judgments and not any(j.get("verdict") in ("agy_wrong", "issue") for j in judgments):
            return "claude_verified"
        reasons = set((rec.get("score") or {}).get("reasons") or [])
        return "thin_accepted" if reasons & THIN_REASONS else "ocr_verified"
    return {"escalated": "review_pending", "needs_attention": "needs_attention", "unverified": "unverified",
            "failed": "image_only", "ok": "legacy_unverified", "unreviewed": "legacy_unverified"}.get(st, "unverified")


def _qa(rec) -> dict:
    sc = rec.get("score") or {}
    return {"status": rec.get("status"), "decision": rec.get("decision"), "attempts": len(rec.get("attempts") or []),
            "metrics": sc.get("metrics") or {}, "reasons": sc.get("reasons") or [], "triggers": sc.get("triggers") or [],
            "validation": [v.get("code") for v in rec.get("validation") or [] if isinstance(v, dict)],
            "judgments": [{k: j.get(k) for k in ("id", "kind", "verdict", "claude_text", "note")}
                          for j in rec.get("judgments") or []]}


def _corpus(an: dict) -> str:
    import ocr_score
    return " ".join(p[1] for p in ocr_score.corpus_parts(an or {}))


def _all_text(an: dict) -> str:
    """Every string an analysis carries, for the injection scan."""
    out = []

    def walk(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for k, v in x.items():
                if k != "reasoning":
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(an or {})
    return "\n".join(out)


def _sha_or_none(path: Path):
    return kc.sha256_file(path) if path.is_file() else None


def _read(path: Path):
    """(JSON object or None, sha256 of exactly the bytes parsed or None when there is no file)."""
    if not path.is_file():
        return None, None
    data = path.read_bytes()
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        obj = None
    return (obj if isinstance(obj, dict) else None), hashlib.sha256(data).hexdigest()


def analyzer_changed(rec: dict) -> bool:
    """True when the analyzer's results/<stem>.json or ref/<stem>.json bytes differ from what ingest read (also for
    a record written before ingest recorded them): the ingest copy is stale, re-run ingest."""
    if "results_sha" not in rec or not rec.get("analysis_dir"):
        return True
    base = Path(rec["analysis_dir"])
    return (_sha_or_none(base / "results" / f"{rec['stem']}.json") != rec["results_sha"]
            or _sha_or_none(base / "ref" / f"{rec['stem']}.json") != rec.get("ref_sha"))


def ingest_lecture(lec: dict, prev_manifest: dict = None) -> dict:
    images = kc.list_images(lec["slides"])
    out_dir = Path(lec["analysis"])
    source = kc.read_json(out_dir / "source.json")  # agy-pdf-analyzer: {"file": <pdf name>, "sha256", "pages"}
    source = source if isinstance(source, dict) else {}
    warnings = []
    plain = sorted(p.name for p in images)
    if plain != [p.name for p in images]:
        warnings.append({"code": "order_differs", "msg": "natural order differs from the analyzer's plain sort; "
                         "slide ids follow natural order and records are joined by file stem"})
    records = []
    for n, img in enumerate(images, start=1):
        sid = kc.slide_id(lec["id"], n)
        res_path, ref_path = out_dir / "results" / f"{img.stem}.json", out_dir / "ref" / f"{img.stem}.json"
        (res, res_sha), (ref, ref_sha) = _read(res_path), _read(ref_path)
        image_sha = kc.sha256_file(img)
        an = (res or {}).get("analysis")
        if ref and ref.get("sha256"):
            provenance = "ok" if ref["sha256"] == image_sha else "stale"
        else:  # no reference, or a PDF text-layer reference (no image behind it)
            provenance = "unverifiable"
        if ref and ref.get("source_sha") and source.get("sha256") and ref["source_sha"] != source["sha256"]:
            provenance = "stale"  # reference built from another version of the PDF
        if res and res.get("file") and res["file"] not in (img.name, source.get("file")):
            provenance = "stale"
        words = [w for ln in (ref or {}).get("lines") or [] for w in ln.get("words") or []]
        layout = (ref or {}).get("layout") or {}
        sc = (res or {}).get("score") or {}
        rec = {
            "id": sid, "lecture": lec["id"], "n": n, "stem": img.stem, "file": img.name, "image": str(img),
            "image_sha": image_sha, "analyzer_page": (res or {}).get("page"), "status": (res or {}).get("status") or "missing",
            "text_check": classify_text_check(res, ref), "provenance": provenance,
            "has_ref": bool(ref), "ref_path": str(ref_path) if ref else None, "analysis_dir": str(out_dir),
            "results_sha": res_sha, "ref_sha": ref_sha,
            "engines": [e.get("engine") for e in (ref or {}).get("engines") or [] if e.get("ok")],
            "digits_checked": any(w.get("weight") == "high" for w in words),
            "triggers": sc.get("triggers") or [], "graphics_ratio": layout.get("graphics_ratio"),
            "size": (ref or {}).get("size"),
            "analysis": an, "analysis_sha": kc.sha256_json(an) if an else None, "qa": _qa(res) if res else None,
            "boilerplate": [], "build_of": None, "flags": [],
            "uncertain_count": _corpus(an).count("[?]") if an else 0,
            "confidence": (an or {}).get("confidence"),
        }
        if provenance == "stale":
            rec["flags"].append("stale_analysis")
        if an:
            inj = scan_injection(_all_text(an))
            if inj:
                rec["flags"].append("injection_like")
                rec["injection_patterns"] = inj
        records.append(rec)
    detect_build_sequences(records)
    detect_boilerplate(records)
    missing = [r["file"] for r in records if r["status"] == "missing"]
    if missing:
        warnings.append({"code": "missing_results", "msg": f"{len(missing)} slide(s) have no analyzer result",
                         "files": missing[:20]})
    stale = [r["id"] for r in records if r["provenance"] == "stale"]
    if stale:
        warnings.append({"code": "stale_analysis", "msg": "image changed after analysis (sha mismatch)", "slides": stale})
    seq = [r["image_sha"] for r in records]
    renum = renumber_diff(prev_seq(prev_manifest, lec["id"]), seq) if prev_manifest else None
    return {"schema": INGEST_SCHEMA, "lecture": lec["id"], "title": lec["title"], "records": records,
            "warnings": warnings, "image_seq": seq, "renumber": renum}


def detect_boilerplate(records: list):
    """Header/footer text repeated on >= half of a lecture's slides (min 3) is collapsed on the cards and
    excluded from concept matching. A build run counts as one slide, so the bullets a progressive reveal keeps on
    every step stay content. Run after detect_build_sequences. Marks rec['boilerplate'] = verbatim indices."""
    seen_in, units = {}, set()
    for r in records:
        if not r.get("analysis"):
            continue
        unit = r.get("build_of") or r["id"]
        units.add(unit)
        for vt in r["analysis"].get("verbatim_text") or []:
            k = norm(str(vt.get("text") or "")) if isinstance(vt, dict) else ""
            if k:
                seen_in.setdefault(k, set()).add(unit)
    n = len(units)
    common = {k for k, u in seen_in.items() if n >= 3 and len(u) >= max(3, BOILERPLATE_SHARE * n)}
    for r in records:
        idx = []
        for i, vt in enumerate((r.get("analysis") or {}).get("verbatim_text") or []):
            if not isinstance(vt, dict):
                continue
            k = norm(str(vt.get("text") or ""))
            if vt.get("region") == "header/footer" or (k in common and vt.get("region") != "title"):
                idx.append(i)
        r["boilerplate"] = idx


def _ws(x) -> str:
    return " ".join(str(x or "").split()).lower()


def _content(rec) -> dict:
    """What a slide shows besides its title, header/footer and page number: text, code, tables, diagram parts."""
    an = rec.get("analysis") or {}
    title = norm(str(an.get("title") or ""))
    text = (norm(str(vt.get("text") or "")) for vt in an.get("verbatim_text") or []
            if isinstance(vt, dict) and vt.get("region") not in META_REGIONS)
    tables = [([_ws(h) for h in tb.get("headers") or []],
               [[_ws(c) for c in row] for row in tb.get("rows") or [] if isinstance(row, list)])
              for tb in an.get("tables") or [] if isinstance(tb, dict)]
    return {"text": " ".join(t for t in text if t and t != title),
            "code": [_ws(c.get("code")) for c in an.get("code_blocks") or [] if isinstance(c, dict) and _ws(c.get("code"))],
            "tables": tables,
            "diagram": {_ws(x) for d in an.get("diagrams") or [] if isinstance(d, dict)
                        for x in list(d.get("elements") or []) + list(d.get("relations") or []) if _ws(x)}}


def _size(c: dict) -> int:
    cells = sum(len(x) for h, rows in c["tables"] for x in h + [v for row in rows for v in row])
    return len(c["text"]) + sum(map(len, c["code"])) + cells + sum(map(len, c["diagram"]))


def _table_in(t, tables) -> bool:
    """Same headers, and every row of t is a row of the other table (blank cells may be filled in later)."""
    def row_in(row, rows):
        return any(len(r) >= len(row) and all(not v or v == r[i] for i, v in enumerate(row)) for r in rows)
    return any((not t[0] or t[0] == h) and all(row_in(row, rows) for row in t[1]) for h, rows in tables)


def _contained(a: dict, b: dict) -> bool:
    if a["text"] and not (a["text"] in b["text"] or overlap(a["text"], bigrams(b["text"])) >= 0.95):
        return False
    return (all(any(c in d for d in b["code"]) for c in a["code"])
            and all(_table_in(t, b["tables"]) for t in a["tables"]) and a["diagram"] <= b["diagram"])


def detect_build_sequences(records: list):
    """Progressive-reveal slides: same title, and everything slide i shows besides the title (text, code blocks,
    table rows, diagram elements and relations) is still on slide i+1, which shows more. Every member but the
    last gets build_of = the last slide's id (the index collapses the run; mentions count the final slide)."""
    runs, cur = [], []
    for a, b in zip(records, records[1:]):
        ta, tb = norm(str((a.get("analysis") or {}).get("title") or "")), norm(str((b.get("analysis") or {}).get("title") or ""))
        ca, cb = _content(a), _content(b)
        if ta and ta == tb and 0 < _size(ca) < _size(cb) and _contained(ca, cb):
            if not cur:
                cur = [a]
            cur.append(b)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    for run in runs:
        last = run[-1]["id"]
        for r in run[:-1]:
            r["build_of"] = last


def prev_seq(manifest: dict, lec_id: str) -> list:
    slides = (manifest or {}).get("slides") or {}
    rows = sorted(((kc.parse_slide_id(s), v) for s, v in slides.items() if (kc.parse_slide_id(s) or ("",))[0] == lec_id),
                  key=lambda x: x[0][1])
    return [v.get("image_sha256") for _, v in rows]


def renumber_diff(old_seq: list, new_seq: list):
    """None when slide ids keep their meaning (same count, no image moved to another position; edits in place
    are fine). Otherwise {"moved": [[old_n, new_n]], "removed": [old_n], "added": [new_n]}."""
    if not old_seq:
        return None
    old_pos = {}
    for i, s in enumerate(old_seq, start=1):
        old_pos.setdefault(s, i)
    new_pos = {}
    for i, s in enumerate(new_seq, start=1):
        new_pos.setdefault(s, i)
    moved = [[old_pos[s], new_pos[s]] for s in new_pos if s in old_pos and old_pos[s] != new_pos[s]]
    if len(old_seq) == len(new_seq) and not moved:
        return None
    removed = [old_pos[s] for s in old_pos if s not in new_pos]
    added = [new_pos[s] for s in new_pos if s not in old_pos]
    return {"moved": sorted(moved), "removed": sorted(removed), "added": sorted(added),
            "old_count": len(old_seq), "new_count": len(new_seq)}


def slide_trust(rec: dict, regions: list, struct_info: dict = None) -> str:
    """Routing trust for the tutor. regions carry a 'state' (unchecked when the struct stage has not run)."""
    struct_info = struct_info or {}
    tc = rec["text_check"]
    if tc == "image_only":
        return "image_only"
    states = [r.get("state", "unchecked") for r in regions]
    if rec["provenance"] == "stale" or tc in LOW_TEXT_CHECKS or any(s in kp_regions.FINAL_BAD for s in states):
        return "low"
    visual = [r for r in regions if r["kind"] == "visual"]
    if tc == "thin_accepted" and kp_regions.has_graphics(rec) and not (rec.get("analysis") or {}).get("diagrams"):
        if not visual or any(r.get("state") not in kp_regions.FINAL_OK for r in visual):
            return "low"
    digits_ok = rec["digits_checked"] or struct_info.get("digits_checked_by_pack", False)
    remaining_q = struct_info.get("uncertain_remaining", rec["uncertain_count"])
    if (tc in VERIFIED_TEXT_CHECKS and digits_ok and remaining_q == 0
            and all(s in kp_regions.FINAL_OK for s in states)):
        return "high"
    return "medium"
