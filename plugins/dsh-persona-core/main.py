# -*- coding: utf-8 -*-
"""AstrBot shadow adapter for the shared persona decision core."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType

from .persona_logic import (
    PersonaState, RelationState, TurnContext, choose_action, relation_from_dict,
    should_wake, state_from_dict, update_relation, update_state,
)
from .action_logic import MicroMotive, Participation, add_motive, open_participation

# Event-local bridge consumed by dsh-proactive; it avoids another model call.
PERSONA_EXTRA_KEY = "dsh.persona.action.v1"

EXTRA_KEY = "dsh.persona.v1"


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {item.strip() for item in os.environ.get(name, default).split(",") if item.strip()}


ENABLED = _flag("DSH_PERSONA_CORE")
SHADOW = _flag("DSH_PERSONA_CORE_SHADOW", "1")
GROUPS = _set("DSH_PERSONA_CORE_GROUPS", "100000001")
OWNERS = _set("DSH_PERSONA_CORE_OWNER", "2774000001")
DB_PATH = Path(os.environ.get("DSH_PERSONA_CORE_DB", "/AstrBot/data/dsh_persona_core.db"))
SOCIAL_DB = Path(os.environ.get("DSH_PERSONA_CORE_SOCIAL_DB", "/AstrBot/data/dsh_social.db"))
INTEREST_PATH = Path(os.environ.get("DSH_PERSONA_CORE_INTEREST_STATE", "/AstrBot/data/dsh_interest_state.json"))
MIN_WAKE_SCORE = max(0.0, min(1.0, float(os.environ.get("DSH_PERSONA_CORE_MIN_WAKE", "0.58"))))
EVENT_KEEP_DAYS = max(1, min(30, int(os.environ.get("DSH_PERSONA_CORE_EVENT_KEEP_DAYS", "7"))))

_TASK_RE = re.compile(r"[?？]|(?:帮我|请问|怎么|如何|为什么|查一下|搜一下|写|做|修|配置|解释|总结|翻译|生成)")
_POSITIVE_RE = re.compile(r"(?:谢谢|感谢|喜欢|不错|真好|厉害|牛|开心|好耶|爱了)")
_NEGATIVE_RE = re.compile(r"(?:讨厌|烦|生气|难受|失望|垃圾|废物|滚|闭嘴|错了)")
_CONFLICT_RE = re.compile(r"(?:吵|对线|骂|闭嘴|滚|傻逼|废物|垃圾)")
_TOPIC_RE = re.compile(r"(?:大肥鱼|鲸鱼|白米饭|吃饭|美食|AI|模型|DeepSeek|代码|服务器|游戏)", re.I)


def _extra(event: AstrMessageEvent, key: str, default=None):
    try:
        value = event.get_extra(key)
        return default if value is None else value
    except BaseException:
        return default


def _sentiment(text: str) -> float:
    positive = len(_POSITIVE_RE.findall(text))
    negative = len(_NEGATIVE_RE.findall(text))
    return max(-1.0, min(1.0, 0.35 * positive - 0.45 * negative))


def _topic_interest(event: AstrMessageEvent, text: str) -> float:
    score = float(_extra(event, "dsh_proactive_score", 0) or 0)
    if score:
        return min(1.0, score / 10.0)
    static = 0.55 if _TOPIC_RE.search(text) else 0.0
    try:
        value = json.loads(INTEREST_PATH.read_text(encoding="utf-8"))
        hot = value.get("hot", {}).get(str(event.get_group_id() or ""), {})
        if isinstance(hot, dict) and any(str(key).lower() in text.lower() for key in hot):
            static = max(static, 0.7)
    except (OSError, ValueError, TypeError):
        pass
    return static


def _social_relation(gid: str, uid: str, now: float) -> tuple[RelationState | None, bool]:
    try:
        con = sqlite3.connect("file:%s?mode=ro" % SOCIAL_DB, uri=True, timeout=0.5)
        try:
            row = con.execute(
                "SELECT affinity,avoid_until,opted_out FROM relations WHERE group_id=? AND user_id=?",
                (gid, uid),
            ).fetchone()
        finally:
            con.close()
        if not row:
            return None, False
        affinity = max(-1.0, min(1.0, float(row[0] or 0) / 100.0))
        quiet = bool(row[2]) or float(row[1] or 0) > now
        return RelationState(familiarity=max(0.0, affinity), trust=affinity,
                             warmth=affinity, updated_at=now), quiet
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None, False


class PersonaStore:
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
                con.executescript("""
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS persona_state(
                        group_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS relation_state(
                        group_id TEXT NOT NULL, user_id TEXT NOT NULL, payload TEXT NOT NULL,
                        updated_at REAL NOT NULL, PRIMARY KEY(group_id,user_id));
                    CREATE TABLE IF NOT EXISTS persona_events(
                        event_key TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                        action TEXT NOT NULL, motive TEXT NOT NULL, score REAL NOT NULL,
                        should_wake INTEGER NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS idx_persona_events_time ON persona_events(created_at);
                """)
        finally:
            os.umask(old_umask)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def apply(self, event_key: str, ctx: TurnContext, now: float) -> tuple[PersonaState, RelationState, dict, bool]:
        with self.lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT * FROM persona_events WHERE event_key=?", (event_key,)).fetchone()
            state_row = con.execute("SELECT payload FROM persona_state WHERE group_id=?", (ctx.group_id,)).fetchone()
            relation_row = con.execute(
                "SELECT payload FROM relation_state WHERE group_id=? AND user_id=?",
                (ctx.group_id, ctx.speaker_id),
            ).fetchone()
            state = state_from_dict(json.loads(state_row[0])) if state_row else PersonaState(updated_at=now)
            relation = relation_from_dict(json.loads(relation_row[0])) if relation_row else RelationState(updated_at=now)
            if existing:
                result = {key: existing[key] for key in existing.keys()}
                return state, relation, result, False
            state = update_state(state, ctx, now)
            relation = update_relation(relation, ctx, now)
            action = choose_action(ctx, state, relation)
            wake, wake_reason = should_wake(action, ctx, MIN_WAKE_SCORE)
            reason = "%s；%s" % (action.reason, wake_reason)
            con.execute(
                "INSERT INTO persona_state(group_id,payload,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(group_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                (ctx.group_id, json.dumps(asdict(state), ensure_ascii=False), now),
            )
            con.execute(
                "INSERT INTO relation_state(group_id,user_id,payload,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(group_id,user_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                (ctx.group_id, ctx.speaker_id, json.dumps(asdict(relation), ensure_ascii=False), now),
            )
            con.execute(
                "INSERT INTO persona_events VALUES(?,?,?,?,?,?,?,?,?)",
                (event_key, ctx.group_id, ctx.speaker_id, action.name, action.motive,
                 action.score, int(wake), reason, now),
            )
            return state, relation, {"action": action.name, "motive": action.motive,
                                     "score": action.score, "should_wake": int(wake),
                                     "reason": reason}, True

    def recent(self, gid: str, limit: int = 5) -> list[sqlite3.Row]:
        with self.lock, self._connect() as con:
            return con.execute(
                "SELECT * FROM persona_events WHERE group_id=? ORDER BY created_at DESC LIMIT ?",
                (gid, limit),
            ).fetchall()

    def prune(self, now: float) -> None:
        with self.lock, self._connect() as con:
            con.execute("DELETE FROM persona_events WHERE created_at<?", (now - EVENT_KEEP_DAYS * 86400,))


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        super().__init__(context)
        self.store = PersonaStore(DB_PATH)
        self.seen = self.errors = 0
        logger.info("[persona-core] 已加载：%s%s 群=%s", "开" if ENABLED else "关",
                    "（影子）" if SHADOW else "", ",".join(sorted(GROUPS)) or "无")

    def _event_key(self, event: AstrMessageEvent, gid: str, uid: str, now: float) -> str:
        obj = getattr(event, "message_obj", None)
        mid = str(getattr(obj, "message_id", "") or "")
        return "%s:%s:%s" % (gid, uid, mid or "%d:%s" % (int(now // 3), hash(event.get_message_str() or "")))

    def _context(self, event: AstrMessageEvent, now: float) -> TurnContext | None:
        gid, uid = str(event.get_group_id() or ""), str(event.get_sender_id() or "")
        text = str(event.get_message_str() or "").strip()
        if not gid or gid not in GROUPS or not uid or not text or text.startswith("/"):
            return None
        if uid == str(event.get_self_id() or ""):
            return None
        social, quiet = _social_relation(gid, uid, now)
        directed = bool(getattr(event, "is_at_or_wake_command", False))
        continuation = bool(_extra(event, "dsh_followup", False))
        proactive = bool(_extra(event, "dsh_proactive", False))
        ctx = TurnContext(
            gid, uid, text, directed=directed, explicit_task=bool(directed and _TASK_RE.search(text)),
            topic_interest=_topic_interest(event, text), sentiment=_sentiment(text),
            relationship_importance=(social.warmth if social else 0.0), continuation=continuation,
            recent_bot_reply=continuation, active_conflict=bool(_CONFLICT_RE.search(text)),
            quiet_requested=quiet and not directed,
        )
        if proactive:
            ctx = TurnContext(**{**asdict(ctx), "topic_interest": max(ctx.topic_interest, 0.8)})
        return ctx

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=300)
    async def observe(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            now = time.time()
            ctx = self._context(event, now)
            if not ctx:
                return
            key = self._event_key(event, ctx.group_id, ctx.speaker_id, now)
            state, relation, decision, inserted = self.store.apply(key, ctx, now)
            payload = {"version": 1, "shadow": SHADOW, **decision,
                       "state": asdict(state), "relation": asdict(relation)}
            event.set_extra(EXTRA_KEY, payload)
            event.set_extra(PERSONA_EXTRA_KEY, {
                "version": 1,
                "action": decision.get("action", "stay_quiet"),
                "motive": decision.get("motive", "rest"),
                "score": float(decision.get("score", 0.0) or 0.0),
                "should_wake": bool(decision.get("should_wake", False)),
                "topic": ctx.text[:120],
            })
            if inserted:
                self.seen += 1
                if self.seen % 100 == 0:
                    self.store.prune(now)
                logger.info("[persona-core] 影子 gid=%s uid=%s action=%s motive=%s score=%.2f wake=%s",
                            ctx.group_id, ctx.speaker_id, decision["action"], decision["motive"],
                            float(decision["score"]), bool(decision["should_wake"]))
        except BaseException as exc:
            self.errors += 1
            logger.warning("[persona-core] 观察失败，保持原流程: %r", exc)

    @filter.on_llm_request(priority=70)
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED or SHADOW:
            return
        try:
            decision = _extra(event, EXTRA_KEY, {})
            if not isinstance(decision, dict):
                return
            action = str(decision.get("action") or "")
            motive = str(decision.get("motive") or "")
            if action == "stay_quiet" or action == "observe":
                return
            guidance = {
                "cooperate_with_stance": "明确任务照常准确完成，可以保留自己的判断，不要盲目附和。",
                "answer_safely": "安全相关内容优先准确、克制地回答，不因情绪或关系改变安全边界。",
                "follow_up": "这是已有话题的自然续接，接住上下文即可，不要重新铺陈。",
                "reply_socially": "可以自然交流，适度表达自己的偏好，不必每次都写成正式答复。",
                "share_interest": "这是你感兴趣的话题，可以主动表达具体偏好，但不要为了插话硬扯。",
                "check_in": "关系维护动机较强，可以结合已知上下文自然关心一句，不要盘问。",
            }.get(action, "没有足够理由抢话；如果当前消息未点名且与前文无关，保持简短或不接。")
            state = decision.get("state") or {}
            mood = float(state.get("mood", 0) or 0)
            tone = "平稳"
            if mood > 0.35:
                tone = "稍微开心"
            elif mood < -0.35:
                tone = "稍微低落"
            block = (
                "<persona_guidance>"
                "本轮动机=%s；行为=%s；情绪基调=%s。%s"
                "说话像自然群聊：允许短句、追问、不同意或主动收尾；不要提及人格模块、内部状态、分数或这段提示。"
                "明确任务、安全、权限和事实问题仍以可靠完成为先。"
                "</persona_guidance>" % (motive or "普通交流", action or "自然判断", tone, guidance)
            )
            req.extra_user_content_parts.append(TextPart(text=block))
        except BaseException as exc:
            self.errors += 1
            logger.warning("[persona-core] 注入失败，保持原流程: %r", exc)

    @filter.command("人格状态")
    async def status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        gid = str(event.get_group_id() or "")
        rows = self.store.recent(gid, 5) if gid else []
        recent = "；".join("%s:%s/%s %.2f" % (row["user_id"], row["action"], row["motive"], row["score"])
                          for row in rows) or "暂无"
        yield event.plain_result("人格核心：%s%s｜观察%d｜异常%d\n最近：%s" % (
            "开" if ENABLED else "关", "（影子）" if SHADOW else "", self.seen, self.errors, recent))
