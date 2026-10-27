"""knowledge_pack.py as a subprocess, on a cp949 console (the Windows default here), through the phase-1 path."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE.parent / "knowledge_pack.py"
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import kp_fixtures  # noqa: E402


def run(*args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env.update({"PYTHONIOENCODING": "cp949"}, **(env_extra or {}))
    cp = subprocess.run([sys.executable, str(CLI), *args], capture_output=True, env=env)
    out = cp.stdout.decode("utf-8", "replace")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = None
    return cp.returncode, data, out + cp.stderr.decode("utf-8", "replace")


class CliTests(unittest.TestCase):
    def test_phase1_path(self):
        with tempfile.TemporaryDirectory() as t:
            cj = str(kp_fixtures.make_course(t))
            code, d, raw = run("preflight", "--course", cj)
            self.assertEqual(code, 0, raw)
            self.assertEqual([l["id"] for l in d["lectures"]], ["db01", "db02"])
            code, d, raw = run("ingest", "--course", cj)
            self.assertEqual(code, 0, raw)
            self.assertEqual(d["lectures"][1]["flags"], {"injection_like": 1})
            code, d, raw = run("build", "--course", cj)
            self.assertEqual(code, 2, raw)  # struct never ran: G4 blocks publishing
            self.assertFalse(d["published"])
            self.assertFalse((Path(t) / "package").exists())
            code, d, raw = run("build", "--course", cj, "--allow-pending")
            self.assertEqual(code, 0, raw)
            self.assertTrue((Path(t) / "package" / "index.md").is_file())
            code, d, raw = run("gate", "--course", cj, "--allow-pending")
            self.assertEqual(code, 0, raw)
            code, d, raw = run("status", "--course", cj)
            self.assertEqual(code, 0, raw)
            self.assertEqual(d["next"], "struct")
            code, d, raw = run("build", "--course", cj, "--allow-pending", "--strict")
            self.assertEqual(code, 2, raw)  # the thin graphic slide is low trust
            self.assertTrue((Path(t) / "package" / "index.md").is_file())  # previous package untouched

    def test_full_path_with_fakes(self):
        import fake_integrator
        import fake_reader
        import kp_common as kc
        import kp_fixtures as fx
        with tempfile.TemporaryDirectory() as t:
            cj = str(fx.make_course(t))
            work = Path(t) / ".kpack"
            self.assertEqual(run("ingest", "--course", cj)[0], 0)
            code, d, raw = run("struct", "--course", cj)
            self.assertEqual(code, 2, raw)
            ing = {l: kc.read_json(work / "ingest" / f"{l}.json") for l in ("db01", "db02")}
            truth = {r["id"]: r["analysis"] for doc in ing.values() for r in doc["records"]}
            refs = {r["id"]: kc.read_json(r["ref_path"]) for doc in ing.values() for r in doc["records"]}
            for _ in range(3):
                st = kc.read_json(work / "struct" / "state.json")
                for bid, b in st["batches"].items():
                    if b["status"] == "pending":
                        fake_reader.answer_batch(work / "struct" / "batches" / f"{bid}.json", truth, refs, st)
                code, d, raw = run("struct", "--course", cj)
                if code == 0:
                    break
            self.assertEqual(code, 0, raw)
            code, d, raw = run("integrate", "--course", cj)
            self.assertEqual(code, 2, raw)
            for lec in ("db01", "db02"):
                fake_integrator.write_deck(work, lec)
            self.assertEqual(run("integrate", "--course", cj)[0], 0)
            code, d, raw = run("merge", "--course", cj)
            self.assertEqual(code, 0, raw)
            code, d, raw = run("build", "--course", cj)
            self.assertEqual(code, 0, raw)
            self.assertTrue(d["published"])
            self.assertEqual(d["registry"]["version"], 1)
            code, d, raw = run("status", "--course", cj)
            self.assertEqual(d["next"], "build")
            gl = (Path(t) / "package" / "glossary" / "db01.md").read_text(encoding="utf-8")
            self.assertIn('<a id="2nf"></a>', gl)
            self.assertIn("선수 개념: 1nf (stated)", gl)
            code, d, raw = run("build", "--course", cj)
            self.assertEqual((code, d["registry"]["version"]), (0, 1))  # unchanged rebuild: same registry

    @unittest.skipUnless(os.name == "nt", "Windows refuses to rename a folder that is another process's cwd")
    def test_build_while_a_tutor_session_holds_the_package(self):
        with tempfile.TemporaryDirectory() as t:
            cj = str(kp_fixtures.make_course(t))
            run("ingest", "--course", cj)
            for _ in range(2):
                self.assertEqual(run("build", "--course", cj, "--allow-pending")[0], 0)
            pkg = Path(t) / "package"
            before = {p.name: (p / "manifest.json").read_bytes() for p in (pkg, Path(t) / "package.prev")}
            holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=str(pkg))
            try:
                time.sleep(0.5)
                code, d, raw = run("build", "--course", cj, "--allow-pending")
            finally:
                holder.kill()
                holder.wait()
            self.assertEqual((code, d and d["ok"], d and d["published"]), (2, False, False), raw)
            self.assertIn("in use", d["error"])
            self.assertEqual({p.name: (p / "manifest.json").read_bytes() for p in (pkg, Path(t) / "package.prev")}, before)
            self.assertIn("Not published", (Path(t) / ".kpack" / "report.md").read_text(encoding="utf-8"))

    def test_renumber_guard(self):
        with tempfile.TemporaryDirectory() as t:
            cj = str(kp_fixtures.make_course(t))
            run("ingest", "--course", cj)
            run("build", "--course", cj, "--allow-pending")
            slides = Path(t) / "db01" / "slides"
            (slides / "slide_003.png").rename(slides / "slide_000.png")  # moves an image to the front
            code, d, raw = run("ingest", "--course", cj)
            self.assertEqual(code, 2, raw)
            self.assertEqual(d["blocked"][0]["lecture"], "db01")
            code, d, raw = run("ingest", "--course", cj, "--accept-renumber")
            self.assertEqual(code, 0, raw)
            self.assertTrue((Path(t) / ".kpack" / "renumber_map.json").is_file())

    def test_bad_course_and_init(self):
        with tempfile.TemporaryDirectory() as t:
            code, d, raw = run("preflight", "--course", str(Path(t) / "none.json"))
            self.assertEqual(code, 5, raw)
            kp_fixtures.make_course(t)
            (Path(t) / "course.json").unlink()
            code, d, raw = run("init", "--root", t, "--course-id", "db", "--title", "데이터베이스", "--write")
            self.assertEqual(code, 0, raw)
            self.assertEqual([l["id"] for l in d["course"]["lectures"]], ["db01", "db02"])
            code, d, raw = run("preflight", "--course", str(Path(t) / "course.json"))
            self.assertEqual(code, 0, raw)

    def test_init_ids_and_layouts(self):
        def init(names, cid="db", pdf=()):
            with tempfile.TemporaryDirectory() as t:
                for n in names:
                    (Path(t) / n / "slides").mkdir(parents=True)
                    (Path(t) / n / "slide_analysis").mkdir()
                for n in pdf:  # agy-pdf-analyzer's default --out
                    (Path(t) / n / "ref").mkdir(parents=True)
                    (Path(t) / n / "source.json").write_text('{"file": "x.pdf"}', encoding="utf-8")
                code, d, raw = run("init", "--root", t, "--course-id", cid, "--write")
                ok = run("preflight", "--course", str(Path(t) / "course.json"))[0] if code == 0 else None
                written = (Path(t) / "course.json").is_file()
            return code, d, ok, written
        for names, want in ((["데이터베이스", "운영체제"], ["l01", "l02"]), (["Lecture 1", "lecture-1"], ["lecture1", "l02"]),
                            (["databasesystems01", "databasesystems02"], ["databasesystem01", "databasesystem02"]),
                            (["1강 데이터베이스"], ["l1"])):
            code, d, ok, _ = init(names)
            self.assertEqual((code, [l["id"] for l in d["course"]["lectures"]], ok), (0, want, 0), names)
        code, d, ok, written = init(["db01"], cid="DB-2024")
        self.assertEqual((code, d["ok"], written), (5, False, False))
        code, d, ok, _ = init([], pdf=["lec1_analysis"])
        self.assertEqual((code, ok), (0, 0))
        self.assertEqual(d["course"]["lectures"], [{"id": "lec1", "title": "lec1", "slides": "lec1_analysis/ref",
                                                    "analysis": "lec1_analysis"}])

    def test_preflight_relays_upstream_statuses_and_pdf_layout(self):
        spec = {"db01": {"slides": [("sql", "failed"), ("table", "escalated"), ("erd", "needs_attention"),
                                    ("formula", "accepted")]}}
        with tempfile.TemporaryDirectory() as t:
            cj = kp_fixtures.make_course(t, spec)
            p = Path(t) / "db01" / "slide_analysis" / "results" / "slide_004.json"
            p.write_text(p.read_text(encoding="utf-8").replace('"status": "accepted"', '"status": "ok"'), encoding="utf-8")
            code, d, raw = run("preflight", "--course", str(cj))
            self.assertEqual(code, 0, raw)
            text = "\n".join(d["warnings"])
            self.assertIn("1 slide(s) failed in the analyzer (re-run slide_analyze.py run", text)
            self.assertIn("2 slide(s) escalated or needing attention", text)
            self.assertIn("legacy reviewer-mode status (run slide_analyze.py rescore", text)
        with tempfile.TemporaryDirectory() as t:
            cj = kp_fixtures.make_pdf_course(t, {"db01": {"slides": [("sql", "accepted"), ("table", "accepted")]}})
            (Path(t) / "db01_analysis" / "ref" / "page_0002.png").unlink()  # page image not extractable
            code, d, raw = run("preflight", "--course", str(cj))
            self.assertEqual(d["lectures"][0]["source_pdf"], "lecture.pdf")
            self.assertEqual(d["lectures"][0]["results_without_image"], ["page_0002.json"])
            self.assertTrue(any("install pymupdf" in w for w in d["warnings"]), d["warnings"])
            code, d, raw = run("ingest", "--course", str(cj))
            self.assertEqual((code, d["lectures"][0]["flags"], d["lectures"][0]["warnings"]), (0, {}, []), raw)

    def test_exit_codes(self):
        with tempfile.TemporaryDirectory() as t:
            cj = str(kp_fixtures.make_course(t))
            code, d, raw = run("gate", "--course", cj)
            self.assertEqual((code, d and d["ok"]), (2, False), raw)
            self.assertIn("ingest", d["error"])
            shim = Path(t) / "nopil" / "PIL"
            shim.mkdir(parents=True)
            (shim / "__init__.py").write_text('raise ImportError("no Pillow (test shim)")\n', encoding="utf-8")
            for cmd in ("preflight", "struct", "build", "gate"):
                code, d, raw = run(cmd, "--course", cj, env_extra={"PYTHONPATH": str(shim.parent)})
                self.assertEqual((code, d and d["error"]), (4, "Pillow is required (pip install pillow)"), cmd + raw)

    def test_missing_shared_exits_4(self):
        with tempfile.TemporaryDirectory() as t:
            code, d, raw = run("status", "--course", "x.json", env_extra={"OCR_EVAL_DIR": t, "HOME": t, "USERPROFILE": t})
            # the sibling-skill candidate still resolves when the skill is installed next to agy-slide-analyzer;
            # only assert the contract when no candidate exists
            if d and not d.get("ok") and "shared code" in str(d.get("error")):
                self.assertEqual(code, 4)


def answer_struct(cj, work, *extra, rounds=4):
    """Drive `struct` with truthful fake readers until it completes; returns the last exit code."""
    import fake_reader
    import kp_common as kc
    code = None
    for _ in range(rounds):
        code, d, raw = run("struct", "--course", cj, *extra)
        if code != 2:
            return code
        docs = [kc.read_json(p) for p in (work / "ingest").glob("*.json")]
        truth = {r["id"]: r["analysis"] for doc in docs for r in doc["records"]}
        refs = {r["id"]: kc.read_json(r["ref_path"]) if r.get("ref_path") else None for doc in docs for r in doc["records"]}
        st = kc.read_json(work / "struct" / "state.json")
        for bid, b in st["batches"].items():
            if b["status"] == "pending":
                fake_reader.answer_batch(work / "struct" / "batches" / f"{bid}.json", truth, refs, st)
    return code


class StatusTests(unittest.TestCase):
    def status(self, cj):
        code, d, raw = run("status", "--course", cj)
        self.assertEqual(code, 0, raw)
        return d

    def test_analyzer_change_after_ingest(self):
        with tempfile.TemporaryDirectory() as t:
            cj = str(kp_fixtures.make_course(t))
            run("ingest", "--course", cj)
            self.assertEqual([l["ingest_fresh"] for l in self.status(cj)["lectures"]], [True, True])
            res = Path(t) / "db01" / "slide_analysis" / "results" / "slide_002.json"  # an analyzer resolve, same image
            res.write_text(res.read_text(encoding="utf-8").replace("응용 책임", "응용 프로그램 책임"), encoding="utf-8")
            d = self.status(cj)
            self.assertEqual([l["ingest_fresh"] for l in d["lectures"]], [False, True])
            self.assertEqual((d["lectures"][0]["analyzer_changed"], d["next"]), (["db01-s002"], "ingest"))
            run("ingest", "--course", cj)
            self.assertEqual(self.status(cj)["next"], "struct")

    def test_struct_must_cover_every_lecture(self):
        with tempfile.TemporaryDirectory() as t:
            cj = str(kp_fixtures.make_course(t))
            work = Path(t) / ".kpack"
            run("ingest", "--course", cj)
            self.assertEqual(answer_struct(cj, work, "--lectures", "db01"), 0)
            d = self.status(cj)
            self.assertEqual(d["next"], "struct")
            self.assertGreater(d["struct_open"], 0)  # db02's formula and visual regions were never read
            self.assertEqual(answer_struct(cj, work), 0)
            d = self.status(cj)
            self.assertEqual((d["next"], d["struct_open"]), ("integrate", 0))

    def test_unreadable_reads_are_not_answers(self):
        with tempfile.TemporaryDirectory() as t:
            cj = str(kp_fixtures.make_course(t))
            work = Path(t) / ".kpack"
            run("ingest", "--course", cj)
            self.assertEqual(run("struct", "--course", cj)[0], 2)
            reads = work / "struct" / "reads" / "b001.json"
            reads.parent.mkdir(parents=True, exist_ok=True)
            reads.write_text('{{"i00001": {{"not_found": true}}}}', encoding="utf-8")  # doubled braces
            d = self.status(cj)
            self.assertIn("b001.md", d["pending_batches"])
            self.assertEqual([Path(p).resolve() for p in d["unreadable_reads"]], [reads.resolve()])
            self.assertIn("unreadable_reads", d["next"])
            code, d, raw = run("struct", "--course", cj)
            self.assertEqual(code, 2, raw)
            self.assertEqual([Path(p).resolve() for p in d["unreadable_reads"]], [reads.resolve()])
            self.assertIn("unreadable_reads", d["next"])
        prompt = (HERE.parent.parent / "prompts" / "struct_read.md").read_text(encoding="utf-8")
        self.assertNotIn("{{", prompt)
        self.assertIn('{"not_found": true}', prompt)

    def test_stale_decks_and_merge(self):
        import fake_integrator
        with tempfile.TemporaryDirectory() as t:
            cj = str(kp_fixtures.make_course(t))
            work = Path(t) / ".kpack"
            run("ingest", "--course", cj)
            self.assertEqual(answer_struct(cj, work), 0)
            run("integrate", "--course", cj)
            for lec in ("db01", "db02"):
                fake_integrator.write_deck(work, lec)
            self.assertEqual(run("integrate", "--course", cj)[0], 0)
            self.assertEqual(self.status(cj)["next"], "merge")
            self.assertEqual(run("merge", "--course", cj)[0], 0)
            self.assertEqual(self.status(cj)["next"], "build")

            def edit(deck):  # re-authored after the merge, same digest: still valid, but the merge is stale
                deck["sections"][-1]["title"] = "프롬프트 인젝션 보안"
            fake_integrator.write_deck(work, "db02", edit=edit)
            d = self.status(cj)
            self.assertEqual(([l["deck"] for l in d["lectures"]], d["next"]), (["valid", "stale"], "integrate"))
            self.assertEqual(run("integrate", "--course", cj)[0], 0)
            self.assertEqual(self.status(cj)["next"], "merge")
            self.assertEqual(run("merge", "--course", cj)[0], 0)
            self.assertEqual(self.status(cj)["next"], "build")
            # the slides change after the deck was validated: the deck is stale until integrate runs again
            res = Path(t) / "db02" / "slide_analysis" / "results" / "slide_001.json"
            res.write_text(res.read_text(encoding="utf-8").replace("w는 가중치", "w는 기울기"), encoding="utf-8")
            run("ingest", "--course", cj)
            self.assertEqual(answer_struct(cj, work), 0)
            d = self.status(cj)
            self.assertEqual(([l["deck"] for l in d["lectures"]], d["next"]), (["valid", "stale"], "integrate"))


if __name__ == "__main__":
    unittest.main()
