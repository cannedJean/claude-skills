#!/usr/bin/env python3
"""mini_test.py - stdlib test runner used when pytest is not installed.

  python mini_test.py tests/test_a.py hidden/test_b.py

Runs, per file, every unittest.TestCase and every top-level `test_*` function that takes no arguments
(plain `assert` style). The current directory is put first on sys.path so tests import the code under test
the same way pytest's rootdir import does. Exit 0 when everything passed, 1 on any failure, error or skip
(acceptance tests must all run).
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import traceback
import unittest


def load(path: str):
    name = "_mt_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main(paths) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, os.getcwd())
    passed = failed = skipped = 0
    for path in paths:
        try:
            mod = load(path)
        except Exception:
            failed += 1
            print(f"ERROR collecting {path}\n{traceback.format_exc()}")
            continue
        suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
        if suite.countTestCases():
            res = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
            n_bad = len(res.failures) + len(res.errors) + len(res.unexpectedSuccesses)
            failed += n_bad
            skipped += len(res.skipped)
            passed += res.testsRun - n_bad - len(res.skipped)
        for name, fn in sorted(vars(mod).items()):
            if not (name.startswith("test_") and inspect.isfunction(fn) and fn.__module__ == mod.__name__):
                continue
            if inspect.signature(fn).parameters:
                failed += 1
                print(f"ERROR {path}::{name} takes arguments (pytest fixtures are not available without pytest)")
                continue
            try:
                fn()
                passed += 1
            except Exception:
                failed += 1
                print(f"FAILED {path}::{name}\n{traceback.format_exc()}")
    print(f"mini_test: {passed} passed, {failed} failed, {skipped} skipped")
    return 0 if failed == 0 and skipped == 0 and passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
