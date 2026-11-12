"""
test_patch_pattern.py - Regression tests for the Gemini main.js security-guard pattern.

Minified identifiers may contain `$` (Gemini app 1.19.0 emits `c.error("security",$m,...)`),
which `\\w` does not match. These tests build a minimal synthetic app.asar and check that
dry_run_patch / patch_asar recognise the guard with plain and `$`-containing identifiers.
"""
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import patch_gemini_app

PORT = 9223


def guard_js(ret, chk, app, env, msg, stop, cont):
    return (
        f'var {ret}={chk}({app}.app.isPackaged&&{env}!=="dev",process.argv,process.execArgv);'
        f'{ret}?(c.error("security",{msg},`(switch: --${{{ret}}})`),{stop}(),{app}.app.exit(1)):{cont}();'
    )


def build_asar(path, main_js):
    """Write a minimal asar: pickle header + JSON index + src/main.js payload."""
    content = main_js.encode("utf-8")
    header = {"files": {"src": {"files": {"main.js": {"offset": "0", "size": len(content)}}}}}
    raw = json.dumps(header).encode("utf-8")
    padded = raw + b"\x00" * ((4 - len(raw) % 4) % 4)
    data = (
        struct.pack("<I", 4)
        + struct.pack("<I", len(padded) + 8)
        + struct.pack("<I", len(padded) + 4)
        + struct.pack("<I", len(raw))
        + padded
        + content
    )
    with open(path, "wb") as f:
        f.write(data)


def read_main(path):
    with open(path, "rb") as f:
        data = f.read()
    off = struct.unpack("<I", data[4:8])[0] + 8
    hs = struct.unpack("<I", data[12:16])[0]
    node = json.loads(data[16 : 16 + hs].decode("utf-8"))["files"]["src"]["files"]["main.js"]
    return data[off + int(node["offset"]) : off + int(node["offset"]) + int(node["size"])]


CASES = {
    # 1.18.0-style: word-character identifiers only
    "plain": ("Ye", "Km", "m", "k", "Qm", "Pt", "fb"),
    # 1.19.0-style: `$` in the message identifier
    "dollar_msg": ("sh", "zm", "h", "k", "$m", "Pt", "pb"),
    # `$` in every identifier slot, including the app and continuation names
    "dollar_all": ("$s", "z$", "$h", "k$", "$m", "$P", "p$"),
}


class TestSecurityGuardPattern(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _asar(self, name, ids):
        res = os.path.join(self.tmp.name, name, "resources")
        os.makedirs(res)
        path = os.path.join(res, "app.asar")
        build_asar(path, "prefix();" + guard_js(*ids) + "0&&(module.exports={});")
        return path

    def test_dry_run_recognises_guard(self):
        for name, ids in CASES.items():
            with self.subTest(case=name):
                res = patch_gemini_app.dry_run_patch(self._asar(name, ids), port=PORT)
                self.assertEqual(res["status"], "dry_run", res)
                self.assertTrue(res["can_patch"], res)

    def test_patch_replaces_guard_preserving_length(self):
        for name, ids in CASES.items():
            with self.subTest(case=name):
                path = self._asar(name, ids)
                before = read_main(path)
                self.assertTrue(patch_gemini_app.patch_asar(path, port=PORT))
                after = read_main(path)
                self.assertEqual(len(before), len(after))
                app, cont = ids[2], ids[6]
                self.assertIn(
                    f'{app}.app.commandLine.appendSwitch("remote-debugging-port","{PORT}");'.encode(), after
                )
                self.assertIn(f"{cont}();".encode(), after)
                self.assertNotIn(b'c.error("security"', after)
                self.assertTrue(patch_gemini_app.is_asar_patched(path, port=PORT))


if __name__ == "__main__":
    unittest.main()
