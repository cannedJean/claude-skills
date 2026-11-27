import copy
import json
import os
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from uitree import FakeTree, Node  # noqa: E402
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


# -- review-fix helpers ----------------------------------------------------
def _walk(d):
    yield d
    for c in d.get("children") or ():
        yield from _walk(c)


def _roots(spec):
    for st in spec["states"].values():
        for d in st["documents"]:
            yield d
        if st.get("desktop"):
            yield st["desktop"]


def _get(spec, node_id):
    for root in _roots(spec):
        for n in _walk(root):
            if n.get("id") == node_id:
                return n
    raise KeyError(node_id)


def _add(spec, parent_id, child):
    for root in _roots(spec):
        for n in _walk(root):
            if n.get("id") == parent_id:
                n.setdefault("children", []).append(child)
                return
    raise KeyError(parent_id)


def _btn(node_id, name, patterns=("invoke",), ct="Button", **kw):
    d = {"id": node_id, "ct": ct, "name": name, "patterns": list(patterns)}
    d.update(kw)
    return d


def _payload(spec, key, mutate=None):
    ctx = _ctx(spec)
    if mutate:
        mutate(ctx)
    try:
        locators.resolve(key, ctx)
    except locators.LocatorMiss as exc:
        return exc.payload
    raise AssertionError("expected LocatorMiss for %s" % key)


def _ids(key, ctx):
    return [n.id for n in locators.resolve(key, ctx)]


class LocatorReviewFixTests(unittest.TestCase):
    # ---- B1: cross-tree id collision -------------------------------------
    def test_popup_node_with_allowed_doc_id_is_not_a_candidate(self):
        spec = _spec()
        for st in spec["states"].values():
            _rename(st["desktop"], "파일", "문서")
        _add(spec, "pop", _btn("conv", "비밀 파일", patterns=(), ct="MenuItem"))
        p = _payload(spec, "app_menu_file")
        blob = json.dumps(p, ensure_ascii=False)
        self.assertNotIn("비밀 파일", blob)
        for c in p["candidates"]:
            self.assertIn(c["name"], privacy.GENERIC_LABELS)

    def test_top_bar_node_with_allowed_doc_id_is_not_a_candidate(self):
        spec = _spec()
        tb = _get(spec, "tb_menu")
        tb["name"] = "메인 메뉴 비밀"
        tb["id"] = "conv"  # collides with an allowed id of the conversation document
        blob = json.dumps(_payload(spec, "app_menu"), ensure_ascii=False)
        self.assertNotIn("비밀", blob)

    def test_popup_generic_label_is_still_a_candidate(self):
        spec = _spec()
        for st in spec["states"].values():
            _rename(st["desktop"], "파일", "문서")
        _add(spec, "pop", _btn("pm_generic", "더보기", patterns=(), ct="MenuItem"))
        names = [c["name"] for c in _payload(spec, "app_menu_file")["candidates"]]
        self.assertEqual(names, ["더보기"])

    # ---- B2: unicode normalisation ---------------------------------------
    def test_match_name_nfd_equals_nfc(self):
        nfd = unicodedata.normalize("NFD", "연구 시작")
        self.assertNotEqual(nfd, "연구 시작")
        self.assertTrue(locators.match_name(nfd, ["연구 시작"]))
        self.assertTrue(locators.match_name("연구 시작", [unicodedata.normalize("NFD", "연구 시작")]))
        self.assertTrue(locators.match_name(nfd, ["re:연구 .*"]))

    # ---- B3: role whitespace ----------------------------------------------
    def test_role_whitespace_is_stripped_in_scope_and_match(self):
        spec = _spec()
        _get(spec, "main")["role"] = " Main "
        _get(spec, "pm_dr")["role"] = " MenuItemCheckBox "
        ctx = _ctx(spec)
        self.assertEqual(_ids("prompt_input", ctx), ["prompt"])
        self.assertEqual(_ids("dr_item", ctx), ["pm_dr"])

    # ---- B4: composer anchor validation ----------------------------------
    def test_anchor_must_not_be_composer_scoped_or_non_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            for label, ov in (
                    ("scope composer", {"prompt_input": {"scope": "composer"}}),
                    ("expect optional", {"prompt_input": {"expect": "optional"}}),
                    ("expect at_least_one", {"prompt_input": {"expect": "at_least_one"}})):
                with self.subTest(label):
                    p = _write(tmp, {"version": 1, "locators": ov})
                    with self.assertRaises(locators.LocatorConfigError) as cm:
                        locators.load_locators(BUNDLED, p)
                    self.assertIn("anchor", str(cm.exception))

    # ---- B5: href host -----------------------------------------------------
    def test_host_ok_rejects_spoofs_schemes_and_malformed(self):
        def node(href=None, value=""):
            return Node(id="n", ct="Hyperlink", aria={"href": href} if href is not None else {},
                        value=value)
        host = "gemini.google.com"
        self.assertTrue(locators._host_ok(node("https://gemini.google.com/signin"), host))
        self.assertTrue(locators._host_ok(node(value="https://gemini.google.com/x"), host))
        for bad in ("https://evil.example.invalid/", "https://gemini.google.com.evil.invalid/",
                    "https://evil.invalid/gemini.google.com", "javascript://gemini.google.com/x",
                    "http://gemini.google.com/x", "https://[bad", 123, ["x"], "", "/signin"):
            with self.subTest(bad=bad):
                self.assertFalse(locators._host_ok(node(bad), host))

    # ---- B6: null automation_id -------------------------------------------
    def test_null_automation_id_does_not_crash_plan_card(self):
        spec = _spec()
        _get(spec, "t0text")["automation_id"] = None
        _get(spec, "conv")["automation_id"] = None
        self.assertEqual(_ids("plan_start", _ctx(spec)), ["plan_start"])

    # ---- B7 / M8 / T14: plan_card scope ------------------------------------
    def test_plan_card_ignores_later_non_plan_turn(self):
        spec = _spec()
        _add(spec, "conv", {"id": "turn2", "ct": "Group",
                            "automation_id": "model-response-message-content-later",
                            "children": [{"id": "turn2t", "ct": "Text", "name": "후속 답변"}]})
        ctx = _ctx(spec)
        self.assertEqual(_ids("plan_start", ctx), ["plan_start"])
        self.assertEqual(_ids("plan_edit", ctx), ["plan_edit"])
        self.assertEqual(_ids("plan_cancel_countdown", ctx), ["plan_cancel"])

    def test_plan_card_nested_prefixed_descendant_belongs_to_outer_turn(self):
        spec = _spec()
        _add(spec, "turn1", {"id": "inner", "ct": "Group",
                             "automation_id": "model-response-message-content-new-body",
                             "children": [{"id": "inner_t", "ct": "Text", "name": "x"}]})
        ctx = _ctx(spec)
        self.assertEqual(_ids("plan_start", ctx), ["plan_start"])
        self.assertEqual(_ids("plan_cancel_countdown", ctx), ["plan_cancel"])

    def test_plan_card_last_turn_with_plan_start_wins(self):
        spec = _spec()
        # the old turn also carries a plan card; only the newest one counts
        _add(spec, "turn0", _btn("old_start", "연구 시작"))
        _add(spec, "turn0", _btn("old_cancel", "취소"))
        ctx = _ctx(spec)
        self.assertEqual(_ids("plan_start", ctx), ["plan_start"])
        self.assertEqual(_ids("plan_cancel_countdown", ctx), ["plan_cancel"])

    def test_plan_card_does_not_see_other_regions(self):
        spec = _spec()
        # a "취소" in the composer / a plan_start outside any turn are not plan-card nodes
        _add(spec, "composer_outer", _btn("stray_start", "연구 시작"))
        ctx = _ctx(spec)
        self.assertEqual(_ids("plan_start", ctx), ["plan_start"])
        self.assertEqual(_ids("plan_cancel_countdown", ctx), ["plan_cancel"])

    def test_plan_card_nested_group_holding_plan_start_keeps_whole_turn(self):
        spec = _spec()
        turn1 = _get(spec, "turn1")
        start = next(c for c in turn1["children"] if c["id"] == "plan_start")
        turn1["children"].remove(start)
        turn1["children"].append({"id": "inner", "ct": "Group",
                                  "automation_id": "model-response-message-content-new-body",
                                  "children": [start]})
        ctx = _ctx(spec)
        self.assertEqual(_ids("plan_start", ctx), ["plan_start"])
        self.assertEqual(_ids("plan_edit", ctx), ["plan_edit"])
        self.assertEqual(_ids("plan_cancel_countdown", ctx), ["plan_cancel"])

    def test_plan_card_miss_candidates_come_from_newest_turn_only(self):
        spec = _spec()
        for st in spec["states"].values():
            for d in st["documents"]:
                _rename(d, "연구 시작", "조사 시작")
        _add(spec, "turn0", _btn("old_btn", "연구 시작 이전"))
        names = [c["name"] for c in _payload(spec, "plan_start")["candidates"]]
        self.assertIn("조사 시작", names)
        self.assertNotIn("연구 시작 이전", names)

    def test_plan_card_unanchored_locator_uses_newest_turn_holding_it(self):
        spec = _spec()
        _add(spec, "turn0", _btn("old_retry", "Retry without Deep Research"))
        _add(spec, "conv", {"id": "turn2", "ct": "Group",
                            "automation_id": "model-response-message-content-later", "children": []})
        self.assertEqual(_ids("plan_retry_without_dr", _ctx(spec)), ["plan_retry"])

    # ---- B8: duplicate ids -------------------------------------------------
    def test_duplicate_ids_are_not_deduplicated_away(self):
        spec = _spec()
        _add(spec, "topbar", _btn("tb_menu", "메인 메뉴"))
        p = _payload(spec, "app_menu")
        self.assertEqual(p["found"], 2)
        self.assertEqual(p["hint"], "ambiguous: narrow names or scope")

    # ---- B9: re: prefixed candidate ----------------------------------------
    def test_candidate_starting_with_re_is_escaped_in_snippet(self):
        spec = _spec()
        for st in spec["states"].values():
            for d in st["documents"]:
                _rename(d, "연구 시작", "re: 연구 시작 (v2")
        p = _payload(spec, "plan_start")
        snip_names = p["override_snippet"]["locators"]["plan_start"]["names"]
        added = [n for n in snip_names if n not in ("연구 시작", "Start research")]
        self.assertEqual(len(added), 1)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(spec, _write(tmp, p["override_snippet"]))
            self.assertEqual(_ids("plan_start", ctx), ["plan_start"])

    # ---- B10 / M10: required keys, blank values ----------------------------
    def test_bundled_requires_version_and_locators(self):
        with tempfile.TemporaryDirectory() as tmp:
            for label, cfg in (("empty", {}), ("version only", {"version": 1}),
                               ("locators only", {"locators": {}})):
                with self.subTest(label):
                    b = Path(tmp) / "b.json"
                    b.write_text(json.dumps(cfg), encoding="utf-8")
                    with self.assertRaises(locators.LocatorConfigError):
                        locators.load_locators(b, None)
            b = Path(tmp) / "b.json"
            b.write_text(json.dumps({"version": 1}), encoding="utf-8")
            ov = _write(tmp, {"version": 1, "locators": {"x": {}}})
            with self.assertRaises(locators.LocatorConfigError):
                locators.load_locators(b, ov)

    def test_blank_names_and_ct_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for label, ent in (("blank name", {"names": ["   "]}),
                               ("empty name", {"names": [""]}),
                               ("empty ct", {"ct": ""}),
                               ("blank ct", {"ct": "  "})):
                with self.subTest(label):
                    with self.assertRaises(locators.LocatorConfigError):
                        locators.load_locators(
                            BUNDLED, _write(tmp, {"version": 1, "locators": {"send": ent}}))

    # ---- B11: BOM ------------------------------------------------------------
    def test_override_with_utf8_bom_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "locators.local.json"
            p.write_bytes(b"\xef\xbb\xbf" + json.dumps(
                {"version": 1, "locators": {"send": {"names": ["보내기"]}}},
                ensure_ascii=False).encode("utf-8"))
            self.assertEqual(
                locators.load_locators(BUNDLED, p)["locators"]["send"]["names"], ["보내기"])

    # ---- B16 / M13: top_bar snapshots only the first match -------------------
    def test_top_bar_snapshots_only_first_matching_document(self):
        spec = _spec()
        st = spec["states"]["s"]
        s1 = copy.deepcopy(st)
        st["documents"].append({"id": "topbar2", "ct": "Document", "role": "RootWebArea",
                                "value": "app://bundle/src/top_bar/two.html", "children": []})
        spec["states"]["s1"] = s1
        spec["transitions"] = [
            {"from": "s", "on": ["act", "b_send", "invoke"], "to": "s1", "after_snapshots": 1}]
        ctx = _ctx(spec)
        ctx.tree.act("b_send", "invoke")
        calls = []
        orig = ctx.tree.snapshot
        ctx.tree.snapshot = lambda doc_id: (calls.append(doc_id), orig(doc_id))[1]
        self.assertEqual(_ids("app_menu", ctx), ["tb_menu"])
        self.assertEqual(calls, ["topbar"])

    # ---- M9: low-similarity candidates --------------------------------------
    def test_no_similar_candidate_lists_top3_but_does_not_extend_snippet(self):
        spec = _spec()
        for st in spec["states"].values():
            for d in st["documents"]:
                _rename(d, "취소", "닫기")
        p = _payload(spec, "plan_cancel_countdown",
                     lambda ctx: ctx.locs["locators"]["plan_cancel_countdown"].update(expect="unique"))
        self.assertEqual(len(p["candidates"]), 3)
        for c in p["candidates"]:
            self.assertLess(c["similarity"], locators.MIN_SIMILARITY)
        self.assertEqual(p["override_snippet"]["locators"]["plan_cancel_countdown"]["names"],
                         ["취소", "Cancel"])

    # ---- T2: payload is scrubbed ----------------------------------------------
    def test_miss_payload_is_scrubbed(self):
        spec = _spec()
        for st in spec["states"].values():
            for d in st["documents"]:
                _rename(d, "연구 시작", "연구 시작 계정")
        p = _payload(spec, "plan_start")
        blob = json.dumps(p, ensure_ascii=False)
        self.assertNotIn("계정", blob)
        self.assertIn("<redacted:account", blob)
        self.assertEqual(p["found"], 0)

    # ---- T5: composer scope --------------------------------------------------
    def _composer_spec(self):
        spec = _spec()
        # distractor outside the composer box, prompt wrapped in an inner group
        _add(spec, "composer_outer", _btn("tools_distractor", "업로드 및 도구"))
        comp = _get(spec, "composer")
        prompt = comp["children"].pop(0)
        comp["children"].insert(0, {"id": "prompt_wrap", "ct": "Group", "children": [prompt]})
        return spec

    def test_composer_scope_is_lowest_ancestor_with_min_buttons(self):
        ctx = _ctx(self._composer_spec())
        self.assertEqual(_ids("tools", ctx), ["b_tools"])
        self.assertEqual(_ids("send", ctx), ["b_send"])
        roots = locators._scope_roots("composer", ctx)
        self.assertEqual([r.id for r in roots], ["composer"])

    def test_composer_scope_honours_min_buttons(self):
        ctx = _ctx(self._composer_spec())
        ctx.locs["scopes"]["composer"]["min_buttons"] = 6
        roots = locators._scope_roots("composer", ctx)
        self.assertEqual([r.id for r in roots], ["composer_outer"])
        with self.assertRaises(locators.LocatorMiss) as cm:
            locators.resolve("tools", ctx)
        self.assertEqual(cm.exception.payload["found"], 2)
        ctx.locs["scopes"]["composer"]["min_buttons"] = 99
        self.assertEqual(locators._scope_roots("composer", ctx), [])

    # ---- T6: patterns / role / ct filters -------------------------------------
    def test_patterns_filter_excludes_nodes_without_pattern(self):
        spec = _spec()
        _add(spec, "composer", _btn("tools_noinvoke", "업로드 및 도구", patterns=()))
        self.assertEqual(_ids("tools", _ctx(spec)), ["b_tools"])

    def test_role_filter_excludes_wrong_role(self):
        spec = _spec()
        _add(spec, "pop", _btn("pm_dr_wrong", "Deep Research", patterns=(),
                               ct="CheckBox", role="checkbox"))
        self.assertEqual(_ids("dr_item", _ctx(spec)), ["pm_dr"])

    def test_ct_filter_excludes_wrong_control_type(self):
        spec = _spec()
        _add(spec, "composer", _btn("tools_link", "업로드 및 도구", ct="Hyperlink"))
        self.assertEqual(_ids("tools", _ctx(spec)), ["b_tools"])

    # ---- T7: at_least_one with several matches ----------------------------------
    def test_at_least_one_returns_all_matches(self):
        spec = _spec()
        _add(spec, "g0", {"id": "newchat2", "ct": "Hyperlink", "name": "새 채팅"})
        self.assertEqual(sorted(_ids("new_chat", _ctx(spec))), ["newchat", "newchat2"])

    # ---- T8: pinned bundled data -----------------------------------------------
    def test_bundled_surfaces_allowed_exact_and_selective(self):
        import re
        locs = locators.load_locators(BUNDLED, None)
        self.assertEqual(locs["surfaces_allowed"], [
            "re:^https://gemini\\.google\\.com/app(/[0-9a-f]{8,})?$",
            "re:^app://bundle/src/top_bar/",
            "re:^blob:"])
        pats = [re.compile(p[3:]) for p in locs["surfaces_allowed"]]

        def allowed(url):
            return any(p.search(url) for p in pats)
        for good in ("https://gemini.google.com/app", "https://gemini.google.com/app/0123abcd",
                     "app://bundle/src/top_bar/index.html", "blob:https://x/uuid"):
            self.assertTrue(allowed(good), good)
        for bad in ("https://evil.example/app", "https://gemini.google.com.evil/app",
                    "https://gemini.google.com/appx", "https://gemini.google.com/app/zz",
                    "http://gemini.google.com/app", "app://bundle/src/other/", "data:text/html,x"):
            self.assertFalse(allowed(bad), bad)

    def test_bundled_locator_names_exact(self):
        locs = locators.load_locators(BUNDLED, None)["locators"]
        want = {
            "prompt_input": ["Gemini 프롬프트 입력", "Enter a prompt for Gemini"],
            "send": ["메시지 보내기", "Send message"],
            "stop_response": ["대답 생성 중지", "Stop response"],
            "tools": ["업로드 및 도구", "Upload and tools"],
            "more_tools": ["도구 더보기", "More tools"],
            "dr_item": ["Deep Research"],
            "dr_on": ["Deep Research 선택 해제", "Deselect Deep Research"],
            "mode_button": ["re:모드 선택 도구 열기.*", "re:Open mode picker.*"],
            "mode_item": ["re:.+"],
            "thinking_menu": ["사고 수준", "Thinking level"],
            "new_chat": ["새 채팅", "New chat"],
            "sidebar_open": ["사이드바 열기", "Open sidebar"],
            "sidebar_close": ["사이드바 닫기", "Close sidebar"],
            "sidebar_more": ["더보기", "Show more"],
            "plan_start": ["연구 시작", "Start research"],
            "plan_edit": ["연구 계획 수정", "Edit the research plan", "Edit plan"],
            "plan_cancel_countdown": ["취소", "Cancel"],
            "plan_retry_without_dr": ["Deep Research 사용하지 않고 다시 시도하기",
                                      "Retry without Deep Research"],
            "app_menu": ["메인 메뉴", "Main menu"],
            "app_menu_file": ["파일", "File"],
            "app_menu_quit": ["re:종료.*", "re:(Quit|Exit).*"],
            "app_menu_view": ["보기", "View"],
            "app_menu_reload": ["re:새로고침.*", "re:Reload.*"],
            "sign_in": ["로그인", "Sign in"],
        }
        self.assertEqual(set(locs), set(want))
        for key, names in want.items():
            self.assertEqual(locs[key]["names"], names, key)

    # ---- T13: candidate filtering -------------------------------------------------
    def test_sidebar_button_with_high_similarity_is_not_a_candidate(self):
        spec = _spec()
        for st in spec["states"].values():
            for d in st["documents"]:
                _rename(d, "연구 시작", "조사 계획")
        _add(spec, "nav2", _btn("sb_btn", "연구 시작 비밀"))
        blob = json.dumps(_payload(spec, "plan_start"), ensure_ascii=False)
        self.assertNotIn("비밀", blob)

    def test_generic_label_in_sidebar_is_a_candidate(self):
        def mutate(ctx):
            ent = ctx.locs["locators"]["sidebar_close"]
            ent.update(expect="unique", names=["사이드바 닫기 버튼"])
        p = _payload(_spec(), "sidebar_close", mutate)
        self.assertEqual(p["candidates"][0]["name"], "사이드바 닫기")
        self.assertIn("사이드바 닫기", p["override_snippet"]["locators"]["sidebar_close"]["names"])

    def test_candidates_capped_and_sorted(self):
        spec = _spec()
        for st in spec["states"].values():
            for d in st["documents"]:
                _rename(d, "연구 시작", "조사 계획")
        for i in range(7):
            _add(spec, "turn1", _btn("extra%d" % i, "연구 시작 " + "가" * (i + 1)))
        p = _payload(spec, "plan_start")
        sims = [c["similarity"] for c in p["candidates"]]
        self.assertEqual(len(sims), locators.MAX_CANDIDATES)
        self.assertEqual(sims, sorted(sims, reverse=True))
        self.assertEqual(locators.MAX_CANDIDATES, 5)

    # ---- T14: scopes ------------------------------------------------------------------
    def test_main_scope_excludes_nodes_outside_main(self):
        spec = _spec()
        _add(spec, "doc", {"id": "out_prompt", "ct": "Edit", "name": "Gemini 프롬프트 입력"})
        self.assertEqual(_ids("prompt_input", _ctx(spec)), ["prompt"])

    def test_document_scope_includes_nodes_outside_main(self):
        spec = _spec()
        _add(spec, "doc", {"id": "out_new", "ct": "Hyperlink", "name": "새 채팅"})
        self.assertEqual(sorted(_ids("new_chat", _ctx(spec))), ["newchat", "out_new"])

    def test_desktop_scope_reaches_popup_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            ov = _write(tmp, {"version": 1, "locators": {
                "app_menu_file": {"scope": "desktop"}, "new_chat": {"scope": "desktop"}}})
            ctx = _ctx(_spec(), ov)
            self.assertEqual(_ids("app_menu_file", ctx), ["pm_file"])
            with self.assertRaises(locators.LocatorMiss):
                locators.resolve("new_chat", ctx)

    # ---- T15: fullmatch -------------------------------------------------------------------
    def test_match_name_regex_is_full_match(self):
        self.assertFalse(locators.match_name("종료하기", ["re:종료"]))
        self.assertFalse(locators.match_name("x종료", ["re:종료"]))
        self.assertTrue(locators.match_name("종료", ["re:종료"]))

    # ---- T19: config edge cases -----------------------------------------------------------------
    def test_config_validation_edge_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = {
                "version 2": {"version": 2},
                "version str": {"version": "1"},
                "version bool": {"version": True},
                "expect": {"version": 1, "locators": {"send": {"expect": "sometimes"}}},
                "patterns": {"version": 1, "locators": {"send": {"patterns": ["smash"]}}},
                "patterns type": {"version": 1, "locators": {"send": {"patterns": "invoke"}}},
                "locators list": {"version": 1, "locators": []},
                "entry not dict": {"version": 1, "locators": {"send": "x"}},
            }
            for label, cfg in bad.items():
                with self.subTest(label):
                    with self.assertRaises(locators.LocatorConfigError):
                        locators.load_locators(BUNDLED, _write(tmp, cfg))
            p = Path(tmp) / "list.json"
            p.write_text("[]", encoding="utf-8")
            with self.assertRaises(locators.LocatorConfigError):
                locators.load_locators(BUNDLED, p)
            with self.assertRaises(locators.LocatorConfigError):
                locators.load_locators(p, None)

    def test_resolve_unknown_key_and_error_hierarchy(self):
        self.assertTrue(issubclass(locators.LocatorConfigError, ValueError))
        with self.assertRaises(locators.LocatorConfigError):
            locators.resolve("no_such_locator", _ctx(_spec()))

    def test_missing_override_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            locs = locators.load_locators(BUNDLED, Path(tmp) / "absent.json")
        self.assertIn("send", locs["locators"])


if __name__ == "__main__":
    unittest.main()
