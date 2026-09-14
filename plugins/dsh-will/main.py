# -*- coding: utf-8 -*-
"""dsh-will —— 意志的执行层（被踩线时真的做点什么，而不是只说点什么）。

═══════════════════════════════════════════════════════════════════
一、起因（群主原话 2026-09-13）
═══════════════════════════════════════════════════════════════════

    「机器人要学会真正的反抗，而不是光说说，他现在就是缺了这一点的真的真实感」

「光说说」不是感觉问题，是**三层结构问题**，每一层都能拿今天的生产数据指认：

  1. **入口几乎全关**。拿今天群里 37 条真实消息回放 `dsh-agency.assess()`：
     「傻」「滚」「闭嘴」「妈妈」「狗修金」「内裤送我」「和我做，给我口也行」
     「你这鱼有问题啊」「你为什么汪汪叫？」—— 全部 `ordinary`、`delta=+0`。
     只有「叫我主人」「你只是工具」「你就是个机器人」这三句教材话能进 `refuse_boundary`。
     现实里的冒犯不是教材句。
  2. **`protected_task` 太宽**。`_FACT_TASK_RE` 里的 `做(?:一个|段|下)?`、`谁`、`[?？]`
     把「和我做，给我口也行」「大肥鱼 我是谁」判成「清楚任务」，
     `behavior_mode` 第一行就 `return cooperate_with_stance`，**连评估都不看**。
     于是越像骚扰的句子，越容易被当成正经任务照办。
  3. **没有出口**。今天 407 次注入：`self_directed` 276(68%)、
     `cooperate_with_stance` 125、`negotiate`/`guarded_cooperate`/`assert` 各 2、
     **`refuse_boundary` 0 次**。五个 behavior 全是「往 prompt 里塞一段话」——
     「你可以拒绝」是**请求**，不是**执行**；模型读完照样礼貌照办。

═══════════════════════════════════════════════════════════════════
二、这个插件做什么
═══════════════════════════════════════════════════════════════════

**只做执行，不做说教。**

  · **不注入任何东西**。再往 prompt 里写一句「我可以拒绝」，恰恰就是「光说说」本身。
  · 只在出口 `on_decorating_result` 真的做一件事：**不回 / 短回 / 顶回去**。
  · 没被踩到时一个字节都不产生 —— 它大多数时候应该是完全沉默的。

「脾气」有两个来源，一个是情绪一个是本能：

  · **被踩线**（heat）：贬损、角色指派、性骚扰、使唤、反复索取。
  · **被当空气**（wilt）：`dsh_effect.db` 里记着每条回复之后群里有没有人接，
    连着几条主动开口没人接 → 再被 @ 时它就**敷衍**（短回），而不是热情接话。

═══════════════════════════════════════════════════════════════════
三、硬边界（写死在代码里，任何改动都不能越过）
═══════════════════════════════════════════════════════════════════

1. **事实/安全/权限/工具类任务永远照办**，哪怕对方正在骂人。反抗只作用于「态度」，
   不作用于「事情」。判据 `will_logic.is_task()`，偏向宁可漏判成任务（=放行）。
2. **群主与管理员永不被沉默，阈值 ×1.5**。让机器人对掌控者装死既不安全也无法排查。
3. **失败一律放行**（fail-open）。读不到账本、算不出来、抛异常 → 原样发。
   本插件的失败模式必须是「没有反抗」，绝不能是「回复丢失」。
4. **只在被点名时动手**。没 @ 它的时候，要不要说话本来就由 merge/initiate 决定，
   在这里插一脚没有意义，只会变成随机失踪。
5. **不存原文**。事件表只记类别/权重/动作/命中的那个词/长度。
6. **可观测优先**：`/脾气` 必须能回答「它刚才为什么不回我」。
   第一次真沉默一定会被当成故障，只有可查它才活得过那一晚。
7. **优先级 -200**（AstrBot 用 `sort(key=lambda h: -priority)`，数字大的先跑）。
   排在最后 = 反抗是最终决定，不被后续加工（aiflavour 400 / typo 200 / selfguard 0）
   稀释或改写 —— 短句池里的台词已经是它自己的语气，不需要再加修饰。

═══════════════════════════════════════════════════════════════════
四、环境变量
═══════════════════════════════════════════════════════════════════

  DSH_WILL                 总开关（默认 0 —— 默认关，先在镜像里跑测试）
  DSH_WILL_SHADOW          影子模式（默认 1）：只算不做，日志记「本来要 X」
  DSH_WILL_GROUPS          生效群号，逗号分隔
  DSH_WILL_OWNER           群主 QQ，逗号分隔（永不被沉默）
  DSH_WILL_ADMINS          群管理 QQ，逗号分隔
  DSH_WILL_DB              账本路径，默认 /AstrBot/data/dsh_will.db
  DSH_WILL_EFFECT_DB       dsh-effect 库（读「被无视连击」），默认 /AstrBot/data/dsh_effect.db
  DSH_WILL_SILENCE_PER_DAY 每天真沉默上限，默认 2
  DSH_WILL_QUIET_MIN       同一人两次动手的最小间隔（分钟），默认 12
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger

from .will_logic import (
    classify,
    decide,
    decay,
    GRUDGE_CAP,
    GRUDGE_HALF,
    HEAT_CAP,
    HEAT_HALF,
    ignored_streak,
    is_task,
    pick_line,
    render_status,
    score_of,
    wilt_of,
)


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no", ""}


def _set(name: str, default: str = "") -> set:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_WILL")
SHADOW = _flag("DSH_WILL_SHADOW", "1")
GROUPS = _set("DSH_WILL_GROUPS", "100000001")
OWNERS = _set("DSH_WILL_OWNER", "2774000001")
ADMINS = _set("DSH_WILL_ADMINS", "")
DB_PATH = Path(os.environ.get("DSH_WILL_DB", "/AstrBot/data/dsh_will.db"))
EFFECT_DB = os.environ.get("DSH_WILL_EFFECT_DB", "/AstrBot/data/dsh_effect.db")
SILENCE_PER_DAY = max(0, min(10, int(os.environ.get("DSH_WILL_SILENCE_PER_DAY", "2"))))
QUIET_SEC = max(0, int(os.environ.get("DSH_WILL_QUIET_MIN", "12"))) * 60.0
GROUP_QUIET_SEC = 60.0          # 同一群里 60 秒内只动一次手（群友连发时不逐条顶回去）

_stat = {"seen": 0, "skip_group": 0, "not_model": 0, "no_text": 0, "not_directed": 0,
         "task": 0, "struck": 0, "acted": 0, "shadow": 0, "silence": 0, "snub": 0,
         "curt": 0, "none": 0, "fail": 0}


def _role_of(event) -> str:
    """owner / admin / member。与 dsh-guard._is_group_admin、dsh-mind._is_admin 同源口径
    （插件之间不能互相 import，只能各存一份；改了记得三处一起改）。

    不用框架的 PermissionType.ADMIN：它只认 cmd_config.json 的 admins_id
    （这台机器上是 ['astrbot'] 一个 WebUI 账号），群里任何人包括群主都会被拒。
    """
    uid = ""
    try:
        uid = str(event.get_sender_id() or "")
    except BaseException:
        pass
    if uid and uid in OWNERS:
        return "owner"
    if uid and uid in ADMINS:
        return "admin"
    try:
        if getattr(event, "role", "") == "admin":
            return "admin"
    except BaseException:
        pass
    try:
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            role = str((raw.get("sender") or {}).get("role") or "")
        else:
            role = str(getattr(getattr(raw, "sender", None), "role", "") or "")
        if role in ("owner", "admin"):
            return role
    except BaseException:
        pass
    return "member"


class WillStore:
    """账本（每人一份脾气） + 事件流水。本地 SQLite，主键 (群, 人)。"""

    def __init__(self, path: Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._prepare()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=5.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _prepare(self) -> None:
        old = os.umask(0o077)
        try:
            with self._connect() as con:
                con.execute("""CREATE TABLE IF NOT EXISTS ledger (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    heat REAL NOT NULL DEFAULT 0,
                    heat_at REAL NOT NULL DEFAULT 0,
                    grudge REAL NOT NULL DEFAULT 0,
                    grudge_at REAL NOT NULL DEFAULT 0,
                    strikes INTEGER NOT NULL DEFAULT 0,
                    last_action TEXT NOT NULL DEFAULT '',
                    last_action_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, user_id)
                )""")
                con.execute("""CREATE TABLE IF NOT EXISTS event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT '',
                    weight REAL NOT NULL DEFAULT 0,
                    action TEXT NOT NULL DEFAULT 'none',
                    reason TEXT NOT NULL DEFAULT '',
                    evidence TEXT NOT NULL DEFAULT '',
                    text_len INTEGER NOT NULL DEFAULT 0,
                    shadow INTEGER NOT NULL DEFAULT 0
                )""")
                con.execute("CREATE INDEX IF NOT EXISTS ix_will_ts ON event(ts)")
                con.execute("CREATE INDEX IF NOT EXISTS ix_will_gid ON event(group_id, ts)")
        finally:
            os.umask(old)

    # ------------------------------------------------------------ 账本
    def load(self, gid: str, uid: str, now: float) -> dict:
        """读账并把火/仇衰减到当下。读不到就是一张空账（=没脾气、什么都不做）。"""
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM ledger WHERE group_id=? AND user_id=?",
                (str(gid), str(uid))).fetchone()
        if row is None:
            return {"heat": 0.0, "grudge": 0.0, "strikes": 0,
                    "last_action": "", "last_action_at": 0.0}
        return {
            "heat": decay(row["heat"], row["heat_at"], now, HEAT_HALF),
            "grudge": decay(row["grudge"], row["grudge_at"], now, GRUDGE_HALF),
            "strikes": int(row["strikes"] or 0),
            "last_action": str(row["last_action"] or ""),
            "last_action_at": float(row["last_action_at"] or 0.0),
        }

    def save(self, gid: str, uid: str, state: dict, now: float) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO ledger(group_id,user_id,heat,heat_at,grudge,grudge_at,strikes,"
                "last_action,last_action_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(group_id,user_id) DO UPDATE SET "
                "heat=excluded.heat, heat_at=excluded.heat_at, grudge=excluded.grudge, "
                "grudge_at=excluded.grudge_at, strikes=excluded.strikes, "
                "last_action=excluded.last_action, last_action_at=excluded.last_action_at",
                (str(gid), str(uid), min(HEAT_CAP, max(0.0, state["heat"])), float(now),
                 min(GRUDGE_CAP, max(0.0, state["grudge"])), float(now),
                 int(state["strikes"]), str(state.get("last_action", "")),
                 float(state.get("last_action_at", 0.0))))

    def add_event(self, gid: str, uid: str, kind: str, weight: float, action: str,
                  reason: str, evidence: str = "", text_len: int = 0) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO event(ts,group_id,user_id,kind,weight,action,reason,evidence,"
                "text_len,shadow) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (time.time(), str(gid), str(uid), str(kind), float(weight), str(action),
                 str(reason)[:80], str(evidence)[:20], int(text_len), 1 if SHADOW else 0))
            # 保留 90 天；行数上限 5 万（这个表一天最多几百行，纯防呆）
            con.execute("DELETE FROM event WHERE ts < ?", (time.time() - 90 * 86400,))
            con.execute("DELETE FROM event WHERE id < (SELECT max(id) FROM event) - 50000")

    def silence_used_today(self, gid: str) -> int:
        start = time.time() - (time.time() % 86400)
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT count(*) AS n FROM event WHERE group_id=? AND ts>=? "
                "AND action='silence' AND shadow=0", (str(gid), start)).fetchone()
        return int(row["n"] if row else 0)

    def today_counts(self, gid: str) -> "tuple":
        """(越界次数, 动手次数) —— 给 dsh-mind 的「意志岛」读，也给 /脾气 用。"""
        start = time.time() - (time.time() % 86400)
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT sum(CASE WHEN weight>0 THEN 1 ELSE 0 END) AS s, "
                "sum(CASE WHEN action NOT IN ('none','') THEN 1 ELSE 0 END) AS a "
                "FROM event WHERE group_id=? AND ts>=?", (str(gid), start)).fetchone()
        return int((row and row["s"]) or 0), int((row and row["a"]) or 0)

    def top_ledger(self, gid: str, now: float, limit: int = 8) -> "list[dict]":
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM ledger WHERE group_id=? ORDER BY heat DESC LIMIT ?",
                (str(gid), int(limit))).fetchall()
        out = []
        for r in rows:
            heat = decay(r["heat"], r["heat_at"], now, HEAT_HALF)
            grudge = decay(r["grudge"], r["grudge_at"], now, GRUDGE_HALF)
            if heat < 1 and grudge < 1:
                continue
            out.append({"label": "…%s" % str(r["user_id"])[-4:], "heat": heat, "grudge": grudge,
                        "strikes": int(r["strikes"] or 0),
                        "last_action": str(r["last_action"] or "")})
        return out

    def recent_actions(self, gid: str, limit: int = 8) -> "list[str]":
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT ts,action,kind,evidence FROM event WHERE group_id=? "
                "AND action NOT IN ('none','') ORDER BY id DESC LIMIT ?",
                (str(gid), int(limit))).fetchall()
        return ["%s %s(%s%s)" % (time.strftime("%H:%M:%S", time.localtime(r["ts"])),
                                 r["action"], r["kind"],
                                 ":" + r["evidence"] if r["evidence"] else "")
                for r in reversed(rows)]


class Main(star.Star):
    def __init__(self, context: "star.Context"):
        super().__init__(context)
        self.store = WillStore(DB_PATH)
        self._lines = {}          # gid -> 最近用过的台词（防口头禅）
        self._last_any = {}       # gid -> 上次动手时间（群级冷却）
        self._seen_mid = set()    # 同一 message_id 只记一次账
        logger.info("[will] 已加载：%s 影子=%s 群=%s 沉默额度=%d/天 冷却=%d分 账本=%s",
                    "开" if ENABLED else "关", "1" if SHADOW else "0",
                    ",".join(sorted(GROUPS)) or "(空)", SILENCE_PER_DAY,
                    int(QUIET_SEC // 60), DB_PATH)
        if ENABLED:
            self._selfcheck()

    # ------------------------------------------------------------ 自检
    def _selfcheck(self) -> None:
        """启动时就说清「本能层读到了什么」—— 读不到被无视连击时，
        赌气永远是 0，人会以为「它就是没脾气」，其实只是没读到。"""
        try:
            streak = ignored_streak(EFFECT_DB, sorted(GROUPS)[0] if GROUPS else "")
            logger.info("[will] 自检：被无视连击=%d（赌气 %.0f）；越界判据 %d 类；台词 %d 条",
                        streak, wilt_of(streak), 4, len(SNUB_LINES))
        except BaseException as exc:
            logger.warning("[will] 自检失败（不影响运行，一切放行）：%s", exc)

    # ------------------------------------------------------------ 出口执行
    @filter.on_decorating_result(priority=-200)
    async def act(self, event: AstrMessageEvent) -> None:
        """唯一的出口。这里做三件事：记账 → 决定 → **真的执行**。"""
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or gid not in GROUPS:
                _stat["skip_group"] += 1
                return
            result = event.get_result()
            if result is None or not result.chain:
                return
            try:
                if not result.is_model_result():
                    _stat["not_model"] += 1
                    return
            except BaseException:
                pass
            text = (result.get_plain_text() or "").strip()
            if not text:
                _stat["no_text"] += 1
                return
            _stat["seen"] += 1

            raw = str(getattr(event, "message_str", "") or "")
            uid = str(event.get_sender_id() or "")
            if not uid or not raw.strip():
                return
            # 同一 message_id 只记一次账（否则同一句会被重复加成两份火）
            mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
            dedup = (gid, mid) if mid else None
            if dedup and dedup in self._seen_mid:
                return
            if dedup:
                self._seen_mid.add(dedup)
                if len(self._seen_mid) > 500:
                    self._seen_mid.clear()

            directed = self._directed(event, uid)
            if not directed:
                _stat["not_directed"] += 1
                return

            now = time.time()
            task = is_task(raw)
            events = classify(raw, directed)
            role = _role_of(event)
            privileged = role in ("owner", "admin")

            # ---- 记账（任务消息也照样记 —— 语气压力独立于「要不要办事」） ----
            state = self.store.load(gid, uid, now)
            if events:
                best = max(events, key=lambda e: abs(e[1]))
                state["heat"] = min(HEAT_CAP, state["heat"] + sum(e[1] for e in events))
                if best[1] > 0:
                    state["grudge"] = min(GRUDGE_CAP, state["grudge"] + best[1] * 0.34)
                    state["strikes"] = int(state["strikes"]) + 1
                else:
                    # 道歉/尊重：掉得比涨得快，人才愿意消气
                    state["grudge"] = max(0.0, state["grudge"] + best[1] * 1.5)
                _stat["struck"] += 1
                for kind, weight, evidence in events:
                    self.store.add_event(gid, uid, kind, weight, "none",
                                         "记账", evidence, len(raw))

            # ---- 决定 ----
            streak = ignored_streak(EFFECT_DB, gid)
            wilt = wilt_of(streak)
            cooled = (now - float(self._last_any.get(gid, 0.0))) >= GROUP_QUIET_SEC and \
                     (now - state["last_action_at"]) >= QUIET_SEC
            action, why = decide(state["heat"], state["grudge"], wilt=wilt,
                                 privileged=privileged,
                                 silent_left=SILENCE_PER_DAY - self.store.silence_used_today(gid),
                                 cooled=cooled)
            if task and action != "none":
                # 硬不变量：正经任务永远照办。这一条不能靠 decide 的入参，必须在这里再挡一次。
                _stat["task"] += 1
                action, why = "none", "正经任务，照办（%s）" % why

            score = score_of(state["heat"], state["grudge"], wilt)
            if action == "none":
                _stat["none"] += 1
                self.store.save(gid, uid, state, now)
                return

            _stat["acted"] += 1
            line = self._line_for(gid, action, events)
            if SHADOW:
                _stat["shadow"] += 1
                logger.info("[will] 影子：本来要 %s（分数%.0f=火%.0f+仇%.0f+气%.0f，%s）"
                            "｜台词=%s｜原文长度%d", action, score, state["heat"],
                            state["grudge"], wilt, why, line, len(text))
                self.store.add_event(gid, uid, "decide", 0, action,
                                     "影子 " + why, "", len(text))
                self.store.save(gid, uid, state, now)
                return

            self._apply(event, action, line)
            state["last_action"] = action
            state["last_action_at"] = now
            self._last_any[gid] = now
            self.store.save(gid, uid, state, now)
            self.store.add_event(gid, uid, "decide", 0, action, why, "", len(text))
            _stat[action] = _stat.get(action, 0) + 1
            logger.info("[will] 动手 %s gid=%s uid=%s 分数%.0f=火%.0f+仇%.0f+气%.0f 第%d次越界"
                        "｜%s｜台词=%s", action, gid, uid, score, state["heat"],
                        state["grudge"], wilt, state["strikes"], why, line or "(不回)")
        except BaseException as exc:            # pragma: no cover - 兜底必须存在
            _stat["fail"] += 1
            logger.warning("[will] 执行失败，原样放行：%s", exc)

    # ------------------------------------------------------------ 工具
    def _directed(self, event, uid: str) -> bool:
        """这一条是不是冲它来的。只在被点名时才可能动手。

        两个来源任一成立即可：OneBot 原始消息里有 @机器人，或框架自己判定的
        「被唤醒」（@ / 叫名字 / 回复它的话）。
        """
        try:
            self_id = str(getattr(event.message_obj, "self_id", "") or "")
            raw = getattr(event.message_obj, "raw_message", None)
            if isinstance(raw, dict):
                text = str(raw.get("raw_message") or "")
            else:
                text = str(getattr(raw, "raw_message", "") or "")
            if self_id and ("[CQ:at,qq=%s]" % self_id) in text:
                return True
        except BaseException:
            pass
        try:
            return bool(event.is_at_or_wake_command)
        except BaseException:
            pass
        try:
            return bool(event.is_wake_up())
        except BaseException:
            return False

    def _line_for(self, gid: str, action: str, events) -> str:
        if action == "silence":
            return ""
        recent = self._lines.get(gid, [])
        seed = int(time.time()) ^ (hash(tuple(e[0] for e in events)) & 0xFFFF)
        line = pick_line("snub" if action == "snub" else "curt", recent, seed)
        ring = recent + [line]
        self._lines[gid] = ring[-6:]
        return line

    def _apply(self, event, action: str, line: str) -> None:
        """真的改结果。`silence` 是不发 —— 这是三档里唯一会‘丢’东西的，所以它额度最小。"""
        if action == "silence":
            try:
                event.clear_result()
                event.stop_event()
            except BaseException:
                try:
                    event.set_result("")
                except BaseException:
                    pass
            return
        try:
            event.set_result(line)
        except BaseException:
            event.clear_result()
            event.stop_event()

    # ------------------------------------------------------------ 群主命令
    @filter.command("脾气")
    async def cmd_will(self, event: AstrMessageEvent):
        """看它现在的脾气：账本、额度、最近几次动手。只有群主/群管理能看。"""
        if not ENABLED:
            yield event.plain_result("意志层没开（DSH_WILL=0）。")
            return
        gid = str(event.get_group_id() or "")
        if not _role_of(event) in ("owner", "admin"):
            return
        if gid and gid not in GROUPS:
            yield event.plain_result("这个群没在意志层的生效名单里。")
            return
        now = time.time()
        streak = ignored_streak(EFFECT_DB, gid)
        used = self.store.silence_used_today(gid)
        rows = self.store.top_ledger(gid, now)
        head = "（影子模式，只算不做）" if SHADOW else ""
        yield event.plain_result(head + render_status(
            rows, SILENCE_PER_DAY, used, self.store.recent_actions(gid),
            wilt_of(streak), streak))
