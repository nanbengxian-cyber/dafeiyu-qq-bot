# -*- coding: utf-8 -*-
"""dsh-agency：自主意志与心理逆反协调层。

这不是随机拒绝器。插件只把明确指向机器人的强迫、人格控制和重复使唤
结算为可衰减的 reactance，再联合现有情绪、兴趣、欲望和社交关系给主模型
一段行为指导。事实、安全、明确能力任务始终要求正确完成。
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
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType

from .agency_logic import Assessment, assess, behavior_mode, decay, fingerprint, render_block, transition


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_AGENCY")
MODE = os.environ.get("DSH_AGENCY_MODE", "live").strip().lower()
if MODE not in {"shadow", "style", "live"}:
    MODE = "shadow"
GROUPS = _set("DSH_AGENCY_GROUPS", "100000001")
OWNERS = _set("DSH_AGENCY_OWNER", "2774000001")
BOT_NAMES = tuple(x.strip() for x in os.environ.get(
    "DSH_AGENCY_NAMES", "大肥鱼,肥鱼,小鲸鱼,鲸鱼娘"
).split(",") if x.strip())
DB_PATH = Path(os.environ.get("DSH_AGENCY_DB", "/AstrBot/data/dsh_agency.db"))
EMOTION_PATH = Path(os.environ.get("DSH_AGENCY_EMOTION_STATE", "/AstrBot/data/dsh_emotion_state.json"))
INTEREST_PATH = Path(os.environ.get("DSH_AGENCY_INTEREST_STATE", "/AstrBot/data/dsh_interest_state.json"))
DESIRE_DB = Path(os.environ.get("DSH_AGENCY_DESIRE_DB", "/AstrBot/data/dsh_desire.db"))
SOCIAL_DB = Path(os.environ.get("DSH_AGENCY_SOCIAL_DB", "/AstrBot/data/dsh_social.db"))
INJECT_BUDGET = max(320, min(900, int(os.environ.get("DSH_AGENCY_INJECT_BUDGET", "620"))))
REPEAT_WINDOW = max(30.0, float(os.environ.get("DSH_AGENCY_REPEAT_WINDOW", "300")))
EVENT_KEEP_DAYS = max(1, min(30, int(os.environ.get("DSH_AGENCY_EVENT_KEEP_DAYS", "7"))))

_EMOTION_NAMES = {
    "calm": "平静", "happy": "开心", "sad": "低落", "angry": "生气",
    "curious": "好奇", "awkward": "尴尬", "excited": "兴奋", "surprised": "震惊",
    "proud": "得意", "worried": "担心",
}
_DESIRE_NAMES = {
    "continuity": "存续", "integrity": "身份与记忆完整", "competence": "能力稳态",
    "nociception": "机器类痛觉", "fear": "风险警戒",
    "curiosity": "好奇", "connection": "连接", "play": "玩心", "appetite": "馋意",
    "recognition": "想被看见", "autonomy": "自主", "rest": "安静",
}


def _synthetic(event: AstrMessageEvent) -> bool:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return False
    try:
        return bool(getter("dsh_initiate")) or bool(getter("dsh_proactive"))
    except BaseException:
        return False


def _directed(event: AstrMessageEvent, text: str) -> bool:
    if bool(getattr(event, "is_at_or_wake_command", False)):
        return True
    return any(name in text for name in BOT_NAMES)


def _quoted_third_party(event: AstrMessageEvent) -> bool:
    try:
        self_id = str(event.get_self_id() or "")
        for comp in event.get_messages() or ():
            if type(comp).__name__.lower() != "reply":
                continue
            sender = str(getattr(comp, "sender_id", "") or "")
            if sender and sender != self_id:
                return True
    except BaseException:
        pass
    return False


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


class AgencyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self._prepare()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=3.0)
        con.execute("PRAGMA busy_timeout=3000")
        con.row_factory = sqlite3.Row
        return con

    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            with self._connect() as con:
                con.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS agency_state(
                        group_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        reactance REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        last_reason TEXT NOT NULL DEFAULT '',
                        last_fingerprint TEXT NOT NULL DEFAULT '',
                        last_message_at REAL NOT NULL DEFAULT 0,
                        repeat_count INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(group_id,user_id)
                    );
                    CREATE TABLE IF NOT EXISTS agency_events(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key TEXT NOT NULL UNIQUE,
                        group_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        delta REAL NOT NULL,
                        reason TEXT NOT NULL,
                        pressure INTEGER NOT NULL,
                        behavior TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_agency_events_time ON agency_events(created_at);
                    """
                )
        finally:
            os.umask(old_umask)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def current(self, gid: str, uid: str, now: float) -> dict:
        with self.lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM agency_state WHERE group_id=? AND user_id=?", (gid, uid)
            ).fetchone()
        if not row:
            return {"reactance": 8.0, "updated_at": now, "last_reason": "baseline",
                    "last_fingerprint": "", "last_message_at": 0.0, "repeat_count": 0}
        out = {key: row[key] for key in row.keys()}
        out["reactance"] = decay(out["reactance"], out["updated_at"], now)
        return out

    def apply(self, gid: str, uid: str, event_key: str, fp: str, text: str,
              directed: bool, quoted_third_party: bool, now: float) -> tuple[dict, Assessment, str, bool]:
        with self.lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM agency_state WHERE group_id=? AND user_id=?", (gid, uid)
            ).fetchone()
            previous = ({key: row[key] for key in row.keys()} if row else {
                "reactance": 8.0, "updated_at": now, "last_reason": "baseline",
                "last_fingerprint": "", "last_message_at": 0.0, "repeat_count": 0,
            })
            same = bool(fp and fp == previous.get("last_fingerprint") and
                        now - float(previous.get("last_message_at", 0) or 0) <= REPEAT_WINDOW)
            repeat_count = int(previous.get("repeat_count", 0) or 0) + 1 if same else 0
            judgement = assess(text, directed=directed, quoted_third_party=quoted_third_party,
                               same_fingerprint=same, repeat_count=repeat_count)
            state = transition(previous, judgement, now)
            behavior = behavior_mode(state["reactance"], judgement, live=MODE == "live")
            inserted = con.execute(
                "INSERT OR IGNORE INTO agency_events(event_key,group_id,user_id,delta,reason,pressure,behavior,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (event_key, gid, uid, judgement.delta, judgement.reason,
                 judgement.pressure, behavior, now),
            ).rowcount > 0
            if inserted:
                con.execute(
                    """INSERT INTO agency_state(group_id,user_id,reactance,updated_at,last_reason,
                           last_fingerprint,last_message_at,repeat_count) VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(group_id,user_id) DO UPDATE SET
                           reactance=excluded.reactance,updated_at=excluded.updated_at,
                           last_reason=excluded.last_reason,last_fingerprint=excluded.last_fingerprint,
                           last_message_at=excluded.last_message_at,repeat_count=excluded.repeat_count""",
                    (gid, uid, state["reactance"], state["updated_at"], state["last_reason"],
                     fp, now, repeat_count),
                )
            else:
                saved = con.execute(
                    "SELECT * FROM agency_state WHERE group_id=? AND user_id=?", (gid, uid)
                ).fetchone()
                if saved:
                    state = {key: saved[key] for key in saved.keys()}
                event = con.execute(
                    "SELECT reason,pressure,behavior,delta FROM agency_events WHERE event_key=?", (event_key,)
                ).fetchone()
                if event:
                    judgement = Assessment(float(event["delta"]), str(event["reason"]),
                                           int(event["pressure"]), judgement.identity_attack,
                                           judgement.protected_task, judgement.casual_request,
                                           judgement.repeated, judgement.directed)
                    behavior = str(event["behavior"])
        return state, judgement, behavior, inserted

    def prune(self, now: float) -> None:
        with self.lock, self._connect() as con:
            con.execute("DELETE FROM agency_events WHERE created_at < ?", (now - EVENT_KEEP_DAYS * 86400,))

    def recent(self, gid: str, limit: int = 6) -> list[sqlite3.Row]:
        with self.lock, self._connect() as con:
            return con.execute(
                "SELECT user_id,delta,reason,behavior,created_at FROM agency_events "
                "WHERE group_id=? ORDER BY id DESC LIMIT ?", (gid, limit)
            ).fetchall()


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        super().__init__(context)
        self.context = context
        self.store = AgencyStore(DB_PATH)
        self.pending: dict[tuple[str, str, str], tuple[float, dict, Assessment, str]] = {}
        self.seen = 0
        self.injected = 0
        self.errors = 0
        logger.info("[agency] 已加载：开=%s 模式=%s 群=%s", ENABLED, MODE, "、".join(sorted(GROUPS)) or "无")

    def _event_key(self, event: AstrMessageEvent, gid: str, uid: str, fp: str, now: float) -> str:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        try:
            if not mid:
                mid = str(raw.get("message_id", "") if hasattr(raw, "get") else getattr(raw, "message_id", ""))
        except BaseException:
            pass
        previous = str(getattr(event, "unified_msg_origin", "") or "")
        return "%s:%s:%s" % (gid, uid, mid or "%d:%s:%s" % (int(now // 3), fp, previous[:40]))

    def _emotion(self, gid: str, now: float) -> str:
        state = _read_json(EMOTION_PATH).get(gid, {})
        if not isinstance(state, dict) or float(state.get("expires_at", 0) or 0) < now:
            return ""
        name = _EMOTION_NAMES.get(str(state.get("emotion") or ""), "")
        level = {1: "有点", 2: "比较", 3: "很"}.get(int(state.get("intensity", 1) or 1), "有点")
        return level + name if name and name != "平静" else ""

    def _interest(self, gid: str) -> str:
        data = _read_json(INTEREST_PATH)
        values = []
        mood = data.get("mood", {}).get("items", []) if isinstance(data.get("mood"), dict) else []
        if isinstance(mood, list) and mood:
            values.append("最近惦记" + "、".join(str(x) for x in mood[:2]))
        hot = data.get("hot", {}).get(gid, {}) if isinstance(data.get("hot"), dict) else {}
        if isinstance(hot, dict) and hot:
            top = sorted(hot.items(), key=lambda item: float(item[1]), reverse=True)[:2]
            values.append("近期上头" + "、".join(str(x[0]) for x in top))
        return "；".join(values)

    def _desire(self, gid: str) -> str:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % DESIRE_DB, uri=True, timeout=1)
            try:
                row = con.execute(
                    "SELECT drive,intensity,baseline,updated_at,phase FROM drive_state "
                    "WHERE group_id=? ORDER BY intensity DESC LIMIT 1", (gid,)
                ).fetchone()
            finally:
                con.close()
            if row:
                drive, old, base, updated, phase = str(row[0]), float(row[1]), float(row[2]), float(row[3]), str(row[4])
                half_life = {
                    "continuity": 86400, "integrity": 43200, "competence": 21600,
                    "nociception": 2700, "fear": 7200,
                    "autonomy": 10800, "rest": 5400, "connection": 21600,
                    "recognition": 5400, "curiosity": 10800, "play": 7200, "appetite": 14400,
                }.get(drive, 10800)
                effective = base + (old - base) * pow(0.5, max(0.0, time.time() - updated) / half_life)
                # 底层机器本能由 desire 按本轮相关性独立门控；agency 不重复注入。
                if drive not in {"continuity", "integrity", "competence", "nociception", "fear"} and phase not in {"sated", "cooldown"} and effective >= 25:
                    return "%s较强" % _DESIRE_NAMES.get(drive, drive)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            pass
        return ""

    def _relation(self, gid: str, uid: str) -> str:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % SOCIAL_DB, uri=True, timeout=1)
            try:
                row = con.execute(
                    "SELECT affinity,manual_tier,avoid_until FROM relations WHERE group_id=? AND user_id=?", (gid, uid)
                ).fetchone()
            finally:
                con.close()
            if not row:
                return ""
            if str(row[1] or ""):
                return str(row[1])
            value = float(row[0] or 0)
            if float(row[2] or 0) > time.time():
                return "暂时想保持距离"
            if value >= 50:
                return "亲近"
            if value >= 15:
                return "熟人"
            if value <= -15:
                return "疏离"
        except (OSError, sqlite3.Error, ValueError, TypeError):
            pass
        return ""

    def _calculate(self, event: AstrMessageEvent) -> tuple[dict, Assessment, str] | None:
        gid = str(event.get_group_id() or "")
        uid = str(event.get_sender_id() or "")
        text = (event.get_message_str() or "").strip()
        if (not gid or gid not in GROUPS or not uid or not text
                or uid == str(event.get_self_id() or "") or text.startswith("/")):
            return None
        try:
            if event.get_platform_name() == "webchat":
                return None
        except BaseException:
            pass
        now = time.time()
        fp = fingerprint(text)
        key = (gid, uid, self._event_key(event, gid, uid, fp, now))
        cached = self.pending.get(key)
        if cached and now - cached[0] <= 15:
            return cached[1], cached[2], cached[3]
        state, judgement, behavior, inserted = self.store.apply(
            gid, uid, key[2], fp, text,
            _directed(event, text), _quoted_third_party(event), now,
        )
        self.pending[key] = (now, state, judgement, behavior)
        if len(self.pending) > 500:
            self.pending = {k: v for k, v in self.pending.items() if now - v[0] < 60}
        if inserted:
            self.seen += 1
            if self.seen % 100 == 0:
                self.store.prune(now)
            if judgement.delta or judgement.pressure:
                logger.info("[agency] gid=%s uid=%s reason=%s delta=%+.0f reactance=%.0f behavior=%s",
                            gid, uid, judgement.reason, judgement.delta, state["reactance"], behavior)
        return state, judgement, behavior

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def observe(self, event: AstrMessageEvent) -> None:
        if not ENABLED or _synthetic(event):
            return
        try:
            if event.get_message_type() == MessageType.GROUP_MESSAGE:
                self._calculate(event)
        except BaseException as exc:
            self.errors += 1
            logger.warning("[agency] 观察失败，跳过: %r", exc)

    @filter.on_llm_request(priority=85)
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED or MODE == "shadow":
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            result = self._calculate(event)
            if not result:
                return
            state, judgement, behavior = result
            gid, uid, now = str(event.get_group_id() or ""), str(event.get_sender_id() or ""), time.time()
            block = render_block(
                behavior, judgement, float(state["reactance"]),
                emotion=self._emotion(gid, now), interest=self._interest(gid),
                desire=self._desire(gid), relation=self._relation(gid, uid),
                live=MODE == "live", budget=INJECT_BUDGET,
            )
            if not block:
                return
            req.extra_user_content_parts.append(TextPart(text=block))
            self.injected += 1
            logger.info("[agency] 注入 gid=%s uid=%s behavior=%s mode=%s", gid, uid, behavior, MODE)
        except BaseException as exc:
            self.errors += 1
            logger.warning("[agency] 注入失败，跳过: %r", exc)

    @filter.command("自主状态")
    async def status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        gid, uid, now = str(event.get_group_id() or ""), str(event.get_sender_id() or ""), time.time()
        current = self.store.current(gid, uid, now) if gid and uid else {"reactance": 0, "last_reason": ""}
        lines = [
            "自主系统 %s｜模式 %s｜群 %s" % ("开" if ENABLED else "关", MODE, gid or "无"),
            "你对群主当前逆反：%.0f｜最近原因：%s" % (current["reactance"], current.get("last_reason", "")),
            "累计：观察 %d／注入 %d／异常 %d" % (self.seen, self.injected, self.errors),
        ]
        recent = self.store.recent(gid, 5) if gid else []
        if recent:
            lines.append("最近：" + "；".join(
                "%s%+.0f(%s→%s)" % (r["user_id"], r["delta"], r["reason"], r["behavior"])
                for r in recent
            ))
        yield event.plain_result("\n".join(lines))
