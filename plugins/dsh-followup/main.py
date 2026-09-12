# -*- coding: utf-8 -*-
"""dsh-followup -- 免 @ 续聊：按上下文关键词相似度决定回复概率。

确定指向（引用机器人）直接回复；普通免 @ 消息则只考虑机器人刚回复过的同一人，
比较“该人上一句 + 机器人回复”与当前消息的关键词。话题越相似，回复概率越高；
话题越远，概率越低。换人、@别人、收尾短句和指令不会由本插件强制唤醒。
"""

from __future__ import annotations

import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.message.components import At, Plain, Reply
from astrbot.core.platform.message_type import MessageType

from .followup_logic import should_wake_followup


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_FOLLOWUP")
SHADOW = _flag("DSH_FOLLOWUP_SHADOW", "0")
GROUPS = _set("DSH_FOLLOWUP_GROUPS", "100000001")
OWNERS = _set("DSH_FOLLOWUP_OWNER", "2774000001")
WINDOW = max(15.0, float(os.environ.get("DSH_FOLLOWUP_WINDOW", "150")))
MAX_CONTEXT = max(80, int(os.environ.get("DSH_FOLLOWUP_CONTEXT_CHARS", "360")))
MIN_SCORE = max(0, min(100, int(os.environ.get("DSH_FOLLOWUP_MIN_SCORE", "20"))))
SOCIAL_DB = os.environ.get("DSH_FOLLOWUP_SOCIAL_DB", "/AstrBot/data/dsh_social.db")


@dataclass
class FollowupState:
    uid: str
    expires_at: float
    context_text: str


_stat = {
    "sent": 0, "armed": 0, "wake": 0, "quote": 0, "ack": 0, "other": 0,
    "sampled_out": 0, "shadow": 0, "score_sum": 0, "scored": 0,
}
_last: list[str] = []


def _reply_of(event):
    for comp in getattr(getattr(event, "message_obj", None), "message", None) or ():
        if isinstance(comp, Reply):
            return comp
    return None


def _at_uids(event) -> set[str]:
    return {
        str(getattr(comp, "qq", "") or "")
        for comp in getattr(getattr(event, "message_obj", None), "message", None) or ()
        if isinstance(comp, At)
    }


def _result_text(event) -> str:
    result = event.get_result()
    chunks: list[str] = []
    for comp in getattr(result, "chain", None) or ():
        if isinstance(comp, Plain):
            text = str(getattr(comp, "text", "") or "").strip()
            if text:
                chunks.append(text)
    if chunks:
        return " ".join(chunks)
    # 某些模型结果在装饰阶段只暴露纯文本接口，保守兼容。
    try:
        return str(result.get_plain_text() or "").strip()
    except Exception:
        return ""


def _clean_context(text: str) -> str:
    value = re.sub(r"\[[^\]]{1,30}\]", " ", text or "")
    return re.sub(r"\s+", " ", value).strip()[-MAX_CONTEXT:]


def social_quiet(gid: str, uid: str, now: float | None = None, db: str | None = None) -> bool:
    """Read dsh-social's explicit temporary boundary without importing that plugin.

    This is deliberately a fail-open, read-only link: a missing/locked/older social database
    must never break ordinary follow-up handling.  It suppresses optional unmentioned chatter
    only; quoted/direct messages are handled before this signal in ``should_wake_followup``.
    """
    if not gid or not uid:
        return False
    path = db or SOCIAL_DB
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=0.5)
        try:
            row = con.execute(
                "SELECT avoid_until,opted_out FROM relations WHERE group_id=? AND user_id=?",
                (gid, uid),
            ).fetchone()
        finally:
            con.close()
        if not row:
            return False
        return bool(row[1]) or float(row[0] or 0) > (time.time() if now is None else float(now))
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._states: dict[str, FollowupState] = {}
        logger.info(
            "[followup] 已加载：%s%s 关键词概率续聊 窗口%.0fs 语义底线%d 上下文%d字 群=%s",
            "开" if ENABLED else "关", "（影子）" if SHADOW else "", WINDOW,
            MIN_SCORE, MAX_CONTEXT, ",".join(sorted(GROUPS)) or "无",
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=900)
    async def wake(self, event: AstrMessageEvent) -> None:
        """WakingCheck 内按续聊分数抽样，抽中才把消息标成被喊。"""
        if not ENABLED or event.get_message_type() != MessageType.GROUP_MESSAGE:
            return
        try:
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
            if not gid or gid not in GROUPS or not uid or uid == self_id:
                return
            if bool(getattr(event, "is_at_or_wake_command", False)):
                return
            getter = getattr(event, "get_extra", None)
            if callable(getter) and (getter("dsh_initiate") or getter("dsh_proactive")):
                return

            quote = _reply_of(event)
            quoted_uid = str(getattr(quote, "sender_id", "") or "") if quote else ""
            quotes_self = bool(self_id and quoted_uid == self_id)
            # Reply 没有 sender_id 时不猜作者，避免把引用群友误当引用机器人。
            ats = _at_uids(event)
            state = self._states.get(gid)
            quiet = social_quiet(gid, uid)
            ok, why, score, probability, hits = should_wake_followup(
                sender_id=uid,
                target_id=state.uid if state else "",
                text=str(event.get_message_str() or ""),
                context_text=state.context_text if state else "",
                now=time.time(),
                expires_at=state.expires_at if state else 0.0,
                quotes_self=quotes_self,
                at_other=bool(ats and self_id not in ats),
                roll=random.random(),
                min_score=MIN_SCORE,
                quiet=quiet,
            )
            if probability and not quotes_self:
                _stat["scored"] += 1
                _stat["score_sum"] += score
            if not ok:
                if why == "只是收尾应答":
                    _stat["ack"] += 1
                elif why == "上下文相似度未抽中":
                    _stat["sampled_out"] += 1
                    logger.info(
                        "[followup] gid=%s uid=%s 不续聊：分%d 概率%.0f%% 交集=%s",
                        gid, uid, score, probability * 100, "/".join(hits) or "无",
                    )
                elif why in {"上下文关联不足", "对方要求少打扰"}:
                    _stat["other"] += 1
                    logger.info("[followup] gid=%s uid=%s 不续聊：%s 分%d", gid, uid, why, score)
                else:
                    _stat["other"] += 1
                return
            detail = "%s 分%d 概率%.0f%% 交集=%s" % (
                why, score, probability * 100, "/".join(hits) or "无")
            if SHADOW:
                _stat["shadow"] += 1
                logger.info("[followup] 影子：gid=%s uid=%s %s", gid, uid, detail)
                return
            event.is_at_or_wake_command = True
            event.is_wake = True
            event.set_extra("dsh_followup", True)
            event.set_extra("dsh_followup_reason", detail)
            event.set_extra("dsh_followup_score", score)
            _stat["wake"] += 1
            if quotes_self:
                _stat["quote"] += 1
            _last.append(time.strftime("%H:%M:%S ") + "%s:%s" % (uid, detail))
            del _last[:-8]
            logger.info("[followup] gid=%s uid=%s 当续聊唤醒：%s", gid, uid, detail)
        except Exception as exc:
            logger.warning("[followup] 唤醒判断失败，保持原流程: %r", exc)

    @filter.on_decorating_result(priority=-900)
    async def arm(self, event: AstrMessageEvent) -> None:
        """模型确实回复后，保存“对方消息 + 机器人回复”作为下一句的相似度上下文。"""
        if not ENABLED:
            return
        try:
            getter = getattr(event, "get_extra", None)
            if callable(getter) and (getter("dsh_initiate") or getter("dsh_proactive")):
                return
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            if not gid or gid not in GROUPS or not uid or uid == str(event.get_self_id() or ""):
                return
            result = event.get_result()
            if result is None or not getattr(result, "chain", None):
                return
            try:
                if not result.is_model_result():
                    return
            except Exception:
                pass
            inbound = str(event.get_message_str() or "")
            outbound = _result_text(event)
            context_text = _clean_context(inbound + " " + outbound)
            if not context_text:
                return
            self._states[gid] = FollowupState(
                uid=uid, expires_at=time.time() + WINDOW, context_text=context_text)
            _stat["sent"] += 1
            _stat["armed"] += 1
            logger.debug(
                "[followup] gid=%s 续聊对象=%s 窗口=%.0fs 上下文=%r",
                gid, uid, WINDOW, context_text[:100],
            )
        except Exception as exc:
            logger.warning("[followup] 记录续聊窗口失败: %r", exc)

    @filter.command("续聊状态")
    async def status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        gid = str(event.get_group_id() or "")
        state = self._states.get(gid)
        left = max(0.0, state.expires_at - time.time()) if state else 0.0
        target = state.uid if state and left else "无"
        avg = (_stat["score_sum"] / _stat["scored"]) if _stat["scored"] else 0.0
        yield event.plain_result(
            "免@续聊：%s%s｜关键词相似度→5%%~95%%概率（至少%d分）｜窗口 %.0fs｜当前对象 %s（剩 %.0fs）\n"
            "已评分 %d（平均 %.0f分）｜抽中回复 %d（引用直回 %d）｜未抽中 %d｜收尾短句跳过 %d｜影子命中 %d\n"
            "最近：%s"
            % ("开" if ENABLED else "关", "（影子）" if SHADOW else "", MIN_SCORE, WINDOW, target, left,
               _stat["scored"], avg, _stat["wake"], _stat["quote"], _stat["sampled_out"],
               _stat["ack"], _stat["shadow"], "｜".join(_last[-3:]) or "还没有")
        )
