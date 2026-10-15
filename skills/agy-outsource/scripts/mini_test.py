#!/usr/bin/env python3
"""mini_test.py - stdlib test runner used when pytest is not installed.

  python mini_test.py [--nonce-stdin] tests/test_a.py hidden/test_b.py

Runs, per file, every unittest.TestCase and every top-level `test_*` function that takes no arguments
(plain `assert` style). The current directory is put first on sys.path so tests import the code under test
the same way pytest's rootdir import does. Exit 0 when everything passed, 1 on any failure, error or skip
(acceptance tests must all run).

The code under test runs in this process, so anything it raises, SystemExit included, counts as a failure and the
result line is still printed last. --nonce-stdin: the first line of stdin is a per-run nonce, read before any test
file is loaded and printed in the result line (`mini_test: 3 passed, 0 failed, 0 skipped [nonce]`); outsource.py
accepts only the line that carries it, so code under test cannot print a passing line of its own and end the run.
While the tests run the nonce is in no Python object at all (see stash): code that walks the frames up from an
exception it caught finds nothing, and code that finds the pipe and reads it consumes it, so this runner's own line
then carries no nonce and is rejected.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import traceback
import unittest


def read_nonce_line() -> bytes:
    """The first line of fd 0, read unbuffered (sys.stdin would keep the chunk it read in its buffer)."""
    data = b""
    while not data.endswith(b"\n") and len(data) < 256:
        chunk = os.read(0, 64)
        if not chunk:
            break
        data += chunk
    return data.strip()


def stash(secret: bytes) -> int:
    """Park secret in an OS pipe whose write end is closed and return the read end: the only trace left in Python is
    a small int, and reading the pipe empties it for good."""
    r, w = os.pipe()
    os.write(w, secret)
    os.close(w)
    return r


def unstash(fd) -> str:
    if fd is None:
        return ""
    try:
        data = os.read(fd, 256)
        os.close(fd)
    except OSError:
        return ""
    return data.decode("ascii", "replace").strip()


def load(path: str):
    name = "_mt_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(argv)
    nonce_fd = None
    if argv and argv[0] == "--nonce-stdin":
        argv.pop(0)
        nonce_fd = stash(read_nonce_line())  # before any test (and the code it imports) is loaded
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
        sys.stdin = open(os.devnull, encoding="utf-8")
    sys.path.insert(0, os.getcwd())
    passed = failed = skipped = 0
    for path in argv:
        try:
            mod = load(path)
        except KeyboardInterrupt:
            raise
        except BaseException:  # SystemExit at import too: it must not skip this runner's result line
            failed += 1
            print(f"ERROR collecting {path}\n{traceback.format_exc()}")
            continue
        n_cases = 0
        try:
            suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
            n_cases = suite.countTestCases()
            if n_cases:
                res = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
                n_bad = len(res.failures) + len(res.errors) + len(res.unexpectedSuccesses)
                failed += n_bad
                skipped += len(res.skipped)
                passed += res.testsRun - n_bad - len(res.skipped)
                failed += max(n_cases - res.testsRun, 0)  # cases the run never reached
        except KeyboardInterrupt:
            raise
        except BaseException:
            failed += max(n_cases, 1)
            print(f"ERROR running the unittest cases of {path}\n{traceback.format_exc()}")
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
            except KeyboardInterrupt:
                raise
            except BaseException:
                failed += 1
                print(f"FAILED {path}::{name}\n{traceback.format_exc()}")
    sys.stdout.flush()
    nonce = unstash(nonce_fd)
    print(f"mini_test: {passed} passed, {failed} failed, {skipped} skipped" + (f" [{nonce}]" if nonce else ""))
    sys.stdout.flush()
    return 0 if failed == 0 and skipped == 0 and passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
