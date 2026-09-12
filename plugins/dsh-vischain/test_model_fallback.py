# -*- coding: utf-8 -*-
"""dsh-vischain 同源模型兜底回归测试。"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

from PIL import Image

os.environ.setdefault("DSH_VIS_MODEL_FALLBACKS", "zhipu-vision:glm-a,zhipu-vision:glm-b")

if "astrbot" not in sys.modules:
    for n in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.core",
              "astrbot.core.provider", "astrbot.core.provider.provider"):
        sys.modules.setdefault(n, types.ModuleType(n))
    class Provider:
        def __init__(self, provider_config, settings):
            self.provider_config = provider_config
    sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object)
    sys.modules["astrbot.api.event"].AstrMessageEvent = object
    sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
        on_astrbot_loaded=lambda: (lambda f: f), command=lambda *a, **k: (lambda f: f))
    sys.modules["astrbot.core"].logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
    sys.modules["astrbot.core.provider.provider"].Provider = Provider

spec = importlib.util.spec_from_file_location("vischain", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class Fake:
    provider_config = {"id": "zhipu-vision", "model": "glm-main", "modalities": ["text", "image"]}
    def get_model(self): return "glm-main"
    async def text_chat(self, **kw): return kw

async def main():
    f = Fake()
    a = m.ModelOverrideProvider(f, "glm-a", "zhipu-vision:glm-a")
    got = await a.text_chat(prompt="x", request_max_retries=1)
    assert got["model"] == "glm-a"
    assert got["request_max_retries"] == 1
    assert f.get_model() == "glm-main"
    assert a.provider_config["id"] == "zhipu-vision:glm-a"
    assert m.MODEL_FALLBACKS == [("zhipu-vision", "glm-a"), ("zhipu-vision", "glm-b")]

    # GIF 即使伪装成 .jpg，也必须标准化成单帧 RGB JPEG 后重试。
    with tempfile.TemporaryDirectory() as td:
        disguised = Path(td) / "qq-image.jpg"
        Image.new("P", (12, 12), 1).save(disguised, format="GIF")
        out = m._normalize_image(str(disguised))
        assert out and out.endswith(".jpg")
        with Image.open(out) as im:
            assert im.format == "JPEG" and im.mode == "RGB"
        os.remove(out)

    class ParseFailOnce(Fake):
        def __init__(self): self.calls = 0
        async def text_chat(self, **kw):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Error code: 400 code 1210 图片输入格式/解析错误")
            assert kw["image_urls"][0].endswith(".jpg")
            return "fixed"

    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "animated.gif"
        Image.new("RGB", (10, 10), "blue").save(source, format="GIF")
        chain = m.ChainProvider([ParseFailOnce()], {})
        got = await chain.text_chat(image_urls=[str(source)])
        assert got == "fixed"

asyncio.run(main())
print("VISCHAIN_MODEL_FALLBACK_TEST_OK")
