# -*- coding: utf-8 -*-
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

# 服务器/CI 上可能没有 tkinter；核心逻辑（配置读写、字段表）不应依赖图形库。
try:
    import tkinter  # noqa: F401
except ModuleNotFoundError:
    tk = types.ModuleType("tkinter")
    tk.Tk = object
    tk.Text = object
    tk.Canvas = object
    tk.StringVar = object
    tk.BooleanVar = object
    ttk = types.SimpleNamespace(
        Frame=object, LabelFrame=object, Label=object, Entry=object,
        Checkbutton=object, Button=object, Combobox=object, Notebook=object,
        Scrollbar=object,
    )
    messagebox = types.SimpleNamespace(
        showwarning=lambda *a, **k: None, showerror=lambda *a, **k: None,
        showinfo=lambda *a, **k: None, askyesno=lambda *a, **k: False,
    )
    filedialog = types.SimpleNamespace(askopenfilename=lambda *a, **k: "")
    tk.ttk = ttk
    tk.messagebox = messagebox
    tk.filedialog = filedialog
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.messagebox"] = messagebox
    sys.modules["tkinter.filedialog"] = filedialog

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("controller_app", ROOT / "app.py")
app = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app
assert SPEC.loader is not None
SPEC.loader.exec_module(app)


class ProfileTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "controller-profile.json"
            values = {
                "host": "bot.example.invalid",
                "port": 22,
                "repo_url": "https://git.example.invalid/o/r.git",
                "password": "should-not-be-saved",
                "key_passphrase": "should-not-be-saved",
                "unknown_key": "dropped",
            }
            app.save_profile(values, path)
            loaded = app.load_profile(path)
            self.assertEqual(loaded["host"], "bot.example.invalid")
            self.assertEqual(loaded["port"], 22)
            self.assertNotIn("unknown_key", loaded)

            raw = path.read_text(encoding="utf-8")
            for forbidden in ("should-not-be-saved", "password", "passphrase", "unknown_key"):
                self.assertNotIn(forbidden, raw)

    def test_secret_fields_are_not_persistable(self):
        for secret in ("password", "key_file", "key_passphrase"):
            self.assertNotIn(secret, app.PROFILE_KEYS)
        self.assertIn("host", app.PROFILE_KEYS)
        self.assertIn("repo_url", app.PROFILE_KEYS)

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(app.load_profile(Path(tmp) / "nope.json"), {})

    def test_corrupt_file_raises_deploy_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "controller-profile.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(app.DeployError):
                app.load_profile(path)

    def test_non_dict_profile_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "controller-profile.json"
            path.write_text(json.dumps([1, 2]), encoding="utf-8")
            with self.assertRaises(app.DeployError):
                app.load_profile(path)


class SchemaTests(unittest.TestCase):
    def test_auth_visibility_rules(self):
        fields = {field.key: field for field in app.DEPLOY_FIELDS}
        self.assertEqual(fields["password"].visible_if, ("auth_type", "密码"))
        self.assertEqual(fields["key_file"].visible_if, ("auth_type", "SSH 私钥"))
        self.assertEqual(fields["key_passphrase"].visible_if, ("auth_type", "SSH 私钥"))
        self.assertIsNone(fields["host"].visible_if)

    def test_public_defaults_exclude_secrets(self):
        defaults = app.config_schema.public_defaults()
        for secret in ("password", "key_file", "key_passphrase"):
            self.assertNotIn(secret, defaults)
        self.assertIn("host", defaults)
        self.assertIn("napcat_port", defaults)

    def test_groups_cover_all_fields(self):
        groups = {field.group for field in app.DEPLOY_FIELDS}
        self.assertEqual(groups, set(app.GROUP_ORDER))


class TutorialTests(unittest.TestCase):
    def test_tutorial_covers_required_steps(self):
        for needle in ("测试连接", "开始部署", "配置与状态", "扫码登录", "私钥"):
            self.assertIn(needle, app.TUTORIAL)

    def test_tutorial_has_no_real_hosts(self):
        self.assertNotIn(".com", app.TUTORIAL)
        self.assertNotIn("http://1", app.TUTORIAL)


if __name__ == "__main__":
    unittest.main()
