# -*- coding: utf-8 -*-
"""dsh-mind 逻辑层回归测试。

跑法（宿主机 py3.8 也能跑，mind_logic 不 import astrbot）：
    python3 test_mind.py

覆盖：衰减一致性、档位阈值、疲劳代理、请求形状解析、岛判定、冲突规则、
草稿预算、以及**真实 fixture 下的只读读取器**（含「路径全坏也不许抛」）。
"""

import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "mind_logic", Path(__file__).with_name("mind_logic.py"))
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name, detail)


NOW = 1_700_000_000.0

# ---------------------------------------------------------------- 衰减一致性
check("欲望：同一时刻不衰减",
      abs(m.drive_effective(100.0, 16.0, NOW, "play", NOW) - 100.0) < 1e-6)
check("欲望：一个半衰期走一半",
      abs(m.drive_effective(100.0, 16.0, NOW - 7200, "play", NOW) - (16.0 + 84.0 / 2)) < 1e-6)
check("欲望：未知 drive 用默认半衰期",
      abs(m.drive_effective(100.0, 16.0, NOW - 10800, "unknown_x", NOW) - (16.0 + 84.0 / 2)) < 1e-6)
check("欲望：updated_at 在未来也不放大",
      abs(m.drive_effective(100.0, 16.0, NOW + 9999, "play", NOW) - 100.0) < 1e-6)
check("逆反：半衰期 6h",
      abs(m.reactance_effective(8.0 + 30.0, NOW - 21600, NOW) - (8.0 + 15.0)) < 1e-6)
check("逆反：未更新则原值",
      abs(m.reactance_effective(40.0, NOW, NOW) - 40.0) < 1e-6)

# ---------------------------------------------------------------- 社交档位
check("档位：亲近要同时够好感和信任", m.social_tier(60, 50) == "亲近")
check("档位：好感够但信任不够只算熟人", m.social_tier(60, 30) == "熟人")
check("档位：熟人", m.social_tier(20, 20) == "熟人")
check("档位：陌生", m.social_tier(0, 20) == "陌生")
check("档位：疏离", m.social_tier(-20, 20) == "疏离")
check("档位：避让", m.social_tier(-60, 20) == "避让")
check("档位：manual_tier 优先", m.social_tier(0, 0, "亲近") == "亲近")
check("档位：非法 manual 被忽略", m.social_tier(0, 0, "乱七八糟") == "陌生")

# ---------------------------------------------------------------- 疲劳代理
fat = {"groups": {"g": [
    {"key": "a", "replies": [{"uid": "u", "ts": NOW}]},
    {"key": "b", "replies": [{"uid": "u", "ts": NOW}, {"uid": "u", "ts": NOW},
                             {"uid": "u", "ts": NOW}]},
]}}
check("疲劳代理：取单话题最大追问数 → 3", m.fatigue_proxy(fat, "g", "u", NOW, 1200) == 3)
fat2 = {"groups": {"g": [{"key": "a", "replies": [{"uid": "u", "ts": NOW},
                                                  {"uid": "u", "ts": NOW}]}]}}
check("疲劳代理：2 次 → 2 档", m.fatigue_proxy(fat2, "g", "u", NOW, 1200) == 2)
check("疲劳代理：1 次不算", m.fatigue_proxy(fat2, "g", "u", NOW, 1200 - 1e9) == 0)
fat3 = {"groups": {"g": [{"key": "a", "replies": [{"uid": "other", "ts": NOW},
                                                  {"uid": "other", "ts": NOW},
                                                  {"uid": "other", "ts": NOW}]}]}}
check("疲劳代理：别人追问不算我的", m.fatigue_proxy(fat3, "g", "u", NOW, 1200) == 0)
check("疲劳代理：坏结构不抛", m.fatigue_proxy({"groups": "x"}, "g", "u", NOW, 1200) == 0)
check("疲劳代理：空数据不抛", m.fatigue_proxy({}, "g", "u", NOW, 1200) == 0)
# 窗口是「现在往前」，所以旧回复要按 now 判
old = {"groups": {"g": [{"key": "a", "replies": [{"uid": "u", "ts": NOW - 5000},
                                                 {"uid": "u", "ts": NOW - 5000}]}]}}
check("疲劳代理：超出窗口不计", m.fatigue_proxy(old, "g", "u", NOW, 1200) == 0)


# ---------------------------------------------------------------- 请求形状
class FakePart:
    def __init__(self, text):
        self.text = text


parts = m.scan_parts([
    FakePart("<emotion_state>比较好奇</emotion_state>"),
    FakePart("<scene>群里在聊别的</scene>"),
    "<spine>被牵了 4 轮</spine>",
    FakePart("没有任何标签的一段"),
])
check("块解析：TextPart 与裸 str 都认", len(parts) == 4, parts)
check("块解析：标签正确",
      [p.tag for p in parts] == ["emotion_state", "scene", "spine", "(无标签)"], parts)
check("块解析：字数是整块长度", parts[0].chars == len("<emotion_state>比较好奇</emotion_state>"))
check("块解析：None 安全", m.scan_parts(None) == [])
check("块解析：空列表安全", m.scan_parts([]) == [])

ctx = [
    {"role": "user", "content": "abc"},
    {"role": "user", "content": [{"type": "text", "text": "de"}]},
    {"role": "assistant", "content": None},
    "不是字典的脏数据",
]
chars = m.scan_contexts(ctx)
check("历史字数：字符串与多模态都算", chars >= 5, chars)
check("历史字数：None 安全", m.scan_contexts(None) == 0)

# ---------------------------------------------------------------- 岛判定
neutral = m.MindSnapshot(gid="g", uid="u")
check("中性快照：零岛", m.islands(neutral) == [])
check("中性草稿：只说平静",
      m.render_draft(neutral) == "<mind_state>内在状态：平静。</mind_state>",
      m.render_draft(neutral))

rich = m.MindSnapshot(gid="g", uid="u")
rich.emotion, rich.intensity, rich.emotion_live, rich.directed = "curious", 2, True, True
rich.drives = {"play": (60.0, 16.0, "active")}
rich.tier, rich.trust = "熟人", 30.0
rich.reactance = m.REACTANCE_BASE + 20.0
rich.fatigue, rich.fatigue_topic = 2, "玩梗复读"
rich.mood_items = ("白米饭",)
rich.hot = (("火锅", 1.4),)
rich.selfworth_kinds, rich.selfworth_ago = ("FREELOAD",), 30.0
rich.awareness_recent = (("poke", 2),)
rich.selfaware_fails = 3
rich.action_quota = (("initiate", 2),)
isles = m.islands(rich)
check("满状态：11 个岛全都数到", len(isles) == 11, [i["key"] for i in isles])
check("岛排序键：欲望偏离最大排最前",
      sorted(isles, key=lambda d: -d["magnitude"])[0]["key"] == "desire",
      [(i["key"], i["magnitude"]) for i in isles])
check("底层欲望不参与排序",
      "continuity" not in "".join(i["detail"] for i in isles))
base_only = m.MindSnapshot(gid="g", uid="u")
base_only.drives = {"fear": (90.0, 8.0, "active"), "continuity": (80.0, 45.0, "active")}
check("只有底层欲望时不算岛", m.islands(base_only) == [], m.islands(base_only))

# ---------------------------------------------------------------- 冲突规则
def snap(**kw):
    s = m.MindSnapshot(gid="g", uid="u")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


c = m.detect_conflicts(snap(emotion="angry", intensity=3, emotion_live=True, tier="熟人"), [])
check("冲突1：对熟人生气", len(c) == 1 and "情绪" in c[0], c)
check("冲突1 反向：生气但陌生 → 不报",
      m.detect_conflicts(snap(emotion="angry", intensity=3, emotion_live=True, tier="陌生"), []) == [])
check("冲突1 反向：熟人但平静 → 不报",
      m.detect_conflicts(snap(emotion="calm", emotion_live=False, tier="熟人"), []) == [])

c = m.detect_conflicts(snap(fatigue=2, mood_items=("白米饭",)), [])
check("冲突2：又馋又烦", len(c) == 1 and "疲劳" in c[0], c)
check("冲突2 反向：馋但不烦",
      m.detect_conflicts(snap(fatigue=0, mood_items=("白米饭",)), []) == [])

c = m.detect_conflicts(snap(fatigue=3, drives={"play": (60.0, 16.0, "active")}), [])
check("冲突3：想说话但烦", any("玩心" in x for x in c), c)
check("冲突3 反向：烦但玩心低于基线",
      not any("玩心" in x for x in m.detect_conflicts(
          snap(fatigue=3, drives={"play": (5.0, 16.0, "sated")}), [])))

c = m.detect_conflicts(snap(reactance=m.REACTANCE_BASE + 20, emotion="happy",
                            intensity=2, emotion_live=True), [])
check("冲突4：逆反高但心情好", any("逆反" in x for x in c), c)
check("冲突4 反向：逆反高且生气 → 不报",
      not any("逆反" in x for x in m.detect_conflicts(
          snap(reactance=m.REACTANCE_BASE + 20, emotion="angry", intensity=2,
               emotion_live=True), [])))

c = m.detect_conflicts(snap(avoid=True, drives={"connection": (60.0, 16.0, "active")}), [])
check("冲突5：避让中但连接高", any("避让" in x for x in c), c)
check("冲突5 反向：避让但连接低",
      not any("避让" in x for x in m.detect_conflicts(
          snap(avoid=True, drives={"connection": (5.0, 16.0, "sated")}), [])))

c = m.detect_conflicts(snap(selfaware_fails=2, emotion="excited", intensity=2,
                            emotion_live=True), [])
check("冲突6：能力失败但情绪高涨", any("能力失败" in x for x in c), c)
check("冲突6 反向：能力失败且平静",
      not any("能力失败" in x for x in m.detect_conflicts(
          snap(selfaware_fails=2, emotion_live=False), [])))

many = m.scan_parts([FakePart("<a_%s>x</a_%s>" % (i, i)) for i in range(9)])
c = m.detect_conflicts(neutral, many)
check("冲突7：块多岛少", any("无状态注入" in x for x in c), c)
c = m.detect_conflicts(rich, [])
check("冲突7 反向：岛多零注入", any("零注入" in x for x in c), c)
check("冲突7 不误报：块少岛多",
      not any("无状态注入" in x for x in m.detect_conflicts(rich, many[:2])))

# ---------------------------------------------------------------- 草稿预算
full = m.render_draft(rich, 900)
check("草稿：有标签", full.startswith("<mind_state>") and full.endswith("</mind_state>"), full)
check("草稿：不含内部数值机制",
      "REACTANCE" not in full and "magnitude" not in full, full)
check("草稿：不含插件字样", "插件" not in full, full)
check("草稿：疲劳优先给分寸", "短一点" in m.render_draft(snap(fatigue=2)), m.render_draft(snap(fatigue=2)))
tiny = m.render_draft(rich, 60)
check("草稿：预算 60 时只留一条且不超预算", len(tiny) <= 60, (len(tiny), tiny))
check("草稿：预算极小也返回合法块",
      m.render_draft(rich, 5).startswith("<mind_state>"), m.render_draft(rich, 5))
check("草稿：超预算丢最小的不截断半句",
      "…" not in tiny and ">" in tiny)

line = m.snapshot_line(rich, many, full, ["a", "b"])
check("日志行：带岛数与冲突数",
      "岛=11/11" in line and "冲突=2" in line and "注入块=9" in line, line)

# ---------------------------------------------------------------- 只读读取器（真实 fixture）
def build_fixture(root: Path):
    p = {}
    p["emotion"] = root / "dsh_emotion_state.json"
    p["emotion"].write_text(json.dumps({"g": {
        "emotion": "curious", "intensity": 2, "directed": True,
        "expires_at": NOW + 600, "evidence": "x"}}), encoding="utf-8")
    p["interest"] = root / "dsh_interest_state.json"
    p["interest"].write_text(json.dumps({
        "mood": {"items": ["白米饭", "蛋糕"]},
        "hot": {"g": {"火锅": 1.4, "复读": 0.2}}}), encoding="utf-8")
    p["fatigue"] = root / "dsh_fatigue_state.json"
    p["fatigue"].write_text(json.dumps({"groups": {"g": [
        {"key": "k", "replies": [{"uid": "u", "ts": NOW}, {"uid": "u", "ts": NOW}]}]}}),
        encoding="utf-8")
    p["selfworth"] = root / "selfworth.json"
    p["selfworth"].write_text(json.dumps({"ledger": {
        "u": {"kinds": {"FREELOAD": 2}, "last": NOW - 30}}}), encoding="utf-8")
    p["initiate"] = root / "dsh_initiate_state.json"
    p["initiate"].write_text(json.dumps({"g": {"day": "2026-09-13", "count": 2}}),
                             encoding="utf-8")
    p["proactive"] = root / "dsh_proactive_state.json"
    p["proactive"].write_text(json.dumps({"g": {"day": "2026-09-13", "count": 1}}),
                              encoding="utf-8")

    def db(name, script):
        path = root / name
        con = sqlite3.connect(str(path))
        con.executescript(script)
        con.close()
        return str(path)

    p["desire"] = db("dsh_desire.db", """
        CREATE TABLE drive_state(group_id TEXT, drive TEXT, intensity REAL,
            baseline REAL, phase TEXT, updated_at REAL);
        INSERT INTO drive_state VALUES('g','play',40.0,16.0,'active',%f);
        INSERT INTO drive_state VALUES('g','fear',60.0,8.0,'active',%f);
    """ % (NOW - 7200, NOW))
    p["social"] = db("dsh_social.db", """
        CREATE TABLE relations(group_id TEXT, user_id TEXT, affinity REAL,
            trust REAL, manual_tier TEXT, avoid_until REAL);
        INSERT INTO relations VALUES('g','u',60.0,50.0,'',0);
    """)
    p["agency"] = db("dsh_agency.db", """
        CREATE TABLE agency_state(group_id TEXT, user_id TEXT, reactance REAL,
            updated_at REAL);
        INSERT INTO agency_state VALUES('g','u',38.0,%f);
    """ % NOW)
    p["awareness"] = db("dsh_awareness.db", """
        CREATE TABLE event(id INTEGER PRIMARY KEY, ts REAL, group_id TEXT,
            kind TEXT, text TEXT);
        INSERT INTO event(ts,group_id,kind,text) VALUES(%f,'g','poke','x');
        INSERT INTO event(ts,group_id,kind,text) VALUES(%f,'g','recall','y');
    """ % (NOW - 60, NOW - 120))
    p["selfaware"] = db("dsh_selfaware.db", """
        CREATE TABLE sense_event(id INTEGER PRIMARY KEY, ts REAL, capability TEXT,
            status TEXT, success INTEGER, latency_ms INTEGER, source TEXT,
            detail TEXT, fingerprint TEXT);
        INSERT INTO sense_event(ts,capability,status,success,source,detail,fingerprint)
            VALUES(%f,'chat','failed',0,'x','','f1');
        INSERT INTO sense_event(ts,capability,status,success,source,detail,fingerprint)
            VALUES(%f,'tts','available',1,'x','','f2');
    """ % (NOW - 60, NOW - 30))
    return p


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    p = build_fixture(root)
    env = {
        "DSH_MIND_EMOTION_STATE": str(p["emotion"]),
        "DSH_MIND_INTEREST_STATE": str(p["interest"]),
        "DSH_MIND_FATIGUE_STATE": str(p["fatigue"]),
        "DSH_MIND_SELFWORTH_STATE": str(p["selfworth"]),
        "DSH_MIND_INITIATE_STATE": str(p["initiate"]),
        "DSH_MIND_PROACTIVE_STATE": str(p["proactive"]),
        "DSH_MIND_DESIRE_DB": p["desire"],
        "DSH_MIND_SOCIAL_DB": p["social"],
        "DSH_MIND_AGENCY_DB": p["agency"],
        "DSH_MIND_AWARENESS_DB": p["awareness"],
        "DSH_MIND_SELFAWARE_DB": p["selfaware"],
    }
    reader = m.MindReader(env=env)
    snap = reader.read("g", "u", NOW, extra=lambda n: 2 if n == "dsh_fatigue_level" else None)

    check("读取：情绪活着且指向我", snap.emotion_live and snap.directed and snap.intensity == 2)
    check("读取：欲望衰减后 play≈28", abs(snap.drives["play"][0] - 28.0) < 0.01, snap.drives)
    check("读取：关系档位按阈值", snap.tier == "亲近", snap.tier)
    check("读取：逆反 6h 内不衰减", abs(snap.reactance - 38.0) < 0.01, snap.reactance)
    check("读取：疲劳优先用 event extra",
          snap.fatigue == 2 and snap.fatigue_from == "extra", (snap.fatigue, snap.fatigue_from))
    check("读取：兴趣今日馋", snap.mood_items == ("白米饭", "蛋糕"), snap.mood_items)
    check("读取：兴趣热度按热度排序", snap.hot[0][0] == "火锅", snap.hot)
    check("读取：自身利益命中", snap.selfworth_kinds == ("FREELOAD",), snap.selfworth_kinds)
    check("读取：群感知事件分类计数",
          dict(snap.awareness_recent) == {"poke": 1, "recall": 1}, snap.awareness_recent)
    check("读取：自我认知只数失败", snap.selfaware_fails == 1, snap.selfaware_fails)
    check("读取：行动额度两条链", dict(snap.action_quota) == {"initiate": 2, "proactive": 1},
          snap.action_quota)
    check("读取：记录了不可观测的脊梁", any("脊梁" in x for x in snap.unreadable), snap.unreadable)
    check("读取：11 个岛全部报到", len(m.islands(snap)) == 11, m.islands(snap))
    check("读取：没有读取失败", not snap.errors, snap.errors)
    check("读取：sources 有名有姓", len(snap.sources) >= 10, snap.sources)

    # extra 拿不到 → 退回粗代理
    snap2 = reader.read("g", "u", NOW, extra=lambda n: None)
    check("读取：无 extra 时退回疲劳代理",
          snap2.fatigue == 2 and snap2.fatigue_from == "proxy", (snap2.fatigue, snap2.fatigue_from))

    # extra 抛异常也不许把读取带崩
    def boom(_n):
        raise RuntimeError("bad adapter")
    snap3 = reader.read("g", "u", NOW, extra=boom)
    check("读取：extra 抛异常仍返回快照", snap3.emotion_live, snap3.errors)

    # 未知群 → 群内维度必须是中性；今日馋/能力失败是**全局**状态（dsh-interest
    # 的「今日馋」本来就不分群），会照旧亮着，这是对的，不是 bug。
    snap4 = reader.read("nope", "nobody", NOW)
    scoped = {"emotion", "desire", "relation", "reactance", "fatigue",
              "awareness", "action"}
    check("读取：未知群时群内维度全中性",
          not (scoped & {i["key"] for i in m.islands(snap4)}), m.islands(snap4))
    check("读取：未知群给出原因", any("无该群" in e for e in snap4.errors), snap4.errors)

    # 路径全坏 → 只记 errors（JSON 类状态缺文件按中性处理，不算错，
    # 与 dsh-guard 的读法一致；SQLite 类必须留痕）
    broken = m.MindReader(env={k: str(root / "missing") for k in env})
    snap5 = broken.read("g", "u", NOW)
    check("读取：全坏路径不抛且 SQLite 全留痕", len(snap5.errors) >= 5, snap5.errors)
    check("读取：全坏路径仍返回合法快照", snap5.emotion == "calm", snap5)

    # 坏 JSON 不许抛
    bad = root / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    env_bad = dict(env)
    env_bad["DSH_MIND_EMOTION_STATE"] = str(bad)
    snap6 = m.MindReader(env=env_bad).read("g", "u", NOW)
    check("读取：坏 JSON 不抛", snap6.emotion == "calm", snap6.errors)

    # 只读约束：绝不能改到 fixture
    before = {str(x): x.read_bytes() for x in root.iterdir() if x.is_file()
              and x.suffix != ".db"}
    reader.read("g", "u", NOW)
    after = {str(x): x.read_bytes() for x in root.iterdir() if x.is_file()
             and x.suffix != ".db"}
    check("只读约束：JSON 一个字节都没动", before == after)

print("MIND_LOGIC_TEST_OK passed=%d failed=%d" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
