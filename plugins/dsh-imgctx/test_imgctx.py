"""dsh-imgctx 引用图片兜底回归测试。"""

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

if "astrbot" not in sys.modules:
    try:
        import astrbot  # noqa: F401
    except ImportError:
        for name in (
            "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.core",
            "astrbot.core.agent", "astrbot.core.agent.message",
            "astrbot.core.platform", "astrbot.core.platform.message_type",
        ):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
        sys.modules["astrbot.api.event"].AstrMessageEvent = object
        sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
            on_llm_request=lambda: (lambda f: f),
            command=lambda *a, **k: (lambda f: f),
        )
        sys.modules["astrbot.core"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            error=lambda *a, **k: None, debug=lambda *a, **k: None,
        )
        sys.modules["astrbot.core.agent.message"].TextPart = type(
            "TextPart", (), {"__init__": lambda self, text="": setattr(self, "text", text)}
        )
        sys.modules["astrbot.core.platform.message_type"].MessageType = types.SimpleNamespace(
            GROUP_MESSAGE="group"
        )

spec = importlib.util.spec_from_file_location("imgctx", Path("/tmp/imgctx_main_new.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Part:
    def __init__(self, text):
        self.text = text


class Req:
    def __init__(self, parts, image_urls=None):
        self.extra_user_content_parts = [Part(x) for x in parts]
        self.image_urls = image_urls or []


with tempfile.TemporaryDirectory() as td:
    current = os.path.join(td, "current.jpg")
    quoted = os.path.join(td, "quoted.gif")
    Path(current).write_bytes(b"current")
    Path(quoted).write_bytes(b"quoted")

    req = Req([
        f"[Image Attachment: path {current}]",
        f"[Image Attachment in quoted message: path {quoted}]",
    ])
    assert m._framework_image_paths(req) == [current, quoted]
    assert m._framework_image_paths(req, quoted=False) == [current]
    assert m._quoted_image_paths(req) == [quoted]
    assert m._quoted_images_need_context(req)

    described = Req([
        f"[Image Attachment in quoted message: path {quoted}]",
        "[Image Caption in quoted message]: 一只猫",
    ])
    assert not m._quoted_images_need_context(described)

    missing = Req(["[Image Attachment in quoted message: path /not/found.jpg]"])
    assert m._quoted_image_paths(missing) == []
    assert not m._quoted_images_need_context(missing)

print("IMGCTX_QUOTED_TEST_OK")
