# -*- coding: utf-8 -*-
"""dsh-vischain 档位去重回归测试 —— 容器内 py3.12 运行。

背景：`DSH_VIS_MODEL_FALLBACKS` 有个非空默认值
（zhipu-vision:glm-4.1v-thinking-flash,zhipu-vision:glm-4v-flash）。
order-v1 让模型档也能写进 DSH_VIS_CHAIN 之后，如果照抄默认兜底不动，
同一个模型就会在 SPEC 里出现两次 —— 撞档者会拿同一个模型连撞两遍，
白白多花一个 ATTEMPT_TIMEOUT(10s)。

本测试盯住：SPEC 保序去重，且第一次出现的位置被保留（顺序语义不变）。

运行：sudo docker cp plugins/dsh-vischain astrbot:/tmp/vs && \
      sudo docker exec astrbot python3 /tmp/vs/test_chain_dedup.py
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

# 故意制造重复：CHAIN 里的模型档与 MODEL_FALLBACKS 默认值撞车
os.environ["DSH_VIS_CHAIN"] = "zhipu-vision:glm-4.1v-thinking-flash,vision-scnet"
os.environ["DSH_VIS_MODEL_FALLBACKS"] = \
    "zhipu-vision:glm-4.1v-thinking-flash,zhipu-vision:glm-4v-flash"

if "astrbot" not in sys.modules:
    for _n in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.core",
               "astrbot.core.provider", "astrbot.core.provider.provider"):
        sys.modules.setdefault(_n, types.ModuleType(_n))

    class _P:
        def __init__(self, provider_config, settings):
            self.provider_config = provider_config

    sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object)
    sys.modules["astrbot.api.event"].AstrMessageEvent = object
    sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
        on_astrbot_loaded=lambda: (lambda f: f),
        command=lambda *a, **k: (lambda f: f))
    sys.modules["astrbot.core"].logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None)
    sys.modules["astrbot.core.provider.provider"].Provider = _P

spec = importlib.util.spec_from_file_location(
    "vs_dedup", Path(__file__).with_name("main.py"))
vis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vis)

FAILS = []


def check(name, cond, detail=""):
    print("%-52s %s %s" % (name, "PASS" if cond else "FAIL", detail if not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    want = [
        ("zhipu-vision", "glm-4.1v-thinking-flash"),   # 第一次出现的位置保留
        ("vision-scnet", None),
        ("zhipu-vision", "glm-4v-flash"),
    ]
    check("SPEC 去重后只剩 3 档", len(vis.SPEC) == 3, repr(vis.SPEC))
    check("保序：重复档留在第一次出现处", vis.SPEC == want,
          "\n  got =%r\n  want=%r" % (vis.SPEC, want))
    check("同一模型不再出现两次",
          vis.SPEC.count(("zhipu-vision", "glm-4.1v-thinking-flash")) == 1,
          repr(vis.SPEC))
    check("CHAIN_SPEC 原始解析不受去重影响", len(vis.CHAIN_SPEC) == 2,
          repr(vis.CHAIN_SPEC))

    print()
    print("CHAIN_DEDUP_TEST_%s" % ("FAIL" if FAILS else "OK"))
    for f in FAILS:
        print("  FAILED:", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
