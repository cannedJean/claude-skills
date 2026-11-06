"""Tests for `agy_task.py batch` (independent tasks fanned out to parallel agy sessions) with a fake agy.

  python -m unittest discover -s scripts/tests     (from the skill folder)
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
FAKE_SLEEP = 2.0


def make_fake_agy(tmp: Path) -> str:
    """A stand-in for agy: sleeps, writes out.md in its cwd, emits one stream-json result event."""
    py = tmp / "fake_agy.py"
    py.write_text(textwrap.dedent(f"""
        import json, os, sys, time
        time.sleep({FAKE_SLEEP})
        prompt = sys.argv[sys.argv.index("-p") + 1]
        open("out.md", "w", encoding="utf-8").write("reviewed: " + prompt[:40])
        pt = sys.argv[sys.argv.index("--print-timeout") + 1] if "--print-timeout" in sys.argv else ""
        print(json.dumps({{"event": "result", "result": {{"status": "SUCCESS", "response": "done " + pt}}}}))
    """), encoding="utf-8")
    if os.name == "nt":
        bat = tmp / "fake_agy.bat"
        bat.write_text(f'@"{sys.executable}" "{py}" %*\r\n', encoding="utf-8")
        return str(bat)
    sh = tmp / "fake_agy"
    sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{py}" "$@"\n', encoding="utf-8")
    sh.chmod(0o755)
    return str(sh)


class Batch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agyb_"))
        self.agy = make_fake_agy(self.tmp)
        (self.tmp / "a.py").write_text("x = 1\n", encoding="utf-8")

    def run_batch(self, tasks, *extra):
        manifest = self.tmp / "manifest.json"
        manifest.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, PYTHONIOENCODING="cp949")  # must not crash on non-cp949 output either
        t0 = time.time()
        cp = subprocess.run([sys.executable, str(SCRIPTS / "agy_task.py"), "--agy-bin", self.agy,
                             "--model", "gemini-4-argon-low", "batch", "-m", str(manifest),
                             "-w", str(self.tmp / "runs"), "--stagger", "0", *extra],
                            capture_output=True, env=env)
        return cp, time.time() - t0

    def test_three_independent_tasks_run_in_parallel(self):
        tasks = [{"name": f"review-{k}", "task": f"/teamwork review dimension {k} – report", "inputs": [str(self.tmp / "a.py")]}
                 for k in ("bugs", "tests", "docs")]
        cp, elapsed = self.run_batch(tasks, "--parallel", "3")
        self.assertEqual(cp.returncode, 0, cp.stderr.decode("utf-8", "replace")[-2000:])
        out = json.loads(cp.stdout.decode("utf-8"))
        self.assertEqual(out["status"], "ok")
        self.assertEqual([t["name"] for t in out["tasks"]], ["review-bugs", "review-tests", "review-docs"])
        self.assertTrue(all(t["status"] == "ok" for t in out["tasks"]))
        wds = {t["workdir"] for t in out["tasks"]}
        self.assertEqual(len(wds), 3)
        for t in out["tasks"]:
            self.assertIn("out.md", t["result"]["changes"]["created"])
            self.assertEqual(t["result"]["timeout"]["value"], "60m")   # adaptive default per task
        # sequential would take >= 3 * FAKE_SLEEP; parallel finishes well under that
        self.assertLess(out["elapsed_sec"], 3 * FAKE_SLEEP - 1.0)
        self.assertTrue((self.tmp / "runs" / "batch_result.json").is_file())

    def test_parallel_1_is_sequential(self):
        tasks = [{"name": f"t{i}", "task": "/teamwork x"} for i in range(2)]
        cp, _ = self.run_batch(tasks, "--parallel", "1")
        out = json.loads(cp.stdout.decode("utf-8"))
        self.assertGreaterEqual(out["elapsed_sec"], 2 * FAKE_SLEEP)

    def test_duplicate_names_rejected(self):
        cp, _ = self.run_batch([{"name": "x", "task": "a"}, {"name": "x", "task": "b"}])
        self.assertEqual(cp.returncode, 5)
        self.assertIn("duplicate", cp.stdout.decode("utf-8") + cp.stderr.decode("utf-8"))

    def test_one_failure_is_reported_not_fatal(self):
        tasks = [{"name": "good", "task": "/teamwork ok"}, {"name": "bad", "task": "/teamwork x", "inputs": [str(self.tmp / "missing.py")]}]
        cp, _ = self.run_batch(tasks, "--parallel", "2")
        out = json.loads(cp.stdout.decode("utf-8"))
        self.assertEqual(cp.returncode, 2)
        self.assertEqual(out["status"], "partial")
        st = {t["name"]: t["status"] for t in out["tasks"]}
        self.assertEqual(st["good"], "ok")
        self.assertNotEqual(st["bad"], "ok")

    def test_per_task_timeout_override(self):
        cp, _ = self.run_batch([{"name": "t", "task": "/teamwork x", "timeout": "45m"}])
        out = json.loads(cp.stdout.decode("utf-8"))
        self.assertEqual(out["tasks"][0]["result"]["timeout"], {"value": "45m", "source": "explicit"})


if __name__ == "__main__":
    unittest.main()
