# -*- coding: utf-8 -*-
"""dsh-desire：分层持续动机状态机（style 模式，不新增主动消息链）。"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType

from .desire_logic import (
    BASELINES, DRIVES, LAYERS, NAMES, choose_primary, classify, decay, default_state,
    foundational_relevance, public_summary, render_block, telemetry_signals, transition,
)


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_DESIRE")
MODE = os.environ.get("DSH_DESIRE_MODE", "style").strip().lower()
if MODE not in {"shadow", "style", "action"}:
    MODE = "shadow"
GROUPS = _set("DSH_DESIRE_GROUPS", "100000001")
OWNERS = _set("DSH_DESIRE_OWNER", "2774000001")
DB_PATH = Path(os.environ.get("DSH_DESIRE_DB", "/AstrBot/data/dsh_desire.db"))
SELFAWARE_DB = Path(os.environ.get("DSH_DESIRE_SELFAWARE_DB", "/AstrBot/data/dsh_selfaware.db"))
ACTIVE_ON = max(40.0, min(90.0, float(os.environ.get("DSH_DESIRE_ACTIVE_ON", "60"))))
ACTIVE_OFF = max(10.0, min(ACTIVE_ON - 5, float(os.environ.get("DSH_DESIRE_ACTIVE_OFF", "35"))))
DELTA_MAX = max(1.0, min(30.0, float(os.environ.get("DSH_DESIRE_EVENT_DELTA_MAX", "15"))))
MIN_DWELL = max(0.0, float(os.environ.get("DSH_DESIRE_MIN_DWELL", "300")))
INJECT_BUDGET = max(100, min(400, int(os.environ.get("DSH_DESIRE_INJECT_BUDGET", "180"))))
TZ = os.environ.get("DSH_DESIRE_TZ", "Asia/Shanghai")
EVENT_KEEP_DAYS = max(1, min(30, int(os.environ.get("DSH_DESIRE_EVENT_KEEP_DAYS", "7"))))


def _synthetic(event: AstrMessageEvent) -> bool:
    """兼容 get_extra 不存在/抛错的适配器；失败时按真人事件继续。"""
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
    return any(name in text for name in ("大肥鱼", "肥鱼", "小鲸鱼", "鲸鱼娘"))


def _hour(now: float) -> int:
    try:
        return datetime.fromtimestamp(now, ZoneInfo(TZ)).hour
    except BaseException:
        return datetime.fromtimestamp(now).hour


class DesireStore:
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
                    CREATE TABLE IF NOT EXISTS schema_version(
                        version INTEGER NOT NULL
                    );
                    INSERT INTO schema_version(version)
                        SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
                    CREATE TABLE IF NOT EXISTS drive_state(
                        group_id TEXT NOT NULL,
                        drive TEXT NOT NULL,
                        intensity REAL NOT NULL,
                        baseline REAL NOT NULL,
                        phase TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        active_since REAL NOT NULL DEFAULT 0,
                        cooldown_until REAL NOT NULL DEFAULT 0,
                        expressions_today INTEGER NOT NULL DEFAULT 0,
                        expression_day TEXT NOT NULL DEFAULT '',
                        last_signal TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY(group_id, drive)
                    );
                    CREATE TABLE IF NOT EXISTS signal_events(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        drive TEXT NOT NULL,
                        delta REAL NOT NULL,
                        kind TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        UNIQUE(event_key, drive)
                    );
                    CREATE INDEX IF NOT EXISTS idx_desire_event_time
                        ON signal_events(created_at);
                    CREATE TABLE IF NOT EXISTS bridge_cursor(
                        source TEXT PRIMARY KEY,
                        last_id INTEGER NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    UPDATE schema_version SET version=3 WHERE version<3;
                    """
                )
        finally:
            os.umask(old_umask)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        return {key: row[key] for key in row.keys()}

    def load_group(self, gid: str, now: float) -> list[dict]:
        with self.lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM drive_state WHERE group_id=?", (gid,)
            ).fetchall()
        found = {str(row["drive"]): self._row(row) for row in rows}
        return [decay(found.get(drive, default_state(drive, now)), now) for drive in DRIVES]

    def save(self, gid: str, state: dict) -> None:
        with self.lock, self._connect() as con:
            con.execute(
                """
                INSERT INTO drive_state(
                    group_id,drive,intensity,baseline,phase,updated_at,active_since,
                    cooldown_until,expressions_today,expression_day,last_signal
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(group_id,drive) DO UPDATE SET
                    intensity=excluded.intensity, baseline=excluded.baseline,
                    phase=excluded.phase, updated_at=excluded.updated_at,
                    active_since=excluded.active_since,
                    cooldown_until=excluded.cooldown_until,
                    expressions_today=excluded.expressions_today,
                    expression_day=excluded.expression_day,
                    last_signal=excluded.last_signal
                """,
                (
                    gid, state["drive"], state["intensity"], state["baseline"],
                    state["phase"], state["updated_at"], state["active_since"],
                    state["cooldown_until"], state["expressions_today"],
                    state["expression_day"], state["last_signal"],
                ),
            )

    def apply(self, gid: str, event_key: str, drive: str, delta: float, kind: str, now: float) -> dict | None:
        """原子写入事件与状态；重复事件返回 None，不重复结算。"""
        with self.lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            inserted = con.execute(
                "INSERT OR IGNORE INTO signal_events(event_key,group_id,drive,delta,kind,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (event_key, gid, drive, max(-DELTA_MAX, min(DELTA_MAX, delta)), kind, now),
            )
            if inserted.rowcount == 0:
                return None
            row = con.execute(
                "SELECT * FROM drive_state WHERE group_id=? AND drive=?", (gid, drive)
            ).fetchone()
            previous = self._row(row) if row else default_state(drive, now)
            state = transition(
                previous, delta, kind, now, active_on=ACTIVE_ON,
                active_off=ACTIVE_OFF, delta_max=DELTA_MAX, min_dwell=MIN_DWELL,
            )
            con.execute(
                """
                INSERT INTO drive_state(
                    group_id,drive,intensity,baseline,phase,updated_at,active_since,
                    cooldown_until,expressions_today,expression_day,last_signal
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(group_id,drive) DO UPDATE SET
                    intensity=excluded.intensity, baseline=excluded.baseline,
                    phase=excluded.phase, updated_at=excluded.updated_at,
                    active_since=excluded.active_since,
                    cooldown_until=excluded.cooldown_until,
                    expressions_today=excluded.expressions_today,
                    expression_day=excluded.expression_day,
                    last_signal=excluded.last_signal
                """,
                (
                    gid, drive, state["intensity"], state["baseline"], state["phase"],
                    state["updated_at"], state["active_since"], state["cooldown_until"],
                    state["expressions_today"], state["expression_day"], state["last_signal"],
                ),
            )
        return state

    def prune(self, now: float) -> None:
        cutoff = now - EVENT_KEEP_DAYS * 86400
        with self.lock, self._connect() as con:
            con.execute("DELETE FROM signal_events WHERE created_at < ?", (cutoff,))

    def cursor(self, source: str) -> int | None:
        with self.lock, self._connect() as con:
            row = con.execute("SELECT last_id FROM bridge_cursor WHERE source=?", (source,)).fetchone()
        return int(row[0]) if row else None

    def set_cursor(self, source: str, last_id: int, now: float) -> None:
        with self.lock, self._connect() as con:
            con.execute(
                "INSERT INTO bridge_cursor(source,last_id,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET last_id=excluded.last_id,updated_at=excluded.updated_at",
                (source, int(last_id), float(now)),
            )

    def recent(self, gid: str, limit: int = 8) -> list[sqlite3.Row]:
        with self.lock, self._connect() as con:
            return con.execute(
                "SELECT drive,delta,kind,created_at FROM signal_events "
                "WHERE group_id=? ORDER BY id DESC LIMIT ?", (gid, limit)
            ).fetchall()


class SelfAwareBridge:
    """只读消费新鲜 self_revision；持久游标防止重启重放历史故障。"""

    SOURCE = "selfaware_revision_v1"

    def __init__(self, path: Path, store: DesireStore) -> None:
        self.path = path
        self.store = store
        self.fresh_seconds = 900.0
        self.last_id = store.cursor(self.SOURCE)
        if self.last_id is None:
            self.last_id = self._max_id()
            store.set_cursor(self.SOURCE, self.last_id, time.time())

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
        return con

    def _max_id(self) -> int:
        try:
            with self._connect() as con:
                return int(con.execute("SELECT COALESCE(MAX(id),0) FROM self_revision").fetchone()[0])
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return 0

    def changes(self, now: float) -> list[tuple[int, object]]:
        try:
            with self._connect() as con:
                rows = con.execute(
                    "SELECT id,ts,subject,old_value,new_value FROM self_revision "
                    "WHERE id>? AND layer='short' ORDER BY id LIMIT 32", (self.last_id,)
                ).fetchall()
            if not rows:
                return []
            self.last_id = max(int(row["id"]) for row in rows)
            self.store.set_cursor(self.SOURCE, self.last_id, now)
            result = []
            for row in rows:
                if 0 <= now - float(row["ts"]) <= self.fresh_seconds:
                    signals = telemetry_signals(
                        str(row["subject"]), str(row["old_value"]), str(row["new_value"])
                    )
                    if signals:
                        result.append((int(row["id"]), signals))
            return result
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return []


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        super().__init__(context)
        self.context = context
        self.store = DesireStore(DB_PATH)
        self.selfaware = SelfAwareBridge(SELFAWARE_DB, self.store)
        self._fresh_foundational: dict[str, float] = {}
        self._seen: dict[str, float] = {}
        self._seen_count = 0
        self._injected = 0
        self._errors = 0
        logger.info(
            "[desire] 已加载：%s 模式=%s 群=%s active=%.0f/%.0f 预算=%d DB=%s",
            "开" if ENABLED else "关", MODE, ",".join(sorted(GROUPS)) or "无",
            ACTIVE_ON, ACTIVE_OFF, INJECT_BUDGET, DB_PATH,
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def observe(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            if event.get_platform_name() == "webchat":
                return
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            if not gid or gid not in GROUPS or uid == str(event.get_self_id() or ""):
                return
            if _synthetic(event):
                return
            text = str(event.get_message_str() or "").strip()
            if not text or text.startswith("/"):
                return
            raw = getattr(event, "message_obj", None)
            mid = str(getattr(raw, "message_id", "") or "")
            now = time.time()
            key = "%s:%s" % (gid, mid or ("%.0f:%s:%s" % (now // 5, uid, text[:60])))
            if now - self._seen.get(key, 0.0) < 5:
                return
            self._seen[key] = now
            if len(self._seen) > 500:
                self._seen = {k: v for k, v in self._seen.items() if now - v < 3600}
            signals = classify(text, _directed(event, text), _hour(now))
            machine_changes = self.selfaware.changes(now)
            if not signals and not machine_changes:
                return
            changed = []
            for signal in signals:
                state = self.store.apply(gid, key, signal.drive, signal.delta, signal.kind, now)
                if state is not None:
                    changed.append("%s%+g→%.0f" % (NAMES[signal.drive], signal.delta, state["intensity"]))
            for sense_id, sense_signals in machine_changes:
                for signal in sense_signals:
                    sense_key = "%s:selfaware:%d" % (gid, sense_id)
                    state = self.store.apply(gid, sense_key, signal.drive, signal.delta, signal.kind, now)
                    if state is not None:
                        changed.append("%s%+g→%.0f" % (NAMES[signal.drive], signal.delta, state["intensity"]))
                        self._fresh_foundational[signal.drive] = now + 300
            if not changed:
                return
            self._seen_count += 1
            if self._seen_count % 50 == 0:
                self.store.prune(now)
            logger.info("[desire] gid=%s 状态变化：%s", gid, "；".join(changed))
        except BaseException as exc:
            self._errors += 1
            logger.warning("[desire] 观察失败，跳过: %r", exc)

    # 排在 decide/clarify 等拦截钩子之后；只有请求没被截停才注入。
    @filter.on_llm_request(priority=90)
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED or MODE == "shadow":
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or gid not in GROUPS:
                return
            now = time.time()
            text = str(event.get_message_str() or "").strip()
            allowed = foundational_relevance(text)
            allowed.update(drive for drive, until in self._fresh_foundational.items() if now <= until)
            states = self.store.load_group(gid, now)
            # 只有明确指向机器人的本能话题，或刚消费到的真实机器遥测，才允许底层本能表达。
            if not _directed(event, text):
                allowed.clear()
            primary = choose_primary(states, now, ACTIVE_ON, allowed_foundational=allowed)
            if not primary:
                return
            block = render_block(primary, INJECT_BUDGET)
            if not block:
                return
            req.extra_user_content_parts.append(TextPart(text=block))
            self._injected += 1
            logger.info(
                "[desire] gid=%s 注入主欲望=%s(%.0f) 模式=%s",
                gid, primary["drive"], primary["intensity"], MODE,
            )
        except BaseException as exc:
            self._errors += 1
            logger.warning("[desire] 注入失败，跳过: %r", exc)

    @filter.command("想干嘛")
    async def public_status(self, event: AstrMessageEvent):
        gid = str(event.get_group_id() or "")
        if not ENABLED or not gid or gid not in GROUPS:
            yield event.plain_result("现在没什么特别想做的。")
            return
        try:
            yield event.plain_result(public_summary(self.store.load_group(gid, time.time()), time.time()))
        except BaseException:
            yield event.plain_result("现在没什么特别强的念头，先随便待着。")

    @filter.command("欲望状态")
    async def owner_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        gid = str(event.get_group_id() or "")
        now = time.time()
        states = self.store.load_group(gid, now) if gid else []
        primary = choose_primary(states, now, ACTIVE_ON, allowed_foundational=set(DRIVES))
        lines = [
            "欲望系统 %s｜模式 %s｜群 %s" % ("开" if ENABLED else "关", MODE, gid or "无"),
            "主欲望：%s" % (
                "%s·%s %.0f" % (LAYERS[primary["drive"]], NAMES[primary["drive"]], primary["intensity"])
                if primary else "暂无"
            ),
            "状态：" + "／".join(
                "%s:%s %.0f(%s)" % (LAYERS[s["drive"]], NAMES[s["drive"]], s["intensity"], s["phase"])
                for s in states
            ),
            "累计：变化消息 %d／注入 %d／异常 %d" % (self._seen_count, self._injected, self._errors),
        ]
        recent = self.store.recent(gid, 5) if gid else []
        if recent:
            lines.append("最近：" + "；".join(
                "%s%+g(%s)" % (NAMES.get(str(r["drive"]), str(r["drive"])), r["delta"], r["kind"])
                for r in recent
            ))
        yield event.plain_result("\n".join(lines))
