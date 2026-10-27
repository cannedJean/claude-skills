"""fake_reader.py - stands in for a blind struct-read subagent: answers a batch from the fixture's ground truth.

    answer_batch(batch_json, truth, refs, mode="truthful", only=None) -> Path(reads file)
    visible(reg, an, ref) -> answer | None    what is drawn inside a code / formula / [?] crop, line by line

truth: {slide_id: true analysis}; refs: {slide_id: reference} (for OCR line texts and boxes).
mode: truthful | noisy (operators flipped, cells swapped, edges reversed, digits changed) | not_found
      | visible (crop items get exactly the lines inside the crop, so a misplaced crop gives a wrong answer)
only: optional set of region locals to answer (the rest is left unanswered).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import kp_compare  # noqa: E402


def truthful(kind: str, part: list, an: dict, ref: dict):
    field, idx = part
    if kind == "code":
        if field == "code_blocks":
            cb = an["code_blocks"][idx]
            return {"language": cb.get("language"), "code": cb["code"]}
        return {"language": None, "code": an["verbatim_text"][idx]["text"]}
    if kind == "table":
        tb = an["tables"][idx]
        rows = ([tb["headers"]] if tb.get("headers") else []) + tb["rows"]
        return {"rows": rows, "header_rows": 1 if tb.get("headers") else 0}
    if kind == "diagram":
        dg = an["diagrams"][idx]
        edges = [kp_compare.parse_relation(r) for r in dg.get("relations") or []]
        return {"nodes": list(dg.get("elements") or []), "edges": [e for e in edges if e]}
    if kind == "formula":
        return {"plain": an["verbatim_text"][idx]["text"], "latex": None}
    if kind == "uncertain":
        return {"text": an["verbatim_text"][idx]["text"]}
    if kind == "line":
        ln = next(l for l in ref["lines"] if l["id"] == idx)
        return ln["text"]
    if kind == "visual":
        return {"lines": [l["text"] for l in ref["lines"]], "nodes": [], "edges": []}
    raise ValueError(kind)


def visible(reg: dict, an: dict, ref: dict, pad: int = 14):
    """The drawn lines fully inside the padded crop box (kp_verify.crop_image pads by 14 px)."""
    # a view item names the region by its box, so a careful reader of the whole slide reads exactly that block
    if reg.get("mode") not in ("crop", "view") or reg["kind"] not in ("code", "formula", "uncertain") or not reg.get("box"):
        return None
    l, t, r, b = reg["box"][0] - pad, reg["box"][1] - pad, reg["box"][2] + pad, reg["box"][3] + pad
    text = "\n".join(ln["text"] for ln in ref["lines"] if ln["box"][0] >= l and ln["box"][1] >= t
                     and ln["box"][0] + ln["box"][2] <= r and ln["box"][1] + ln["box"][3] <= b)
    if reg["kind"] == "code":
        field, idx = reg["part"]
        return {"language": an["code_blocks"][idx].get("language") if field == "code_blocks" else None, "code": text}
    return {"plain": text, "latex": None} if reg["kind"] == "formula" else {"text": text}


def noisy(kind: str, ans):
    if kind == "code":
        return dict(ans, code=ans["code"].replace(">=", ">"))
    if kind == "table":
        rows = [list(r) for r in ans["rows"]]
        if len(rows) > 2:
            rows[1][1], rows[2][1] = rows[2][1], rows[1][1]
        return dict(ans, rows=rows)
    if kind == "diagram":
        return dict(ans, edges=[dict(e, **{"from": e["to"], "to": e["from"]}) for e in ans["edges"]])
    if kind == "formula":
        return dict(ans, plain=ans["plain"].replace("+", "-"))
    if kind in ("line",):
        return re.sub(r"\d", lambda m: str((int(m.group(0)) + 1) % 10), ans)
    if kind == "uncertain":
        return {"text": ans["text"] + " 추가"}
    return ans


def answer_batch(batch_json, truth: dict, refs: dict, state: dict, mode="truthful", only=None) -> Path:
    b = json.loads(Path(batch_json).read_text(encoding="utf-8"))
    out = {}
    for item in b["items"]:
        ans_item = {}
        for slot, rid in item["regions"].items():
            reg = state["regions"][rid]
            if only and reg["local"] not in only:
                continue
            sid = reg["slide"]
            if mode == "not_found":
                a = {"not_found": True}
            elif mode == "visible" and visible(reg, truth[sid], refs.get(sid)) is not None:
                a = visible(reg, truth[sid], refs.get(sid))
            else:
                a = truthful(reg["kind"], reg["part"], truth[sid], refs.get(sid))
                if mode == "noisy":
                    a = noisy(reg["kind"], a)
            ans_item[slot] = a
        if not ans_item:
            continue
        out[item["id"]] = ans_item["_"] if item["type"] == "crop" else ans_item
    reads = Path(batch_json).parent.parent / "reads" / f"{b['batch']}.json"
    reads.parent.mkdir(parents=True, exist_ok=True)
    reads.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return reads
