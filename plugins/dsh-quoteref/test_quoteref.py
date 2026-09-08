"""dsh-quoteref 离线回测。

两件事：计数逻辑（第几次才引用、冷却挡不挡）和提问判定（跟 dsh-drift 同一套，
「吧」不算疑问那条是拿真语料量出来的，必须守住）。
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
                     "astrbot.api.message_components", "astrbot.core"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
        sys.modules["astrbot.api.event"].AstrMessageEvent = object
        sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
            on_decorating_result=lambda: (lambda f: f),
            command=lambda *a, **k: (lambda f: f))

        class _C:
            def __init__(self, **kw):
                self.__dict__.update(kw)
        mc = sys.modules["astrbot.api.message_components"]
        mc.At = type("At", (_C,), {})
        mc.Plain = type("Plain", (_C,), {})
        mc.Reply = type("Reply", (_C,), {})
        sys.modules["astrbot.core"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            error=lambda *a, **k: None, debug=lambda *a, **k: None)

spec = importlib.util.spec_from_file_location("qr", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


# ============================================================ 计数
E = m.EVERY
# 前 E-1 次都不引用，第 E 次引用
for i in range(1, E):
    ok, why = m.should_quote(i, 1000.0, 0.0)
    assert not ok, (i, why)
    assert "还没到" in why
ok, why = m.should_quote(E, 1000.0, 0.0)
assert ok, why
# 第 2E 次又该引用
assert m.should_quote(E * 2, 99999.0, 0.0)[0]
# 中间那些不引用
assert not m.should_quote(E * 2 - 1, 99999.0, 0.0)[0]
print("  计数：每 %d 次引用一次，边界正确" % E)

# ============================================================ 冷却
now = 10_000.0
ok, why = m.should_quote(E, now, now - 10)          # 刚引用过 10 秒
assert not ok and "冷却" in why, why
ok, why = m.should_quote(E, now, now - (m.COOLDOWN + 1))
assert ok, why
# 从没引用过（last=0）不该被冷却挡住
assert m.should_quote(E, now, 0.0)[0]
print("  冷却：%.0fs 内不重复引用，首次不被挡" % m.COOLDOWN)

# 一串连问：到了次数但冷却没过，只放第一次
fired = 0
last = 0.0
t = 1000.0
for i in range(1, E * 4 + 1):
    ok, _ = m.should_quote(i, t, last)
    if ok:
        fired += 1
        last = t
    t += 5.0                                        # 每 5 秒一条，模拟连问
assert fired == 1, "连问时应该只引用一次，实际 %d 次" % fired
print("  连问 %d 条（每 5 秒一条）只引用 %d 次" % (E * 4, fired))

# ============================================================ 提问判定
for t in ("大肥鱼这是什么？", "你怎么看", "咋整", "为什么不理我", "能不能画个图",
          "多久能好", "你在吗", "这是啥呢", "干嘛不说话", "哪个更好"):
    assert m.looks_question(t), t
# 「吧」不是疑问助词（软化/推测）—— 这条是拿真语料量出来的，别改回去
for t in ("但是svg这程度真的离谱了吧", "那这个应该也能用的吧", "那就这样吧",
          "行吧", "我先睡了", "这图不错", "笑死"):
    assert not m.looks_question(t), t
print("  提问判定：10 条真提问全中，7 条非提问（含 4 条「吧」结尾）全放行")

# ============================================================ 旋钮
assert m.GROUPS == {"100000001"} or m.GROUPS == set(), m.GROUPS
assert m.EVERY >= 2, "每次都引用就不是真人了"
assert m.COOLDOWN >= 0
assert m.DROP_AT, "默认必须摘掉 At —— 真人不会既引用又艾特"

print("QUOTEREF_TEST_OK every=%d cooldown=%.0fs drop_at=%s"
      % (m.EVERY, m.COOLDOWN, m.DROP_AT))
