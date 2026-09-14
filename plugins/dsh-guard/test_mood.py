# test_mood.py —— dsh-guard 动态判罚层（mood_logic）纯逻辑测试
#
# 不依赖 astrbot，容器内可直跑：
#   docker cp mood_logic.py test_mood.py astrbot:/tmp/ && docker exec astrbot \
#     python3 /tmp/test_mood.py
#
# 覆盖：
#   A 阈值参数（含配错时的回退）      B 情绪 -> 宽容度
#   C 欲望（含 sated/cooldown 门控）  D 关系档位 / 逆反 / 疲劳
#   E 总宽容度封顶                    F 严重度一档调整（含「不越界」）
#   G 警告门槛 / 时长倍率 / 硬类不吃心情
#   H 状态读取（真建 SQLite + JSON，含失败回退与缓存）
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mood_logic as M  # noqa: E402

fails = []


def check(name, got, want):
    if got == want:
        print("  PASS %s" % name)
    else:
        print("  FAIL %s\n       got=%r\n       want=%r" % (name, got, want))
        fails.append(name)


def near(name, got, want, eps=0.001):
    check(name, abs(float(got) - float(want)) <= eps, True)


def mood(**kw):
    base = dict(emotion="calm", intensity=0, emotion_live=False)
    base.update(kw)
    return M.Mood(**base)


def drives(**kw):
    """drive -> (eff, baseline, phase)。"""
    return {k: (v, 16.0, "latent") for k, v in kw.items()}


print("A. 阈值参数")
T = M.Tunables()
check("A1 默认权重 1.0", T.weight, 1.0)
check("A2 默认最多推一档", T.max_step, 1)
check("A3 权重为 0 时宽容度恒为 0", M.leniency(mood(emotion="angry", intensity=3,
                                                emotion_live=True),
                                          M.Tunables(weight=0.0))[0], 0.0)
check("A4 权重上限被夹到 3", M.Tunables.from_env({"DSH_GUARD_MOOD_WEIGHT": "99"}).weight, 3.0)
check("A5 阈值写反时回退到默认", M.Tunables.from_env({
    "DSH_GUARD_MOOD_MILD_LENIENT_AT": "2.0",   # 弱宽容线跑到弱严格线右边 = 写反了
}).len_mild_strict, 0.5)
check("A5b 默认严格线是 -1.0（一个强信号就够推动一档）", M.Tunables().len_lenient, -1.0)
check("A6 参数是垃圾字符串时不炸", M.Tunables.from_env(
    {"DSH_GUARD_MOOD_WEIGHT": "abc"}).weight, 1.0)
check("A7 max_step 夹在 0~2", M.Tunables.from_env({"DSH_GUARD_MOOD_MAX_STEP": "9"}).max_step, 2)

print("B. 情绪 -> 宽容度")
_len, tags = M.leniency(mood(emotion="calm", intensity=3, emotion_live=False))
check("B1 平静没贡献", (_len, tags), (0.0, []))
check("B2 过期情绪不算（emotion_live=False）",
      M.leniency(mood(emotion="angry", intensity=3, emotion_live=False))[0], 0.0)
_len, tags = M.leniency(mood(emotion="angry", intensity=3, emotion_live=True))
near("B3 强度3的生气 = 严格 1.0", _len, -1.0)
check("B4 标签可读", tags, ["angry3"])
_len, _ = M.leniency(mood(emotion="angry", intensity=1, emotion_live=True))
near("B5 强度1的生气只有一半", _len, -0.5)
_len, tags = M.leniency(mood(emotion="angry", intensity=3, emotion_live=True, directed=True))
near("B6 被点名骂过 -> 更严格一点", _len, -1.2)
check("B7 指向信息在标签里", "·指向我" in tags[0], True)
near("B8 开心 -> 更宽容", M.leniency(mood(emotion="happy", intensity=3,
                                       emotion_live=True))[0], 0.6)
near("B9 低落 -> 略宽容（不想管事）", M.leniency(mood(emotion="sad", intensity=3,
                                                 emotion_live=True))[0], 0.3)
near("B10 惊讶是 0", M.leniency(mood(emotion="surprised", intensity=3,
                                    emotion_live=True))[0], 0.0)
near("B11 未知情绪不炸且为 0", M.leniency(mood(emotion="furious", intensity=3,
                                            emotion_live=True))[0], 0.0)

print("C. 欲望（阈值是基线上浮的量）")
near("C1 玩心高于基线 30 -> 宽容（单路也被 0.5 的封顶压住）",
     M.leniency(mood(drives=drives(play=50.0)))[0], 0.5)
near("C2 玩心没到阈值 -> 0", M.leniency(mood(drives=drives(play=40.0)))[0], 0.0)
near("C3 戒备高于基线 10 -> 严格", M.leniency(mood(drives=drives(fear=30.0)))[0], -0.5)
check("C4 sated 的 drive 不算数（与 dsh-agency 同款门控）",
      M.leniency(M.Mood(drives={"play": (50.0, 16.0, "sated")}))[0], 0.0)
check("C5 cooldown 的 drive 不算数",
      M.leniency(M.Mood(drives={"fear": (40.0, 8.0, "cooldown")}))[0], 0.0)
near("C6 欲望总和封顶 0.5（生产校准：常驻玩心不该单独改变动不动手）",
     M.leniency(mood(drives=drives(play=99.0, connection=99.0, rest=99.0)))[0], 0.5)
near("C7 严格方向也封顶 -0.5", M.leniency(mood(drives=drives(fear=99.0,
                                                        nociception=99.0)))[0], -0.5)
# 这条是拿生产数据校准出来的边界：机器人长期是玩心 64 + 想搭话 100，
# 如果欲望能推动一档，就会对**所有人**恒定降一档（骂战直接变成放过且不留记录）
check("C9 欲望单路只改门槛、不改严重度", M.severity_step(2, 0.5), 2)
check("C10 欲望单路确实改了门槛", M.warn_need(2, 0.5), 3)
check("C8 无数据不炸", M.leniency(M.Mood())[0], 0.0)

print("D. 关系 / 逆反 / 疲劳")
near("D1 亲近 -> 宽容", M.leniency(M.Mood(tier="亲近"))[0], 1.0)
near("D2 熟人 -> 半档", M.leniency(M.Mood(tier="熟人"))[0], 0.5)
near("D3 疏离 -> 严格", M.leniency(M.Mood(tier="疏离"))[0], -0.5)
near("D4 避让 -> 更严格", M.leniency(M.Mood(tier="避让"))[0], -1.0)
near("D5 高信任 +0.3", M.leniency(M.Mood(tier="陌生", trust=50))[0], 0.3)
near("D6 低信任 -0.3", M.leniency(M.Mood(tier="陌生", trust=5))[0], -0.3)
check("D7 信任基线 20 无贡献", M.leniency(M.Mood(tier="陌生", trust=20))[0], 0.0)
near("D8 逆反高出基线 8~22 -> -0.35", M.leniency(M.Mood(reactance=18.0))[0], -0.35)
near("D9 逆反高出 22+ -> -0.7", M.leniency(M.Mood(reactance=35.0))[0], -0.7)
check("D10 逆反在基线 -> 0", M.leniency(M.Mood(reactance=8.0))[0], 0.0)
near("D11 疲劳 2 级 -0.35", M.leniency(M.Mood(fatigue=2))[0], -0.35)
near("D12 疲劳 3 级 -0.7", M.leniency(M.Mood(fatigue=3))[0], -0.7)
check("D13 疲劳 1 级不算", M.leniency(M.Mood(fatigue=1))[0], 0.0)

print("E. 总宽容度")
near("E1 叠加：生气(-1.0) + 玩心(封顶 +0.5) → -0.5（生气仍比玩心重）",
     M.leniency(mood(emotion="angry", intensity=3, emotion_live=True,
                     drives=drives(play=99.0)))[0], -0.5)
check("E2 封顶 +3", M.leniency(mood(emotion="happy", intensity=3, emotion_live=True,
                                   tier="亲近", trust=60))[0] <= 3.0, True)
check("E3 封顶 -3", M.leniency(mood(emotion="angry", intensity=3, emotion_live=True,
                                   directed=True, tier="避让", trust=1,
                                   reactance=60.0, fatigue=3))[0] >= -3.0, True)
_len, tags = M.leniency(mood(emotion="angry", intensity=3, emotion_live=True,
                            tier="避让", fatigue=3, reactance=35.0))
check("E4 因子标签能说清是谁推严的",
      all(t in tags for t in ("angry3", "避让", "烦3")), True)

print("F. 严重度调整（心情最多推一档，且不越界）")
check("F1 严格：sev1 -> 2（放过变警告）", M.severity_step(1, -1.0), 2)
check("F2 严格：sev0 不动（没事就是没事）", M.severity_step(0, -1.0), 0)
check("F3 严格：sev2 -> 3（封顶不超 3）", M.severity_step(2, -1.0), 3)
check("F4 严格：sev3 已在顶", M.severity_step(3, -1.0), 3)
check("F5 宽容：sev2 -> 1（这次算了）", M.severity_step(2, 1.0), 1)
check("F6 宽容：sev3 -> 2（先警告）", M.severity_step(3, 1.0), 2)
check("F7 宽容：sev1 不动", M.severity_step(1, 1.0), 1)
check("F8 中间区不动", M.severity_step(2, 0.2), 2)
check("F9 边界值 -1.0 生效", M.severity_step(1, -1.0), 2)
check("F10 边界值 -0.99 不生效（弱信号不改严重度）", M.severity_step(1, -0.99), 1)
check("F11 max_step=0 时完全不动", M.severity_step(1, -3.0, M.Tunables(max_step=0)), 1)
check("F12 权重 0 时完全不动", M.severity_step(1, -3.0, M.Tunables(weight=0.0)), 1)
check("F13 max_step=2 时推两档", M.severity_step(1, -3.0, M.Tunables(max_step=2)), 3)
check("F14 两档也不越过 3", M.severity_step(2, -3.0, M.Tunables(max_step=2)), 3)
# ★ 最重要的一条：心情抬出来的只能警告，不能禁言 ★
check("F15 放过被抬成要动手 -> 标记为 raised", M.raised_only(1, 2), True)
check("F16 sev0 不可能被抬（severity_step 不动 0）",
      M.raised_only(0, M.severity_step(0, -3.0)), False)
check("F17 本来够格的判定不算 raised（禁言照常）", M.raised_only(2, 3), False)
check("F18 原样不动不算 raised", M.raised_only(2, 2), False)

print("G. 警告门槛 / 时长 / 硬类")
check("G1 严格：门槛降一档", M.warn_need(3, -1.5), 2)
check("G2 严格：门槛最低 1", M.warn_need(1, -1.5), 1)
check("G3 宽容：门槛升一档", M.warn_need(2, 1.5), 3)
check("G4 中间区原样", M.warn_need(2, 0.0), 2)
check("G5 max_step=0 原样", M.warn_need(2, 1.5, M.Tunables(max_step=0)), 2)
# 两档设计：弱信号（±0.5~±1.0）只改门槛，不改严重度
check("G5a 弱信号改门槛（-0.6）", M.warn_need(2, -0.6), 1)
check("G5b 弱信号不改严重度（-0.6）", M.severity_step(1, -0.6), 1)
check("G5c 弱信号改门槛（+0.6）", M.warn_need(2, 0.6), 3)
check("G5d 弱信号不改严重度（+0.6）", M.severity_step(2, 0.6), 2)
near("G6 严格时长 ×1.225", M.duration_factor(-1.5), 1.225)
near("G7 宽容时长 ×0.775", M.duration_factor(1.5), 0.775)
near("G8 极值被夹在 ×1.5（满值也只是 ×1.45）", M.duration_factor(-9.0), 1.5)
near("G9 极值被夹在 ×0.5", M.duration_factor(9.0), 0.5)
check("G10 严格把 300 秒拉到 345", M.apply_duration(300, -1.0), 345)
check("G11 宽容把 300 秒缩到 255", M.apply_duration(300, 1.0), 255)
check("G12 时长仍不超硬上限", M.apply_duration(1800, -3.0, cap=1800), 1800)
check("G13 时长有下限（不会被心情砍到 1 秒）", M.apply_duration(60, 3.0), 60)
check("G14 硬类不吃心情（违法/广告/色情）",
      M.apply_duration(300, -1.5, hard=True), 300)
check("G15 刷屏一天不吃心情（用户明确要的惩罚）",
      M.apply_duration(86400, 1.5, bypass_cap=True), 86400)
check("G16 权重 0 时时长原样", M.apply_duration(300, -1.5, tun=M.Tunables(weight=0.0)), 300)

print("G2. 类型分类：哪些不吃心情")
check("G2a 违法是硬类", M.classify({"illegal": True}), ("illegal", True))
check("G2b 广告是硬类", M.classify({"ad": True}), ("ad", True))
check("G2c 色情索要是硬类", M.classify({"nsfw": True}), ("nsfw", True))
check("G2d 围攻是硬类（即使是 attack）", M.classify({"attack": True, "pileon": True}),
      ("pileon", True))
check("G2e 骂战是软类（心情生效）", M.classify({"attack": True}), ("attack", False))
check("G2f 政治是软类（心情生效）", M.classify({"politics": True}), ("politics", False))
check("G2g 类型优先级沿用 decide 的顺序",
      M.classify({"politics": True, "nsfw": True}), ("politics", False))
check("G2h 没有类型 = 硬类（不动）", M.classify({}), ("", True))

print("H. 状态读取（真建 SQLite + JSON）")
tmp = Path(tempfile.mkdtemp())
now = time.time()
gid, uid = "100000001", "2001"
E_UP = {"scene": "群级情绪，读不到就当中性"}


def write_emotion(path, **kw):
    row = {"emotion": "calm", "intensity": 0, "expires_at": 0.0, "directed": False}
    row.update(kw)
    path.write_text(json.dumps({gid: row}, ensure_ascii=False), encoding="utf-8")


def make_desire(path):
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE drive_state(group_id TEXT, drive TEXT, intensity REAL,
        baseline REAL, phase TEXT, updated_at REAL, active_since REAL DEFAULT 0,
        cooldown_until REAL DEFAULT 0, expressions_today INTEGER DEFAULT 0,
        expression_day TEXT DEFAULT '', last_signal TEXT DEFAULT '')""")
    con.execute("INSERT INTO drive_state VALUES (?,?,?,?,?,?,0,0,0,'','food_topic')",
                (gid, "play", 80.0, 16.0, "latent", now))
    con.execute("INSERT INTO drive_state VALUES (?,?,?,?,?,?,0,0,0,'','x')",
                (gid, "fear", 60.0, 8.0, "latent", now))
    con.execute("INSERT INTO drive_state VALUES (?,?,?,?,?,?,0,0,0,'','x')",
                (gid, "appetite", 99.0, 14.0, "sated", now))   # sated 不算
    con.commit()
    con.close()


def make_social(path, affinity, trust, manual=""):
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE relations(group_id TEXT, user_id TEXT, affinity REAL,
        trust REAL, affinity_updated_at REAL, avoid_until REAL, manual_tier TEXT,
        opted_out INTEGER, updated_at REAL)""")
    con.execute("INSERT INTO relations VALUES (?,?,?,?,?,0,?,0,?)",
                (gid, uid, affinity, trust, now, manual, now))
    con.commit()
    con.close()


def make_agency(path, reactance):
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE agency_state(group_id TEXT, user_id TEXT, reactance REAL,
        updated_at REAL, last_reason TEXT, last_fingerprint TEXT, last_message_at REAL,
        repeat_count INTEGER)""")
    con.execute("INSERT INTO agency_state VALUES (?,?,?,?,'x','f',0,0)",
                (gid, uid, reactance, now))
    con.commit()
    con.close()


def env_for(emo=None, desire=None, social=None, agency=None, fatigue=None, **extra):
    e = {
        "DSH_GUARD_MOOD": "1",
        "DSH_GUARD_MOOD_EMOTION": str(emo or (tmp / "none_emotion.json")),
        "DSH_GUARD_MOOD_DESIRE_DB": str(desire or (tmp / "none_desire.db")),
        "DSH_GUARD_MOOD_SOCIAL_DB": str(social or (tmp / "none_social.db")),
        "DSH_GUARD_MOOD_AGENCY_DB": str(agency or (tmp / "none_agency.db")),
        "DSH_GUARD_MOOD_FATIGUE": str(fatigue or (tmp / "none_fatigue.json")),
        "DSH_GUARD_MOOD_CACHE": "0",
    }
    e.update(extra)
    return e


emo_p = tmp / "emotion.json"
write_emotion(emo_p, emotion="angry", intensity=3, directed=True, expires_at=now + 600)
des_p = tmp / "desire.db"
make_desire(des_p)
soc_p = tmp / "social.db"
make_social(soc_p, 60.0, 50.0, "亲近")
age_p = tmp / "agency.db"
make_agency(age_p, 40.0)
fat_p = tmp / "fatigue.json"
fat_p.write_text(json.dumps({"version": 1, "groups": {gid: [
    {"key": "x", "sample": "x", "last_at": now,
     "replies": [{"uid": uid, "ts": now - 10},
                 {"uid": uid, "ts": now - 20},
                 {"uid": uid, "ts": now - 30},
                 {"uid": "999", "ts": now - 40}]}]}}, ensure_ascii=False), encoding="utf-8")

r = M.MoodReader(env=env_for(emo_p, des_p, soc_p, age_p, fat_p))
m = r.read(gid, uid, now)
check("H1 情绪读到了", (m.emotion, m.intensity, m.directed, m.emotion_live),
      ("angry", 3, True, True))
check("H2 玩心/戒备都读到了", (round(m.drives["play"][0]), round(m.drives["fear"][0])),
      (80, 60))
check("H3 关系档位=亲近", (m.tier, round(m.trust)), ("亲近", 50))
near("H4 逆反读到并衰减", m.reactance, 40.0, eps=1.0)
check("H5 疲劳按同一人的追问次数分档（3 次 -> 3 级）", m.fatigue, 3)
check("H6 来源清单可读", set(m.sources), {"关系", "情绪", "疲劳", "欲望", "逆反"})
check("H7 全都能读到时不报错", m.errors, ())
_len, tags = M.leniency(m, M.Tunables())
check("H8 五路叠加后仍然封顶在 -3（不允许无限严）", _len >= -3.0, True)
check("H9 这次叠加的净效果是严格", _len < -0.5, True)
# 具体值固定下来，任何一路权重被改动都会在这里现形：
near("H10 五路叠加的具体值 = -1.0（生气-1.2 + 欲望+0.3 + 亲近+1.3 "
     "+ 逆反-0.7 + 疲劳-0.7）", _len, -1.0)

print("H2. 读不到 / 读坏了一律中性（不能成为新的静默失效点）")
r0 = M.MoodReader(env=env_for())
m0 = r0.read(gid, uid, now)
check("H2a 什么都没装 -> 中性", (m0.emotion, m0.tier, m0.reactance, m0.fatigue),
      ("calm", "陌生", 8.0, 0))
check("H2b 中性宽容度 = 0（等于原来的固定规则）",
      M.leniency(m0, M.Tunables())[0], 0.0)
check("H2c 缺失的来源记进 errors 以便排查",
      all("读失败" in x or "未读到" in x for x in m0.errors) and len(m0.errors) >= 4, True)
bad = tmp / "bad.json"
bad.write_text("{ this is not json", encoding="utf-8")
bad_db = tmp / "bad.db"
bad_db.write_text("not a database at all", encoding="utf-8")
rb = M.MoodReader(env=env_for(bad, bad_db, bad_db, bad_db, bad))
mb = rb.read(gid, uid, now)
check("H2d 坏 JSON + 坏库也不抛异常", (mb.emotion, mb.tier), ("calm", "陌生"))
check("H2e 关掉总开关直接中性", M.MoodReader(env=env_for(
    emo_p, des_p, soc_p, age_p, fat_p, **{"DSH_GUARD_MOOD": "0"})).read(
        gid, uid, now).emotion, "calm")
# 过期情绪 = 中性（TTL 到了就不该再影响判罚）
exp_p = tmp / "expired.json"
write_emotion(exp_p, emotion="angry", intensity=3, expires_at=now - 10)
check("H2f TTL 过期后的情绪不参与",
      M.MoodReader(env=env_for(exp_p)).read(gid, uid, now).emotion_live, False)
# 别人的关系 / 逆反不该算到这个人头上
check("H2g 别的用户查不到关系就是陌生",
      M.MoodReader(env=env_for(social=soc_p)).read(gid, "8888", now).tier, "陌生")
check("H2h 别的用户查不到逆反就是基线",
      M.MoodReader(env=env_for(agency=age_p)).read(gid, "8888", now).reactance, 8.0)

print("H3. 缓存（同一波消息不重复开库）")
rc = M.MoodReader(env=env_for(emo_p, des_p, soc_p, age_p, fat_p,
                              **{"DSH_GUARD_MOOD_CACHE": "60"}))
first = rc.read(gid, uid, now)
emo_p.write_text(json.dumps({gid: {"emotion": "happy", "intensity": 3,
                                   "expires_at": now + 600, "directed": False}}),
                 encoding="utf-8")
check("H3a 缓存期内不重读", rc.read(gid, uid, now).emotion, first.emotion)
check("H3b 缓存过期后重读", rc.read(gid, uid, now + 61).emotion, "happy")
check("H3c 关掉缓存时立刻重读",
      M.MoodReader(env=env_for(emo_p)).read(gid, uid, now).emotion, "happy")

print("H4. 摘要文案（/禁言状态 和日志都用它）")
text = M.describe(m, -1.2, ["angry3·指向我", "戒备60"])
check("H4a 带宽容度和因子", ("len=-1.2" in text and "angry3" in text), True)
check("H4b 带来源", "欲望" in text, True)
check("H4c 失败也在文案里", "失败:" in M.describe(m0, 0.0, []), True)

print()
if fails:
    print("FAILED %d: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL PASS")
