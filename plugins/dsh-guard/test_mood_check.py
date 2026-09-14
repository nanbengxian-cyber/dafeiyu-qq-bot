# test_mood_check.py —— 动态判罚层的**集成**测试：跑真实的 check() 处理器
#
# 与 test_mood.py 的分工：那边测纯函数（宽容度/档位/时长），这边测
# 「插件真的这么判吗」—— 用假事件驱动 main.Main.check()，把 LLM 判定桩掉、
# 影子模式打开、群号限定在不存在的测试群，所以**不花一分钱 token、
# 也绝不碰真群**。
#
# 只在这台机器的 astrbot 容器里跑（需要 astrbot / astrbot.api）：
#   sudo docker cp main.py mood_logic.py dsh_link.py test_mood_check.py astrbot:/tmp/
#   sudo docker exec astrbot python3 /tmp/test_mood_check.py
# （dsh_link.py 要先放到上一级目录，脚本自己会把 /tmp 的父级加进 sys.path）
import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGINS_ROOT = HERE.parent
sys.path.insert(0, str(PLUGINS_ROOT))
sys.path.insert(0, str(HERE))

TEST_GID = "999999001"          # 不存在的测试群：即使影子模式被误关也不会伤到真群
TEST_UID = "2001"
fails = []


def check(name, got, want):
    if got == want:
        print("  PASS %s" % name)
    else:
        print("  FAIL %s\n       got=%r\n       want=%r" % (name, got, want))
        fails.append(name)


def near(name, got, want, eps=0.001):
    check(name, abs(float(got) - float(want)) <= eps, True)


# ---------------------------------------------------------------- 假状态
tmp = Path(tempfile.mkdtemp())
now = time.time()
EMO = tmp / "emotion.json"
FAT = tmp / "fatigue.json"
DES, SOC, AGE = tmp / "desire.db", tmp / "social.db", tmp / "agency.db"


def write_emotion(emotion="calm", intensity=0, directed=False, expires=0.0):
    EMO.write_text(json.dumps({TEST_GID: {
        "emotion": emotion, "intensity": intensity, "directed": directed,
        "expires_at": expires, "started_at": now, "evidence": "", "source": "test",
    }}, ensure_ascii=False), encoding="utf-8")


def make_desire(play=16.0, fear=8.0):
    DES.unlink(missing_ok=True)
    con = sqlite3.connect(str(DES))
    con.execute("""CREATE TABLE drive_state(group_id TEXT, drive TEXT, intensity REAL,
        baseline REAL, phase TEXT, updated_at REAL, active_since REAL DEFAULT 0,
        cooldown_until REAL DEFAULT 0, expressions_today INTEGER DEFAULT 0,
        expression_day TEXT DEFAULT '', last_signal TEXT DEFAULT '')""")
    for drive, eff, base in (("play", play, 16.0), ("fear", fear, 8.0)):
        con.execute("INSERT INTO drive_state VALUES (?,?,?,?,?,?,0,0,0,'','test')",
                    (TEST_GID, drive, eff, base, "latent", now))
    con.commit()
    con.close()


def make_social(affinity=0.0, trust=20.0):
    SOC.unlink(missing_ok=True)
    con = sqlite3.connect(str(SOC))
    con.execute("""CREATE TABLE relations(group_id TEXT, user_id TEXT, affinity REAL,
        trust REAL, affinity_updated_at REAL, avoid_until REAL, manual_tier TEXT,
        opted_out INTEGER, updated_at REAL)""")
    con.execute("INSERT INTO relations VALUES (?,?,?,?,?,0,'',0,?)",
                (TEST_GID, TEST_UID, affinity, trust, now, now))
    con.commit()
    con.close()


def make_agency(reactance=8.0):
    AGE.unlink(missing_ok=True)
    con = sqlite3.connect(str(AGE))
    con.execute("""CREATE TABLE agency_state(group_id TEXT, user_id TEXT, reactance REAL,
        updated_at REAL, last_reason TEXT, last_fingerprint TEXT, last_message_at REAL,
        repeat_count INTEGER)""")
    con.execute("INSERT INTO agency_state VALUES (?,?,?,?,'x','f',0,0)",
                (TEST_GID, TEST_UID, reactance, now))
    con.commit()
    con.close()


write_emotion()
FAT.write_text(json.dumps({"version": 1, "groups": {TEST_GID: []}}), encoding="utf-8")
make_desire()
make_social()
make_agency()

# ---------------------------------------------------------------- 假事件/日志
class Rec:
    def __init__(self):
        self.lines = []

    def _add(self, fmt, a):
        try:
            self.lines.append(fmt % a if a else str(fmt))
        except BaseException:
            self.lines.append(str(fmt))

    def info(self, fmt, *a, **k):
        self._add(fmt, a)

    def warning(self, fmt, *a, **k):
        self._add(fmt, a)

    def error(self, fmt, *a, **k):
        self._add(fmt, a)

    def debug(self, *a, **k):
        pass

    def has(self, needle):
        return any(needle in x for x in self.lines)

    def line_with(self, needle):
        for x in self.lines:
            if needle in x:
                return x
        return ""


class FakeBot:
    def __init__(self, role):
        self.role = role
        self.calls = []

    async def call_action(self, action, **kw):
        self.calls.append((action, kw))
        if action == "get_group_member_info":
            return {"role": self.role, "user_id": kw.get("user_id")}
        return {}


class FakeEvent:
    def __init__(self, text, uid=TEST_UID, role="member", at=False):
        self.message_str = text
        self.bot = FakeBot(role)
        self.message_obj = type("M", (), {
            "self_id": "3752949000",
            "raw_message": {"sender": {"role": role}},
        })()
        self.uid, self.gid, self.at = str(uid), TEST_GID, at
        self.sent, self._extra, self.stopped = [], {}, False

    def get_group_id(self):
        return self.gid

    def get_sender_id(self):
        return self.uid

    def get_self_id(self):
        return "3752949000"

    def get_sender_name(self):
        return "阿强"

    def get_messages(self):
        return [type("At", (), {"qq": "3752949000"})()] if self.at else []

    @property
    def unified_msg_origin(self):
        return "aiocqcqq:GroupMessage:%s" % self.gid

    def stop_event(self):
        self.stopped = True

    async def send(self, chain):
        self.sent.append(chain)

    def plain_result(self, text):
        return type("R", (), {"text": text})()

    def get_extra(self, key):
        return self._extra.get(key)

    def set_extra(self, key, value):
        self._extra[key] = value


# ---------------------------------------------------------------- 加载插件
os.environ.update({
    "DSH_GUARD": "1",
    "DSH_GUARD_SHADOW": "1",            # 只判不禁：测试绝不真的禁言
    "DSH_GUARD_GROUPS": TEST_GID,       # 限定测试群，双保险
    "DSH_GUARD_MOOD": "1",
    "DSH_GUARD_MOOD_CACHE": "0",
    "DSH_GUARD_MOOD_EMOTION": str(EMO),
    "DSH_GUARD_MOOD_DESIRE_DB": str(DES),
    "DSH_GUARD_MOOD_SOCIAL_DB": str(SOC),
    "DSH_GUARD_MOOD_AGENCY_DB": str(AGE),
    "DSH_GUARD_MOOD_FATIGUE": str(FAT),
    "DSH_GUARD_MOOD_WARN_CD": "300",
    "DSH_GUARD_BAN_SEC": "300",         # 与生产一致，长短变化才看得出
    "DSH_GUARD_BAN_SEC_HIGH": "600",
    "DSH_GUARD_TIMEOUT": "5",
})
spec = importlib.util.spec_from_file_location("dsh_guard_main", HERE / "main.py")
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)              # noqa: E402
LOG = Rec()
G.logger = LOG                          # 换成可断言的记录器
g_judge = {"result": None}


async def fake_judge(self, umo, name, text, ctx):
    return dict(g_judge["result"])


G.Main._judge = fake_judge
PLUGIN = G.Main(None)
COUNTERS = ("warned", "banned", "shadow_ban", "mood_read", "mood_err", "mood_up",
            "mood_down", "mood_warn", "mood_warn_cd", "mood_ban_sec", "flood_ban")


def reset():
    """每个用例都从**已知的中性状态**开始。

    ★ 踩过的坑 ★ 一开始只清计数器，不清状态文件：C 用例写了 play=99 +
    关系「亲近」，这两项把 E 用例的「生气」抵消掉了（-1.2 +0.8 +1.0 +0.3），
    E/F/J 的断言全崩。用例之间共享状态文件就必须显式归零。
    """
    write_emotion()
    make_desire()
    make_social()
    make_agency()
    FAT.write_text(json.dumps({"version": 1, "groups": {TEST_GID: []}}),
                   encoding="utf-8")
    for k in COUNTERS:
        G._stat[k] = 0
    G._warns.clear()
    G._flood.clear()
    G._flood_strikes.clear()
    G._warn_cd.clear()
    G._last.clear()
    LOG.lines.clear()


def flags(**kw):
    d = {k: False for k in G._BOOLS}
    d.update({"severity": 0, "why": ""})
    d.update(kw)
    return d


# ★ 测试句子的选择很讲究 ★ 必须满足三条才走到「判定 + 动态判罚」那一层：
#   ① 预筛命中（否则连模型都不问）② 不触发直接辱骂代码闸门（那条路不吃心情）
#   ③ 不被 escalate() 代码兜底抬到 sev3（那也是硬类）
# 第一次写这组用例时用了「你他妈就是废物」，它被直接辱骂闸门接走 -> 断言全崩，
# 这类「测试自己写错、却看起来像功能坏了」的坑只能靠这条注释省下一次。
T_SOFT = "你们觉得现在的政策合理吗，我觉得体制有问题"
T_AD = "加我微信，日入过千带你飞"
T_ILLEGAL = "有卖大麻的吗"
T_GROUP = "日本人都该死，都是畜生"


def run(text, result, uid=TEST_UID, role="member"):
    g_judge["result"] = result
    ev = FakeEvent(text, uid=uid, role=role)
    asyncio.run(PLUGIN.check(ev))
    return ev


print("A. 中性状态 = 原来的固定行为（动态层不能改变基线）")
reset()
run(T_SOFT, flags(attack=True, severity=2))
check("A1 首次 sev2 -> 警告", G._stat["warned"], 1)
check("A2 没有禁言", G._stat["shadow_ban"], 0)
check("A3 读了状态", G._stat["mood_read"], 1)
check("A4 中性时不打心情日志（没有因子、没有失败）", LOG.has("心情："), False)
reset()
run(T_SOFT, flags(attack=True, severity=2))
run(T_SOFT, flags(attack=True, severity=2))
check("A5 第二次 sev2 -> 禁言 300 秒（与生产配置一致）",
      (G._stat["warned"], G._stat["shadow_ban"], LOG.has("本该禁 阿强 300 秒")),
      (1, 1, True))
check("A6 时长没被心情改过", G._stat["mood_ban_sec"], 0)

print("B. 生气：态度类变严（一档）")
reset()
write_emotion("angry", 3, directed=True, expires=now + 1800)
run(T_SOFT, flags(attack=True, severity=2))
check("B1 sev2 被抬成 sev3 -> 直接禁", G._stat["shadow_ban"], 1)
check("B2 时长按 ×1.18 变长：600 -> 708", LOG.has("本该禁 阿强 708 秒"), True)
check("B3 时长变化被计数", G._stat["mood_ban_sec"], 1)
check("B4 日志里有可解释的因子", ("angry" in LOG.line_with("心情：")
                              and "·指向我" in LOG.line_with("心情：")), True)
check("B5 记的是更严格方向", (G._stat["mood_down"], G._stat["mood_up"]), (1, 0))

print("C. 心情好 + 关系亲近：态度类变宽")
reset()
write_emotion("happy", 3, expires=now + 1800)
make_desire(play=99.0)
make_social(60.0, 50.0)
run(T_SOFT, flags(attack=True, severity=2))
check("C1 sev2 降到 1 -> 这次算了", (G._stat["warned"], G._stat["shadow_ban"]), (0, 0))
check("C2 记的是更宽容方向", (G._stat["mood_up"], G._stat["mood_down"]), (1, 0))
check("C3 因子可解释（玩心/亲近）",
      ("玩心" in LOG.line_with("心情：") and "亲近" in LOG.line_with("心情：")), True)

print("D. ★ 硬边界：行为类与代码兜底不吃心情 ★")
reset()
write_emotion("angry", 3, directed=True, expires=now + 1800)
run(T_ILLEGAL, flags(illegal=True, severity=3))
check("D1 违法照旧禁（时间不变：600 秒）", LOG.has("本该禁 阿强 600 秒"), True)
check("D2 违法根本不读心情（连状态都不读）", G._stat["mood_read"], 0)
check("D3 时长没被改", G._stat["mood_ban_sec"], 0)
reset()
run(T_GROUP, flags(attack=True, severity=2, why="仇恨"))
check("D4 代码兜底（诅咒家人）抬到 sev3 且不吃心情", LOG.has("本该禁 阿强 600 秒"), True)
check("D5 代码兜底也不读心情", G._stat["mood_read"], 0)
reset()
run(T_AD, flags(ad=True, severity=2))
run(T_AD, flags(ad=True, severity=2))
check("D6 广告是行为类：第一次警告第二次禁 300 秒",
      (G._stat["warned"], LOG.has("本该禁 阿强 300 秒")), (1, True))
check("D7 广告不读心情", G._stat["mood_read"], 0)
# 围攻（pileon）虽然归到 attack 执行，但属于事实类
reset()
run(T_SOFT, flags(attack=True, pileon=True, severity=2))
check("D8 围攻算硬类：不吃心情", G._stat["mood_read"], 0)

print("E. ★ 硬边界：心情只能把「放过」变成「警告」，绝不能变成禁言 ★")
reset()                                                  # 中性状态：sev2 只会警告
run(T_SOFT, flags(attack=True, severity=2))              # 铺垫一次 24h 内的累犯记录
check("E1 铺垫：先有一次警告", G._stat["warned"], 1)
before_ban = G._stat["shadow_ban"]
write_emotion("angry", 3, directed=True, expires=now + 1800)
run(T_SOFT, flags(attack=True, severity=1))              # 本来放过，生气 -> sev2
check("E2 心情抬高只给警告", G._stat["warned"], 2)
check("E3 没有因为翻旧账就禁言", G._stat["shadow_ban"], before_ban)
check("E4 加重出的警告被单独计数", G._stat["mood_warn"], 1)

print("F. 心情加重出的警告有同群冷却（防一串「收着点」刷屏）")
run(T_SOFT, flags(attack=True, severity=1))
check("F1 冷却期内不再警告", G._stat["warned"], 2)
check("F2 冷却挡下被计数", G._stat["mood_warn_cd"], 1)
check("F3 仍然记了心情", G._stat["mood_up"] + G._stat["mood_down"] >= 1, True)
check("F4 冷却期内不记累犯（不能在没警告过的前提下禁人）", len(G._warns), 1)

print("G. 刷屏/违法这类行为闸门不受心情影响（用户明确要的惩罚力度）")
reset()
write_emotion("happy", 3, expires=now + 1800)           # 心情极好也不许打折
G.FLOOD_MAX = 3
for _ in range(3):
    run("晚安", flags(severity=0))     # 刷屏路径在判定之前，result 用不到
check("G1 刷屏禁言照旧 86400 秒（不被心情砍半）",
      LOG.has("本该禁 阿强 86400 秒"), True)
check("G2 刷屏路径不读心情", G._stat["mood_read"], 0)

print("H. 关掉动态层 = 完整回到老行为（用于对照上线效果）")
reset()
write_emotion("angry", 3, directed=True, expires=now + 1800)
G.MOOD_ON = False
run(T_SOFT, flags(attack=True, severity=2))
run(T_SOFT, flags(attack=True, severity=2))
check("H1 sev2 不抬成 sev3（第二次仍是 300 秒而非 708）",
      LOG.has("本该禁 阿强 300 秒"), True)
check("H2 心情一次都没读", G._stat["mood_read"], 0)
G.MOOD_ON = True

print("I. 硬约束仍然兜住（心情碰不到）")
reset()
write_emotion("angry", 3, directed=True, expires=now + 1800)
run(T_SOFT, flags(attack=True, severity=3), role="admin")
check("I1 管理员不禁（即使是 sev3 + 生气）", G._stat["shadow_ban"], 0)
check("I2 记下「对方是管理员」", G._stat["skip_admin"], 1)
reset()
G.WHITELIST.add(TEST_UID)
run(T_SOFT, flags(attack=True, severity=3))
check("I3 白名单不禁", (G._stat["shadow_ban"], G._stat["skip_white"]), (0, 1))
G.WHITELIST.discard(TEST_UID)
reset()
keep = G.GROUPS
G.GROUPS = {"123456"}          # 只盯别的群（空集合的语义是「全群通用」，不能用来测）
run(T_SOFT, flags(attack=True, severity=3))
check("I4 不在限定群里完全不处理",
      (G._stat["skip_group"], list(LOG.lines)), (1, []))
G.GROUPS = keep

print("J. 状态读坏时中性化（动态层不能成为新的静默失效点）")
reset()
EMO.write_text("不是 json", encoding="utf-8")
run(T_SOFT, flags(attack=True, severity=2))
run(T_SOFT, flags(attack=True, severity=2))
check("J1 情绪文件坏了仍按固定规则判（第二次禁 300 秒）",
      LOG.has("本该禁 阿强 300 秒"), True)
check("J2 失败被计数", G._stat["mood_err"] >= 1, True)
check("J3 失败原因写在日志里", "失败:" in LOG.line_with("心情："), True)
write_emotion()

print("K. /禁言状态 能看到动态层的验收指标")
reset()
write_emotion("angry", 3, directed=True, expires=now + 1800)
run(T_SOFT, flags(attack=True, severity=2))
ev = FakeEvent("/禁言状态", role="admin")
out = []


async def collect():
    async for r in PLUGIN.cmd_status(ev):
        out.append(getattr(r, "text", str(r)))


asyncio.run(collect())
text = "\n".join(out)
check("K1 有「动态判罚」一行", "动态判罚：" in text, True)
check("K2 能看到读状态与失败次数", ("读状态" in text and "失败" in text), True)
check("K3 能看到偏向统计", "更宽容" in text and "更严格" in text, True)
check("K4 能看到最近几次心情", "最近几次心情：" in text, True)
check("K5 硬边界写在给管理员看的说明里",
      "不会变成禁言" in text and "不看心情" in text, True)
ev2 = FakeEvent("/禁言状态", role="member")
out2 = []


async def collect2():
    async for r in PLUGIN.cmd_status(ev2):
        out2.append(getattr(r, "text", str(r)))


asyncio.run(collect2())
check("K6 普通群友仍然看不到任何阈值", "动态判罚" not in "\n".join(out2), True)

print()
if fails:
    print("FAILED %d: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL PASS")
