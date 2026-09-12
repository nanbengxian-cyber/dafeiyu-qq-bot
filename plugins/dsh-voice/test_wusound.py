"""悟声后端纯回归测试：用本地 aiohttp 假会话，不消耗真实点数。"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path

os.environ.update({
    "DSH_VOICE_BACKEND": "wusound",
    "DSH_VOICE_API_BASE": "https://v1.wusound.cn/api",
    "DSH_VOICE_TTS_URL": "https://v1.wusound.cn/api/tts/simple-generate",
    "DSH_VOICE_API_KEY": "test-key",
    "DSH_VOICE_REF": "541c8342-f7a4-4852-9cba-abc0f69ca628",
})
spec = importlib.util.spec_from_file_location("voice_wusound", Path("/tmp/voice_main_new.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

assert m.BACKEND == "wusound"
assert m._valid_ref("541c8342-f7a4-4852-9cba-abc0f69ca628")
assert not m._valid_ref("626bb6d3f3364c9cbc3aa6a67300a664")

class Resp:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body if isinstance(body, bytes) else body.encode()
        self.headers = headers or {}
    async def text(self): return self._body.decode()
    async def read(self): return self._body
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

class Session:
    def __init__(self): self.body = None; self.headers = None; self.url = None
    def post(self, url, json=None, headers=None, timeout=None):
        self.body, self.headers = json, headers
        return Resp(200, __import__("json").dumps({"status": 200, "data": {"audio": "https://storage.example/a.mp3"}}))
    def get(self, url, timeout=None):
        self.url = url
        return Resp(200, b"ID3" + b"x" * 300)

async def main():
    s = Session()
    audio, err = await m._wusound_tts_once(s, "测试", "541c8342-f7a4-4852-9cba-abc0f69ca628")
    assert not err and audio.startswith(b"ID3")
    assert s.body == {"text": "测试", "voiceId": "541c8342-f7a4-4852-9cba-abc0f69ca628", "promptId": "default"}
    assert s.headers["Authorization"] == "Bearer test-key"
    assert s.url == "https://storage.example/a.mp3"
    print("WUSOUND_BACKEND_TEST_OK")

asyncio.run(main())
