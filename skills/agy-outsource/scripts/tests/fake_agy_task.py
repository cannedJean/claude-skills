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
  quota      prints agy's RESOURCE_EXHAUSTED line and writes nothing
"""
import json
import os
import re
import sys
from pathlib import Path

FUT = "from __future__ import annotations\n\n"
GOOD = {
    "U1-normalize": {
        "slugkit/__init__.py": '"""slugkit - URL slugs."""\n',
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
        "slugkit/registry.py": FUT + 'from slugkit.slug import slugify\n\n\nclass SlugRegistry:\n    """Unique slugs."""\n\n'
        '    def __init__(self, sep: str = "-") -> None:\n        self.sep = sep\n        self._taken = set()\n\n'
        '    def add(self, text: str) -> str:\n        """New unique slug."""\n'
        '        base = slugify(text, sep=self.sep) or "item"\n        slug, n = base, 2\n'
        '        while slug in self._taken:\n            slug = f"{base}{self.sep}{n}"\n            n += 1\n'
        '        self._taken.add(slug)\n        return slug\n\n'
        '    def __contains__(self, slug: object) -> bool:\n        return slug in self._taken\n\n'
        '    def __len__(self) -> int:\n        return len(self._taken)\n',
    },
}
BUGGY = {  # (file, old, new) applied to GOOD
    "U1-normalize": ("slugkit/normalize.py", 'unicodedata.normalize("NFC", ', '('),
    "U2-slugify": ("slugkit/slug.py", 'if sep == "" or (max_len is not None and max_len < 1):', 'if sep == "":'),
    "U3-registry": ("slugkit/registry.py", 'slugify(text, sep=self.sep) or "item"', 'slugify(text, sep=self.sep)'),
}
SIGNATURE = {
    "U1-normalize": ("slugkit/normalize.py", "def collapse_whitespace(text: str)", "def collapse_whitespace(s: str)"),
    "U2-slugify": ("slugkit/slug.py", '*, sep: str = "-"', 'sep: str = "-"'),
    "U3-registry": ("slugkit/registry.py", "def add(self, text: str)", "def add(self, title: str)"),
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
    wd = Path(args[args.index("-w") + 1])
    prompt = Path(args[args.index("-f") + 1]).read_text(encoding="utf-8")
    unit = re.search(r"^# Work order (\S+) ", prompt, re.M).group(1)
    sc = scenario_for(unit)
    retry = "Previous attempt was REJECTED" in prompt
    if sc == "quota":
        sys.stderr.write('AGY_ERROR: {"code": 429, "message": "RESOURCE_EXHAUSTED: quota reached. Resets in 2h 10m."}\n')
        print(json.dumps({"status": "error"}))
        return 3
    files = dict(GOOD[unit])
    if sc == "always" or (sc == "buggy" and not retry):
        files = patch(files, BUGGY[unit])
    if sc == "signature" and not retry:
        files = patch(files, SIGNATURE[unit])
        if unit == "U1-normalize":
            files["slugkit/normalize.py"] = files["slugkit/normalize.py"].replace('" ".join(text.split())', '" ".join(s.split())')
        if unit == "U3-registry":
            files["slugkit/registry.py"] = files["slugkit/registry.py"].replace("slugify(text,", "slugify(title,")
    if sc == "print" and not retry:
        f = next(iter(k for k in files if k != "slugkit/__init__.py"))
        files[f] += '\nprint("debug")\n'
    for rel, src in files.items():
        (wd / rel).parent.mkdir(parents=True, exist_ok=True)
        (wd / rel).write_text(src, encoding="utf-8")
    if sc == "extra" and not retry:
        (wd / "NOTES.md").write_text("notes\n", encoding="utf-8")
    if sc == "tamper" and not retry:
        for t in wd.glob("tests/test_*.py"):
            t.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    if not (sc == "nomanifest" and not retry):
        (wd / "DELIVERY.json").write_text(json.dumps({
            "unit": unit, "files": sorted(GOOD[unit]), "symbols": [], "assumptions": [f"fake:{sc}"],
            "self_check": "canned"}), encoding="utf-8")
    print(json.dumps({"status": "ok", "agent_status": "SUCCESS", "conversation_id": "fake", "fake": sc, "retry": retry}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
