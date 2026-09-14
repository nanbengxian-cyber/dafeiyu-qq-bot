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
from astrbot.core.platform.message_type import MessageType  # noqa: F401

# AstrBot 以文件方式加载插件目录，不保证它是可 import 的 Python 包。
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
# 热重载时 AstrBot 只清 data.plugins.* 命名空间，而 event_ring 是顶层模块，
# 会留在 sys.modules 里，导致新版 main.py 仍 import 到旧模块（实测报
# "cannot import name 'render_poke_note'"）。先掀掉缓存再导入。
sys.modules.pop("event_ring", None)

from event_ring import (  # noqa: E402
    KIND_OTHER,
    KIND_POKE,
    KIND_RECALL,
    clean_text,
    classify,
    describe,
    needs_detail,
    render_detail,
    render_index,
    render_poke_note,
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
OWNERS = _set("DSH_AWARENESS_OWNER", "2774000001")
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
# 戳一戳事实的窗口：被戳后这段时间内的每一轮都告诉它是谁戳的。
POKE_MIN = _int("DSH_AWARENESS_POKE_MIN", 10, 1, 240)
POKE_ENABLED = _flag("DSH_AWARENESS_POKE")
# 单条事件正文入库上限（明细阶段再截到 TEXT_MAX）。
STORE_TEXT_MAX = 120

_stat = {
    "seen": 0,
    "stored": 0,
    "recall": 0,
    "recall_lost": 0,
    "poke": 0,
    "poke_note": 0,
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


def _poke_target(event) -> str:
    """戳一戳戳的是谁。Poke 组件优先，回落到 raw 的 target_id。"""
    for comp in getattr(event.message_obj, "message", None) or []:
        fn = getattr(comp, "target_id", None)
        if callable(fn):
            try:
                got = fn()
            except BaseException:
                got = None
            if got:
                return str(got)
    raw = getattr(event.message_obj, "raw_message", None)
    return str(_rg(raw, "target_id") or "")


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
    """只放行「消息」与「通知」两类事件。

    照抄 dsh-poke 那个已在生产跑通的写法：**判据必须极简**。
    通知类事件（撤回/戳一戳）没有正文，`message_type`、平台名等字段与常规
    消息并不一致，在 filter 里多做一层判断就会把整类通知静默拦掉
    （实测：poke 采集全丢）。群归属等业务判断一律放到 handler 里做。
    """

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            post = _rg(raw, "post_type")
            sub = _rg(raw, "sub_type")
            comps = [type(c).__name__ for c in (getattr(event.message_obj, "message", None) or [])]
            if post != "message" or sub or any("Poke" in c for c in comps):
                # 诊断：非普通消息的事件（通知类 / 戳一戳）原样记下来。
                # 戳一戳采集丢失就卡在这一层，必须能看到它的真实字段。
                try:
                    dump = repr(dict(raw))[:400] if hasattr(raw, "keys") else repr(raw)[:400]
                except BaseException:
                    dump = "<无法 dump>"
                logger.info(
                    "[awareness] 事件诊断 post=%r sub=%r raw_type=%s mtype=%s comps=%s dump=%s",
                    post, sub, type(raw).__name__,
                    str(getattr(event.message_obj, "type", "?")), comps, dump,
                )
            # 诊断期全放行：由 handler 做业务判断，先确认事件能不能走到这里。
            return True
        except BaseException as exc:
            logger.warning("[awareness] filter 异常 %r", exc)
            return True


def _claim_front_of_queue() -> int:
    """把自己的 handler 提到最前面，抢在会 stop_event 的插件之前看到事件。

    AstrBot 的 handler 注册表按 extras_configs["priority"] 降序排，但框架里
    没有任何入口能设置它（恒为 0），实际顺序就等于插件加载顺序 —— 而每次热重载
    本插件都会被 append 到末尾。

    dsh-poke 对戳一戳事件在 finally 里无条件 event.stop_event()（它要防止空消息
    走到 LLM，这么做是对的），排在它后面的 handler 就再也看不到戳一戳了：
    实测 DB 里 poke 一直是 0 条，而同期真实被戳了多次。

    观测方只需要「先看一眼」，不消费事件，所以这里给自己一个真实的优先级。
    返回被提权的 handler 数量。
    """
    try:
        from astrbot.core.star.star_handler import star_handlers_registry
    except BaseException as exc:
        logger.warning("[awareness] 无法导入 handler 注册表: %r", exc)
        return 0
    try:
        handlers = getattr(star_handlers_registry, "_handlers", None)
        if not handlers:
            return 0
        mine = 0
        for handler in handlers:
            path = str(getattr(handler, "handler_module_path", ""))
            if "dsh-awareness" in path or "dsh_awareness" in path:
                handler.extras_configs["priority"] = 100
                mine += 1
        if mine:
            handlers.sort(key=lambda h: -h.extras_configs.get("priority", 0))
        return mine
    except BaseException as exc:
        logger.warning("[awareness] 调整 handler 顺序失败: %r", exc)
        return 0


class Main(star.Star):
    def __init__(self, context, config=None):
        super().__init__(context, config)
        self._last_cleanup = 0.0
        try:
            init_db()
        except BaseException as exc:
            logger.error("[awareness] 初始化失败，感知停用: %r", exc)
            return
        bumped = _claim_front_of_queue()
        logger.info(
            "[awareness] 已加载：%s 群=%s 保留%d分钟/%d条 索引=%s(%d分钟) "
            "明细=%s(%d分钟/最多%d条/预算%d字) 戳一戳=%s(%d分钟内告知谁戳的) "
            "抢位=%d个handler 库=%s",
            "开" if ENABLED else "关",
            "、".join(sorted(GROUPS)) or "全部",
            KEEP_MIN, KEEP_MAX,
            "开" if INDEX_ENABLED else "关", INDEX_MIN,
            "开" if DETAIL_ENABLED else "关", DETAIL_MIN, DETAIL_MAX, DETAIL_BUDGET,
            "开" if POKE_ENABLED else "关", POKE_MIN,
            bumped,
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

        kind, text, extra = classify(_parts_of(event))
        if kind == KIND_POKE:
            # 有些实现把戳一戳当「带 Poke 组件的消息」上报（post_type=message）。
            # 走这条路时通知分支永远不会触发，必须在这里收成同一个 poke 事实，
            # 否则只会留下一条没有「谁戳了谁」的空记录。
            who = str(event.get_sender_id() or "")
            target = _poke_target(event)
            if who and target:
                self._record_poke_row(
                    gid, who, str(event.get_self_id() or ""), target, int(time.time()),
                )
            return

        uid = str(event.get_sender_id() or "")
        me = str(event.get_self_id() or "")
        if not uid or uid == me:
            return
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
        if _rg(raw, "sub_type") == "poke":
            self._record_poke(raw, gid)
            return
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

    def _name_for(self, gid: str, uid: str) -> str:
        """戳一戳通知里平台把昵称写成了 QQ 号；用这个人以前发言时的昵称补全。"""
        if not gid or not uid:
            return ""
        con = _conn()
        try:
            row = con.execute(
                "SELECT name FROM event WHERE group_id=? AND uid=? AND name!='' AND name!=?"
                " ORDER BY ts DESC LIMIT 1",
                (gid, uid, uid),
            ).fetchone()
        except BaseException:
            row = None
        finally:
            con.close()
        return str(row[0]) if row and row[0] else ""

    def _record_poke_row(self, gid: str, who: str, me: str, target: str, stamp) -> None:
        """戳一戳：记下谁戳了谁。

        被戳的是机器人时尤其重要 —— 通知的正文是空的、昵称又是 QQ 号，
        不补这一条，机器人在后续轮次里只会反问「戳谁呀？」。
        戳一戳可能以「通知」或「带 Poke 组件的消息」两种形态到达，统一从这里入库。
        """
        name = self._name_for(gid, who) or who
        extra = {"by": who, "by_name": name, "target": target}
        if me and target == me:
            extra["to_me"] = True
        row = {
            "ts": time.time(),
            "group_id": gid,
            "uid": who,
            "name": name,
            "kind": KIND_POKE,
            "text": "",
            "message_id": "poke:%s:%s:%s" % (who, target, stamp),
            "extra": extra,
        }
        if self._insert(row):
            _stat["poke"] += 1
            logger.info(
                "[awareness] 记录戳一戳 gid=%s %s(%s) -> %s%s",
                gid, name, who, target, "（戳的是机器人）" if extra.get("to_me") else "",
            )

    def _record_poke(self, raw, gid: str) -> None:
        """通知形态的戳一戳（post_type=notice）。"""
        who = str(_rg(raw, "user_id") or "")
        target = str(_rg(raw, "target_id") or "")
        if not who or not target:
            return
        self._record_poke_row(
            gid, who, str(_rg(raw, "self_id") or ""), target,
            _rg(raw, "time") or int(time.time()),
        )

    # --- 采集入口 ---------------------------------------------------------
    @filter.custom_filter(AwarenessFilter)
    async def on_event(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            is_notice = _rg(raw, "post_type") == "notice"
            sub = _rg(raw, "sub_type")
            is_poke = sub == "poke" or _poke_target(event) != ""
            if is_notice:
                # 通知事件量小但关键（撤回/戳一戳）。先落日志再判断，
                # 免得又被某一层过滤静默吃掉 —— 「戳一戳全丢」那次就是这么埋住的。
                logger.info(
                    "[awareness] 收到通知 sub=%s gid=%r raw_gid=%r uid=%s target=%r",
                    sub, str(event.get_group_id() or ""), _rg(raw, "group_id"),
                    str(event.get_sender_id() or ""), _rg(raw, "target_id"),
                )
            # 刻意不检查 message_type：戳一戳可能不带 group_id，adapter 会把它
            # 归成 FRIEND_MESSAGE，按消息类型判断就会把这一整类丢掉。
            # 群归属一律只看 gid，够用且不会误伤。
            gid = str(event.get_group_id() or _rg(raw, "group_id") or "")
            if not gid and is_poke and len(GROUPS) == 1:
                # 只监控一个群时兜底归到它，否则这条事实会整条丢掉。
                gid = next(iter(GROUPS))
            if not gid or (GROUPS and gid not in GROUPS):
                _stat["skipped_group"] += 1
                return
            _stat["seen"] += 1
            self._record_event(event, gid)
            self._cleanup(time.time())
        except BaseException as exc:
            # 采集异常必须可见：静默失败正是「戳一戳全丢」那次事故的成因。
            logger.warning("[awareness] 采集异常: %r", exc)

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
            me = str(event.get_self_id() or "")
            rows = self._load(gid, now - INDEX_MIN * 60)
            if not rows:
                return

            blocks = []
            if INDEX_ENABLED:
                block = render_index(rows, window_min=INDEX_MIN, now=now)
                if block:
                    blocks.append(block)
                    _stat["index"] += 1

            # 被戳的事实：平台不给正文、昵称又被写成 QQ 号，
            # 不补这一条它就会反问「戳谁呀？」
            if POKE_ENABLED:
                note = render_poke_note(rows, now=now, me=me, window_min=POKE_MIN)
                if note:
                    blocks.append(note)
                    _stat["poke_note"] += 1

            if DETAIL_ENABLED:
                uid = str(event.get_sender_id() or "")
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
            "采集：看到%d条，入库%d条，撤回%d条（原文未留存%d次），戳一戳%d次"
            % (_stat["seen"], _stat["stored"], _stat["recall"], _stat["recall_lost"],
               _stat["poke"]),
            "注入：索引%d次，触发%d次，明细%d次，戳一戳告知%d次"
            % (_stat["index"], _stat["trigger"], _stat["detail"], _stat["poke_note"]),
            "配置：保留%d分钟/%d条，索引窗%d分钟，明细窗%d分钟最多%d条/%d字"
            % (KEEP_MIN, KEEP_MAX, INDEX_MIN, DETAIL_MIN, DETAIL_MAX, DETAIL_BUDGET),
        ]
        yield event.plain_result("\n".join(lines))
