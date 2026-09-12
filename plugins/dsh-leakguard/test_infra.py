# -*- coding: utf-8 -*-
"""dsh-leakguard 的「基础设施报错不进群」规则回归。

正例用**生产日志里那 11 条真实报错原文**（不是编的），负例用**真实群聊里
大肥鱼说过的话**。两头都来自生产，所以这个测试量的就是真实误伤率。

跑法（容器内 py3.12）：
    docker exec astrbot python3 /tmp/lg/test_infra.py
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DSH_LEAKGUARD", "1")

import main as m  # noqa: E402

# 静音：import astrbot.core 会顺带把 AstrBot 的文件日志 sink 挂到生产日志上
# （见 docs/71 结论 7）。这是独立进程，remove() 只影响自己。
try:
    from loguru import logger as _lg
    _lg.remove()
except Exception:
    pass


RX_OUT = re.compile(
    r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\].*"
    r"respond\.stage:\d+\]: Prepare to send - \S+?/\d+: (.*)$"
)
LOGS = "/AstrBot/data/logs"
BOT = "3752949717"


def read_lines():
    files = []
    for name in sorted(os.listdir(LOGS)):
        if name.startswith("astrbot") and name.endswith(".log"):
            files.append(os.path.join(LOGS, name))
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield line.rstrip("\n")


def collect():
    """回生产日志取：真实报错原文（正例）+ 大肥鱼真实说过的话（负例）。"""
    errors, said = [], []
    for line in read_lines():
        m2 = RX_OUT.match(line)
        if not m2:
            continue
        text = m2.group(1) or ""
        if "LLM 响应错误" in text or "All chat models failed" in text:
            errors.append(text.strip())
            continue
        for comp in text.split(" "):
            comp = re.sub(r"\[引用消息[^\]]*\]\s*|\[At:\d+\]\s*", "", comp).strip()
            if comp and not comp.startswith("/"):
                said.append(comp)
    return errors, said


class FakeResult:
    def __init__(self, text):
        self.chain = [type("C", (), {"text": text})()]
        self.cleared = False
        self.is_model_result = lambda: True

    def get_plain_text(self):
        return "".join(getattr(c, "text", "") or "" for c in self.chain)


class FakeEvent:
    def __init__(self, text):
        self._r = FakeResult(text)
        self.stopped = False

    def get_group_id(self):
        return list(m.GROUPS)[0] if m.GROUPS else "100000001"

    def get_result(self):
        return self._r

    def clear_result(self):
        self._r.cleared = True
        self._r.chain = []

    def stop_event(self):
        self.stopped = True


async def main():
    errors, said = collect()
    assert errors, "没从生产日志里取到报错原文，测试前提不成立"
    assert len(said) > 500, "真实说话样本太少（%d）" % len(said)

    # ---- 正例：11 条真实报错必须全部命中
    misses = [e for e in errors if not m._infra_error(e)]
    assert not misses, "漏掉了 %d 条真实报错，例如：%s" % (len(misses), misses[0][:120])

    # ---- 负例：真实聊天一句都不能误伤
    fp = [s for s in said if m._infra_error(s)]
    assert not fp, "误伤 %d 条真实聊天，例如：%r" % (len(fp), fp[:5])

    # ---- 行为：命中后必须清空 + 停事件（=沉默），且计入统计
    gate = m.Main.__new__(m.Main)
    before = m._stat["infra"]
    ev = FakeEvent(errors[0])
    gate._handle_infra(ev, errors[0])
    assert ev._r.cleared, "命中后没有 clear_result，报错还是会发出去"
    assert ev.stopped, "命中后没有 stop_event"
    assert m._stat["infra"] == before + 1, "统计没加上"

    # ---- 边界：整句是报错才拦；聊天里提到这些词不该拦
    borderline = [
        ("你那个 traceId 打出来我看看", False),
        ("我就余额不足，穷", False),
        ("Error code: 402 是啥意思", False),
        ("traceId 和 'code': 都贴一下，'message': 也要", True),
        ("Traceback (most recent call last):", True),
    ]
    bad = [(t, want) for t, want in borderline if m._infra_error(t) != want]
    assert not bad, "边界判错：%r" % bad

    print("LEAKGUARD_INFRA_TEST_OK")
    print("  正例 %d 条真实报错 → 全部拦下" % len(errors))
    print("  负例 %d 条真实聊天 → 0 误伤" % len(said))
    print("  边界 %d 条含技术词的正常聊天 → 判对" % len(borderline))
    print("  命中行为：clear_result + stop_event（沉默，不是换一句安慰话）")
    print("  其中 402 余额不足 %d 条、连接/超时/5xx %d 条"
          % (sum(1 for e in errors if "402" in e),
             sum(1 for e in errors if "402" not in e)))


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
