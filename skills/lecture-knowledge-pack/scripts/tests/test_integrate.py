"""kp_integrate: digests, deck validation, mention matching, merge, registry (append-only)."""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import fake_integrator  # noqa: E402
import kp_common as kc  # noqa: E402
import kp_fixtures  # noqa: E402
import kp_gate  # noqa: E402
import kp_ingest  # noqa: E402
import kp_integrate as ki  # noqa: E402
import kp_render  # noqa: E402


def setup(root):
    course = kc.load_course(kp_fixtures.make_course(root))
    for lec in course["lectures"]:
        kc.write_json(course["work"] / "ingest" / f"{lec['id']}.json", kp_ingest.ingest_lecture(lec))
    return course


def integrate(course, edit=None, db02=None):
    """Like a subagent per lecture: read the current digest, write the deck (edit applies to db01), validate."""
    ki.advance_integrate(course, course["lectures"], {})
    for lec in course["lectures"]:
        fake_integrator.write_deck(course["work"], lec["id"], {"db01": edit, "db02": db02}[lec["id"]])
    return ki.advance_integrate(course, course["lectures"], {})


def build(course):
    ctx = kp_render.build_context(course, course["lectures"], {"allow_pending": True})
    prep = kp_render.prepare(ctx)
    out = course["package"].with_name("package.staging")
    man = kp_render.render(ctx, prep, out)
    gate = kp_gate.run_gate(out, ctx, prep)
    if gate["passed"]:
        kp_gate.publish(out, course["package"])
        if ctx["concepts"]:
            ki.registry_after_publish(course, ctx["concepts"], man["build_id"])
    return ctx, prep, gate


def failures(gate):
    return [x for x in gate["checks"] if not x["ok"]]


def published(t):
    """A course whose first build is published (registry v1 with the six fixture concepts)."""
    course = setup(t)
    integrate(course)
    ki.advance_merge(course)
    _, _, gate = build(course)
    assert gate["passed"], failures(gate)
    return course


def answer(course, **reads):
    kc.write_json(course["work"] / "merge_reads.json", reads)
    return ki.advance_merge(course)


def resolved(course):
    return (kc.read_json(course["work"] / "concepts.resolved.json") or {}).get("concepts")


def drop_erd(d):
    d["concepts"] = [c for c in d["concepts"] if c["id"] != "new:erd"]


def deprecate_erd(d):
    drop_erd(d)
    d["deprecated"] = [{"id": "erd", "reason": "moved", "superseded_by": "dbms"}]


class DigestAndDeckTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.course = setup(self.t.name)
        self.res = ki.advance_integrate(self.course, self.course["lectures"], {})

    def tearDown(self):
        self.t.cleanup()

    def test_digest_pending_then_valid(self):
        self.assertFalse(self.res["complete"])
        d = (self.course["work"] / "integrate/db01/digest.md").read_text(encoding="utf-8")
        self.assertIn("digest_sha: `", d)
        self.assertIn("기존 개념 registry", d)
        self.assertNotIn("SSAFY 데이터베이스", d)  # boilerplate stays out of the digest
        self.assertIn("빌드 단계", d)
        self.assertLess(kc.est_tokens(d), 25000)
        res = integrate(self.course)
        self.assertTrue(res["complete"], res)

    def errors_for(self, edit, registry=None):
        lec = self.course["lectures"][0]
        fake_integrator.write_deck(self.course["work"], "db01", edit)
        deck = kc.read_json(self.course["work"] / "integrate/db01/deck.json")
        sha = fake_integrator.digest_sha(self.course["work"] / "integrate/db01/digest.md")
        return " | ".join(ki.check_deck(lec["id"], deck, ki.slide_rows(self.course, "db01"), registry, sha))

    def test_each_deck_error(self):
        self.assertEqual(self.errors_for(None), "")
        cases = [
            (lambda d: d["sections"][0].update(last="db01-s002"), "must start at db01-s003"),
            (lambda d: d["sections"].pop(), "do not cover"),
            (lambda d: d["concepts"][0].update(term="SELECTS"), "does not appear on its definition slide"),
            (lambda d: d["concepts"][0].update(id="Select"), "use an existing registry id or new:"),
            (lambda d: d["concepts"].append(copy.deepcopy(d["concepts"][0])), "appears twice"),
            (lambda d: d["concepts"][3]["aliases"]["ko"].append("정"), "too short"),
            (lambda d: d["objectives"][0].update(bloom="know"), "bloom must be"),
            (lambda d: d["objectives"][0].update(evidence=["db02-s001"]), "is not a slide of db01"),
            (lambda d: d["prerequisites"].append({"concept": "new:1nf", "requires": "new:1nf", "kind": "inferred"}), "require itself"),
            (lambda d: d.update(digest_sha="0" * 64), "digest_sha does not match"),
            (lambda d: d["exam_points"][0].update(concepts=["new:nope"]), "unknown concept"),
            (lambda d: d["concepts"][0].update(term="중"), "too short"),  # a 1-character term would match everywhere
            # header/footer text is not lecture content: the term must be visible on the slide itself
            (lambda d: d["concepts"].append({"id": "new:database", "term": "데이터베이스", "aliases": {},
                                             "definition_slide": "db01-s002"}), "only in its header/footer"),
            # values of the wrong type are errors for the subagent, not tracebacks
            (lambda d: d["concepts"][0].update(definition_slide=["db01-s001"]), "is not a slide of db01"),
            (lambda d: d["sections"][0].update(first=["db01-s001"]), "first/last must be slide ids"),
            (lambda d: d["prerequisites"][0].update(requires=["new:1nf"]), "must be concept ids"),
            (lambda d: d["objectives"][0].update(evidence=[["db01-s001"]]), "must be a list of strings"),
            (lambda d: d["objectives"][0].update(bloom=["apply"]), "bloom must be"),
            (lambda d: d["objectives"][0].update(concepts="new:select"), "must be a list"),
            (lambda d: d["concepts"][0].update(id=["new:select"]), "use an existing registry id"),
            (lambda d: d["concepts"].append("new:join"), "concepts[5] is not an object"),
            (lambda d: d["concepts"][0]["aliases"]["en"].append(5), "is not a string"),
            (lambda d: d.update(deprecated=[{"id": ["erd"]}]), "id must be an existing registry id"),
        ]
        for edit, msg in cases:
            self.assertIn(msg, self.errors_for(edit), msg)

    def test_alias_matching(self):
        self.assertFalse(ki.alias_in("PK", "PKG 설치", "pkg설치"))
        self.assertTrue(ki.alias_in("PK", "PK는 NULL 불가", "pk는null불가"))
        self.assertTrue(ki.alias_in("기본키", "기본 키는 하나다", "기본키는하나다"))
        self.assertFalse(ki.alias_in("SELECT", "selection sort", "selectionsort"))
        no = [("I/O", "function 호출"), ("I/O", "relation과 transaction"), ("O(n)", "version 2"), ("A/B", "database"),
              ("1:N", "1NF: 원자값만 가진다."), ("정규화", "비정규화의 장점"), ("정규화", "반정규화"), ("IN", "Data stored in a table")]
        yes = [("Primary Key", "Primary\nKey"), ("Primary Key", "Primary  Key"), ("I/O", "디스크 I/O 비용"),
               ("1:N", "학생 -> 수강 : 1:N"), ("정규화", "비정규화와 정규화"), ("정규화", "정규화는"), ("IN", "WHERE id IN (1, 2)"),
               ("select", "SELECT name"), ("O(n)", "시간 복잡도 O(n)")]
        for alias, raw in no + yes:
            self.assertEqual(ki.alias_in(alias, raw, ki.norm(raw)), (alias, raw) in yes, (alias, raw))

    def test_digest_sha_covers_the_slides_only(self):
        work = self.course["work"]
        sha = ki.current_digest_sha(self.course, self.course["lectures"][0])
        self.assertEqual(sha, ki.current_digest_sha(self.course, "db01"))
        self.assertEqual(sha, kc.read_json(work / "integrate/db01/check.json")["digest_sha"])
        self.assertIn(f"digest_sha: `{sha}`", (work / "integrate/db01/digest.md").read_text(encoding="utf-8"))
        kc.write_json(self.course["registry"], {"concepts": {"x": {"term": "SELECT", "aliases": [], "status": "active"}}})
        self.assertEqual(ki.current_digest_sha(self.course, "db01"), sha)  # registry = context
        ing = kc.read_json(work / "ingest/db01.json")
        ing["records"][0]["text_check"] = "claude_verified"
        kc.write_json(work / "ingest/db01.json", ing)
        self.assertEqual(ki.current_digest_sha(self.course, "db01"), sha)  # trust labels = context
        ing["records"][0]["analysis"]["code_blocks"][0]["code"] = "\n".join(f"-- {i}" for i in range(20))
        kc.write_json(work / "ingest/db01.json", ing)
        sha2 = ki.current_digest_sha(self.course, "db01")
        self.assertNotEqual(sha2, sha)
        ing["records"][0]["analysis"]["code_blocks"][0]["code"] += " changed"  # past the 15 lines the digest shows
        kc.write_json(work / "ingest/db01.json", ing)
        self.assertNotEqual(ki.current_digest_sha(self.course, "db01"), sha2)

    def test_digest_respects_the_token_limit(self):
        lec = self.course["lectures"][0]
        text = "".join(chr(0xAC00 + (i * 37) % 2000) + (" " if i % 4 == 3 else "") for i in range(500))
        recs = [{"id": f"db01-s{i + 1:03d}", "analysis": {"slide_type": "concept", "title": f"제목 {i}",
                 "verbatim_text": [{"region": "body", "text": text[j * 7:] + text[:j * 7]} for j in range(4)]},
                 "analysis_sha": f"x{i}", "boilerplate": [], "build_of": None, "text_check": "ocr_verified"} for i in range(90)]
        kc.write_json(self.course["work"] / "ingest/db01.json", {"records": recs})
        kc.write_json(self.course["registry"], {"concepts": {f"c-{i}": {"term": f"개념 {text[i:i + 6]}", "aliases": [
            text[i:i + 9], f"Concept Name {i}"], "status": "active"} for i in range(400)}})
        out = ki.advance_integrate(self.course, [lec], {})["lectures"][0]
        self.assertLessEqual(out["digest_tokens"], 25000)
        self.assertNotIn("warning", out)
        out = ki.advance_integrate(self.course, [lec], {"digest_max_tokens": 3000})["lectures"][0]
        self.assertIn("over the 3000 limit", out["warning"])

    def test_registry_rows_for_this_lecture_come_first(self):
        reg = {"concepts": {f"a-{i:03d}": {"term": f"용어{i:03d}", "aliases": [], "status": "active"} for i in range(50)}}
        reg["concepts"]["zz-select"] = {"term": "SELECT", "aliases": [], "status": "active", "lecture": "db01"}
        reg["concepts"]["zz-old"] = {"term": "OLD", "aliases": [], "status": "deprecated", "superseded_by": "zz-select"}
        d = ki.build_digest(self.course, self.course["lectures"][0], reg, 1200, n_rows=5)
        self.assertIn("| zz-select | SELECT |  | db01 |", d)  # its term is on db01-s001, so it survives the row cap
        self.assertIn("(외 46개 생략)", d)
        self.assertIn("| zz-old | OLD | zz-select |", d)  # deprecated ids are shown so that they are not reused


class MergeTests(unittest.TestCase):
    def test_resolved_concepts(self):
        with tempfile.TemporaryDirectory() as t:
            course = setup(t)
            integrate(course)
            res = ki.advance_merge(course)
            self.assertTrue(res["complete"], res)
            doc = kc.read_json(course["work"] / "concepts.resolved.json")
            self.assertEqual(doc["decks_sha"], ki.decks_sha(course))
            c = doc["concepts"]
            self.assertEqual(sorted(c), ["1nf", "2nf", "dbms", "erd", "linear-regression", "select"])
            self.assertEqual(c["2nf"]["requires"], [{"requires": "1nf", "kind": "stated", "evidence": ["db01-s006"]}])
            self.assertIn("제1정규형", c["1nf"]["aliases_derived"])  # not on any slide
            self.assertEqual([m["slide"] for m in c["1nf"]["mentions"]], ["db01-s006"])  # build step folded
            self.assertEqual(c["select"]["mentions"][0], {"slide": "db01-s001", "role": "definition"})
            self.assertTrue(all(x["grounded"] for x in c.values()))
            deck = kc.read_json(course["work"] / "integrate/db01/deck.resolved.json")
            self.assertEqual(deck["objectives"][0]["concepts"], ["select"])

    def test_definition_on_a_build_step_counts_as_a_mention(self):
        with tempfile.TemporaryDirectory() as t:
            course = setup(t)
            recs = {r["id"]: r for r in kc.read_json(course["work"] / "ingest/db01.json")["records"]}
            self.assertEqual(recs["db01-s005"]["build_of"], "db01-s006")
            integrate(course, lambda d: d["concepts"][3].update(definition_slide="db01-s005"))
            self.assertTrue(ki.advance_merge(course)["complete"])
            self.assertEqual(resolved(course)["1nf"]["mentions"], [{"slide": "db01-s005", "role": "definition"},
                                                                    {"slide": "db01-s006", "role": "mention"}])

    def test_stated_without_evidence_becomes_inferred(self):
        with tempfile.TemporaryDirectory() as t:
            course = setup(t)
            integrate(course, lambda d: d["prerequisites"].append(
                {"concept": "new:erd", "requires": "new:dbms", "kind": "stated", "evidence": ["db01-s003"]}))
            ki.advance_merge(course)
            c = kc.read_json(course["work"] / "concepts.resolved.json")["concepts"]
            self.assertEqual(c["erd"]["requires"][0]["kind"], "inferred")

    def test_cycle_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            course = setup(t)
            integrate(course, lambda d: d["prerequisites"].append(
                {"concept": "new:1nf", "requires": "new:2nf", "kind": "inferred", "evidence": []}))
            res = ki.advance_merge(course)
            self.assertFalse(res["complete"])
            self.assertIn("cycle", " ".join(res["errors"]))

    def test_stale_resolved_files_are_removed(self):
        with tempfile.TemporaryDirectory() as t:
            course = setup(t)
            work = course["work"]
            files = [work / "concepts.resolved.json", work / "integrate/db01/deck.resolved.json",
                     work / "integrate/db02/deck.resolved.json"]
            integrate(course)
            self.assertTrue(ki.advance_merge(course)["complete"])
            before = ki.decks_sha(course)
            # a deck re-authored against the same digest: the resolved files no longer describe it
            integrate(course, lambda d: d["sections"][0].update(title="새 제목"))
            self.assertNotEqual(ki.decks_sha(course), before)
            self.assertFalse(any(p.is_file() for p in files))
            self.assertTrue(ki.advance_merge(course)["complete"])
            self.assertTrue(all(p.is_file() for p in files))
            # a merge that fails leaves nothing behind for build to publish
            integrate(course, lambda d: d["prerequisites"].append(
                {"concept": "new:1nf", "requires": "new:2nf", "kind": "inferred", "evidence": []}))
            ki.advance_integrate(course, course["lectures"], {})
            self.assertFalse(ki.advance_merge(course)["complete"])
            self.assertFalse(any(p.is_file() for p in files))
            # slides changed after integrate: merge refuses the decks until integrate runs again
            integrate(course)
            ing = kc.read_json(work / "ingest/db01.json")
            ing["records"][0]["analysis"]["code_blocks"][0]["code"] = "FIND name FROM students;"
            kc.write_json(work / "ingest/db01.json", ing)
            res = ki.advance_merge(course)
            self.assertIn("decks not valid for the current slides: db01", res["error"])
            self.assertFalse(any(p.is_file() for p in files))

    def test_merge_queue_round_trip(self):
        concepts = {"primary-key": {"term": "기본 키", "aliases_all": ["primary key"]},
                    "pk": {"term": "기본키", "aliases_all": ["PK"]},
                    "foreign-key": {"term": "외래 키", "aliases_all": ["foreign key"]}}
        pairs = ki.merge_candidates(concepts, set())
        self.assertEqual([(p["a"], p["b"]) for p in pairs], [("pk", "primary-key")])
        self.assertEqual(ki.merge_candidates(concepts, {("pk", "primary-key")}), [])

    def test_ko_en_swapped_duplicates_are_candidates(self):
        swaps = [({"term": "정규화", "aliases_all": ["normalization"]}, {"term": "Normalization", "aliases_all": ["정규화"]}),
                 ({"term": "PK", "aliases_all": ["Primary Key"]}, {"term": "기본 키", "aliases_all": ["PK"]}),
                 ({"term": "트랜잭션", "aliases_all": []}, {"term": "Transaction", "aliases_all": ["트랜잭션"]}),
                 ({"term": "기본 키", "aliases_all": ["기본키"]}, {"term": "Primary Key", "aliases_all": ["기본 키"]})]
        for a, b in swaps:
            self.assertEqual(len(ki.merge_candidates({"a": a, "b": b}, set())), 1, (a, b))
        self.assertEqual(ki.merge_candidates({"a": {"term": "2NF", "aliases_all": ["제2정규형"]},
                                              "b": {"term": "1NF", "aliases_all": ["제1정규형"]}}, set()), [])

    def test_duplicates_go_through_the_queue(self):
        with tempfile.TemporaryDirectory() as t:
            course = setup(t)
            integrate(course, lambda d: d["concepts"].append(
                {"id": "new:dbms-system", "term": "DBMS", "aliases": {"ko": [], "en": [], "abbr": []},
                 "definition_slide": "db01-s002", "definition": "x"}))
            res = ki.advance_merge(course)
            self.assertFalse(res["complete"])
            self.assertTrue((course["work"] / "merge_queue.md").is_file())
            res = answer(course, same=[["dbms", "dbms-system"]], distinct=[])
            self.assertTrue(res["complete"], res)
            c = kc.read_json(course["work"] / "concepts.resolved.json")["concepts"]
            self.assertNotIn("dbms-system", c)
            self.assertFalse((course["work"] / "merge_queue.md").is_file())

    def test_same_slug_for_different_terms_is_queued(self):
        def bias(d):
            d["concepts"].append({"id": "new:select", "term": "편향", "aliases": {"ko": [], "en": ["bias"], "abbr": []},
                                  "definition_slide": "db02-s001", "definition": "직선을 이동시키는 상수 b"})
            d["objectives"].append({"id": "db02-o2", "text": "편향의 역할을 설명할 수 있다", "bloom": "understand",
                                    "concepts": ["new:select"], "evidence": ["db02-s001"]})
        for verdict in ("distinct", "same"):
            with tempfile.TemporaryDirectory() as t:
                course = setup(t)
                integrate(course, db02=bias)
                res = ki.advance_merge(course)
                self.assertEqual(res["pairs"], 1, res)
                self.assertIn("| select | SELECT | db01 | select-2 | 편향 | db02 |",
                              (course["work"] / "merge_queue.md").read_text(encoding="utf-8"))
                self.assertTrue(answer(course, **{verdict: [["select", "select-2"]]})["complete"])
                c = resolved(course)
                o2 = kc.read_json(course["work"] / "integrate/db02/deck.resolved.json")["objectives"][1]["concepts"]
                if verdict == "distinct":
                    self.assertEqual((c["select-2"]["term"], o2), ("편향", ["select-2"]))
                    self.assertNotIn("bias", c["select"]["aliases_derived"])
                else:
                    self.assertNotIn("select-2", c)
                    self.assertEqual(o2, ["select"])
                    self.assertIn("편향", c["select"]["aliases_seen"])  # the second term is kept as an alias
                integrate(course, db02=bias)  # the answer is remembered
                self.assertTrue(ki.advance_merge(course)["complete"])

    def test_bad_merge_reads_are_errors(self):
        with tempfile.TemporaryDirectory() as t:
            course = setup(t)
            integrate(course)
            for bad in ({"same": ["dbms", "dbms-system"]}, [["a", "b"]], {"distinct": [["a", 1]]},
                        {"cross_prerequisites": [{"concept": "a", "requires": "b", "evidence": "db01-s001"}]}):
                kc.write_json(course["work"] / "merge_reads.json", bad)
                res = ki.advance_merge(course)
                self.assertFalse(res["complete"])
                self.assertIn("merge_reads.json:", res["errors"][0])
                self.assertTrue((course["work"] / "merge_reads.json").is_file())  # kept for fixing


class RegistryTests(unittest.TestCase):
    def test_append_only_and_stable(self):
        with tempfile.TemporaryDirectory() as t:
            course = setup(t)
            integrate(course)
            ki.advance_merge(course)
            _, _, gate = build(course)
            self.assertTrue(gate["passed"], failures(gate))
            reg = kc.read_json(course["registry"])
            self.assertEqual(reg["version"], 1)
            self.assertEqual(sorted(reg["concepts"]), ["1nf", "2nf", "dbms", "erd", "linear-regression", "select"])
            self.assertEqual(reg["concepts"]["linear-regression"]["lecture"], "db02")
            # a rebuild with nothing changed keeps the registry version
            integrate(course)
            ki.advance_merge(course)
            build(course)
            self.assertEqual(kc.read_json(course["registry"])["version"], 1)
            # dropping a registry concept from every deck is refused ...
            integrate(course, drop_erd)
            res = ki.advance_merge(course)
            self.assertFalse(res["complete"])
            self.assertIn("erd", " ".join(res["errors"]))
            # ... unless it is deprecated, which keeps the id for learner records
            integrate(course, deprecate_erd)
            res = ki.advance_merge(course)
            self.assertTrue(res["complete"], res)
            _, _, gate = build(course)
            self.assertTrue(gate["passed"], failures(gate))
            reg = kc.read_json(course["registry"])
            self.assertEqual(reg["version"], 2)
            self.assertEqual(reg["concepts"]["erd"]["status"], "deprecated")
            self.assertEqual(reg["concepts"]["erd"]["superseded_by"], "dbms")
            gl = (course["package"] / "glossary/index.md").read_text(encoding="utf-8")
            self.assertIn("폐기된 개념", gl)
            # the next deck need not repeat the deprecation: it is carried forward from the registry
            integrate(course, drop_erd)
            self.assertIn("| erd | ERD | dbms |", (course["work"] / "integrate/db01/digest.md").read_text(encoding="utf-8"))
            res = ki.advance_merge(course)
            self.assertTrue(res["complete"], res)
            self.assertEqual((resolved(course)["erd"]["status"], resolved(course)["erd"]["superseded_by"]), ("deprecated", "dbms"))
            _, _, gate = build(course)
            self.assertTrue(gate["passed"], failures(gate))
            self.assertEqual(kc.read_json(course["registry"])["version"], 2)

    def test_registry_change_keeps_decks_valid(self):
        with tempfile.TemporaryDirectory() as t:
            course = setup(t)
            ki.advance_integrate(course, course["lectures"], {})
            bare = lambda d: d["concepts"][0].update(id="select")  # noqa: E731
            fake_integrator.write_deck(course["work"], "db01", bare)
            self.assertIn("use an existing registry id", " ".join(ki.advance_integrate(course, course["lectures"][:1], {})
                                                                  ["lectures"][0]["errors"]))
            integrate(course)
            ki.advance_merge(course)
            build(course)
            self.assertEqual(kc.read_json(course["registry"])["version"], 1)
            # the publish grew the registry: the decks the subagents wrote stay valid, nobody re-authors them
            res = ki.advance_integrate(course, course["lectures"], {})
            self.assertTrue(res["complete"], res)
            self.assertIn("| select | SELECT |", (course["work"] / "integrate/db01/digest.md").read_text(encoding="utf-8"))
            self.assertTrue(ki.advance_merge(course)["complete"])
            _, _, gate = build(course)
            self.assertTrue(gate["passed"], failures(gate))
            # ids are still checked against the current registry: the bare id is valid now
            fake_integrator.write_deck(course["work"], "db01", bare)
            self.assertEqual(ki.advance_integrate(course, course["lectures"][:1], {})["lectures"][0]["status"], "valid")

    def test_registry_id_reused_by_new_slug(self):
        with tempfile.TemporaryDirectory() as t:
            course = published(t)
            ki.advance_integrate(course, course["lectures"], {})
            d = (course["work"] / "integrate/db01/digest.md").read_text(encoding="utf-8")
            self.assertIn("| select | SELECT |", d)  # the digest shows registry ids to reuse
            integrate(course)  # the fake still writes new:select -> must map onto registry 'select'
            res = ki.advance_merge(course)
            self.assertTrue(res["complete"], res)
            # another slug with exactly the registry term maps onto the registry id too
            integrate(course, lambda d: d["concepts"][0].update(id="new:select-statement"))
            self.assertTrue(ki.advance_merge(course)["complete"])
            self.assertNotIn("select-statement", resolved(course))

    def test_registry_alias_hit_is_queued_not_mapped(self):
        def loose(d):
            d["concepts"][4]["aliases"]["ko"].append("정규화")  # 2nf

        def norm_concept(d):
            loose(d)
            d["concepts"].append({"id": "new:normalization", "term": "정규화", "aliases": {"ko": [], "en": ["normalization"], "abbr": []},
                                  "definition_slide": "db01-s005", "definition": "x"})
            d["objectives"].append({"id": "db01-o2", "text": "정규화의 목적을 설명할 수 있다", "bloom": "understand",
                                    "concepts": ["new:normalization"], "evidence": ["db01-s005"]})
        with tempfile.TemporaryDirectory() as t:
            course = setup(t)
            integrate(course, loose)
            ki.advance_merge(course)
            build(course)
            self.assertIn("정규화", kc.read_json(course["registry"])["concepts"]["2nf"]["aliases"])
            integrate(course, norm_concept)
            res = ki.advance_merge(course)
            self.assertEqual(res.get("pairs"), 1, res)
            self.assertIn("| 2nf | 2NF | db01 | normalization | 정규화 | db01 | shared name 정규화 |",
                          (course["work"] / "merge_queue.md").read_text(encoding="utf-8"))
            self.assertTrue(answer(course, distinct=[["2nf", "normalization"]])["complete"])
            rd = kc.read_json(course["work"] / "integrate/db01/deck.resolved.json")
            self.assertEqual(rd["objectives"][1]["concepts"], ["normalization"])

    def test_renamed_registry_concept_is_queued(self):
        def rename(d):
            d["concepts"][2].update(id="new:er-model", term="ERD 예시")  # was new:erd
        with tempfile.TemporaryDirectory() as t:
            course = published(t)
            integrate(course, rename)
            res = ki.advance_merge(course)
            self.assertEqual(res.get("pairs"), 1, res)
            self.assertIn("| er-model | ERD 예시 | db01 | erd | ERD | registry (db01) |",
                          (course["work"] / "merge_queue.md").read_text(encoding="utf-8"))
            self.assertTrue(answer(course, same=[["erd", "er-model"]])["complete"])
            c = resolved(course)
            self.assertEqual((c["erd"]["status"], c["erd"]["definition_slide"]), ("active", "db01-s003"))
            self.assertNotIn("er-model", c)

    def test_deprecation_rules(self):
        with tempfile.TemporaryDirectory() as t:
            course = published(t)
            reg = kc.read_json(course["registry"])
            ki.advance_integrate(course, course["lectures"], {})
            lec, rows = course["lectures"][0], ki.slide_rows(course, "db01")

            def errors(edit):
                fake_integrator.write_deck(course["work"], "db01", edit)
                deck = kc.read_json(course["work"] / "integrate/db01/deck.json")
                return " | ".join(ki.check_deck(lec["id"], deck, rows, reg, ki.current_digest_sha(course, lec)))
            self.assertEqual(errors(deprecate_erd), "")
            cases = [
                (lambda d: (drop_erd(d), d.update(deprecated=[{"id": "erd", "superseded_by": "erd"}])), "superseded by itself"),
                (lambda d: d.update(concepts=[c for c in d["concepts"] if c["id"] not in ("new:erd", "new:dbms")],
                                    deprecated=[{"id": "erd", "superseded_by": "dbms"}, {"id": "dbms", "superseded_by": "erd"}]),
                 "superseded_by dbms is deprecated too"),
                (lambda d: d.update(deprecated=[{"id": "erd", "superseded_by": "dbms"}]), "also listed under concepts"),
                (lambda d: (drop_erd(d), d.update(deprecated=[{"id": "erd", "superseded_by": "nope"}])), "is unknown"),
            ]
            for edit, msg in cases:
                self.assertIn(msg, errors(edit), msg)
            # another lecture cannot retire a concept a deck still defines
            integrate(course, db02=lambda d: d.update(deprecated=[{"id": "erd", "reason": "x", "superseded_by": None}]))
            res = ki.advance_merge(course)
            self.assertIn("erd: deprecated by db02 but still a concept of db01", res["errors"])
            # two decks retiring one id must agree on its replacement
            integrate(course, deprecate_erd, db02=lambda d: d.update(
                deprecated=[{"id": "erd", "reason": "x", "superseded_by": "linear-regression"}]))
            res = ki.advance_merge(course)
            self.assertIn("erd: db01 and db02 deprecate it with different superseded_by", res["errors"])

    def test_deprecated_id_is_never_reused(self):
        with tempfile.TemporaryDirectory() as t:
            course = published(t)
            integrate(course, deprecate_erd)
            ki.advance_merge(course)
            build(course)
            self.assertEqual(kc.read_json(course["registry"])["concepts"]["erd"]["status"], "deprecated")
            res = integrate(course)  # the fake writes new:erd again
            self.assertIn("deprecated ids are never reused", " ".join(res["lectures"][0]["errors"]))
            # the same term under a new slug is a new concept: never mapped onto the deprecated id
            integrate(course, lambda d: d["concepts"][2].update(id="new:er-diagram"))
            self.assertTrue(ki.advance_merge(course)["complete"])
            c = resolved(course)
            self.assertEqual((c["erd"]["status"], c["er-diagram"]["status"]), ("deprecated", "active"))
            _, _, gate = build(course)
            self.assertTrue(gate["passed"], failures(gate))
            reg = kc.read_json(course["registry"])["concepts"]
            self.assertEqual((reg["erd"]["status"], reg["er-diagram"]["status"]), ("deprecated", "active"))

    def test_merge_refuses_a_deck_that_revives_a_deprecated_id(self):
        with tempfile.TemporaryDirectory() as t:
            course = published(t)
            integrate(course)
            reg = kc.read_json(course["registry"])  # deprecated after the deck was validated
            reg["concepts"]["erd"].update(status="deprecated", superseded_by="dbms")
            kc.write_json(course["registry"], reg)
            res = ki.advance_merge(course)
            self.assertIn("erd: deprecated in the registry (superseded by dbms), but db01 lists it as a concept again",
                          " ".join(res["errors"]))

    def test_published_deprecation_follows_its_retired_successor(self):
        def retire_dbms(sup, also=()):
            def edit(d):
                drop_erd(d)
                d["concepts"] = [c for c in d["concepts"] if c["id"] != "new:dbms"]
                d["deprecated"] = [{"id": "dbms", "reason": "x", "superseded_by": sup}] + list(also)
            return edit
        for sup, also, want in (("new:select", (), ("select", "select")),  # erd -> dbms -> select becomes erd -> select
                                (None, (), "no longer an active concept"),  # a dead end: the decks must say what erd is
                                (None, ({"id": "erd", "superseded_by": None},), (None, None))):
            with tempfile.TemporaryDirectory() as t:
                course = published(t)
                integrate(course, deprecate_erd)  # erd -> dbms is published
                self.assertTrue(ki.advance_merge(course)["complete"])
                self.assertTrue(build(course)[2]["passed"])
                integrate(course, retire_dbms(sup, also))  # then dbms is retired; erd is carried forward
                res = ki.advance_merge(course)
                if isinstance(want, str):
                    self.assertIn(want, " ".join(res.get("errors") or []), res)
                    continue
                self.assertTrue(res["complete"], res)
                c = resolved(course)
                self.assertEqual((c["erd"]["superseded_by"], c["dbms"]["superseded_by"]), want)
                _, _, gate = build(course)
                self.assertTrue(gate["passed"], failures(gate))
                erd = kc.read_json(course["registry"])["concepts"]["erd"]
                self.assertEqual(erd["superseded_by"], c["erd"]["superseded_by"])

    def test_registry_records_status_changes(self):
        with tempfile.TemporaryDirectory() as t:
            course = published(t)
            res = {"concepts": {"erd": {"term": "ERD", "status": "deprecated", "superseded_by": "dbms"}}}
            self.assertEqual(ki.registry_after_publish(course, res, "b2")["version"], 2)
            res["concepts"]["erd"]["superseded_by"] = "select"
            self.assertEqual(ki.registry_after_publish(course, res, "b3")["version"], 3)
            res["concepts"]["erd"].update(status="active", superseded_by=None)
            self.assertEqual(ki.registry_after_publish(course, res, "b4")["version"], 4)
            erd = kc.read_json(course["registry"])["concepts"]["erd"]
            self.assertEqual([h["change"] for h in erd["history"]], ["created", "deprecated", "superseded_by_changed", "reactivated"])
            self.assertEqual((erd["status"], erd["superseded_by"]), ("active", None))


if __name__ == "__main__":
    unittest.main()
