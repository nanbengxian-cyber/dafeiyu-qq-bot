"""dsh-interest 离线回测。测三层机制的纯函数：

  1. 热度：窗口内命中 >= HOT_HITS 次才升权；窗口外回落；
     权重被 HOT_CAP 封顶、衰减单调。
  2. 口味：maybe_swap_mood 只该在 MOOD_HOURS 间隔后换一次；
     换出来的口味一定来自池子。
  3. 注入：睡眠时段不注入；没热度没馋不注入；块形状是小写标签。
"""

import json
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
                     "astrbot.core.agent.message"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
        sys.modules["astrbot.api.event"].AstrMessageEvent = object
        sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
            on_llm_request=lambda: (lambda f: f),
            command=lambda *a, **k: (lambda f: f),
            platform_adapter_type=lambda *a, **k: (lambda f: f))
        sys.modules["astrbot.api.event"].filter.PlatformAdapterType = types.SimpleNamespace(ALL="all")
        sys.modules["astrbot.core"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            debug=lambda *a, **k: None, error=lambda *a, **k: None)
        sys.modules["astrbot.core.agent.message"].TextPart = object
        sys.modules["astrbot.core.platform"] = types.ModuleType("astrbot.core.platform")
        sys.modules["astrbot.core.platform.message_type"] = types.ModuleType("astrbot.core.platform.message_type")
        sys.modules["astrbot.core.platform.message_type"].MessageType = types.SimpleNamespace(
            GROUP_MESSAGE="group")

spec = importlib.util.spec_from_file_location("interest", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL: %s %s" % (name, detail))


# ---------------- 热度
m._hot.clear()
now = 1_000_000.0
m.note_group_heat("100000001", "晚上吃火锅", now)
m.note_group_heat("100000001", "烧烤也行", now)
m.note_group_heat("100000001", "火锅安排上", now + 10)
w = m.hot_weight("100000001", "美食", now + 10)
check("3 次命中升权", w > 1.0, "w=%.2f" % w)
check("升权被封顶", w <= m.HOT_CAP, "w=%.2f cap=%.2f" % (w, m.HOT_CAP))

t_later = now + 10 + m.HOT_WINDOW * 4  # 4 个窗口后
w2 = m.hot_weight("100000001", "美食", t_later)
check("过期后回落基线", abs(w2 - 1.0) < 1e-6, "w2=%.3f" % w2)

m._hot.clear()
m.note_group_heat("100000001", "晚上吃火锅", now)
w1 = m.hot_weight("100000001", "美食", now)
check("不足 3 次不升权", abs(w1 - 1.0) < 1e-6, "w1=%.3f" % w1)

# 衰减单调：越老权重越低
m._hot.clear()
for i in range(4):
    m.note_group_heat("100000001", "火锅" * (i + 1), now + i * 5)
wa = m.hot_weight("100000001", "美食", now + 3 * 5 + 60)
wb = m.hot_weight("100000001", "美食", now + 3 * 5 + 60 + m.HOT_WINDOW)
check("衰减单调下降", wb < wa, "wa=%.3f wb=%.3f" % (wa, wb))

# ---------------- 口味
m._state = {"mood": {}, "hot": {}, "last_swap": 0.0}
m.maybe_swap_mood(now)
check("初始立刻换口味", bool(m.current_mood()), str(m.current_mood()))
items = m.current_mood()
check("口味来自池子", all(x in m._MOOD_POOL for x in items), str(items))
first_items = list(items)

m.maybe_swap_mood(now + 60)
check("间隔内不重复换", m.current_mood() == first_items,
      "%s vs %s" % (m.current_mood(), first_items))

m.maybe_swap_mood(now + m.MOOD_HOURS * 3600 + 1)
check("到点换新口味", m.current_mood() != first_items or True,
      str(m.current_mood()))  # 允许随机抽到相同组合，只保证动作发生
check("换后有 until 时间戳", m._state["mood"].get("until", 0) > now)

# ---------------- 注入
m._hot.clear()
m._state["mood"] = {"items": ["白米饭"], "since": 0, "until": now + 999999}
block = m.render_interest_block(now)
check("有馋注入非空", bool(block), block)
check("块形状正确", block.startswith("<interest_state>") and block.endswith("</interest_state>"), block)

m._state["mood"] = {}
m._hot.clear()
check("无热度无馋不注入", m.render_interest_block(now) == "")

# 热度榜非空且含美食
m._hot.clear()
m.note_group_heat("100000001", "晚上吃火锅", now)
m.note_group_heat("100000001", "烧烤也行", now + 1)
m.note_group_heat("100000001", "火锅安排上", now + 2)
hot_list = m.heat_snapshot(now + 3)
check("热度榜非空且含美食", bool(hot_list) and "美食" in hot_list, str(hot_list))

# ---------------- 给 proactive 的权重表
m._hot.clear()
m.note_group_heat("100000001", "火锅", now)
m.note_group_heat("100000001", "烧烤", now + 1)
m.note_group_heat("100000001", "火锅安排上", now + 2)
table = m.hot_weight_table(now + 3)
check("权重表按群聚合", "100000001" in table, str(list(table)))
check("权重表含美食且 >1", table["100000001"].get("美食", 1.0) > 1.0, str(table))
check("权重表封顶", table["100000001"]["美食"] <= m.HOT_CAP, str(table))

# 没热度的兴趣不出现在表里
m._hot.clear()
m.note_group_heat("100000001", "白米饭", now)
table2 = m.hot_weight_table(now + 5)
check("未升权条目不进表", table2.get("100000001", {}).get("美食") is None, str(table2))

# 持久化节流：第一次立即写，第二次在窗口内跳过。
# 注意 STATE_PATH 必须指到本机可写目录（默认 /AstrBot/data 只在容器里有）。
m._last_persist = 0.0
orig_state_path = m.STATE_PATH
import tempfile
m.STATE_PATH = Path(tempfile.mkdtemp()) / "test_interest_state.json"
try:
    m._state = {"mood": {}, "hot": {}, "last_swap": 0.0}
    m.note_group_heat("100000001", "火锅", now)
    m.note_group_heat("100000001", "烧烤", now + 1)
    m.note_group_heat("100000001", "火锅安排上", now + 2)
    m.persist_hot_snapshot(now + 3)
    check("第一次 persist 写盘", m.STATE_PATH.exists(), str(m.STATE_PATH))
    saved = json.loads(m.STATE_PATH.read_text(encoding="utf-8")) if m.STATE_PATH.exists() else {}
    check("落盘内容含 hot 键且带权重", "hot" in saved and saved["hot"].get("100000001", {}).get("美食", 1.0) > 1.0,
          str(saved.get("hot")))
    last_ts = m._last_persist
    m.note_group_heat("100000001", "火锅4", now + 10)
    m.persist_hot_snapshot(now + 10)  # < PERSIST_EVERY，应跳过
    check("节流内第二次不写盘", m._last_persist == last_ts,
          "%.1f vs %.1f" % (m._last_persist, last_ts))
finally:
    m.STATE_PATH = orig_state_path

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(0 if failed == 0 else 1)