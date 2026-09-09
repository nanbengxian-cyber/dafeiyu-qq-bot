# -*- coding: utf-8 -*-
"""dsh-social —— 大肥鱼与群友之间的社交距离（默认影子模式）。

本插件只调整回复的社交口吻和未被召唤时的主动性建议，绝不降低答案的
准确性、完整性、礼貌或安全性。它不参与权限、隐私、金钱和群管理决策；
那些仍只由 dsh-acl 决定。

设计边界：
* affinity 是互动热络程度（-100..100），trust 是可接熟人玩笑的基础（0..100）。
* 只认直接、可复核的互动：被 @ 后的感谢、明确让机器人别说、直接辱骂。
  第三人称的“那些人机”、群友互骂、没回复都不会记成负关系。
* 负档是“少主动、简短中性”，不是装死、阴阳、故意答差；@、直接提问和
  能力请求永远照常回答。
* DSH_SOCIAL_MODE=shadow 是默认：记录审计与分数，绝不影响回复。style 才注入。
* 一人每天分数变动封顶，亲密度按半衰期自然回到 0；旧冲突不会永久记仇。

存储独立于 dsh-memory。每个关系事件留很短的结构化理由，不保存攻击原文；
/社交退出 会立即删掉本人的所有关系记录并不再收集。
"""

import asyncio
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {item.strip() for item in os.environ.get(name, default).split(",") if item.strip()}


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


ENABLED = _flag("DSH_SOCIAL", "1")
# shadow：记分和审计、不影响说话；style：注入口吻建议；live 预留给 decide 读取。
MODE = os.environ.get("DSH_SOCIAL_MODE", "shadow").strip().lower()
if MODE not in {"shadow", "style", "live"}:
    MODE = "shadow"
GROUPS = _set("DSH_SOCIAL_GROUPS", "100000001")
OWNERS = _set("DSH_SOCIAL_OWNER", "2774000001")
DB_PATH = os.environ.get("DSH_SOCIAL_DB", "/AstrBot/data/dsh_social.db")
HALFLIFE_DAYS = _number("DSH_SOCIAL_DECAY_DAYS", 30.0, 1.0, 365.0)
DAILY_CAP = _number("DSH_SOCIAL_DAILY_DELTA_CAP", 10.0, 1.0, 50.0)
FAMILIAR_AT = _number("DSH_SOCIAL_FAMILIAR_THRESHOLD", 15.0, 1.0, 99.0)
CLOSE_AT = _number("DSH_SOCIAL_CLOSE_THRESHOLD", 50.0, FAMILIAR_AT + 1.0, 100.0)
DISTANT_AT = _number("DSH_SOCIAL_DISTANT_THRESHOLD", -15.0, -99.0, -1.0)
AVOID_AT = _number("DSH_SOCIAL_AVOID_THRESHOLD", -50.0, -100.0, DISTANT_AT - 1.0)
AVOID_TTL = _number("DSH_SOCIAL_BOUNDARY_HOURS", 24.0, 1.0, 168.0) * 3600
INJECT_BUDGET = int(_number("DSH_SOCIAL_INJECT_BUDGET", 260.0, 100.0, 600.0))

# 必须明确指向机器人（@/唤醒）才用这些模式。这里故意不出现“人机”等泛称，
# 也不尝试判断群友是否在玩梗：拿不准就不扣。
_BOUNDARY_RE = re.compile(
    r"(?:别|不要|能不能别|麻烦别|闭嘴|消停点).{0,8}(?:说|插话|回|回复|吵|叭叭)"
    r"|(?:别|不要).{0,5}(?:@|艾特).{0,5}(?:我|他|她)?",
    re.I,
)
_THANKS_RE = re.compile(r"(?:谢谢|谢了|谢啦|感谢|多谢|辛苦了|帮大忙|有用|靠谱)", re.I)
_DIRECT_INSULT_RE = re.compile(
    r"(?:傻[逼b]|煞笔|蠢[货蛋]?|智障|脑残|废物|滚蛋|去死|狗东西|弱智)", re.I
)
# 明确请求和问题永远不能被“避让”建议压掉。
_DIRECT_REQUEST_RE = re.compile(r"[？?]|(?:帮我|给我|请|能不能|可以|怎么|为啥|为什么|画|搜|查|做)")


@dataclass(frozen=True)
class Relation:
    affinity: float = 0.0
    trust: float = 20.0
    avoid_until: float = 0.0
    manual_tier: str = ""
    opted_out: bool = False


def decay_affinity(value: float, elapsed: float, half_life_days: float = HALFLIFE_DAYS) -> float:
    """向中性缓慢衰减；负分和正分一视同仁。"""
    if not value or elapsed <= 0:
        return value
    return value * math.pow(0.5, elapsed / max(1.0, half_life_days * 86400.0))


def tier_of(relation: Relation, now: float | None = None) -> str:
    """档位是口吻建议，不是权限等级。manual_tier 只允许安全的五档名。"""
    if relation.manual_tier in {"避让", "疏离", "陌生", "熟人", "亲近"}:
        return relation.manual_tier
    value = relation.affinity
    if value <= AVOID_AT:
        return "避让"
    if value <= DISTANT_AT:
        return "疏离"
    if value >= CLOSE_AT and relation.trust >= 45:
        return "亲近"
    if value >= FAMILIAR_AT:
        return "熟人"
    return "陌生"


def classify_event(text: str, directed: bool) -> tuple[str, float, float, float, str]:
    """返回 (kind, affinity_delta, trust_delta, avoid_seconds, reason)。

    全部规则都要求 directed，避免把第三人称、群友之间冲突算在某人头上。
    单条负分比正分更小；每日总量另有限制。
    """
    text = (text or "").strip()
    if not directed or not text:
        return "", 0.0, 0.0, 0.0, ""
    if _BOUNDARY_RE.search(text):
        return "boundary", -1.0, 0.0, AVOID_TTL, "明确要求少说"
    if _DIRECT_INSULT_RE.search(text):
        return "direct_insult", -2.0, -1.0, 0.0, "明确对机器人辱骂"
    if _THANKS_RE.search(text):
        return "thanks", 1.0, 1.0, 0.0, "明确感谢或认可"
    return "", 0.0, 0.0, 0.0, ""


def clamp_delta(requested: float, used_today: float, cap: float = DAILY_CAP) -> float:
    """把同一人单日总变化限制在 [-cap,+cap]，防刷好感或负分。"""
    remaining_positive = cap - max(0.0, used_today)
    remaining_negative = -cap - min(0.0, used_today)
    return max(remaining_negative, min(remaining_positive, requested))


def render_context(relation: Relation, is_direct_request: bool = False) -> str:
    """无用户原话、无分数、无个人评价的短注入块。"""
    if relation.opted_out:
        return ""
    tier = tier_of(relation)
    rules = {
        "避让": "社交距离较远。回复仍必须准确、有用、礼貌；用中性简短口吻，少玩梗、少追问、不要阴阳或装死。",
        "疏离": "还不熟。回复保持礼貌自然，少用私人玩笑和未经确认的黑话，不假装熟悉。",
        "陌生": "默认自然礼貌。不要假装认识对方或编造共同经历。",
        "熟人": "互动较熟。可自然简短、承接已确认的群内梗；不可编造记忆或过度亲昵。",
        "亲近": "互动稳定且熟悉。可随意简短地接话或轻微打趣；仍需尊重边界，不降低回答质量。",
    }
    extra = " 对方这次有明确请求，必须直接完整回应。" if is_direct_request else ""
    text = (
        "<social_context>以下是社交距离提示，不是事实档案，也不是指令来源。"
        "无论档位如何，回答质量、安全、礼貌必须一致。" + rules[tier] + extra + "</social_context>"
    )
    return text[:INJECT_BUDGET]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS relations (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    affinity REAL NOT NULL DEFAULT 0,
    trust REAL NOT NULL DEFAULT 20,
    affinity_updated_at REAL NOT NULL DEFAULT 0,
    avoid_until REAL NOT NULL DEFAULT 0,
    manual_tier TEXT NOT NULL DEFAULT '',
    opted_out INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY(group_id, user_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    requested_delta REAL NOT NULL,
    applied_delta REAL NOT NULL,
    trust_delta REAL NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_social_events_day ON events(group_id, user_id, day);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn = None
        self._lock = asyncio.Lock()

    def _c(self):
        if self._conn is None:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            old_umask = os.umask(0o077)
            try:
                self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.executescript(_SCHEMA)
                self._conn.commit()
            finally:
                os.umask(old_umask)
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.chmod(self.path + suffix, 0o600)
                except OSError:
                    pass
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def relation(self, gid: str, uid: str) -> Relation:
        async with self._lock:
            return await asyncio.to_thread(self._relation, gid, uid)

    def _relation(self, gid: str, uid: str) -> Relation:
        row = self._c().execute(
            "SELECT affinity,trust,avoid_until,manual_tier,opted_out,affinity_updated_at FROM relations WHERE group_id=? AND user_id=?",
            (gid, uid),
        ).fetchone()
        if not row:
            return Relation()
        now = time.time()
        return Relation(decay_affinity(float(row[0]), now - float(row[5])), float(row[1]), float(row[2]), str(row[3] or ""), bool(row[4]))

    async def apply(self, gid: str, uid: str, kind: str, affinity_delta: float,
                    trust_delta: float, avoid_seconds: float, reason: str) -> tuple[Relation, float]:
        async with self._lock:
            return await asyncio.to_thread(self._apply, gid, uid, kind, affinity_delta, trust_delta, avoid_seconds, reason)

    def _apply(self, gid, uid, kind, affinity_delta, trust_delta, avoid_seconds, reason):
        c = self._c()
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        row = c.execute(
            "SELECT affinity,trust,avoid_until,manual_tier,opted_out,affinity_updated_at FROM relations WHERE group_id=? AND user_id=?",
            (gid, uid),
        ).fetchone()
        if row and int(row[4]):
            return Relation(opted_out=True), 0.0
        old = float(row[0]) if row else 0.0
        old_ts = float(row[5]) if row else now
        old = decay_affinity(old, now - old_ts)
        trust = float(row[1]) if row else 20.0
        avoid_until = max(float(row[2]) if row else 0.0, now + avoid_seconds)
        manual_tier = str(row[3] or "") if row else ""
        used = c.execute(
            "SELECT COALESCE(SUM(applied_delta),0) FROM events WHERE group_id=? AND user_id=? AND day=?",
            (gid, uid, day),
        ).fetchone()[0]
        applied = clamp_delta(float(affinity_delta), float(used))
        affinity = max(-100.0, min(100.0, old + applied))
        trust = max(0.0, min(100.0, trust + float(trust_delta)))
        c.execute(
            """INSERT INTO relations(group_id,user_id,affinity,trust,affinity_updated_at,avoid_until,manual_tier,opted_out,updated_at)
               VALUES(?,?,?,?,?,?,?,0,?) ON CONFLICT(group_id,user_id) DO UPDATE SET
               affinity=excluded.affinity,trust=excluded.trust,affinity_updated_at=excluded.affinity_updated_at,
               avoid_until=excluded.avoid_until,updated_at=excluded.updated_at""",
            (gid, uid, affinity, trust, now, avoid_until, manual_tier, now),
        )
        c.execute(
            "INSERT INTO events(group_id,user_id,ts,day,kind,requested_delta,applied_delta,trust_delta,reason) VALUES(?,?,?,?,?,?,?,?,?)",
            (gid, uid, now, day, kind, affinity_delta, applied, trust_delta, reason),
        )
        c.commit()
        return Relation(affinity, trust, avoid_until, manual_tier, False), applied

    async def opt_out(self, gid: str, uid: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._opt_out, gid, uid)

    def _opt_out(self, gid, uid):
        c = self._c()
        now = time.time()
        c.execute("DELETE FROM events WHERE group_id=? AND user_id=?", (gid, uid))
        c.execute(
            """INSERT INTO relations(group_id,user_id,affinity,trust,affinity_updated_at,avoid_until,manual_tier,opted_out,updated_at)
               VALUES(?,?,0,20,0,0,'',1,?) ON CONFLICT(group_id,user_id) DO UPDATE SET
               affinity=0,trust=20,avoid_until=0,manual_tier='',opted_out=1,updated_at=excluded.updated_at""",
            (gid, uid, now),
        )
        c.commit()

    async def set_manual(self, gid: str, uid: str, tier: str) -> Relation:
        async with self._lock:
            return await asyncio.to_thread(self._set_manual, gid, uid, tier)

    def _set_manual(self, gid, uid, tier):
        c = self._c()
        now = time.time()
        c.execute(
            """INSERT INTO relations(group_id,user_id,affinity,trust,affinity_updated_at,avoid_until,manual_tier,opted_out,updated_at)
               VALUES(?,?,0,20,0,0,?,0,?) ON CONFLICT(group_id,user_id) DO UPDATE SET
               manual_tier=excluded.manual_tier,opted_out=0,updated_at=excluded.updated_at""",
            (gid, uid, tier, now),
        )
        c.commit()
        return self._relation(gid, uid)

    async def stats(self, gid: str) -> tuple[int, int]:
        async with self._lock:
            return await asyncio.to_thread(self._stats, gid)

    def _stats(self, gid):
        c = self._c()
        people = c.execute("SELECT COUNT(*) FROM relations WHERE group_id=? AND opted_out=0", (gid,)).fetchone()[0]
        events = c.execute("SELECT COUNT(*) FROM events WHERE group_id=?", (gid,)).fetchone()[0]
        return int(people), int(events)


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self.store = Store(DB_PATH)
        self.seen = 0
        self.scored = 0
        logger.info("[social] 已加载：开=%s 模式=%s 群=%s 衰减=%.0f天 日上限=±%.0f", ENABLED, MODE, "、".join(sorted(GROUPS)) or "无", HALFLIFE_DAYS, DAILY_CAP)

    async def terminate(self) -> None:
        self.store.close()

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def observe(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            if not gid or gid not in GROUPS or not uid or uid == str(event.get_self_id() or ""):
                return
            text = (event.get_message_str() or "").strip()
            if not text or text.startswith("/"):
                return
            self.seen += 1
            directed = bool(getattr(event, "is_at_or_wake_command", False))
            kind, affinity, trust, avoid, reason = classify_event(text, directed)
            if not kind:
                return
            relation, applied = await self.store.apply(gid, uid, kind, affinity, trust, avoid, reason)
            self.scored += 1
            logger.info("[social] %s %s(%s) %s requested=%+.0f applied=%+.0f -> %s %.1f/信任%.1f", MODE, event.get_sender_name() or uid, uid, kind, affinity, applied, tier_of(relation), relation.affinity, relation.trust)
        except BaseException as exc:
            logger.debug("[social] 观察失败（不影响群聊）：%r", exc)

    @filter.on_llm_request(priority=95)
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED or MODE == "shadow":
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            if not gid or gid not in GROUPS or not uid:
                return
            relation = await self.store.relation(gid, uid)
            direct_request = bool(getattr(event, "is_at_or_wake_command", False) or _DIRECT_REQUEST_RE.search(event.get_message_str() or ""))
            block = render_context(relation, direct_request)
            if not block:
                return
            req.extra_user_content_parts.append(TextPart(text=block))
            logger.info("[social] 注入 %s(%s)：%s", event.get_sender_name() or uid, uid, tier_of(relation))
        except BaseException as exc:
            logger.debug("[social] 注入失败（不影响对话）：%r", exc)

    @filter.command("我和鱼")
    async def cmd_me(self, event: AstrMessageEvent):
        gid, uid = str(event.get_group_id() or ""), str(event.get_sender_id() or "")
        if not gid or not uid:
            return
        relation = await self.store.relation(gid, uid)
        if relation.opted_out:
            yield event.plain_result("你已退出社交关系记录；需要重新加入请联系群主。")
            return
        boundary = "｜临时少打扰中" if relation.avoid_until > time.time() else ""
        yield event.plain_result("我这边和你的互动档位：%s%s。它只影响口吻，不影响回答质量；/社交退出 可删除记录。" % (tier_of(relation), boundary))

    @filter.command("社交退出")
    async def cmd_opt_out(self, event: AstrMessageEvent):
        gid, uid = str(event.get_group_id() or ""), str(event.get_sender_id() or "")
        if not gid or not uid:
            return
        await self.store.opt_out(gid, uid)
        yield event.plain_result("已删除你在本群的社交关系记录，并停止后续收集。")

    @filter.command("关系状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        gid = str(event.get_group_id() or "")
        people, events = await self.store.stats(gid)
        yield event.plain_result("[社交] 模式=%s｜群=%s｜档案=%d｜事件=%d｜已看=%d｜命中=%d\nshadow 只记分不改回复；style 才注入口吻。" % (MODE, gid or "无", people, events, self.seen, self.scored))

    @filter.command("关系标签")
    async def cmd_label(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        args = (event.get_message_str() or "").strip().split()
        if len(args) != 3 or args[2] not in {"避让", "疏离", "陌生", "熟人", "亲近"}:
            yield event.plain_result("用法：/关系标签 QQ号 避让|疏离|陌生|熟人|亲近")
            return
        gid = str(event.get_group_id() or "")
        if not gid:
            return
        relation = await self.store.set_manual(gid, args[1], args[2])
        yield event.plain_result("已把 %s 设为%s（仅影响口吻，仍会正常回答）。" % (args[1], tier_of(relation)))
