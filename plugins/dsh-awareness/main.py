"""dsh-awareness —— 群事件感知（含撤回），以「类懒加载」方式注入。

用户需求原话：「他现在的认知还不够仔细，比如说群友发了什么撤回了什么等，
但是认知太多又不行，做一个类懒加载的认知。」

做法：

1. **采集**：所有群消息 + OneBot notice 事件（撤回/戳一戳）都落进本地事件环
   （SQLite，默认保留 120 分钟 / 4000 条）。
2. **撤回**：平台只给被撤回消息的 message_id，不给正文。所以每条消息入库时
   保存 message_id，收到 group_recall 时回查还原「谁撤了什么」。
3. **注入（懒加载）**：
   - 常驻：一行薄索引（约 80~110 字），只说「有什么可查」，不展开内容；
   - 按需：本轮被点名、是群主在问、或话里提到刚才/撤回/谁说的，才展开明细
     （默认最近 8 分钟最多 18 条，硬预算 700 字）。

零依赖外部服务；所有内容经敏感过滤（凭据/隐私命中整条丢弃）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType

# AstrBot 以文件方式加载插件目录，不保证它是可 import 的 Python 包。
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from event_ring import (  # noqa: E402
    KIND_OTHER,
    KIND_RECALL,
    clean_text,
    classify,
    describe,
    needs_detail,
    render_detail,
    render_index,
    scrub_for_store,
)


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no", ""}


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _set(name: str, default: str = "") -> set:
    raw = os.environ.get(name, default)
    return {item.strip() for item in str(raw).split(",") if item.strip()}


ENABLED = _flag("DSH_AWARENESS")
GROUPS = _set("DSH_AWARENESS_GROUPS")
OWNERS = _set("DSH_AWARENESS_OWNER", "2774067216")
DB = os.environ.get("DSH_AWARENESS_DB", "/AstrBot/data/dsh_awareness.db")

KEEP_MIN = _int("DSH_AWARENESS_KEEP_MIN", 120, 5, 1440)
KEEP_MAX = _int("DSH_AWARENESS_KEEP_MAX", 4000, 100, 50000)
INDEX_MIN = _int("DSH_AWARENESS_INDEX_MIN", 30, 1, 1440)
DETAIL_MIN = _int("DSH_AWARENESS_DETAIL_MIN", 8, 1, 240)
DETAIL_MAX = _int("DSH_AWARENESS_DETAIL_MAX", 18, 1, 60)
DETAIL_BUDGET = _int("DSH_AWARENESS_DETAIL_BUDGET", 700, 200, 3000)
TEXT_MAX = _int("DSH_AWARENESS_TEXT_MAX", 40, 8, 200)
INDEX_ENABLED = _flag("DSH_AWARENESS_INDEX")
DETAIL_ENABLED = _flag("DSH_AWARENESS_DETAIL")
# 单条事件正文入库上限（明细阶段再截到 TEXT_MAX）。
STORE_TEXT_MAX = 120

_stat = {
    "seen": 0,
    "stored": 0,
    "recall": 0,
    "recall_lost": 0,
    "skipped_group": 0,
    "index": 0,
    "detail": 0,
    "trigger": 0,
}


def _rg(raw, key, default=None):
    """notice 的 raw_message 可能是 dict 也可能是 aiocqhttp.Event，三种取法都试。"""
    if isinstance(raw, dict):
        return raw.get(key, default)
    try:
        return raw[key]
    except BaseException:
        pass
    return getattr(raw, key, default)


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=3.0)
    con.execute("PRAGMA busy_timeout=3000")
    return con


def init_db() -> None:
    parent = os.path.dirname(DB)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = _conn()
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS event ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  ts REAL NOT NULL,"
            "  group_id TEXT NOT NULL,"
            "  uid TEXT NOT NULL DEFAULT '',"
            "  name TEXT NOT NULL DEFAULT '',"
            "  kind TEXT NOT NULL,"
            "  text TEXT NOT NULL DEFAULT '',"
            "  message_id TEXT NOT NULL DEFAULT '',"
            "  extra TEXT NOT NULL DEFAULT '{}'"
            ")"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_event_gid_ts ON event(group_id, ts)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_event_mid ON event(message_id)")
        con.commit()
    finally:
        con.close()


def _parts_of(event) -> list:
    """把 astrbot 消息组件压成 event_ring 能吃的轻量结构。"""
    out = []
    for comp in getattr(event.message_obj, "message", None) or []:
        name = type(comp).__name__
        item = {"type": name}
        text = getattr(comp, "text", None)
        if text:
            item["text"] = str(text)
        qq = getattr(comp, "qq", None)
        if qq is not None:
            item["qq"] = str(qq)
        out.append(item)
    return out


def _at_bot(event, me: str) -> bool:
    for comp in getattr(event.message_obj, "message", None) or []:
        name = type(comp).__name__
        if name == "AtAll":
            return True
        if name == "At" and me and str(getattr(comp, "qq", "") or "") == me:
            return True
    return False


class AwarenessFilter(CustomFilter):
    """只放行本群的消息与通知事件。

    照抄 dsh-poke 的成熟做法：撤回是 notice 事件，message_str 为空，
    必须用 CustomFilter 才能稳定走到 handler。
    """

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return False
            if event.get_platform_name() == "webchat":
                return False
            gid = str(event.get_group_id() or "")
            if not gid or (GROUPS and gid not in GROUPS):
                return False
            raw = getattr(event.message_obj, "raw_message", None)
            post = _rg(raw, "post_type")
            if post and post not in ("message", "notice"):
                return False
            return True
        except BaseException:
            return False


class Main(star.Star):
    def __init__(self, context, config=None):
        super().__init__(context, config)
        self._last_cleanup = 0.0
        try:
            init_db()
        except BaseException as exc:
            logger.error("[awareness] 初始化失败，感知停用: %r", exc)
            return
        logger.info(
            "[awareness] 已加载：%s 群=%s 保留%d分钟/%d条 索引=%s(%d分钟) "
            "明细=%s(%d分钟/最多%d条/预算%d字) 库=%s",
            "开" if ENABLED else "关",
            "、".join(sorted(GROUPS)) or "全部",
            KEEP_MIN, KEEP_MAX,
            "开" if INDEX_ENABLED else "关", INDEX_MIN,
            "开" if DETAIL_ENABLED else "关", DETAIL_MIN, DETAIL_MAX, DETAIL_BUDGET,
            DB,
        )

    # --- 写入 -------------------------------------------------------------
    def _cleanup(self, now: float) -> None:
        """过期清理：按时间窗 + 条数硬顶。最多每分钟做一次。"""
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
        con = _conn()
        try:
            con.execute("DELETE FROM event WHERE ts<?", (now - KEEP_MIN * 60,))
            con.execute(
                "DELETE FROM event WHERE id NOT IN "
                "(SELECT id FROM event ORDER BY ts DESC LIMIT ?)",
                (KEEP_MAX,),
            )
            con.commit()
        except BaseException as exc:
            logger.debug("[awareness] 清理失败: %r", exc)
        finally:
            con.close()

    def _insert(self, row: dict) -> bool:
        con = _conn()
        try:
            mid = str(row.get("message_id") or "")
            if mid:
                # 同一个 message_id 只记一次（通知与消息可能重复到达）。
                exists = con.execute(
                    "SELECT 1 FROM event WHERE message_id=? LIMIT 1", (mid,)
                ).fetchone()
                if exists:
                    return False
            con.execute(
                "INSERT INTO event(ts,group_id,uid,name,kind,text,message_id,extra)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    float(row.get("ts") or time.time()),
                    str(row.get("group_id") or ""),
                    str(row.get("uid") or ""),
                    str(row.get("name") or ""),
                    str(row.get("kind") or KIND_OTHER),
                    str(row.get("text") or ""),
                    mid,
                    json.dumps(row.get("extra") or {}, ensure_ascii=False),
                ),
            )
            con.commit()
            return True
        except BaseException as exc:
            logger.debug("[awareness] 写入失败: %r", exc)
            return False
        finally:
            con.close()

    def _record_event(self, event: AstrMessageEvent, gid: str) -> None:
        """消息事件入库；其中撤回通知单独还原原文。"""
        raw = getattr(event.message_obj, "raw_message", None)
        post = _rg(raw, "post_type")
        if post == "notice":
            self._record_notice(raw, gid)
            return

        uid = str(event.get_sender_id() or "")
        me = str(event.get_self_id() or "")
        if not uid or uid == me:
            return
        kind, text, extra = classify(_parts_of(event))
        text = scrub_for_store(text, STORE_TEXT_MAX)
        if text == "" and kind == "text":
            # 纯文本但整条被敏感过滤掉 —— 不留空壳记录。
            return
        if not text and kind == KIND_OTHER:
            return
        row = {
            "ts": time.time(),
            "group_id": gid,
            "uid": uid,
            "name": clean_text(event.get_sender_name() or uid, 24),
            "kind": kind,
            "text": text,
            "message_id": str(getattr(event.message_obj, "message_id", "") or ""),
            "extra": extra,
        }
        if self._insert(row):
            _stat["stored"] += 1

    def _record_notice(self, raw, gid: str) -> None:
        notice_type = _rg(raw, "notice_type")
        if notice_type != "group_recall":
            return
        mid = str(_rg(raw, "message_id") or "")
        target_uid = str(_rg(raw, "user_id") or "")
        operator = str(_rg(raw, "operator_id") or "")
        me = ""
        origin = None
        if mid:
            con = _conn()
            try:
                origin = con.execute(
                    "SELECT ts,uid,name,kind,text,extra FROM event"
                    " WHERE message_id=? AND group_id=?"
                    " ORDER BY ts DESC LIMIT 1",
                    (mid, gid),
                ).fetchone()
            except BaseException:
                origin = None
            finally:
                con.close()
        extra = {
            "by": operator or target_uid,
            "by_name": "",
            "target": target_uid,
        }
        text = ""
        if origin:
            text = scrub_for_store(origin[4] or "", STORE_TEXT_MAX)
            extra["orig_name"] = clean_text(origin[2] or "", 24)
            extra["orig_uid"] = str(origin[1] or "")
            extra["orig_kind"] = str(origin[3] or "")
        else:
            # 原文没留存（超出保留窗或机器人当时不在）——照实记，不编内容。
            _stat["recall_lost"] += 1
        by_name = ""
        if operator and operator == target_uid:
            by_name = extra.get("orig_name") or ""
        extra["by_name"] = by_name
        row = {
            "ts": time.time(),
            "group_id": gid,
            "uid": operator or target_uid,
            "name": by_name or (extra.get("orig_name") or ""),
            "kind": KIND_RECALL,
            "text": text,
            "message_id": "recall:%s" % (mid or time.time()),
            "extra": extra,
        }
        if self._insert(row):
            _stat["recall"] += 1
            logger.info(
                "[awareness] 记录撤回 gid=%s 原发送者=%s 原文=%s",
                gid, extra.get("orig_uid") or target_uid or "?",
                ("有(%d字)" % len(text)) if text else "未留存",
            )

    # --- 采集入口 ---------------------------------------------------------
    @filter.custom_filter(AwarenessFilter)
    async def on_event(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid:
                _stat["skipped_group"] += 1
                return
            _stat["seen"] += 1
            self._record_event(event, gid)
            self._cleanup(time.time())
        except BaseException as exc:
            logger.debug("[awareness] 采集异常: %r", exc)

    # --- 读取 -------------------------------------------------------------
    def _load(self, gid: str, since: float, limit: int = 1500) -> list:
        con = _conn()
        try:
            rows = con.execute(
                "SELECT ts,uid,name,kind,text,extra FROM event"
                " WHERE group_id=? AND ts>=? ORDER BY ts ASC LIMIT ?",
                (gid, since, limit),
            ).fetchall()
        except BaseException:
            return []
        finally:
            con.close()
        out = []
        for ts, uid, name, kind, text, extra in rows:
            try:
                extra_obj = json.loads(extra or "{}")
            except (TypeError, ValueError):
                extra_obj = {}
            out.append({
                "ts": ts, "uid": uid, "name": name, "kind": kind,
                "text": text, "extra": extra_obj,
            })
        return out

    # priority=110：排在 selfaware(100) 同层之后，索引块跟着机器自我走。
    @filter.on_llm_request(priority=110)
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED:
            return
        try:
            gid = str(getattr(event.message_obj, "group_id", "") or "")
            if not gid or (GROUPS and gid not in GROUPS):
                _stat["skipped_group"] += 1
                return
            now = time.time()
            rows = self._load(gid, now - INDEX_MIN * 60)
            if not rows:
                return

            blocks = []
            if INDEX_ENABLED:
                block = render_index(rows, window_min=INDEX_MIN, now=now)
                if block:
                    blocks.append(block)
                    _stat["index"] += 1

            if DETAIL_ENABLED:
                uid = str(event.get_sender_id() or "")
                me = str(event.get_self_id() or "")
                body = event.get_message_str() or ""
                at_bot = _at_bot(event, me)
                from_owner = bool(uid and uid in OWNERS)
                if needs_detail(body, at_bot=at_bot, from_owner=from_owner):
                    _stat["trigger"] += 1
                    recent = [r for r in rows if float(r.get("ts") or 0) >= now - DETAIL_MIN * 60]
                    detail = render_detail(
                        recent,
                        window_min=DETAIL_MIN,
                        limit=DETAIL_MAX,
                        budget=DETAIL_BUDGET,
                        text_max=TEXT_MAX,
                        now=now,
                    )
                    if detail:
                        blocks.append(detail)
                        _stat["detail"] += 1

            for block in blocks:
                req.extra_user_content_parts.append(TextPart(text=block))
            if blocks:
                logger.info(
                    "[awareness] 注入 索引=%d 明细=%d 窗口%d条 共%d块/%d字",
                    _stat["index"], _stat["detail"], len(rows),
                    len(blocks), sum(len(b) for b in blocks),
                )
        except BaseException as exc:
            logger.debug("[awareness] 注入异常: %r", exc)

    # --- 指令 -------------------------------------------------------------
    @filter.command("群感知")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看群事件感知的采集与注入情况（群主用）。"""
        uid = str(event.get_sender_id() or "")
        if OWNERS and uid not in OWNERS:
            yield event.plain_result("这个指令只有群主能用。")
            return
        gid = str(event.get_group_id() or "")
        now = time.time()
        rows = self._load(gid, now - INDEX_MIN * 60) if gid else []
        lines = [
            describe(rows, now),
            "",
            "采集：看到%d条，入库%d条，撤回%d条（原文未留存%d次）"
            % (_stat["seen"], _stat["stored"], _stat["recall"], _stat["recall_lost"]),
            "注入：索引%d次，触发%d次，明细%d次"
            % (_stat["index"], _stat["trigger"], _stat["detail"]),
            "配置：保留%d分钟/%d条，索引窗%d分钟，明细窗%d分钟最多%d条/%d字"
            % (KEEP_MIN, KEEP_MAX, INDEX_MIN, DETAIL_MIN, DETAIL_MAX, DETAIL_BUDGET),
        ]
        yield event.plain_result("\n".join(lines))
