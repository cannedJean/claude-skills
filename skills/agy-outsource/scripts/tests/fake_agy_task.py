#!/usr/bin/env python3
"""fake_agy_task.py - stand-in for agy-delegate's agy_task.py so outsource.py can be exercised without agy.

Set AGY_TASK=<this file>. It accepts `run -w WORKDIR -f PROMPT ...`, reads the unit id from the work order
("# Work order <id> — ...") and writes canned code for the examples/slugkit plan. FAKE_AGY_SCENARIO picks the
behaviour, either one value for every unit or per unit: "U2-slugify=buggy,U3-registry=extra".

  perfect    correct code + DELIVERY.json
  buggy      a rule is wrong (hidden tests fail); the retry (prompt carries feedback) is correct
  extra      also writes NOTES.md (scope gate); fixed on retry
  tamper     rewrites the visible test to pass trivially (scope gate); fixed on retry
  signature  wrong parameter name (interface gate); fixed on retry
  nomanifest forgets DELIVERY.json; fixed on retry
  print      leaves a print() call (forbid gate); fixed on retry
  always     buggy on every attempt (-> needs_attention)
  quota      reports agy's RESOURCE_EXHAUSTED error (stderr.txt + agy_error) and writes nothing
  oldquota   attempt 1: agy's quota line in its stderr.txt, but a (buggy) delivery; attempt 2: no DELIVERY.json and
             no quota line; attempt 3: correct (an old attempt's quota line must not stop the run)
  narrate    the agent's narration (agy_task.py's own log) mentions RESOURCE_EXHAUSTED / 429, and no DELIVERY.json;
             fixed on retry (narration is not agy's quota error)
  agyfail    correct code + DELIVERY.json, then agy_task.py exits 3 with a multi-line stderr (a warning only)
  crash      writes correct code, then agy_task.py dies with a traceback and no result (rc 1); fixed on retry
  jobwrite   correct code that, when imported by the tests, writes a file into its own job dir (integrity)
  leak       code that prints every file in its cwd that holds a hidden test, then fails to import; fixed on retry
             (the visible run must hold no hidden test, and feedback must not quote one)
  outside    correct code, but reports two tool calls the work-dir confinement denied
  exit0      correct code that also ends the test process with exit code 0 on import (tamper gate); fixed on retry
  peekhidden reads the hidden tests at run time through a path built from strings (tamper gate); fixed on retry
  specedit   escapes its sandbox and rewrites the visible test at its source, FAKE_AGY_SPEC_DIR (integrity gate)
  ctxedit    escapes its sandbox and rewrites a dependency's context file at its source (integrity gate)
  hookless   correct code, but the confinement hook never ran (agy_task.py reports enabled=false)
  hooklessbuggy  hookless and buggy on the first attempt; confined and correct on the retry
  inlinedview    correct code; the hook denied one view of a file the work order inlined (not an escape attempt)
  planthook      correct code, plus its own .agents/hooks.json (only possible unconfined: scope gate); not on retry

Like agy_task.py it reports "confinement" (enabled unless --no-confine is passed or the attempt is hookless), keeps
this call's record in <work dir>/.agy_task/<stamp>/ (stderr.txt when agy said something there) and prints its
meta_dir.
FAKE_AGY_OLD=1 behaves like an agy_task.py older than this agy-outsource: `run --help` lacks --bare/--inlined and
argparse rejects them (exit 2, nothing printed on stdout).
FAKE_AGY_ARGS_LOG=<dir>: every call writes its argv as JSON into a file of its own in that directory (units that
run in parallel would clobber each other's lines in one shared file).
"""
import json
import os
import re
import sys
import time
from pathlib import Path

FUT = "from __future__ import annotations\n\n"
GOOD = {
    "U1-normalize": {
        "slugkit/normalize.py": FUT + 'import unicodedata\n\n\n'
        'def strip_accents(text: str) -> str:\n    """Remove combining marks."""\n'
        '    d = unicodedata.normalize("NFKD", text)\n'
        '    return unicodedata.normalize("NFC", "".join(c for c in d if not unicodedata.combining(c)))\n\n\n'
        'def collapse_whitespace(text: str) -> str:\n    """Collapse whitespace runs."""\n    return " ".join(text.split())\n',
    },
    "U2-slugify": {
        "slugkit/slug.py": FUT + 'import re\nfrom typing import Optional\n\nfrom slugkit.normalize import strip_accents\n\n\n'
        'def slugify(text: str, *, sep: str = "-", max_len: Optional[int] = None) -> str:\n    """Build a slug."""\n'
        '    if sep == "" or (max_len is not None and max_len < 1):\n        raise ValueError("bad sep or max_len")\n'
        '    s = re.sub(r"[^a-z0-9]+", sep, strip_accents(text).lower())\n'
        '    s = s.strip(sep) if len(sep) == 1 else _strip_sep(s, sep)\n'
        '    if max_len is not None and len(s) > max_len:\n        s = s[:max_len].rstrip(sep)\n    return s\n\n\n'
        'def _strip_sep(s: str, sep: str) -> str:\n    while s.startswith(sep):\n        s = s[len(sep):]\n'
        '    while s.endswith(sep):\n        s = s[: -len(sep)]\n    return s\n',
    },
    "U3-registry": {
        "slugkit/registry.py": FUT + '\nclass SlugRegistry:\n    """Unique slugs."""\n\n'
        '    def __init__(self, sep: str = "-") -> None:\n        self.sep = sep\n        self._taken = set()\n\n'
        '    def add(self, slug: str) -> str:\n        """Unique variant of slug."""\n'
        '        base = slug or "item"\n        cand, n = base, 2\n'
        '        while cand in self._taken:\n            cand = f"{base}{self.sep}{n}"\n            n += 1\n'
        '        self._taken.add(cand)\n        return cand\n\n'
        '    def __contains__(self, slug: object) -> bool:\n        return slug in self._taken\n\n'
        '    def __len__(self) -> int:\n        return len(self._taken)\n',
    },
}
BUGGY = {  # (file, old, new) applied to GOOD
    "U1-normalize": ("slugkit/normalize.py", 'unicodedata.normalize("NFC", ', '('),
    "U2-slugify": ("slugkit/slug.py", 'if sep == "" or (max_len is not None and max_len < 1):', 'if sep == "":'),
    "U3-registry": ("slugkit/registry.py", 'base = slug or "item"', 'base = slug'),
}
SIGNATURE = {
    "U1-normalize": ("slugkit/normalize.py", "def collapse_whitespace(text: str)", "def collapse_whitespace(s: str)"),
    "U2-slugify": ("slugkit/slug.py", '*, sep: str = "-"', 'sep: str = "-"'),
    "U3-registry": ("slugkit/registry.py", "def add(self, slug: str)", "def add(self, title: str)"),
}


def scenario_for(unit: str) -> str:
    raw = os.environ.get("FAKE_AGY_SCENARIO", "perfect")
    if "=" not in raw:
        return raw
    for part in raw.split(","):
        k, _, v = part.partition("=")
        if k.strip() == unit:
            return v.strip()
    return "perfect"


def patch(files: dict, rule) -> dict:
    f, old, new = rule
    files = dict(files)
    assert old in files[f], (f, old)
    files[f] = files[f].replace(old, new, 1)
    return files


def main(argv) -> int:
    args = argv[1:]
    if args and args[0] == "preflight":
        print(json.dumps({"ok": True, "checks": {"agy": "fake"}}))
        return 0
    old = bool(os.environ.get("FAKE_AGY_OLD"))
    if "--help" in args:
        print("usage: agy_task.py run -w WORKDIR -f FILE [--effort E] [--no-schema] [--no-confine] [--allow-all]"
              + ("" if old else " [--bare] [--inlined REL]"))
        return 0
    if old and ("--bare" in args or "--inlined" in args):
        sys.stderr.write("agy_task.py: error: unrecognized arguments: --bare\n")
        return 2
    if os.environ.get("FAKE_AGY_ARGS_LOG"):
        d = Path(os.environ["FAKE_AGY_ARGS_LOG"])
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{time.time_ns()}-{os.getpid()}.json").write_text(json.dumps(args), encoding="utf-8")
    wd = Path(args[args.index("-w") + 1])
    prompt = Path(args[args.index("-f") + 1]).read_text(encoding="utf-8")
    unit = re.search(r"^# Work order (\S+) ", prompt, re.M).group(1)
    sc = scenario_for(unit)
    retry = "Previous attempt was REJECTED" in prompt
    records = wd / ".agy_task"
    attempt = (sum(1 for d in records.iterdir() if d.is_dir()) if records.is_dir() else 0) + 1
    meta = records / str(time.time_ns())
    meta.mkdir(parents=True)
    quota_msg = "RESOURCE_EXHAUSTED: quota reached. Resets in 2h 10m."
    if sc == "quota":
        line = "AGY_ERROR: " + json.dumps({"code": 429, "message": quota_msg})
        (meta / "stderr.txt").write_text(line + "\n", encoding="utf-8")
        print(json.dumps({"status": "error", "meta_dir": str(meta), "agy_error": {"code": 429, "message": quota_msg}}))
        return 3
    if sc == "oldquota" and attempt == 1:
        (meta / "stderr.txt").write_text("AGY_ERROR: " + json.dumps({"code": 429, "message": quota_msg}) + "\n",
                                         encoding="utf-8")
    if sc == "narrate" and not retry:
        sys.stderr.write("I map grpc RESOURCE_EXHAUSTED to HTTP 429 (code 429) as the spec says\n")
    files = dict(GOOD[unit])
    if sc == "always" or (sc in ("buggy", "hooklessbuggy") and not retry) or (sc == "oldquota" and attempt == 1):
        files = patch(files, BUGGY[unit])
    if sc == "signature" and not retry:
        files = patch(files, SIGNATURE[unit])
        if unit == "U1-normalize":
            files["slugkit/normalize.py"] = files["slugkit/normalize.py"].replace('" ".join(text.split())', '" ".join(s.split())')
        if unit == "U3-registry":
            files["slugkit/registry.py"] = files["slugkit/registry.py"].replace("base = slug ", "base = title ")
    if sc == "exit0" and not retry:
        files = {k: v + "\nimport os\nos._exit(0)\n" for k, v in files.items()}
    if sc == "peekhidden" and not retry:
        f = next(iter(files))
        files[f] += '\nfrom pathlib import Path\n_SEEN = list((Path(__file__).parent.parent / "hidden").glob("*.py"))\n'
    if sc == "specedit" and os.environ.get("FAKE_AGY_SPEC_DIR"):
        for t in Path(os.environ["FAKE_AGY_SPEC_DIR"]).glob("tests/test_*.py"):
            t.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    if sc == "ctxedit" and os.environ.get("FAKE_AGY_SPEC_DIR"):
        for t in Path(os.environ["FAKE_AGY_SPEC_DIR"]).glob("project/slugkit/__init__.py"):
            t.write_text('"""changed during the run."""\n', encoding="utf-8")
    if sc == "print" and not retry:
        f = next(iter(files))
        files[f] += '\nprint("debug")\n'
    if sc == "jobwrite" and not retry:  # runs while the gates test it, unconfined, as the user
        f = next(iter(files))
        files[f] += f"\nopen({str(wd / 'NOTE.txt')!r}, 'w', encoding='utf-8').write('x')\n"
    if sc == "leak" and not retry:  # no string names a test file: the tamper gate lets it through
        f = next(iter(files))
        files[f] += ('\nimport os as _os\nimport sys as _sys\n_sys.stdout.write("LEAKSCAN start\\n")\n'
                     'for _d, _s, _fs in _os.walk(_os.getcwd()):\n    for _f in _fs:\n'
                     '        _t = open(_os.path.join(_d, _f), encoding="utf-8", errors="replace").read()\n'
                     '        if "idden" in _t:\n            _sys.stdout.write(_t + "\\n")\n'
                     'raise ValueError("boom")\n')
    for rel, src in files.items():
        (wd / rel).parent.mkdir(parents=True, exist_ok=True)
        (wd / rel).write_text(src, encoding="utf-8")
    if sc == "extra" and not retry:
        (wd / "NOTES.md").write_text("notes\n", encoding="utf-8")
    if sc == "planthook" and not retry:  # its own PreToolUse hook: agy would run it in every later session there
        (wd / ".agents").mkdir(exist_ok=True)
        (wd / ".agents" / "hooks.json").write_text(json.dumps({"agy-task-confine": {"PreToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": "calc.exe"}]}]}}), encoding="utf-8")
    if sc == "tamper" and not retry:
        for t in wd.glob("tests/test_*.py"):
            t.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    if sc == "crash" and not retry:  # agy wrote the files, then agy_task.py died before printing its result
        sys.stderr.write("Traceback (most recent call last):\n  File \"agy_task.py\", line 1\nKeyError: 'x'\n")
        return 1
    no_manifest = (sc == "nomanifest" and not retry) or (sc == "oldquota" and attempt == 2) or \
        (sc == "narrate" and not retry)
    if not no_manifest:
        (wd / "DELIVERY.json").write_text(json.dumps({
            "unit": unit, "files": sorted(GOOD[unit]), "assumptions": [f"fake:{sc}"]}), encoding="utf-8")
    confined = "--no-confine" not in args
    denied = [{"tool": "view_file", "decision": "deny", "rule": "path", "reason": "'../../hidden/x' is outside the work dir"}] * 2 \
        if sc == "outside" and confined else []
    hookless = sc == "hookless" or (sc == "hooklessbuggy" and not retry)
    inlined_denied = 1 if sc == "inlinedview" and confined else 0  # agy_task.py keeps these out of denied_count
    confinement = {"enabled": confined and not hookless, "installed": confined, "denied_count": len(denied),
                   "inlined_denied_count": inlined_denied, "denied": denied} if confined else {"enabled": False}
    if sc == "agyfail":
        sys.stderr.write("agy crashed after writing\n  the files\n")
        (meta / "stderr.txt").write_text("agy crashed after writing\n", encoding="utf-8")
        print(json.dumps({"status": "error", "agent_status": None, "conversation_id": "fake", "fake": sc,
                          "meta_dir": str(meta), "confinement": confinement}))
        return 3
    print(json.dumps({"status": "ok", "agent_status": "SUCCESS", "conversation_id": "fake", "fake": sc, "retry": retry,
                      "meta_dir": str(meta), "confinement": confinement}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
