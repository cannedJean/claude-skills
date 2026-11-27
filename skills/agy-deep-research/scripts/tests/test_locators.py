import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from uitree import FakeTree  # noqa: E402
import privacy  # noqa: E402
import locators  # noqa: E402

BUNDLED = Path(HERE).parent / "locators.json"
REQUIRED = [
    "prompt_input", "send", "stop_response", "tools", "more_tools", "dr_item", "dr_on",
    "mode_button", "mode_item", "thinking_menu", "new_chat", "sidebar_open",
    "sidebar_close", "sidebar_more", "plan_start", "plan_edit", "plan_cancel_countdown",
    "plan_retry_without_dr", "app_menu", "app_menu_file", "app_menu_quit",
    "app_menu_view", "app_menu_reload", "sign_in",
]


def _spec(name="locators_page_ko.json"):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as fh:
        return json.load(fh)


def _rename(node, old, new):
    if node.get("name") == old:
        node["name"] = new
    for c in node.get("children") or ():
        _rename(c, old, new)


def _ctx(spec, override=None):
    tree = FakeTree(spec)
    doc = tree.snapshot(tree.documents()[0].id)
    locs = locators.load_locators(BUNDLED, override)
    return locators.LocatorContext(tree, doc, locs, privacy.allowed_ids(doc))


def _write(tmp, data):
    p = Path(tmp) / "locators.local.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


class LocatorTests(unittest.TestCase):
    def test_bundled_file_has_required_keys_ko_en(self):
        locs = locators.load_locators(BUNDLED, None)
        for key in REQUIRED:
            self.assertIn(key, locs["locators"], key)
        flat = " ".join(n for e in locs["locators"].values() for n in e["names"])
        for label in ["연구 시작", "Start research", "연구 계획 수정", "Edit the research plan",
                      "Edit plan", "취소", "Cancel", "Deep Research 선택 해제",
                      "Deselect Deep Research", "도구 더보기", "More tools", "메시지 보내기",
                      "Send message", "대답 생성 중지", "Stop response", "업로드 및 도구",
                      "Upload and tools", "re:모드 선택 도구 열기.*", "re:Open mode picker.*",
                      "새 채팅", "New chat", "메인 메뉴", "Main menu", "파일", "File",
                      "re:종료.*", "re:(Quit|Exit).*", "보기", "View", "re:새로고침.*",
                      "re:Reload.*", "로그인", "Sign in", "Gemini 프롬프트 입력",
                      "Enter a prompt for Gemini"]:
            self.assertIn(label, flat)
        self.assertEqual(sorted(locs["error_phrases"]), ["요청하신 작업을 처리하는 중에 오류가 발생했습니다"])
        self.assertEqual(sorted(locs["safety_phrases"]), ["단지 언어 모델일 뿐", "도와드릴 수가 없습니다"])
        self.assertIn("조사 계획 생성 중", locs["status_phrases"])
        self.assertEqual(locs["model_aliases"]["pro"], "Neon")
        self.assertEqual(len(locs["surfaces_allowed"]), 3)

    def test_match_name(self):
        self.assertTrue(locators.match_name("연구   시작 ", ["연구 시작"]))
        self.assertFalse(locators.match_name("연구 시작하기", ["연구 시작"]))
        self.assertTrue(locators.match_name("종료 (Ctrl+Q)", ["re:종료.*"]))
        self.assertFalse(locators.match_name("x종료", ["re:종료.*"]))

    def test_resolve_unique_in_composer_scope(self):
        ctx = _ctx(_spec())
        self.assertEqual([n.id for n in locators.resolve("tools", ctx)], ["b_tools"])
        self.assertEqual([n.id for n in locators.resolve("send", ctx)], ["b_send"])
        self.assertIn("prompt_input", ctx.anchor_cache)

    def test_resolve_plan_card_scope_uses_automation_id_prefix(self):
        ctx = _ctx(_spec())
        # "취소" also exists in the composer region; only the plan card one counts
        self.assertEqual([n.id for n in locators.resolve("plan_cancel_countdown", ctx)], ["plan_cancel"])
        self.assertEqual([n.id for n in locators.resolve("plan_start", ctx)], ["plan_start"])

    def test_miss_payload_has_candidates_and_snippet(self):
        spec = _spec()
        for st in spec["states"].values():
            for d in st["documents"]:
                _rename(d, "연구 시작", "조사 시작")
        ctx = _ctx(spec)
        with self.assertRaises(locators.LocatorMiss) as cm:
            locators.resolve("plan_start", ctx)
        p = cm.exception.payload
        self.assertEqual(p["code"], "UI_LOCATOR_MISS")
        self.assertEqual(p["locator"], "plan_start")
        self.assertEqual(p["scope"], "plan_card")
        self.assertEqual(p["expect"], "unique")
        self.assertEqual(p["found"], 0)
        self.assertLessEqual(len(p["candidates"]), 5)
        self.assertIn("조사 시작", [c["name"] for c in p["candidates"]])
        c0 = p["candidates"][0]
        self.assertEqual(set(c0), {"name", "ct", "role", "similarity"})
        self.assertEqual(c0["similarity"], round(c0["similarity"], 2))
        snip = p["override_snippet"]
        self.assertEqual(snip["version"], 1)
        self.assertIn("조사 시작", snip["locators"]["plan_start"]["names"])
        self.assertIn("연구 시작", snip["locators"]["plan_start"]["names"])
        self.assertEqual(privacy.scrub(p), p)

    def test_override_file_alone_recovers(self):
        spec = _spec()
        for st in spec["states"].values():
            for d in st["documents"]:
                _rename(d, "연구 시작", "조사 시작")
        with self.assertRaises(locators.LocatorMiss) as cm:
            locators.resolve("plan_start", _ctx(spec))
        with tempfile.TemporaryDirectory() as tmp:
            ov = _write(tmp, cm.exception.payload["override_snippet"])
            ctx = _ctx(spec, ov)
            self.assertEqual([n.id for n in locators.resolve("plan_start", ctx)], ["plan_start"])
            # field-level merge keeps the other fields
            self.assertEqual(ctx.locs["locators"]["plan_start"]["scope"], "plan_card")

    def test_override_rejects_unknown_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ({"version": 1, "locators": {"send": {"nmaes": ["x"]}}},
                        {"version": 1, "extra": 1},
                        {"version": 1, "locators": {"send": {"names": "x"}}},
                        {"version": 1, "locators": {"send": {"scope": "nowhere"}}},
                        {"version": 1, "locators": {"send": {"names": ["re:("]}}},
                        {"version": 1, "locators": {"send": {"names": []}}}):
                with self.subTest(bad=bad):
                    ov = _write(tmp, bad)
                    with self.assertRaises(locators.LocatorConfigError):
                        locators.load_locators(BUNDLED, ov)
        with tempfile.TemporaryDirectory() as tmp:
            ov = _write(tmp, {"version": 1, "locators": {"send": {"nmaes": ["x"]}}})
            with self.assertRaises(locators.LocatorConfigError) as cm:
                locators.load_locators(BUNDLED, ov)
            self.assertIn("send", str(cm.exception))
            self.assertIn("nmaes", str(cm.exception))

    def test_override_rejects_foreign_href_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            ov = _write(tmp, {"version": 1, "locators": {"sign_in": {"href_host": "evil.example.invalid"}}})
            with self.assertRaises(locators.LocatorConfigError) as cm:
                locators.load_locators(BUNDLED, ov)
            self.assertIn("href_host", str(cm.exception))
            ok = _write(tmp, {"version": 1, "locators": {"sign_in": {"href_host": "gemini.google.com"}}})
            self.assertEqual(
                locators.load_locators(BUNDLED, ok)["locators"]["sign_in"]["href_host"],
                "gemini.google.com")

    def test_en_synthetic_fixture_resolves_all_keys(self):
        spec = _spec("locators_page_en_synthetic.json")
        self.assertEqual(spec["_note"], "synthetic: Korean recording with labels substituted")
        ctx = _ctx(spec)
        for key in REQUIRED:
            nodes = locators.resolve(key, ctx)
            self.assertGreaterEqual(len(nodes), 1, key)

    def test_ko_fixture_resolves_all_keys(self):
        ctx = _ctx(_spec())
        for key in REQUIRED:
            self.assertGreaterEqual(len(locators.resolve(key, ctx)), 1, key)

    def test_candidates_never_include_sidebar_titles(self):
        spec = _spec()
        for st in spec["states"].values():
            for d in st["documents"]:
                _rename(d, "새 채팅", "새 대화")
        ctx = _ctx(spec)
        ctx.locs["locators"]["new_chat"]["expect"] = "unique"
        with self.assertRaises(locators.LocatorMiss) as cm:
            locators.resolve("new_chat", ctx)
        blob = json.dumps(cm.exception.payload, ensure_ascii=False)
        for secret in ("비밀 대화 제목", "테스트", "example.invalid", "새 대화"):
            self.assertNotIn(secret, blob)

    def test_composer_miss_when_anchor_missing(self):
        spec = _spec()
        for st in spec["states"].values():
            for d in st["documents"]:
                _rename(d, "Gemini 프롬프트 입력", "다른 입력")
        with self.assertRaises(locators.LocatorMiss) as cm:
            locators.resolve("tools", _ctx(spec))
        self.assertEqual(cm.exception.payload["locator"], "prompt_input")

    def test_measured_triples_resolve(self):
        for fx, names in (("locators_page_ko.json", ("사고 수준", "Deep Research", "Pro")),
                          ("locators_page_en_synthetic.json", ("Thinking level", "Deep Research", "Pro"))):
            ctx = _ctx(_spec(fx))
            for key, (ct, role), name in (
                    ("thinking_menu", ("MenuItem", "menuitem"), names[0]),
                    ("dr_item", ("CheckBox", "menuitemcheckbox"), names[1]),
                    ("mode_item", ("RadioButton", "menuitemradio"), names[2])):
                ent = ctx.locs["locators"][key]
                self.assertEqual((ent["ct"], ent.get("role")), (ct, role), key)
                nodes = locators.resolve(key, ctx)
                self.assertEqual([(n.ct, n.role, n.name) for n in nodes], [(ct, role, name)], key)
        # mode_item must not match plain MenuItems such as File/View
        ids = [n.id for n in locators.resolve("mode_item", _ctx(_spec()))]
        self.assertEqual(ids, ["pm_mode"])

    def test_retry_name_whitespace_normalized(self):
        ent = locators.load_locators(BUNDLED, None)["locators"]["plan_retry_without_dr"]
        self.assertTrue(locators.match_name(" Deep Research 사용하지 않고 다시 시도하기 ", ent["names"]))
        self.assertTrue(locators.match_name(" Retry without Deep Research ", ent["names"]))
        self.assertEqual([n.id for n in locators.resolve("plan_retry_without_dr", _ctx(_spec()))], ["plan_retry"])

    def test_menu_items_measured_names(self):
        ctx = _ctx(_spec())
        self.assertEqual([n.name for n in locators.resolve("app_menu_quit", ctx)], ["종료 Ctrl+Q"])
        self.assertEqual([n.name for n in locators.resolve("app_menu_reload", ctx)], ["새로고침 Ctrl+R"])

    def test_expect_classes(self):
        locs = locators.load_locators(BUNDLED, None)["locators"]
        want = {
            "unique": ["prompt_input", "tools", "plan_start", "plan_edit", "mode_button", "app_menu"],
            "at_least_one": ["new_chat", "more_tools", "dr_item", "thinking_menu", "mode_item",
                             "app_menu_file", "app_menu_view", "app_menu_quit", "app_menu_reload"],
            "optional": ["send", "stop_response", "dr_on", "plan_cancel_countdown",
                         "plan_retry_without_dr", "sidebar_open", "sidebar_close",
                         "sidebar_more", "sign_in"],
        }
        for exp, keys in want.items():
            for k in keys:
                self.assertEqual(locs[k]["expect"], exp, k)

    def test_at_least_one_miss_on_zero(self):
        spec = _spec()
        for st in spec["states"].values():
            _rename(st["desktop"], "파일", "문서")
        with self.assertRaises(locators.LocatorMiss) as cm:
            locators.resolve("app_menu_file", _ctx(spec))
        self.assertEqual(cm.exception.payload["found"], 0)
        self.assertEqual(cm.exception.payload["expect"], "at_least_one")

    def test_unique_miss_on_two_has_hint_and_no_added_name(self):
        spec = _spec()
        for st in spec["states"].values():
            for d in st["documents"]:
                _rename(d, "연구 계획 수정", "연구 시작")
        with self.assertRaises(locators.LocatorMiss) as cm:
            locators.resolve("plan_start", _ctx(spec))
        p = cm.exception.payload
        self.assertEqual(p["found"], 2)
        self.assertEqual(p["hint"], "ambiguous: narrow names or scope")
        orig = locators.load_locators(BUNDLED, None)["locators"]["plan_start"]["names"]
        self.assertEqual(p["override_snippet"]["locators"]["plan_start"]["names"], orig)

    def test_override_top_level_limited_to_version_and_locators(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ({"version": 1, "surfaces_allowed": ["re:^x"]},
                        {"version": 1, "scopes": {"main": {}}},
                        {"version": 1, "model_aliases": {}}):
                with self.subTest(bad=bad):
                    with self.assertRaises(locators.LocatorConfigError):
                        locators.load_locators(BUNDLED, _write(tmp, bad))

    def test_scope_config_type_checks(self):
        def mutated(fn):
            with tempfile.TemporaryDirectory() as tmp:
                cfg = json.loads(BUNDLED.read_text(encoding="utf-8"))
                fn(cfg)
                p = Path(tmp) / "b.json"
                p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
                return locators.load_locators(p, None)
        cases = {
            "min_buttons str": lambda c: c["scopes"]["composer"].update(min_buttons="3"),
            "min_buttons 0": lambda c: c["scopes"]["composer"].update(min_buttons=0),
            "min_buttons bool": lambda c: c["scopes"]["composer"].update(min_buttons=True),
            "prefix empty": lambda c: c["scopes"]["plan_card"].update(ancestor_automation_id_prefix=""),
            "anchor unknown": lambda c: c["scopes"]["composer"].update(anchor="nope"),
            "names empty": lambda c: c["locators"]["send"].update(names=[]),
        }
        for label, fn in cases.items():
            with self.subTest(label):
                with self.assertRaises(locators.LocatorConfigError):
                    mutated(fn)

    def test_malformed_json_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "locators.local.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(locators.LocatorConfigError):
                locators.load_locators(BUNDLED, p)

    def test_top_bar_scope_resolves(self):
        ctx = _ctx(_spec())
        self.assertEqual([n.id for n in locators.resolve("app_menu", ctx)], ["tb_menu"])

    def test_href_host_match_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ov = _write(tmp, {"version": 1, "locators": {"sign_in": {"href_host": "gemini.google.com"}}})
            ctx = _ctx(_spec(), ov)
            self.assertEqual([n.id for n in locators.resolve("sign_in", ctx)], ["signin"])
            ctx.doc.find_all(lambda n, a: n.id == "signin")[0].aria["href"] = "https://evil.example.invalid/"
            self.assertEqual(locators.resolve("sign_in", ctx), [])


if __name__ == "__main__":
    unittest.main()
