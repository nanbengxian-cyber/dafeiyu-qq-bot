# -*- coding: utf-8 -*-
from __future__ import annotations

"""dsh-fatigue -- 同一件事说多了，耐心会自然耗尽。

这不是另一套固定“不耐烦话术”。插件在输入侧维护按群、话题、发送者隔离的回复账本，
只在机器人确实发出回复后增加疲劳；同一人无新增信息地反复追问会逐级缩短回复、转开
或拒绝，换人偶尔问同题不会立刻被迁怒。20 分钟没再碰同题就自然恢复。

联动协议：
- dsh-clarify 在同一次语境分类中写入 dsh_topic_key / dsh_topic_revisit，不额外调用模型；
- 本插件把 dsh_fatigue_level / dsh_fatigue_topic 写回 event，供后续插件读取；
- dsh-proactive / dsh-initiate 合成事件命中高疲劳话题时直接沉默，避免自己翻旧话题；
- 注入块明确不覆盖 dsh-emotion 的真实情绪，且会被 dsh-ctxclean 自动清掉。

环境变量：DSH_FATIGUE、DSH_FATIGUE_GROUPS、DSH_FATIGUE_OWNER、
DSH_FATIGUE_STATE、DSH_FATIGUE_WINDOW、DSH_FATIGUE_TTL、
DSH_FATIGUE_MATCH、DSH_FATIGUE_SUPPRESS_PROACTIVE、DSH_FATIGUE_SUPPRESS_INITIATE。
"""

import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType

from .fatigue_logic import clean_topic, fatigue_level, find_topic, note_reply, render_fatigue, topic_similarity


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_FATIGUE")
GROUPS = _set("DSH_FATIGUE_GROUPS", "100000001")
OWNERS = _set("DSH_FATIGUE_OWNER", "2774000001")
STATE_PATH = Path(os.environ.get("DSH_FATIGUE_STATE", "/AstrBot/data/dsh_fatigue_state.json"))
WINDOW = max(120.0, float(os.environ.get("DSH_FATIGUE_WINDOW", "1200")))
TTL = max(WINDOW, float(os.environ.get("DSH_FATIGUE_TTL", "7200")))
MATCH = min(0.95, max(0.4, float(os.environ.get("DSH_FATIGUE_MATCH", "0.62"))))
MAX_TOPICS = max(5, int(os.environ.get("DSH_FATIGUE_MAX_TOPICS", "24")))
SUPPRESS_PROACTIVE = _flag("DSH_FATIGUE_SUPPRESS_PROACTIVE")
SUPPRESS_INITIATE = _flag("DSH_FATIGUE_SUPPRESS_INITIATE")

_stat = {"seen": 0, "inject1": 0, "inject2": 0, "inject3": 0,
         "recorded": 0, "proactive_silent": 0, "initiate_silent": 0,
         "fallback_revisit": 0, "fail": 0}
_last: deque[str] = deque(maxlen=10)


def _load() -> dict:
    try:
        obj = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(obj, dict) and isinstance(obj.get("groups"), dict):
            return obj
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 1, "groups": {}}


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _prune(topics: list[dict], now: float) -> list[dict]:
    live = [x for x in topics if isinstance(x, dict) and now - float(x.get("last_at", 0) or 0) <= TTL]
    live.sort(key=lambda x: float(x.get("last_at", 0) or 0), reverse=True)
    return live[:MAX_TOPICS]


def _event_topic(event: AstrMessageEvent) -> tuple[str, str, bool, str]:
    """返回 key, sample, revisit, source。"""
    if event.get_extra("dsh_proactive"):
        sample = str(event.get_extra("dsh_proactive_text") or "").strip()
        return clean_topic(sample), sample, False, "proactive"
    if event.get_extra("dsh_initiate"):
        facts = event.get_extra("dsh_initiate_facts") or {}
        sample = str(facts.get("topic") or facts.get("angle") or "").strip()
        return clean_topic(sample), sample, False, "initiate"
    sample = str(event.message_str or "").strip()
    key = clean_topic(str(event.get_extra("dsh_topic_key") or "")) or clean_topic(sample)
    revisit = bool(event.get_extra("dsh_topic_revisit"))
    return key, sample, revisit, "normal"


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        super().__init__(context)
        self.context = context
        self.state = _load()
        self._lock = asyncio.Lock()
        logger.info(
            "[fatigue] 已加载：%s 群=%s 窗口%.0f分钟 恢复%.1f小时 匹配≥%.0f%% 主动探头抑制=%s 冷场开口抑制=%s",
            "开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无",
            WINDOW / 60, TTL / 3600, MATCH * 100,
            "开" if SUPPRESS_PROACTIVE else "关", "开" if SUPPRESS_INITIATE else "关",
        )

    @filter.on_llm_request(priority=1800)
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            gid = str(event.get_group_id() or "")
            if not gid or (GROUPS and gid not in GROUPS):
                return
            key, sample, revisit, source = _event_topic(event)
            if not key or (source == "normal" and sample.startswith(("/", "／", "!", "！"))):
                return
            uid = str(event.get_sender_id() or "")
            now = time.time()
            async with self._lock:
                topics = _prune(self.state.setdefault("groups", {}).get(gid, []), now)
                self.state["groups"][gid] = topics
                entry = find_topic(topics, key, sample, MATCH)

                # clarify 不可用时，只对几乎相同的原句作保守兜底；不靠大类词误伤新问题。
                if source == "normal" and not revisit and entry:
                    same_uid = any(
                        str(x.get("uid", "")) == uid and now - float(x.get("ts", 0) or 0) <= WINDOW
                        for x in entry.get("replies", []) if isinstance(x, dict)
                    )
                    if same_uid and topic_similarity(sample, entry.get("sample", "")) >= 0.82:
                        revisit = True
                        _stat["fallback_revisit"] += 1

                # 合成事件没有真实提问者：按整个话题的近期回复次数算，达到明显疲劳才沉默。
                if source != "normal":
                    recent = [x for x in (entry or {}).get("replies", [])
                              if now - float(x.get("ts", 0) or 0) <= WINDOW]
                    level = 2 if len(recent) >= 3 else 1 if len(recent) >= 2 else 0
                    should_stop = ((source == "proactive" and SUPPRESS_PROACTIVE and level >= 2)
                                   or (source == "initiate" and SUPPRESS_INITIATE and level >= 2))
                    if should_stop:
                        event.set_extra("dsh_fatigue_level", level)
                        event.set_extra("dsh_fatigue_topic", (entry or {}).get("key", key))
                        event.stop_event()
                        _stat[source + "_silent"] += 1
                        _last.append(time.strftime("%H:%M:%S ") + "%s沉默｜%s" % (source, key[:18]))
                        logger.info("[fatigue] gid=%s %s 命中疲劳话题=%s level=%d，不自行翻出来",
                                    gid, source, key, level)
                    return

                level = fatigue_level(entry, uid, now, WINDOW, revisit)
                _stat["seen"] += 1
                event.set_extra("dsh_fatigue_topic", (entry or {}).get("key", key))
                event.set_extra("dsh_fatigue_sample", sample[:120])
                event.set_extra("dsh_fatigue_level", level)
                event.set_extra("dsh_fatigue_source", source)
                event.set_extra("dsh_fatigue_revisit", revisit)
                if level <= 0:
                    return
                block = render_fatigue(level)
                req.extra_user_content_parts.append(TextPart(text=block))
                _stat["inject%d" % level] += 1
                _last.append(time.strftime("%H:%M:%S ") + "L%d｜%s｜uid=%s" % (level, key[:18], uid))
                logger.info("[fatigue] gid=%s uid=%s 话题=%s revisit=%s level=%d 已注入",
                            gid, uid, key, revisit, level)
        except BaseException as exc:
            _stat["fail"] += 1
            logger.warning("[fatigue] 注入失败，保持原流程: %r", exc)

    @filter.after_message_sent()
    async def remember(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or (GROUPS and gid not in GROUPS):
                return
            key = str(event.get_extra("dsh_fatigue_topic") or "")
            sample = str(event.get_extra("dsh_fatigue_sample") or event.message_str or "").strip()
            if not key or not sample:
                return
            result = event.get_result()
            if result is None or not (result.get_plain_text() or "").strip():
                return
            # on_llm_request 的低优先级插件可能在本插件之后才 stop_event；AstrBot 仍会
            # 调 after_message_sent，但这时没有真正发出去，绝不能把它记成“已经回答”。
            if bool(getattr(event, "is_stopped", lambda: False)()):
                return
            try:
                if not result.is_model_result():
                    return
            except BaseException:
                pass
            uid = str(event.get_sender_id() or "")
            now = time.time()
            async with self._lock:
                groups = self.state.setdefault("groups", {})
                topics = _prune(groups.get(gid, []), now)
                old = find_topic(topics, key, sample, MATCH)
                fresh = note_reply(old, key, sample, uid, now)
                if old is not None:
                    topics.remove(old)
                topics.insert(0, fresh)
                groups[gid] = topics[:MAX_TOPICS]
                _save(self.state)
            _stat["recorded"] += 1
        except BaseException as exc:
            _stat["fail"] += 1
            logger.warning("[fatigue] 记录回复失败，不影响发言: %r", exc)

    @filter.command("厌烦状态")
    async def status(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        if OWNERS and uid not in OWNERS:
            return
        gid = str(event.get_group_id() or "")
        now = time.time()
        topics = _prune(self.state.get("groups", {}).get(gid, []), now)
        rows = []
        for item in topics[:5]:
            recent = [x for x in item.get("replies", [])
                      if now - float(x.get("ts", 0) or 0) <= WINDOW]
            if recent:
                rows.append("%s(%d轮)" % (item.get("key") or "?", len(recent)))
        s = _stat
        yield event.plain_result(
            "话题疲劳：%s｜窗口%.0f分钟｜%.1f小时自然恢复\n"
            "注入 L1/L2/L3=%d/%d/%d｜确认回复%d｜主动沉默%d｜冷场沉默%d｜兜底%d｜失败%d\n"
            "当前活跃：%s"
            % ("开" if ENABLED else "关", WINDOW / 60, TTL / 3600,
               s["inject1"], s["inject2"], s["inject3"], s["recorded"],
               s["proactive_silent"], s["initiate_silent"], s["fallback_revisit"], s["fail"],
               "、".join(rows) or "暂无"))
