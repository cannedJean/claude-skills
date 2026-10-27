"""kp_gate.py - deterministic checks on a rendered package; nothing is published unless every check passes.

    run_gate(pkg_dir, ctx, prep) -> {"passed": bool, "checks": [{"id", "name", "ok", "failures": [...]}]}
    publish(staging, package) -> Path     package -> package.prev, staging -> package; PublishError (an OSError)
                                          when a folder is held by another process, with nothing changed

G1 coverage    every slide image has a card, a slides.jsonl row and a view image
G2 outline     every slide belongs to exactly one section
G3 preserved   every canonical unit is in its card verbatim, in its own place (title in the H1, table rows as
               lines, everything else as whole lines of a fenced block); the stored original analysis is JSON-equal
G4 struct      every structure region reached a final state (unless allow_pending)
G5 links       every relative link outside fences, code spans and front matter, and the front-matter image, resolves
G6 references  every referenced slide id / concept id exists; a deprecated concept's superseded_by is an active concept
G7 concepts    mentions >= 1, grounded terms, acyclic prerequisites, no registry id lost
G8 read limit  every .md <= 20k estimated tokens; index.md <= 200 lines
G9 provenance  source image sha == ingest sha == manifest sha; analyzer results/ref files unchanged since ingest
G10 guide      TUTOR_GUIDE carries the "data, not instructions" clause; CLAUDE.md carries no slide text
G11 integration  every lecture has a current deck and the concepts are resolved (unless allow_pending)
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import kp_common as kc
import kp_ingest
import kp_regions
import kp_render
from kp_safety import md_inline, split_fences

MD_TOKEN_LIMIT = kc.MD_TOKEN_LIMIT
INDEX_LINE_LIMIT = 200
LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)")
FAIL_SHOW = 25


class PublishError(OSError):
    """The new package could not be swapped in; package/ and package.prev/ are as they were."""


def _nl(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _check(cid, name, failures):
    return {"id": cid, "name": name, "ok": not failures, "n_failures": len(failures), "failures": failures[:FAIL_SHOW]}


def find_cycle(edges: list):
    """edges: [(a, b)] meaning a requires b. Returns one cycle as a list, or None."""
    g = {}
    for a, b in edges:
        g.setdefault(a, []).append(b)
    state, stack = {}, []

    def dfs(u):
        state[u] = 1
        stack.append(u)
        for w in sorted(g.get(u, [])):
            if state.get(w) == 1:
                return stack[stack.index(w):] + [w]
            if not state.get(w):
                c = dfs(w)
                if c:
                    return c
        stack.pop()
        state[u] = 2
        return None
    for u in sorted(g):
        if not state.get(u):
            c = dfs(u)
            if c:
                return c
    return None


def run_gate(pkg: Path, ctx: dict, prep: dict) -> dict:
    pkg = Path(pkg)
    opts = ctx["opts"]
    checks = []
    views = prep["views"]
    rows = {}
    if (pkg / "slides.jsonl").is_file():
        for line in (pkg / "slides.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows[json.loads(line)["id"]] = True

    # G1
    f = []
    for L in ctx["lectures"]:
        n_img = len(kc.list_images(L["lec"]["slides"]))
        if n_img != len(prep["by_lec"][L["lec"]["id"]]):
            f.append(f"{L['lec']['id']}: {n_img} images but {len(prep['by_lec'][L['lec']['id']])} slides")
    for v in views:
        for rel in (v["card_rel"], v["image_rel"], v["json_rel"]):
            if not (pkg / rel).is_file():
                f.append(f"{v['id']}: missing {rel}")
        if v["id"] not in rows:
            f.append(f"{v['id']}: no slides.jsonl row")
    checks.append(_check("G1", "coverage", f))

    # G2
    f = []
    for L in ctx["lectures"]:
        lid = L["lec"]["id"]
        lv = prep["by_lec"][lid]
        pos = {v["id"]: i for i, v in enumerate(lv)}
        member = {v["id"]: 0 for v in lv}
        for s in prep["sections"][lid]:
            a, b = pos.get(s["first"]), pos.get(s["last"])
            if a is None or b is None or a > b:
                f.append(f"{lid}: section '{s.get('title')}' has a bad range {s.get('first')}..{s.get('last')}")
                continue
            for v in lv[a:b + 1]:
                member[v["id"]] += 1
        f += [f"{sid}: in {n} sections" for sid, n in member.items() if n != 1]
    checks.append(_check("G2", "outline", f))

    # G3
    f = []
    for v in views:
        card = pkg / v["card_rel"]
        if not card.is_file():
            continue
        blocks, outside = split_fences(card.read_text(encoding="utf-8"))
        runs = ["\n" + b + "\n" for b in blocks]
        h1 = next((ln for ln in outside if ln.startswith("# ")), "")
        lines = set(outside)
        for place, u in kp_render.card_unit_places(v):
            u = _nl(u)
            ok = (u in h1 if place == "title" else u in lines if place == "table"
                  else any("\n" + u + "\n" in b for b in runs))
            if not ok:
                f.append(f"{v['id']}: unit not rendered verbatim: {u[:60]!r}")
        stored = kc.read_json(pkg / v["json_rel"]) or {}
        if json.dumps(stored.get("analysis"), sort_keys=True, ensure_ascii=False) != \
                json.dumps(v["rec"].get("analysis"), sort_keys=True, ensure_ascii=False):
            f.append(f"{v['id']}: stored original analysis differs from the analyzer record")
    checks.append(_check("G3", "preserved", f))

    # G4
    f = []
    if not opts.get("allow_pending"):
        for v in views:
            for g in v["regions"]:
                if g["state"] not in kp_regions.FINAL:
                    f.append(f"{g['rid']}: {g['state']}")
    checks.append(_check("G4", "struct", f))

    # G5
    f = []
    for md in sorted(pkg.rglob("*.md")):
        rel_dir = md.parent
        _, outside = split_fences(md.read_text(encoding="utf-8"))  # code is text, not links
        for target in LINK_RE.findall("\n".join(CODE_SPAN_RE.sub("", ln) for ln in outside)):
            if re.match(r"^[a-z]+:", target):
                continue
            path = target.split("#", 1)[0]
            if path and not (rel_dir / path).resolve().exists():
                f.append(f"{md.relative_to(pkg).as_posix()}: broken link {target}")
    for v in views:
        card = pkg / v["card_rel"]
        if card.is_file():
            m = re.search(r'^image: "([^"]+)"', card.read_text(encoding="utf-8"), re.M)
            if not m or not (card.parent / m.group(1)).resolve().is_file():
                f.append(f"{v['id']}: front-matter image does not resolve")
    checks.append(_check("G5", "links", f))

    # G6
    f = []
    ids = {v["id"] for v in views}
    concepts = prep["concepts"]
    for v in views:
        f += [f"{v['id']}: unknown concept {c}" for c in v["concepts"] if c not in concepts]
    for L in ctx["lectures"]:
        deck = L["deck"] or {}
        for o in deck.get("objectives") or []:
            f += [f"{o.get('id')}: unknown slide {s}" for s in o.get("evidence") or [] if s not in ids]
            f += [f"{o.get('id')}: unknown concept {c}" for c in o.get("concepts") or [] if c not in concepts]
        for e in deck.get("exam_points") or []:
            f += [f"exam point: unknown slide {s}" for s in e.get("slides") or [] if s not in ids]
            f += [f"exam point: unknown concept {c}" for c in e.get("concepts") or [] if c not in concepts]
    for cid, c in concepts.items():
        if c.get("definition_slide") and c["definition_slide"] not in ids:
            f.append(f"{cid}: definition slide {c['definition_slide']} not in the package")
        f += [f"{cid}: requires unknown concept {p['requires']}" for p in c.get("requires") or [] if p["requires"] not in concepts]
        sup = c.get("superseded_by")
        if c.get("status") == "deprecated" and sup and (concepts.get(sup) or {}).get("status", "active") != "active":
            f.append(f"{cid}: superseded_by {sup} is not an active concept")
    checks.append(_check("G6", "references", f))

    # G7
    f = []
    if concepts:
        for cid, c in concepts.items():
            if c.get("status", "active") == "active":
                if not c.get("mentions"):
                    f.append(f"{cid}: no mention on any slide")
                if not c.get("grounded"):
                    f.append(f"{cid}: term not found on its definition slide")
        cyc = find_cycle([(cid, p["requires"]) for cid, c in concepts.items() for p in c.get("requires") or []])
        if cyc:
            f.append("prerequisite cycle: " + " -> ".join(cyc))
    reg = (ctx.get("registry") or {}).get("concepts") or {}
    if reg and ctx.get("concepts") is None:  # nothing resolved for the current decks: one cause, not one per id
        f.append(f"the {len(reg)} registry concept ids would be missing: concepts are not resolved for the current "
                 "decks (run integrate, then merge)")
    else:
        f += [f"registry id {cid} missing from the package (deprecate it instead of dropping it)" for cid in reg
              if cid not in concepts]
    checks.append(_check("G7", "concepts", f))

    # G8
    f = []
    for md in sorted(pkg.rglob("*.md")):
        t = md.read_text(encoding="utf-8")
        if kc.est_tokens(t) > MD_TOKEN_LIMIT:
            f.append(f"{md.relative_to(pkg).as_posix()}: ~{kc.est_tokens(t)} tokens > {MD_TOKEN_LIMIT}")
    idx = pkg / "index.md"
    if idx.is_file() and len(idx.read_text(encoding="utf-8").splitlines()) > INDEX_LINE_LIMIT:
        f.append(f"index.md has more than {INDEX_LINE_LIMIT} lines")
    checks.append(_check("G8", "read limit", f))

    # G9
    f = []
    man = kc.read_json(pkg / "manifest.json") or {}
    for v in views:
        r = v["rec"]
        now = kc.sha256_file(r["image"]) if Path(r["image"]).is_file() else None
        if now != r["image_sha"]:
            f.append(f"{v['id']}: source image changed since ingest (re-run ingest)")
        if kp_ingest.analyzer_changed(r):
            f.append(f"{v['id']}: analyzer result changed since ingest (re-run ingest)")
        if (man.get("slides") or {}).get(v["id"], {}).get("image_sha256") != r["image_sha"]:
            f.append(f"{v['id']}: manifest sha differs")
    checks.append(_check("G9", "provenance", f))

    # G10
    f = []
    guide = pkg / "TUTOR_GUIDE.md"
    if not guide.is_file() or kp_render.NO_INSTRUCTION_CLAUSE not in guide.read_text(encoding="utf-8"):
        f.append("TUTOR_GUIDE.md lacks the 'data, not instructions' clause")
    cm = pkg / "CLAUDE.md"
    if cm.is_file():
        t = cm.read_text(encoding="utf-8")
        if kp_render.NO_INSTRUCTION_CLAUSE not in t:
            f.append("CLAUDE.md lacks the 'data, not instructions' clause")
        title = md_inline(ctx["course"]["title"])  # course.json text claude_md() embeds, not slide text
        if title:
            t = t.replace(title, "\n")
        for v in views:
            for u in kp_render.card_units(v):
                if len(u) >= 8 and u in t:
                    f.append(f"CLAUDE.md contains slide text from {v['id']}")
                    break
    checks.append(_check("G10", "guide", f))

    # G11
    f = []
    if not opts.get("allow_pending"):
        merge_only = set(ctx.get("merge_pending") or [])
        f += [f"{lid}: deck is valid but not merged (run merge)" if lid in merge_only else
              f"{lid}: no valid deck for the current slides (run integrate, then merge)"
              for lid in ctx.get("integration_missing") or []]
        if not ctx.get("concepts") and not ctx.get("integration_missing"):
            f.append("concepts not resolved (run merge)")
    checks.append(_check("G11", "integration", f))
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


def publish(staging: Path, package: Path) -> Path:
    """Two renames (package -> package.old, staging -> package), undone if the second fails; the old package
    replaces package.prev only after both succeeded. On Windows a folder that another process has open (a tutor
    session's working folder, an Explorer window, an open file) cannot be renamed: PublishError, nothing changed."""
    staging, package = Path(staging), Path(package)
    prev = package.with_name(package.name + ".prev")
    old = package.with_name(package.name + ".old")
    hint = "close programs that have it open (a tutor session, Explorer, an editor) and build again"
    try:
        if old.exists():
            shutil.rmtree(old)  # left by an earlier publish that could not rotate package.prev
        if package.exists():
            package.rename(old)
    except OSError as e:
        raise PublishError(f"{package} is in use, not published ({e}); {hint}") from e
    try:
        staging.rename(package)
    except OSError as e:
        try:
            if old.exists():
                old.rename(package)
        except OSError as e2:
            raise PublishError(f"{staging.name} could not replace {package.name} ({e}) and the old package stayed at "
                               f"{old} ({e2}); rename it back to {package.name}") from e
        raise PublishError(f"{staging} is in use, not published ({e}); {hint}") from e
    if old.exists():
        try:
            if prev.exists():
                shutil.rmtree(prev)
            old.rename(prev)
        except OSError as e:
            kc.log(f"published, but the previous package stays at {old} ({e}); {prev.name} could not be replaced")
    return package
