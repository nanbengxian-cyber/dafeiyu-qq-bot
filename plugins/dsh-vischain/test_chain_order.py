# -*- coding: utf-8 -*-
"""dsh-vischain 档位顺序一等公民（order-v1）回归测试 —— 容器内 py3.12 运行。

背景：2026-09-13 用当天 157 次识图的真实战绩回查，四档成功率是
  4.1v-thinking-flash 76% / vision-scnet 72% / glm-4v-flash 66% / 基础 glm-4.6v 16%
而旧结构里 CHAIN 只认纯 provider id、MODEL_FALLBACKS 一律追加在最后，
于是**成功率最低的那档被钉死在第 2 位**，卡在 76% 那档前面 —— 每次第一档
超时都要先掏 10 秒撞这个 16% 的档。用真实成功/失败序列重排模拟：P90 18.0s→12.2s。

本测试盯住四件事：
  ① DSH_VIS_CHAIN 支持 "pid:model" 写法，且顺序就是配置写的顺序；
  ② 模型档和显式 provider 档能混排，不再被强制赶到最后；
  ③ 已经建好的链条，第二次 _ensure 不动手（不能每 30 秒白重建）；
  ④ WebUI 换掉 provider 实例后，_link_alive 能发现并触发重建（不能静默失效）。

运行：sudo docker cp plugins/dsh-vischain astrbot:/tmp/vs && \
      sudo docker exec astrbot python3 /tmp/vs/test_chain_order.py
"""
import os
import sys
import types

os.environ["DSH_VIS_ALIAS"] = "vision-opus5"
# 期望顺序：zhipu 的 thinking 档 → vision-scnet → glm-4v → 基础 glm-4.6v（最差垫底）
os.environ["DSH_VIS_CHAIN"] = "zhipu-vision:glm-4.1v-thinking-flash,vision-scnet"
os.environ["DSH_VIS_MODEL_FALLBACKS"] = "zhipu-vision:glm-4v-flash,zhipu-vision:glm-4.6v-flash"
os.environ["DSH_VIS_DEAD_TTL"] = "0"
os.environ["DSH_VIS_SLOW_TTL"] = "0"
os.environ["DSH_VIS_CONSEC_BAN"] = "3"

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
        on_astrbot_loaded=lambda: (lambda f: f),
        command=lambda *a, **k: (lambda f: f))
    sys.modules["astrbot.core"].logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None)

    class _Provider:
        def __init__(self, provider_config, settings):
            self.provider_config = provider_config

    sys.modules["astrbot.core.provider.provider"].Provider = _Provider

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as vis  # noqa: E402

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond)))
    print("%-52s %s %s" % (name, "PASS" if cond else "FAIL", detail if not cond else ""))


class FakeProv:
    def __init__(self, pid, model="m"):
        self.provider_config = {"id": pid, "model": model, "modalities": ["text", "image"]}

    def get_model(self):
        return self.provider_config["model"]


class FakePM:
    """只实现 _ensure 用得到的两个接口：inst_map 和 get_config。"""

    def __init__(self, insts):
        self.inst_map = {i.provider_config["id"]: i for i in insts}


class FakeCtx:
    def get_config(self):
        return {}


def main():
    # ---------- ① 解析：pid 与 pid:model 混写 ----------
    check("CHAIN_SPEC 解析出 model 写法",
          vis.CHAIN_SPEC[0] == ("zhipu-vision", "glm-4.1v-thinking-flash"),
          repr(vis.CHAIN_SPEC[:1]))
    check("CHAIN_SPEC 保留纯 provider 写法",
          vis.CHAIN_SPEC[1] == ("vision-scnet", None), repr(vis.CHAIN_SPEC[1:2]))
    check("SPEC = CHAIN_SPEC + MODEL_FALLBACKS（4 档）", len(vis.SPEC) == 4, repr(vis.SPEC))
    check("兼容读法 CHAIN 只留 pid",
          vis.CHAIN == ["zhipu-vision", "vision-scnet"], repr(vis.CHAIN))

    # ---------- ② 建链条：顺序 = 配置顺序 ----------
    scnet = FakeProv("vision-scnet", "DeepSeek-V4.1-Flash-Event")
    zhipu = FakeProv("zhipu-vision", "glm-4.6v-flash")
    alias = FakeProv("vision-opus5", "claude-opus-5")
    pm = FakePM([scnet, zhipu, alias])

    m = vis.Main(FakeCtx())
    check("首次 _ensure 动过手", m._ensure(pm) is True)
    check("ALIAS 被换成 ChainProvider", isinstance(pm.inst_map["vision-opus5"], vis.ChainProvider))

    got = [vis._spec_of(lk) for lk in m.chain.links]
    want = [
        ("zhipu-vision", "glm-4.1v-thinking-flash"),   # 76% 那档必须排第一
        ("vision-scnet", None),                        # 第二，provider 多样性
        ("zhipu-vision", "glm-4v-flash"),
        ("zhipu-vision", "glm-4.6v-flash"),            # 16% 那档垫底
    ]
    check("档位顺序完全按配置", got == want, "\n  got =%r\n  want=%r" % (got, want))
    check("最差档不再卡在第 2 位", got[1][1] is None and got[-1][1] == "glm-4.6v-flash")
    check("第一档就是模型档", isinstance(m.chain.links[0], vis.ModelOverrideProvider))

    # ---------- ③ 幂等：已经对了就不许重建 ----------
    before = m.chain
    check("第二次 _ensure 不动手", m._ensure(pm) is False)
    check("链条对象没被换掉", m.chain is before)

    # ---------- ④ 活实例核对：provider 被 reload 换掉后必须重建 ----------
    # WebUI 改 provider 走 provider_manager.reload()，inst_map[id] 变成新裸实例。
    # 旧判据只比「显式 provider」，现在第一档是 model 档、显式档只剩 vision-scnet，
    # 如果新判据漏了 model 档，这里就会静默失效。
    pm.inst_map["zhipu-vision"] = FakeProv("zhipu-vision", "glm-4.6v-flash")
    check("底座被换掉 → 触发重建", m._ensure(pm) is True)
    check("重建后仍然指向新实例",
          m.chain.links[0].base is pm.inst_map["zhipu-vision"])
    check("重建后顺序不变", [vis._spec_of(lk) for lk in m.chain.links] == want)

    # 显式 provider 那一档被换掉也要发现（旧判据唯一覆盖的情形，不能退化）
    pm.inst_map["vision-scnet"] = FakeProv("vision-scnet", "DeepSeek-V4.1-Flash-Event")
    check("显式档被换掉 → 也重建", m._ensure(pm) is True)

    # ---------- ⑤ 缺档容错：某档 provider 不存在时跳过而不是崩 ----------
    pm2 = FakePM([zhipu, alias])          # 故意不放 vision-scnet
    m2 = vis.Main(FakeCtx())
    check("缺档仍能建起来", m2._ensure(pm2, quiet=True) is True)
    check("缺失的档被跳过，只剩 3 档", len(m2.chain.links) == 3, repr(len(m2.chain.links)))
    check("日志文案里点名缺失档", "vision-scnet" in (m2.note or ""), repr(m2.note))

    print()
    bad = [n for n, ok in CHECKS if not ok]
    print("CHAIN_ORDER_TEST_%s（%d/%d）" % ("FAIL" if bad else "OK", len(CHECKS) - len(bad), len(CHECKS)))
    for n in bad:
        print("  FAILED:", n)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
