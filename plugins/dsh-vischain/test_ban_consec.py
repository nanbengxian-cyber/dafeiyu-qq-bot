# dsh-vischain 连败拉黑逻辑单测（容器内 py3.12 运行）
# 运行前设好 env：DSH_VIS_CONSEC_BAN=3, DSH_VIS_RETRY=0, DSH_VIS_DEAD_TTL=0
import asyncio
import os
import sys

os.environ["DSH_VIS_CONSEC_BAN"] = "3"
os.environ["DSH_VIS_RETRY"] = "0"
os.environ["DSH_VIS_DEAD_TTL"] = "0"
os.environ["DSH_VIS_CHAIN"] = "vision-scnet,zhipu-vision"
os.environ["DSH_VIS_ALIAS"] = "vision-opus5"
os.environ["DSH_VIS_MODEL_FALLBACKS"] = ""
os.environ["DSH_VIS_TIMEOUT"] = "5"

sys.path.insert(0, "/AstrBot/data/plugins/dsh-vischain")
import main as vis  # noqa: E402


class MockProv:
    def __init__(self, pid, fail=False):
        self.provider_config = {"id": pid, "model": "m", "modalities": ["text", "image"]}
        self._fail = fail
        self.calls = 0

    def get_model(self):
        return "m"

    async def text_chat(self, *args, **kwargs):
        self.calls += 1
        if self._fail:
            raise RuntimeError("insufficient_quota: 余额不足 code 402")
        return "ok"


async def main():
    assert vis.CONSEC_BAN == 3, vis.CONSEC_BAN
    scnet = MockProv("vision-scnet", fail=True)
    zhipu = MockProv("zhipu-vision", fail=False)
    chain = vis.ChainProvider([scnet, zhipu], {})

    results = []
    for _ in range(6):
        r = await chain.text_chat("x", image_urls=["/tmp/vistest.png"])
        results.append(r)

    print("results:", results)
    print("scnet.calls:", scnet.calls)
    print("zhipu.calls:", zhipu.calls)
    print("_banned:", vis._banned)
    print("_consec:", vis._consec)

    ok = (
        scnet.calls == 3
        and zhipu.calls == 6
        and "vision-scnet" in vis._banned
        and all(r == "ok" for r in results)
    )
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


raise SystemExit(asyncio.run(main()))