"""agy_pytest_nonce.py - pytest plugin outsource.py loads (`-p agy_pytest_nonce`) when pytest runs its test gates.

pytest's own summary line ("3 passed in 0.01s") can be printed by the code under test too, which shares the test
process; it would then end the process early with exit code 0. Like mini_test.py's --nonce-stdin, this plugin reads a
per-run nonce from the first line of stdin when it is imported with AGY_PYTEST_NONCE_STDIN set (outsource.py sets it;
the plugin takes it out of the environment again). `-p` plugins are imported before pytest starts capturing and
before any test file or the code it imports is loaded. It parks the nonce in an OS pipe (no Python object holds it
while the tests run), and as the very last output prints

    pytest-nonce: 3 passed, 0 failed, 0 skipped [nonce]

from pytest's own counts. outsource.py accepts only that line, carrying that nonce, as the last line of the run.
Errors (collection, setup, teardown) and unexpected passes count as failed; skips and expected failures as skipped.
"""
from __future__ import annotations

import os

try:
    import pytest
    _trylast = pytest.hookimpl(trylast=True)
except ImportError:  # imported outside pytest (outsource's own unit tests)
    def _trylast(fn):
        return fn

NONCE_ENV = "AGY_PYTEST_NONCE_STDIN"  # set by outsource.py: only then is stdin's first line the nonce


def _read_nonce_line() -> bytes:
    data = b""
    try:
        while not data.endswith(b"\n") and len(data) < 256:
            chunk = os.read(0, 64)
            if not chunk:
                break
            data += chunk
    except OSError:
        return b""
    return data.strip()


def _stash(secret: bytes):
    r, w = os.pipe()
    os.write(w, secret)
    os.close(w)
    return r


def _unstash(fd) -> str:
    try:
        data = os.read(fd, 256)
        os.close(fd)
    except (OSError, TypeError):
        return ""
    return data.decode("ascii", "replace").strip()


_FD = _stash(_read_nonce_line()) if os.environ.pop(NONCE_ENV, None) else None


def result_line(stats: dict, nonce: str) -> str:
    n = lambda *keys: sum(len(stats.get(k) or ()) for k in keys)  # noqa: E731
    passed, failed, skipped = n("passed"), n("failed", "error", "xpassed"), n("skipped", "xfailed")
    return f"pytest-nonce: {passed} passed, {failed} failed, {skipped} skipped" + (f" [{nonce}]" if nonce else "")


@_trylast
def pytest_unconfigure(config):
    """After pytest's own summary (written in pytest_sessionfinish): the last line of the run."""
    tr = config.pluginmanager.get_plugin("terminalreporter")
    line = result_line(getattr(tr, "stats", None) or {}, _unstash(_FD))
    if tr is not None:
        tr.write_line(line)
        flush = getattr(tr, "flush", None)
        if callable(flush):
            flush()
    else:
        print(line, flush=True)
