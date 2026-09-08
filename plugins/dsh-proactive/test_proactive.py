"""dsh-proactive 离线回测。断言全部来自真实语料 + Codex QQ-Enhancer 的评分语义。

重点不是「能不能触发」，而是：
  1. 低价值/纯服务请求绝不触发（防话痨）。
  2. 命中兴趣词就该拿到足够高的分（白米饭 8 分是单类强触发）。
  3. 冷却/额度/阈值这三把闸门的纯函数行为正确。
  4. 群主说话有 +2 的倾斜。
"""

import importlib.util
import sys
import types
from pathlib import Path

if "astrbot" not in sys.modules:
    try:
        import astrbot  # noqa: F401
    except ImportError:
        for name in ("astrbot", "astrbot.api", "astrbot.api.event",
                     "astrbot.core", "astrbot.core.agent",
                     "astrbot.core.agent.message", "astrbot.core.platform.message_type"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
        sys.modules["astrbot.api.event"].AstrMessageEvent = object
        sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
            on_llm_request=lambda: (lambda f: f),
            command=lambda *a, **k: (lambda f: f),
            platform_adapter_type=lambda *a, **k: (lambda f: f),
            custom_filter=lambda *a, **k: (lambda f: f))
        sys.modules["astrbot.api.event"].filter.PlatformAdapterType = types.SimpleNamespace(ALL="all")
        sys.modules["astrbot.core"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            debug=lambda *a, **k: None, error=lambda *a, **k: None)
        sys.modules["astrbot.core.agent.message"].TextPart = object
        sys.modules["astrbot.core.platform.message_type"].MessageType = types.SimpleNamespace(
            GROUP_MESSAGE="group", PRIVATE_MESSAGE="private")
        sys.modules["astrbot.core.platform.astrbot_message"] = types.ModuleType("astrbot.core.platform.astrbot_message")
        sys.modules["astrbot.core.platform.astrbot_message"].AstrBotMessage = object
        sys.modules["astrbot.core.platform.astrbot_message"].Group = object
        sys.modules["astrbot.core.platform.astrbot_message"].MessageMember = object
        sys.modules["astrbot.core.star.filter.custom_filter"] = types.ModuleType("astrbot.core.star.filter.custom_filter")
        sys.modules["astrbot.core.star.filter.custom_filter"].CustomFilter = object
        sys.modules["aiocqhttp"] = types.ModuleType("aiocqhttp")
        sys.modules["aiocqhttp"].Event = type("Event", (), {"from_payload": staticmethod(lambda p: p)})

spec = importlib.util.spec_from_file_location("proactive", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def decide(text, owner=False, gap_ms=999_999_999, count=0, ts=0):
    return m.should_proactively_reply(
        text,
        {"senderId": ("2774000001" if owner else "1234567890"), "groupId": "100000001"},
        last_reply_ms=0 if ts else 0,
        now_ms=ts or 1_000_000_000_000,
        count=count,
    )


passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL: %s %s" % (name, detail))


# ---------------- 触发类：真实语料里出现过的句子
ok, why, score, hits = decide("不准吃大白饭")
check("大白饭触发", ok, "%s score=%d hits=%s" % (why, score, hits))
check("大白饭分高", score >= 8, "score=%d" % score)

ok, why, score, hits = decide("大肥鱼竟然吃免费的大白饭，真是杂鱼呢")
check("吃免费大白饭触发", ok, "%s %d" % (why, score))

ok, why, score, hits = decide("夜宵给我吃")
check("夜宵触发", ok, "%s %d" % (why, score))

ok, why, score, hits = decide("饿啊")
check("饿啊触发", ok, "%s %d hits=%s" % (why, score, hits))

ok, why, score, hits = decide("哥哥哥哥推荐点好吃的呗")
check("推荐好吃的触发", ok, "%s %d" % (why, score))

ok, why, score, hits = decide("这鱼今天怎么这么安静")
check("点名触发", ok, "%s %d" % (why, score))

ok, why, score, hits = decide("白米饭加老干妈")
check("白米饭加老干妈触发", ok)

# ---------------- 不触发类
for msg in ("666", "哈哈哈", "草", "典", "？", "1", "嗯", "收到", "ok"):
    ok, why, score, hits = decide(msg)
    check("低价值不触发: %r" % msg, not ok, why)

ok, why, _, _ = decide("帮我修一下电脑")
check("纯服务请求不触发", not ok, why)

ok, why, score, hits = decide("帮我查一下白米饭的营养")
check("服务请求带兴趣词触发", ok, "%s %d %s" % (why, score, hits))

# ---------------- 群主加权
# 测试代码里不能出现真实 QQ 号（脱敏铁律），monkeypatch OWNER 为假号。
m.OWNER = "2774000001"
_, _, score_member, hits_member = decide("火锅奶茶安排上", owner=False)
_, _, score_owner, hits_owner = decide("火锅奶茶安排上", owner=True)
check("群主说话加分", score_owner > score_member,
      "owner=%d member=%d" % (score_owner, score_member))
check("群主命中列表带群主", "群主" in hits_owner, hits_owner)

# ---------------- 冷却 / 额度
ok, why, _, _ = m.should_proactively_reply(
    "白米饭", {"senderId": "1", "groupId": "100000001"},
    last_reply_ms=1_000_000_000_000 - 60_000,  # 1 分钟前刚说过 < 10 分钟
    now_ms=1_000_000_000_000, count=0)
check("冷却中不触发", not ok, why)

ok, why, _, _ = m.should_proactively_reply(
    "白米饭", {"senderId": "1", "groupId": "100000001"},
    last_reply_ms=0, now_ms=1_000_000_000_000, count=5)
check("额度用完不触发", not ok, why)

ok, why, _, _ = m.should_proactively_reply(
    "白米饭", {"senderId": "1", "groupId": "100000001"},
    last_reply_ms=0, now_ms=1_000_000_000_000, count=0)
check("冷却额度都过就触发", ok, why)

# ---------------- 分数稳定性
_, _, s1, _ = decide("白米饭加老干妈")
_, _, s2, _ = decide("白米饭加老干妈")
check("同消息同分（稳定抖动）", s1 == s2, "%d vs %d" % (s1, s2))

# ---------------- 纯函数不炸
check("空消息安全", m.score_interest("")[0] == 0)
check("None 安全", m.score_interest(None)[0] == 0)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
