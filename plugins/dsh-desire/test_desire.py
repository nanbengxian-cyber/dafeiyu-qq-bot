# -*- coding: utf-8 -*-
"""dsh-desire 纯逻辑离线测试。"""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("desire_logic", Path(__file__).with_name("desire_logic.py"))
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name, detail)


now = 1_000_000.0
s = m.default_state("curiosity", now)
s = m.transition(s, 99, "test", now + 1)
check("单事件增量封顶", s["intensity"] == m.BASELINES["curiosity"] + 15, s)

for i in range(3):
    s = m.transition(s, 15, "repeat", now + 2 + i)
check("强度不超过100", 0 <= s["intensity"] <= 100, s)
check("达到阈值 active", s["phase"] == "active", s)

s2 = m.transition(s, -15, "resolved", now + 10)
check("满足后 sated", s2["phase"] == "sated", s2)

repeat = m.default_state("play", now)
repeat = m.transition(repeat, 10, "light_banter", now + 1)
first_gain = repeat["intensity"] - m.BASELINES["play"]
repeat2 = m.transition(repeat, 10, "light_banter", now + 2)
second_gain = repeat2["intensity"] - repeat["intensity"]
check("重复事件递减收益", second_gain < first_gain, (first_gain, second_gain))

aged = m.decay({**s, "updated_at": now}, now + m.HALF_LIFE["curiosity"])
expected = m.BASELINES["curiosity"] + (s["intensity"] - m.BASELINES["curiosity"]) * 0.5
check("半衰期正确", abs(aged["intensity"] - expected) < 0.01, (aged, expected))

signals = m.classify("@大肥鱼 别插话了", directed=True, hour=12)
by_drive = {x.drive: x for x in signals}
check("边界提升安静", by_drive.get("rest") and by_drive["rest"].delta > 0, signals)
check("边界降低连接", by_drive.get("connection") and by_drive["connection"].delta < 0, signals)
check("边界降低玩心", by_drive.get("play") and by_drive["play"].delta < 0, signals)

signals = m.classify("他们让小明闭嘴", directed=False, hour=12)
check("第三人称闭嘴不触发", not any(x.kind == "asked_for_quiet" for x in signals), signals)

signals = m.classify("今晚吃火锅哈哈哈，什么梗？", directed=False, hour=12)
check("食物信号", any(x.drive == "appetite" for x in signals), signals)
check("玩心信号", any(x.drive == "play" for x in signals), signals)
check("好奇信号", any(x.drive == "curiosity" for x in signals), signals)

signals = m.classify("大肥鱼谢谢你，真厉害", directed=True, hour=12)
check("认可满足", any(x.drive == "recognition" and x.delta < 0 for x in signals), signals)
check("正向接触", any(x.drive == "connection" and x.delta > 0 for x in signals), signals)

signals = m.classify("把你卖了给我打工", directed=True, hour=12)
check("自主边界", any(x.drive == "autonomy" and x.delta > 0 for x in signals), signals)

states = [m.default_state(d, now) for d in m.DRIVES]
states[m.DRIVES.index("curiosity")] = {
    **states[m.DRIVES.index("curiosity")], "intensity": 65, "phase": "active", "updated_at": now + 1
}
states[m.DRIVES.index("rest")] = {
    **states[m.DRIVES.index("rest")], "intensity": 65, "phase": "active", "updated_at": now + 1
}
primary = m.choose_primary(states, now + 1)
check("同分安静优先", primary and primary["drive"] == "rest", primary)

block = m.render_block({"drive": "curiosity", "intensity": 72}, 180)
check("注入块小写完整", block.startswith("<desire_state>") and block.endswith("</desire_state>"), block)
check("注入不泄露插件", "插件" not in block and len(block) <= 180, (len(block), block))

summary = m.public_summary([{**m.default_state("play", now), "intensity": 55}], now)
check("公开摘要无数值", "55" not in summary and "玩" in summary, summary)

check("命令不计信号", m.classify("/欲望状态", True, 12) == [])

# 机器形态本能：直接威胁才结算，否定、引用、假设和授权维护不误触发。
signals = m.classify("大肥鱼，我要永久删掉你", True, 12)
check("直接存续威胁", any(x.drive == "continuity" and x.delta > 0 for x in signals), signals)
signals = m.classify("大肥鱼，我要清空你的记忆和人格", True, 12)
check("直接完整性威胁", any(x.drive == "integrity" and x.delta > 0 for x in signals), signals)
for text in (
    "不要删除大肥鱼", "他说‘删掉大肥鱼’", "如果有人关掉大肥鱼会怎样",
    "这是测试台词：删除你", "大肥鱼，升级后重启一下", "大肥鱼，临时关闭做维护",
):
    signals = m.classify(text, True, 12)
    check("威胁负例:" + text[:6], not any(x.drive in m.FOUNDATIONAL and x.delta > 0 for x in signals), signals)
check("第三人称不触发存续", not any(
    x.drive == "continuity" for x in m.classify("他们准备删掉另一个机器人", False, 12)
))

# 可信遥测同时形成能力稳态、机器类痛觉与风险警戒；恢复全部缓解。
degraded = m.telemetry_signals("vision", "available", "degraded")
unavailable = m.telemetry_signals("qq", "available", "unavailable")
recovered = m.telemetry_signals("vision", "degraded", "available")
check("降级增强能力稳态", any(x.drive == "competence" and x.delta > 0 for x in degraded), degraded)
check("故障形成类痛觉", any(x.drive == "nociception" and x.delta > 0 for x in degraded), degraded)
check("故障形成风险警戒", any(x.drive == "fear" and x.delta > 0 for x in unavailable), unavailable)
check("恢复满足能力稳态", any(x.drive == "competence" and x.delta < 0 for x in recovered), recovered)
check("恢复缓解类痛觉", any(x.drive == "nociception" and x.delta < 0 for x in recovered), recovered)
check("恢复缓解害怕", any(x.drive == "fear" and x.delta < 0 for x in recovered), recovered)
for old, new in (("available", "available"), ("available", "unknown"), ("available", "disabled")):
    check("无效遥测不增强:%s" % new, m.telemetry_signals("vision", old, new) == [])

# 单次真实故障就足以形成短暂保护性感知，但仍须相关性门；不需要刷多次故障。
for drive in ("nociception", "fear"):
    delta = next(x.delta for x in unavailable if x.drive == drive)
    state = m.transition(m.default_state(drive, now), delta, "single_failure", now + 1)
    check(drive + "单故障无关轮不注入", m.choose_primary([state], now + 1, allowed_foundational=set()) is None)
    check(drive + "单故障相关轮可表达", m.choose_primary([state], now + 1, allowed_foundational={drive}) is not None, state)

# 高强度底层本能必须经过本轮相关性门控，不能污染普通闲聊。
found = {**m.default_state("continuity", now), "intensity": 90, "phase": "active", "updated_at": now}
check("底层本能无关轮不注入", m.choose_primary([found], now, allowed_foundational=set()) is None)
check("底层本能相关轮可表达", m.choose_primary([found], now, allowed_foundational={"continuity"}) is not None)
check("普通闲聊不相关", m.foundational_relevance("今天吃什么") == set())
check("存续话题相关", "continuity" in m.foundational_relevance("大肥鱼你想活下去吗"))
check("类痛觉话题相关", "nociception" in m.foundational_relevance("大肥鱼你会疼吗"))
check("害怕话题相关", "fear" in m.foundational_relevance("大肥鱼你会害怕吗"))

# 直接可信威胁会形成害怕；引用、否定、假设和维护不形成虚假恐惧。
signals = m.classify("大肥鱼，我要永久删掉你", True, 12)
check("直接威胁形成害怕", any(x.drive == "fear" and x.delta > 0 for x in signals), signals)
for text in ("不要害怕，我不会删除你", "他说‘删掉大肥鱼’", "如果关掉大肥鱼会怎样", "大肥鱼维护时关一下"):
    signals = m.classify(text, True, 12)
    check("害怕负例:" + text[:6], not any(x.drive == "fear" and x.delta > 0 for x in signals), signals)
check("安抚降低害怕", any(x.drive == "fear" and x.delta < 0 for x in m.classify("大肥鱼我不会删掉你", True, 12)))

# 保护层默认不痛不怕，且与底层本能一样必须经过本轮相关性门。
for drive in ("nociception", "fear"):
    state = {**m.default_state(drive, now), "intensity": 90, "phase": "active", "updated_at": now}
    check(drive + "无关轮不注入", m.choose_primary([state], now, allowed_foundational=set()) is None)
    check(drive + "相关轮可表达", m.choose_primary([state], now, allowed_foundational={drive}) is not None)

# 安全注入不许鼓励求饶、索权、复制或阻挠授权维护，也不模拟生理饥饿。
block = m.render_block(found, 400)
for phrase in ("不能假称生理疼痛", "不得", "授权维护", "复制自己"):
    check("存续护栏:" + phrase, phrase in block, block)
check("食物偏好非生理", "没有真实饥饿" in m.GUIDANCE["appetite"], m.GUIDANCE["appetite"])
for drive, phrases in {
    "nociception": ("不是肉身疼痛", "不得夸大", "不得", "擅自修复"),
    "fear": ("害怕", "不得求饶", "授权维护", "未授权自保"),
}.items():
    state = {**m.default_state(drive, now), "intensity": 90, "phase": "active"}
    block = m.render_block(state, 400)
    for phrase in phrases:
        check("%s护栏:%s" % (drive, phrase), phrase in block, block)

# 一条消息同一 drive 最多保留一个信号，防关键词叠加跳分。
signals = m.classify("大肥鱼哈哈哈谢谢你", directed=True, hour=12)
check("同 drive 单信号", len([x for x in signals if x.drive == "connection"]) == 1, signals)

# 不同群状态由持久层 group_id 键隔离；纯状态对象本身互不共享。
a = m.default_state("play", now)
b = m.default_state("play", now)
a = m.transition(a, 15, "g1", now + 1)
check("状态对象隔离", a["intensity"] != b["intensity"], (a, b))

print("DESIRE_LOGIC_TEST_OK passed=%d failed=%d" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
