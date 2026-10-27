"""kp_common.py - plumbing shared by every lecture-knowledge-pack module.

    find_shared(cands=None) -> Path      agy-slide-analyzer/scripts ($OCR_EVAL_DIR, sibling skill, ~/.claude/skills)
    SHARED_DIR, SHARED_ERR               set at import; SHARED_ERR is the exception when the shared code is missing
    load_course(path) -> dict            course.json, validated (CourseError on violation -> exit 5)
    slide_id(lec, n), natural_key(s)     db03-s012; 'slide_2' sorts before 'slide_10'
    read_json / write_json / sha256_*    UTF-8 everywhere, deterministic JSON
    est_tokens(text), MD_TOKEN_LIMIT     conservative token estimate (Hangul/CJK/kana ~1 each); per-.md cap

The shared modules (textnorm, ocr_score, ocr_ref, crops, slide_analyze, slide_validate) are imported from the
agy-slide-analyzer skill, never copied. Every module of this skill is prefixed kp_ so that none of them can
shadow a shared or stdlib module once the shared folder is on sys.path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

COURSE_SCHEMA = "kpack.course/1"
ID_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")
SLIDE_ID_RE = re.compile(r"^([a-z][a-z0-9]{1,15})-s(\d{3,4})$")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_WORK = ".kpack"
DEFAULT_PACKAGE = "package"

EXIT_OK, EXIT_PENDING, EXIT_DEPS, EXIT_ARGS = 0, 2, 4, 5


class CourseError(ValueError):
    """course.json is missing, malformed or inconsistent (exit 5)."""


# --------------------------------------------------------------------------- shared code
def shared_candidates() -> list:
    return [os.environ.get("OCR_EVAL_DIR"),
            SKILL_DIR.parent / "agy-slide-analyzer" / "scripts",
            Path.home() / ".claude" / "skills" / "agy-slide-analyzer" / "scripts"]


def find_shared(cands=None) -> Path:
    for c in (shared_candidates() if cands is None else cands):
        if c and (Path(c) / "textnorm.py").is_file() and (Path(c) / "slide_analyze.py").is_file():
            return Path(c).resolve()
    raise FileNotFoundError("agy-slide-analyzer/scripts not found; install that skill next to this one or set OCR_EVAL_DIR")


try:
    SHARED_DIR = find_shared()
    if str(SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(SHARED_DIR))
    import textnorm  # noqa: E402,F401
    import ocr_score  # noqa: E402,F401
    SHARED_ERR = None
except Exception as _e:  # reported by the CLI as exit 4
    SHARED_DIR, SHARED_ERR = None, _e


# --------------------------------------------------------------------------- io
def stdout_utf8():
    """JSON with Korean text must not die on a legacy console code page (cp949)."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def log(msg: str):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def read_json(path, default=None):
    p = Path(path)
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return default


def dumps(data, indent=2) -> str:
    return json.dumps(data, ensure_ascii=False, indent=indent)


def write_json(path, data, indent=2) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dumps(data, indent) + "\n", encoding="utf-8")
    return p


def write_text(path, text: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def sha256_json(obj) -> str:
    return sha256_text(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


# --------------------------------------------------------------------------- ids and ordering
def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def slide_id(lec: str, n: int) -> str:
    return f"{lec}-s{n:03d}"


def parse_slide_id(sid: str):
    m = SLIDE_ID_RE.match(sid or "")
    return (m.group(1), int(m.group(2))) if m else None


def list_images(folder) -> list:
    """Slide images in natural order (the analyzer itself uses plain sorted(); see kp_ingest for the check)."""
    folder = Path(folder)
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT),
                  key=lambda p: natural_key(p.name))


HANGUL_RE = re.compile(r"[가-힣ㄱ-ㆎ]")
# Hangul (syllables, jamo), CJK ideographs (+ ext. A, compatibility, ext. B+), kana, halfwidth kana: ~1 token each
WIDE_RE = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
                     r"\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f\U00020000-\U0003134f]")
MD_TOKEN_LIMIT = 20000  # per package .md file (gate G8; the glossary index splits to stay under it)


def est_tokens(text: str) -> int:
    """Conservative estimate: Hangul, CJK ideographs and kana ~1 token each, everything else ~3.2 chars per token."""
    if not text:
        return 0
    h = len(WIDE_RE.findall(text))
    return int(h * 1.0 + (len(text) - h) / 3.2) + 1


# --------------------------------------------------------------------------- course.json
def load_course(path) -> dict:
    """Read and validate course.json. Paths are resolved against the file's folder."""
    p = Path(path).resolve()
    data = read_json(p)
    if not isinstance(data, dict):
        raise CourseError(f"{p}: not a JSON object (or missing)")
    if data.get("schema") != COURSE_SCHEMA:
        raise CourseError(f"{p}: schema must be '{COURSE_SCHEMA}'")
    cid = data.get("course_id")
    if not isinstance(cid, str) or not ID_RE.match(cid):
        raise CourseError(f"course_id '{cid}' must match {ID_RE.pattern}")
    lectures = data.get("lectures")
    if not isinstance(lectures, list) or not lectures:
        raise CourseError("lectures must be a non-empty list")
    base = p.parent
    seen, seen_out = set(), {}
    out = []
    for i, lec in enumerate(lectures):
        if not isinstance(lec, dict):
            raise CourseError(f"lectures[{i}] is not an object")
        lid = lec.get("id")
        if not isinstance(lid, str) or not ID_RE.match(lid):
            raise CourseError(f"lectures[{i}].id '{lid}' must match {ID_RE.pattern}")
        if lid in seen:
            raise CourseError(f"duplicate lecture id '{lid}'")
        seen.add(lid)
        slides = (base / lec.get("slides", "")).resolve() if lec.get("slides") else None
        analysis = (base / lec.get("analysis", "")).resolve() if lec.get("analysis") else None
        if not slides or not slides.is_dir():
            raise CourseError(f"lecture {lid}: slides folder '{lec.get('slides')}' does not exist")
        if not analysis or not analysis.is_dir():
            raise CourseError(f"lecture {lid}: analysis folder '{lec.get('analysis')}' does not exist")
        key = os.path.normcase(str(analysis))
        if key in seen_out:
            raise CourseError(f"lectures {seen_out[key]} and {lid} share the analysis folder {analysis}: "
                              "the analyzer would silently reuse one lecture's results for the other "
                              "(run slide_analyze.py with a separate --out per lecture)")
        seen_out[key] = lid
        out.append({"id": lid, "title": str(lec.get("title") or lid), "slides": slides, "analysis": analysis})
    work = (base / data.get("work", DEFAULT_WORK)).resolve()
    package = (base / data.get("package", DEFAULT_PACKAGE)).resolve()
    return {"path": p, "base": base, "course_id": cid, "title": str(data.get("title") or cid),
            "subject": str(data.get("subject") or ""), "lectures": out, "work": work, "package": package,
            "registry": base / "concept_registry.json"}


def select_lectures(course: dict, spec: str = None) -> list:
    if not spec:
        return course["lectures"]
    want = [s.strip() for s in spec.split(",") if s.strip()]
    known = {l["id"]: l for l in course["lectures"]}
    bad = [w for w in want if w not in known]
    if bad:
        raise CourseError(f"unknown lecture id(s): {', '.join(bad)}")
    return [known[w] for w in want]
