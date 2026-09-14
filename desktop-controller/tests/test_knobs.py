# -*- coding: utf-8 -*-
import json
import unittest
from pathlib import Path

import knobs as knobs_mod

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "deploy" / "console-config.json"
ENV_EXAMPLE = ROOT / "deploy" / "robot.env.example"


def knob(**kwargs):
    base = {"key": "DSH_X", "label": "测试项", "type": "str"}
    base.update(kwargs)
    return knobs_mod.parse_knobs({"knobs": [base]})[0]


class ParseTests(unittest.TestCase):
    def test_real_manifest_parses(self):
        raw = json.loads(SCHEMA.read_text(encoding="utf-8"))
        parsed = knobs_mod.parse_knobs(raw)
        self.assertGreaterEqual(len(parsed), 15)
        keys = {item.key for item in parsed}
        self.assertIn("DSH_DECIDE", keys)
        self.assertIn("DSH_WEB_MOD", keys)
        self.assertEqual(len(keys), len(parsed), "配置项 key 不应重复")
        self.assertTrue(knobs_mod.group_order(parsed))

    def test_manifest_matches_env_example(self):
        """清单和模板必须一致，否则界面上的项在服务器上找不到变量。"""
        raw = json.loads(SCHEMA.read_text(encoding="utf-8"))
        parsed = knobs_mod.parse_knobs(raw)
        values = knobs_mod.env_values(ENV_EXAMPLE.read_text(encoding="utf-8"))
        missing = [item.key for item in parsed if item.key not in values]
        self.assertEqual(missing, [], "robot.env.example 缺少这些变量：%s" % missing)

    def test_unknown_type_is_skipped_not_fatal(self):
        raw = {"knobs": [
            {"key": "DSH_A", "type": "color-picker"},
            {"key": "DSH_B", "type": "bool"},
        ]}
        parsed = knobs_mod.parse_knobs(raw)
        self.assertEqual([item.key for item in parsed], ["DSH_B"])

    def test_invalid_keys_and_duplicates_skipped(self):
        raw = {"knobs": [
            {"key": "lower_case", "type": "bool"},
            {"key": "", "type": "bool"},
            {"key": "DSH_OK", "type": "bool"},
            {"key": "DSH_OK", "type": "bool"},
            "not-a-dict",
        ]}
        parsed = knobs_mod.parse_knobs(raw)
        self.assertEqual([item.key for item in parsed], ["DSH_OK"])

    def test_enum_without_options_is_skipped(self):
        self.assertEqual(knobs_mod.parse_knobs({"knobs": [
            {"key": "DSH_A", "type": "enum"}
        ]}), [])

    def test_non_list_manifest_returns_empty(self):
        self.assertEqual(knobs_mod.parse_knobs({"knobs": "oops"}), [])
        self.assertEqual(knobs_mod.parse_knobs(None), [])


class CoerceTests(unittest.TestCase):
    def test_bool_accepts_common_words(self):
        item = knob(type="bool")
        for value, expect in ((True, "1"), (False, "0"), ("1", "1"), ("0", "0"),
                              ("true", "1"), ("false", "0"), ("是", "1"), ("否", "0")):
            self.assertEqual(knobs_mod.coerce_value(item, value), expect)

    def test_bool_rejects_garbage(self):
        with self.assertRaises(knobs_mod.KnobError):
            knobs_mod.coerce_value(knob(type="bool"), "maybe")

    def test_int_range_and_format(self):
        item = knob(type="int", min=0, max=60)
        self.assertEqual(knobs_mod.coerce_value(item, "12"), "12")
        self.assertEqual(knobs_mod.coerce_value(item, 12.0), "12")
        for bad in ("abc", "", "1.5", "-1", "61"):
            with self.subTest(bad=bad):
                with self.assertRaises(knobs_mod.KnobError):
                    knobs_mod.coerce_value(item, bad)

    def test_float_range_and_format(self):
        item = knob(type="float", min=0, max=1)
        self.assertEqual(knobs_mod.coerce_value(item, "0.7"), "0.7")
        self.assertEqual(knobs_mod.coerce_value(item, 1), "1")
        with self.assertRaises(knobs_mod.KnobError):
            knobs_mod.coerce_value(item, "1.2")

    def test_enum_must_be_in_options(self):
        item = knob(type="enum", options=["a", "b"])
        self.assertEqual(knobs_mod.coerce_value(item, "a"), "a")
        with self.assertRaises(knobs_mod.KnobError):
            knobs_mod.coerce_value(item, "c")

    def test_str_rejects_newline_and_overlong(self):
        item = knob(type="str")
        with self.assertRaises(knobs_mod.KnobError):
            knobs_mod.coerce_value(item, "a\nDSH_HACK=1")
        with self.assertRaises(knobs_mod.KnobError):
            knobs_mod.coerce_value(item, "x" * 201)
        self.assertEqual(knobs_mod.coerce_value(item, "  hello  "), "hello")

    def test_display_and_enabled(self):
        item = knob(type="bool", default="1")
        self.assertTrue(knobs_mod.is_enabled(item, None))
        self.assertFalse(knobs_mod.is_enabled(item, "0"))
        self.assertTrue(knobs_mod.is_enabled(item, "yes"))
        # 变量存在但写成空值：插件读到的就是空串，判定为关，界面必须如实显示
        self.assertFalse(knobs_mod.is_enabled(item, ""))
        self.assertEqual(knobs_mod.display_value(item, None), "1")
        self.assertEqual(knobs_mod.display_value(item, ""), "")
        self.assertEqual(knobs_mod.display_value(item, "0"), "0")

    def test_secret_state_never_returns_value(self):
        item = knob(type="str", secret=True)
        self.assertEqual(knobs_mod.secret_state(item, "sk-real-secret"), "已设置")
        self.assertEqual(knobs_mod.secret_state(item, ""), "未设置")
        self.assertEqual(knobs_mod.secret_state(item, None), "未设置")


class EnvTextTests(unittest.TestCase):
    TEXT = "# 注释\nDSH_A=1\n\n# 分节\nDSH_B=2\n"

    def test_replace_preserves_comments_and_order(self):
        new_text, changed = knobs_mod.apply_env_text(self.TEXT, {"DSH_B": "3"})
        self.assertEqual(changed, {"DSH_B": ("2", "3")})
        self.assertIn("# 注释", new_text)
        self.assertIn("# 分节", new_text)
        self.assertIn("DSH_A=1", new_text)
        self.assertIn("DSH_B=3", new_text)
        self.assertEqual(new_text.splitlines()[1], "DSH_A=1")

    def test_unchanged_returns_empty_changed(self):
        new_text, changed = knobs_mod.apply_env_text(self.TEXT, {"DSH_A": "1"})
        self.assertEqual(changed, {})
        self.assertEqual(new_text, self.TEXT)

    def test_missing_key_appended_under_managed_section(self):
        new_text, changed = knobs_mod.apply_env_text(self.TEXT, {"DSH_NEW": "9"})
        self.assertEqual(changed, {"DSH_NEW": (None, "9")})
        self.assertIn(knobs_mod.MANAGED_SECTION, new_text)
        self.assertIn("DSH_NEW=9", new_text)
        # 追加不覆盖原有内容
        self.assertIn("DSH_B=2", new_text)

    def test_second_add_lands_in_existing_section(self):
        once, _ = knobs_mod.apply_env_text(self.TEXT, {"DSH_NEW": "9"})
        twice, changed = knobs_mod.apply_env_text(once, {"DSH_OTHER": "8"})
        self.assertEqual(twice.count(knobs_mod.MANAGED_SECTION), 1)
        self.assertIn("DSH_OTHER=8", twice)
        self.assertIn(("DSH_OTHER", (None, "8")), changed.items())

    def test_value_with_newline_is_rejected(self):
        with self.assertRaises(knobs_mod.KnobError):
            knobs_mod.apply_env_text(self.TEXT, {"DSH_A": "1\nDSH_B=2"})

    def test_invalid_key_is_rejected(self):
        with self.assertRaises(knobs_mod.KnobError):
            knobs_mod.apply_env_text(self.TEXT, {"bad-key": "1"})

    def test_env_values_ignores_comments_and_blank(self):
        self.assertEqual(knobs_mod.env_values(self.TEXT), {"DSH_A": "1", "DSH_B": "2"})


if __name__ == "__main__":
    unittest.main()
