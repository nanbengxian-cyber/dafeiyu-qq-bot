# -*- coding: utf-8 -*-
"""冷却提示改成"人话"的回归测试（dsh-voice / dsh-imagegen）。

测的不是"字符串对不对"，而是**真跑一遍命令处理器**，看冷却时到底往群里吐什么、
以及会不会连着重样——只换成另一句固定的话，过两天它自己就成了新的口头禅。

跑法（容器内 py3.12）：
    docker exec astrbot python3 /tmp/cool/test_cool.py
"""
import os
import sys
import time

# ---------------------------------------------------------------- 先堵日志
# 【重要】import astrbot.core 会在 import 期把 AstrBot 的文件 sink 挂到生产日志上
# （docs/71 结论 7）。之后 logger.remove() 已经太晚——污染早就发生了
# （实测：本测试曾在生产日志留下两条 "Added llm tool: send_voice"）。
# 真正的堵法是在 import 之前把 Logger.add 换成空操作。
from loguru import logger as _lg  # noqa: E402

_LGR = type(_lg)
_ADD = _LGR.add
_LGR.add = lambda self, *a, **k: 0  # type: ignore[assignment]

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DSH_VOICE", "1")
os.environ.setdefault("DSH_IMG", "1")

try:
    import dsh_voice_main as V      # noqa: E402
    import dsh_imagegen_main as I   # noqa: E402
finally:
    _LGR.add = _ADD  # type: ignore[assignment]
    try:
        _lg.remove()
    except Exception:
        pass

import asyncio  # noqa: E402

SID = "grp:100000001"


class FakeEvent:
    def __init__(self, text, sid=SID):
        self.message_str = text
        self.unified_msg_origin = sid
        self._extra = {}

    def plain_result(self, text):
        return type("R", (), {"text": text})()

    def set_extra(self, k, v):
        self._extra[k] = v

    def get_extra(self, k, d=None):
        return self._extra.get(k, d)


async def collect(gen):
    out = []
    async for r in gen:
        out.append(getattr(r, "text", None) or str(r))
    return out


async def check(mod, label, pool, cmd, handler_name, n=12):
    handler = getattr(mod.Main.__new__(mod.Main), handler_name)
    texts = []
    for i in range(n):
        mod._last_call[SID] = time.time()          # 冷却拉满 → 走提示分支
        gate = getattr(mod, "_notice_last", None)  # 绕过 90s 去重，逐条验变体
        if gate is not None:
            gate.clear()
        got = await collect(handler(FakeEvent(cmd + " 随便说点啥")))
        assert len(got) == 1, "%s 第%d次冷却该只回一条，实际 %r" % (label, i, got)
        t = got[0]
        assert t in pool, "%s 冷却提示不在人话池里：%r" % (label, t)
        assert "冷却" not in t and "秒" not in t, "%s 还有机器词：%r" % (label, t)
        texts.append(t)
    dup = [i for i in range(1, len(texts)) if texts[i] == texts[i - 1]]
    assert not dup, "%s 出现连着重复：%r" % (label, texts)
    assert len(set(texts)) >= len(pool), \
        "%s 变体没轮起来，%d 次只出了 %d 种：%r" % (label, n, len(set(texts)), texts)
    print("  %s：%d 次冷却 → %d 种说法，无连着重复" % (label, n, len(set(texts))))
    print("      %s" % " / ".join(sorted(set(texts))))


async def main():
    assert len(V._COOL_POOL) >= 5 and len(I._COOL_POOL) >= 5
    shared = set(V._COOL_POOL) & set(I._COOL_POOL)
    assert len(shared) <= 3, \
        "两个插件的池子重合太多（%d 句），全群只听到同一套话" % len(shared)
    await check(V, "dsh-voice（/说话）", V._COOL_POOL, "/说话", "cmd_speak")
    await check(I, "dsh-imagegen（/画图）", I._COOL_POOL, "/画图", "cmd_draw")
    print("COOL_LINE_TEST_OK")
    print("  旧的『慢点，还有 N 秒冷却』全量日志出现过 114 次（09-12 一天 34 次）")
    print("  新文案：无「冷却」、无秒数倒计时、有变体、不连着重复")


if __name__ == "__main__":
    asyncio.run(main())
