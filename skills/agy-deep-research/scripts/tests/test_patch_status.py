#!/usr/bin/env python3
"""test_patch_status.py - read-only patch diagnostics that surface app updates before a research step.

Every Gemini update installs a fresh, unpatched `app-<version>` directory. These tests pin down:
  - patch_status(): installed version, patched or not, whether SECURITY_GUARD_RE still matches, and whether the app
    was patched before at an older version (an update undid the patch) - all without writing anything
  - patch_asar() records the version it patched in the patch state file
  - dry_run_patch() on a reshaped guard returns context snippets so a human can see what changed without unpacking
  - `deep_research.py preflight` carries the same facts under checks.gemini_app.patch
All against synthetic app.asar files in a temp dir; the installed app is never read or written.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import patch_gemini_app  # noqa: E402
from test_patch_pattern import build_asar, guard_js  # noqa: E402

PORT = 9223
IDS_1_21_1 = ("ob", "vw", "w", "N", "Cw", "cn", "cx")


class PatchStatusTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="patch_status_")).resolve()
        self.base = self.tmp / "Gemini"
        self.state = self.tmp / "state" / "patch_state.json"
        self._saved = (patch_gemini_app.GEMINI_BASE_DIR, patch_gemini_app.PATCH_STATE_FILE)
        patch_gemini_app.GEMINI_BASE_DIR = str(self.base)
        patch_gemini_app.PATCH_STATE_FILE = str(self.state)

    def tearDown(self) -> None:
        patch_gemini_app.GEMINI_BASE_DIR, patch_gemini_app.PATCH_STATE_FILE = self._saved
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install(self, version: str, main_js: str | None = None) -> Path:
        res = self.base / f"app-{version}" / "resources"
        res.mkdir(parents=True)
        path = res / "app.asar"
        build_asar(str(path), main_js or ("prefix();" + guard_js(*IDS_1_21_1) + "0&&(module.exports={});"))
        return path

    def test_fresh_install_reports_unpatched_and_patchable(self) -> None:
        asar = self._install("1.21.1")
        before = asar.read_bytes()
        st = patch_gemini_app.patch_status(port=PORT)
        self.assertEqual(st["installed"], True)
        self.assertEqual(st["app_version"], "1.21.1")
        self.assertFalse(st["patched"])
        self.assertTrue(st["pattern_match"])
        self.assertIsNone(st["last_patched_version"])
        self.assertFalse(st["update_needs_repatch"])
        self.assertEqual(asar.read_bytes(), before, "patch_status must not write")
        self.assertFalse(self.state.exists())

    def test_patch_records_version_and_status_follows(self) -> None:
        asar = self._install("1.21.1")
        self.assertTrue(patch_gemini_app.patch_asar(str(asar), port=PORT))
        recorded = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(recorded["app_version"], "1.21.1")
        self.assertEqual(recorded["port"], PORT)
        st = patch_gemini_app.patch_status(port=PORT)
        self.assertTrue(st["patched"])
        self.assertEqual(st["last_patched_version"], "1.21.1")
        self.assertFalse(st["update_needs_repatch"])

    def test_update_after_patch_is_reported(self) -> None:
        self._install("1.21.1")
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({"app_version": "1.21.1", "port": PORT}), encoding="utf-8")
        self._install("1.22.0")
        st = patch_gemini_app.patch_status(port=PORT)
        self.assertEqual(st["app_version"], "1.22.0")
        self.assertFalse(st["patched"])
        self.assertEqual(st["last_patched_version"], "1.21.1")
        self.assertTrue(st["update_needs_repatch"])

    def test_reshaped_guard_reports_mismatch_with_context(self) -> None:
        reshaped = ('prefix();var ob=vw(w.app.isPackaged,process.argv);'
                    'if(ob){c.error("security",Cw,ob);cn();w.app.exit(1)}else cx();0&&(module.exports={});')
        asar = self._install("1.30.0", reshaped)
        st = patch_gemini_app.patch_status(port=PORT)
        self.assertFalse(st["pattern_match"])
        dry = patch_gemini_app.dry_run_patch(str(asar), port=PORT)
        self.assertEqual(dry["status"], "dry_run_pattern_mismatch")
        joined = "\n".join(s["snippet"] for s in dry["context_snippets"])
        self.assertIn('c.error("security"', joined)
        self.assertIn("isPackaged", joined)
        self.assertTrue(all("offset" in s and "anchor" in s for s in dry["context_snippets"]))

    def test_no_install_is_reported_not_raised(self) -> None:
        st = patch_gemini_app.patch_status(port=PORT)
        self.assertFalse(st["installed"])
        self.assertIsNone(st["app_version"])

    def test_preflight_carries_patch_status(self) -> None:
        self._install("1.21.1")
        env = dict(os.environ, GEMINI_APP_BASE_DIR=str(self.base), AGY_DR_PATCH_STATE=str(self.state),
                   AGY_TASK=str(TESTS_DIR / "fake_agy_dr.py"), PYTHONIOENCODING="utf-8")
        cp = subprocess.run([sys.executable, str(SCRIPTS_DIR / "deep_research.py"), "preflight",
                             "--target-dir", str(self.tmp / "target")],
                            capture_output=True, text=True, encoding="utf-8", env=env)
        out = json.loads(cp.stdout)
        patch = out["checks"]["gemini_app"]["patch"]
        self.assertEqual(patch["app_version"], "1.21.1")
        self.assertFalse(patch["patched"])
        self.assertTrue(patch["pattern_match"])
        self.assertIn("update_needs_repatch", patch)
        self.assertEqual(out["checks"]["gemini_app"]["latest_app_dir"], str(self.base / "app-1.21.1"))


if __name__ == "__main__":
    unittest.main()
