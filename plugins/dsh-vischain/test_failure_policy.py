# dsh-vischain 失败分类与同轮重试回归测试
import asyncio
import os
import sys

os.environ["DSH_VIS_CONSEC_BAN"] = "3"
os.environ["DSH_VIS_RETRY"] = "2"
os.environ["DSH_VIS_DEAD_TTL"] = "0"
os.environ["DSH_VIS_SLOW_TTL"] = "180"
os.environ["DSH_VIS_CHAIN"] = "first,second"
os.environ["DSH_VIS_MODEL_FALLBACKS"] = ""
os.environ["DSH_VIS_TIMEOUT"] = "0.05"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as vis  # noqa: E402


class MockProv:
    def __init__(self, pid, error=None, result="ok"):
        self.provider_config = {"id": pid, "model": "m", "modalities": ["text", "image"]}
        self.error = error
        self.result = result
        self.calls = 0

    def get_model(self):
        return "m"

    async def text_chat(self, *args, **kwargs):
        self.calls += 1
        error = self.error
        if callable(error):
            error = error()
        if error is not None:
            raise error
        return self.result


async def run():
    vis._slow.clear(); vis._dead.clear(); vis._banned.clear(); vis._consec.clear()
    timeout = MockProv("first", asyncio.TimeoutError())
    healthy = MockProv("second")
    got = await vis.ChainProvider([timeout, healthy], {}).text_chat("x", image_urls=["x.jpg"])
    assert got == "ok"
    assert timeout.calls == 1, "timeout/429 must switch provider without same-tier retries"
    assert "first" not in vis._banned and vis._consec.get("first", 0) == 0

    vis._slow.clear(); vis._dead.clear(); vis._banned.clear(); vis._consec.clear()
    limited = MockProv("first", RuntimeError("Error code: 429 - rate limit"))
    got = await vis.ChainProvider([limited, healthy], {}).text_chat("x", image_urls=["x.jpg"])
    assert got == "ok" and limited.calls == 1
    assert "first" not in vis._banned

    vis._slow.clear(); vis._dead.clear(); vis._banned.clear(); vis._consec.clear()
    image_bad = MockProv("first", RuntimeError("code '1301': contentfilter"))
    chain = vis.ChainProvider([image_bad, healthy], {})
    for _ in range(3):
        assert await chain.text_chat("x", image_urls=["x.jpg"]) == "ok"
    assert "first" not in vis._banned and vis._consec.get("first", 0) == 0

    vis._slow.clear(); vis._dead.clear(); vis._banned.clear(); vis._consec.clear()
    channel_bad = MockProv("first", RuntimeError("model_not_found: no available channel"))
    chain = vis.ChainProvider([channel_bad, healthy], {})
    for _ in range(3):
        assert await chain.text_chat("x", image_urls=["x.jpg"]) == "ok"
    assert channel_bad.calls == 3 and "first" in vis._banned

    print("ALL PASS")


raise SystemExit(asyncio.run(run()))
