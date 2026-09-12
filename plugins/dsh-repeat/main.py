# -*- coding: utf-8 -*-
"""dsh-repeat -- 群友连续发送三条相同纯文本后，大肥鱼跟着复读一次。"""

import os
import re
import sys
import time
from collections import deque
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger
from astrbot.core.platform.message_type import MessageType

_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)
from dsh_link import TurnClaim, blocks, claim_turn  # type: ignore  # noqa: E402


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {part.strip() for part in os.environ.get(name, default).split(",") if part.strip()}


ENABLED = _flag("DSH_REPEAT")
GROUPS = _set("DSH_REPEAT_GROUPS", "100000001")
TRIGGER = max(2, int(os.environ.get("DSH_REPEAT_TRIGGER", "3")))
WINDOW = max(3.0, float(os.environ.get("DSH_REPEAT_WINDOW", "90")))
MAX_LENGTH = max(1, int(os.environ.get("DSH_REPEAT_MAX_LENGTH", "100")))
COOLDOWN = max(1.0, float(os.environ.get("DSH_REPEAT_COOLDOWN", "30")))

# gid -> (规范化文本, 连续次数, 最后时间, 本轮是否已复读)
_state: dict[str, tuple[str, int, float, bool]] = {}
_recent_ids: dict[str, deque[str]] = {}
_stat = {"seen": 0, "triggered": 0, "empty": 0, "rich": 0, "long": 0, "duplicate": 0, "cooldown": 0, "fail": 0}
_last: deque[str] = deque(maxlen=8)
_last_sent: dict[str, float] = {}


def normalize_text(text: str) -> str:
    """只统一空白；标点和大小写不同仍视为不同消息。"""
    return re.sub(r"\s+", " ", text or "").strip()


def advance(previous: tuple[str, int, float, bool] | None, text: str, now: float,
            trigger: int = TRIGGER, window: float = WINDOW) -> tuple[tuple[str, int, float, bool], bool]:
    """推进单群连续计数，返回（新状态，是否在这一条触发）。"""
    if previous and previous[0] == text and now - previous[2] <= window:
        count = previous[1] + 1
        fired = previous[3]
    else:
        count = 1
        fired = False
    should_fire = count >= trigger and not fired
    return (text, count, now, fired or should_fire), should_fire


def _plain_only(event: AstrMessageEvent) -> bool:
    """复读仅处理纯文本，避免复制 @、图片、转发、命令等富消息。"""
    chain = getattr(event.message_obj, "message", None) or []
    return bool(chain) and all(isinstance(component, Plain) for component in chain)


def _message_id(event: AstrMessageEvent) -> str:
    mid = getattr(event.message_obj, "message_id", None)
    if not mid:
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            mid = raw.get("message_id")
        else:
            try:
                mid = raw["message_id"]
            except BaseException:
                mid = getattr(raw, "message_id", None)
    return str(mid or "")


def _seen_message(gid: str, mid: str) -> bool:
    if not mid:
        return False
    recent = _recent_ids.setdefault(gid, deque(maxlen=80))
    if mid in recent:
        return True
    recent.append(mid)
    return False


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        super().__init__(context)
        self.context = context
        logger.info(
            "[repeat] 已加载：%s 群=%s 连续%d条 窗口%.0fs 最长%d字 冷却%.0fs",
            "开" if ENABLED else "关", ",".join(sorted(GROUPS)) or "全部",
            TRIGGER, WINDOW, MAX_LENGTH, COOLDOWN,
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=500)
    async def collect(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            if event.get_platform_name() == "webchat":
                return
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            if not gid or (GROUPS and gid not in GROUPS):
                return
            if uid == str(event.get_self_id() or ""):
                return
            mid = _message_id(event)
            if _seen_message(gid, mid):
                _stat["duplicate"] += 1
                return
            _stat["seen"] += 1
            text = normalize_text(event.get_message_str() or "")
            if not text or text.startswith("/"):
                _state.pop(gid, None)
                _stat["empty"] += 1
                return
            if not _plain_only(event):
                _state.pop(gid, None)
                _stat["rich"] += 1
                return
            if len(text) > MAX_LENGTH:
                _state.pop(gid, None)
                _stat["long"] += 1
                return

            now = time.time()
            state, fire = advance(_state.get(gid), text, now)
            _state[gid] = state
            if not fire:
                return
            if blocks(event, "repeat"):
                logger.info("[repeat] gid=%s 三连命中但本轮已由更高优先动作接管，不复读", gid)
                return
            gap = now - _last_sent.get(gid, 0.0)
            if gap < COOLDOWN:
                _stat["cooldown"] += 1
                logger.info("[repeat] gid=%s 三连命中但冷却中(%.1fs)，不发", gid, gap)
                return

            # 先占冷却，避免并发到达的第4条造成双发；失败时撤销。
            _last_sent[gid] = now
            try:
                await event.send(MessageChain(chain=[Plain(text)]))
            except BaseException:
                _last_sent.pop(gid, None)
                raise
            claim_turn(event, TurnClaim(
                owner="dsh-repeat", kind="repeat_sent", priority=40,
                block_proactive=True, already_replied=True,
            ))
            # 复读本身已经是完整回应，不能再让主 LLM 对同一条消息补第二答。
            event.stop_event()
            _stat["triggered"] += 1
            _last.append(time.strftime("%H:%M:%S ") + "%s：%s" % (gid, text[:40]))
            logger.info("[repeat] gid=%s 连续%d条，已复读：%r", gid, state[1], text[:100])
        except BaseException as exc:
            _stat["fail"] += 1
            logger.warning("[repeat] 处理失败: %r", exc)

    @filter.command("复读状态")
    async def status(self, event: AstrMessageEvent):
        lines = [
            "复读 %s｜连续%d条｜窗口%.0fs｜最长%d字｜冷却%.0fs" % (
                "开" if ENABLED else "关", TRIGGER, WINDOW, MAX_LENGTH, COOLDOWN),
            "群：%s" % ("、".join(sorted(GROUPS)) or "全部"),
            "累计：看见%d／复读%d／富消息%d／过长%d／重复事件%d／冷却%d／失败%d" % (
                _stat["seen"], _stat["triggered"], _stat["rich"], _stat["long"],
                _stat["duplicate"], _stat["cooldown"], _stat["fail"]),
        ]
        if _last:
            lines.append("最近：\n" + "\n".join(_last))
        yield event.plain_result("\n".join(lines))
