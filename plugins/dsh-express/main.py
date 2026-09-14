# -*- coding: utf-8 -*-
"""dsh-express —— 多媒体主动表达决策层（v1.0.0）。

【为什么需要这个插件】
dsh-voice 的情绪主动语音、dsh-imagegen/dsh-video 的概念图/概念短片，各自都有
一套完整且经过生产验证的安全机制（内容审核、冷却、配额、fail-closed）。
问题是它们的**触发入口全是被动的**：

  · dsh-voice 的 should_emotion_voice 挂在 on_llm_response 上，只在
    「模型已经生成了一条文字回复」时才可能把这条回复改成语音；
  · dsh-imagegen / dsh-video 的 auto_concept 只在「群友描述了明确视觉成品」
    时才出图，跟机器人自己的情绪状态无关；
  · dsh-steal 的 auto_serve 只在「模型回复里带 [贴纸:名]」时才发图。

结果：机器人即使心里有情绪（curious / happy / proud / sad…），也**只用文字
表达**。2026-09-12~14 三天全量日志里，情绪主动语音只成功 3 次且全是 angry(3)
—— 因为 dsh-emotion 的强度生成只有 angry 能到 3，其余情绪上限 2，而语音侧
原先的全局阈值恰好就是 3。用户体感「从没见他主动发过语音/图片/视频」就是这么来的。

【本插件做什么】
不重复造任何生成代码，也不直接调用别的插件的私有函数（那太脆，插件加载顺序一
变就崩）。它只做一件事：在 on_llm_request 时，读 dsh-emotion 落盘的当前主情绪，
若达到本插件自己的分情绪门槛，就往请求里**注入一段简短的「许可 + 提示」**，
告诉模型：「你现在有情绪，可以主动用语音/图片/视频/贴纸表达它，挑一个最贴切的，
不要勉强，一轮最多一个」。

模型仍然自己决定要不要做、做哪个。**所有安全闸门都在各自的插件里，本插件不
新增也不绕过任何一道** —— 审核、冷却、配额、fail-closed 全部由
send_voice / generate_image / generate_video / [贴纸:] 自己管。本插件只额外
加一道「表达级」的总冷却与小时配额，防止情绪一来就刷屏。

【为什么用注入而不是直接调用】
  ① 复用全部现有安全机制，零重复代码；
  ② 模型能根据当下语境判断「现在发语音合适还是发张图合适」，比插件硬编码
     「curious → 发语音」合理得多；
  ③ 不改 imagegen/video/voice/steal 任何一行代码，风险面为零。

【不做什么】
  · 不改 dsh-emotion 的强度语义（那会影响全链路情绪注入强度）；
  · 不碰多模态配置（zhipu-vision / vision-opus5 / vision-opus5-thinking）；
  · 不动 QQ 出口、NapCat、代理；
  · 自主表达在 v1 里只走「模型已有回复」的路径，不另起独立探头。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import time
from collections import deque
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger

# 跟 dsh-voice 读同一个情绪状态文件，两边共用一套状态机。
EMOTION_STATE_PATH = os.environ.get(
    "DSH_EXPRESS_EMOTION_STATE",
    os.environ.get("DSH_EMOTION_STATE", "/AstrBot/data/dsh_emotion_state.json"),
)

# ---------------------------------------------------------------- 配置旋钮

ENABLED = os.environ.get("DSH_EXPRESS_ENABLED", "1") not in ("0", "false", "False")
GROUPS = {
    x.strip() for x in os.environ.get("DSH_EXPRESS_GROUPS", "").split(",") if x.strip()
}
# 表达级冷却：两次「表达触发」之间的最短间隔（秒）。比语音的 900s 短，
# 因为这里还包括图片/贴纸，成本更低、更该多来一点。
COOLDOWN = max(30, int(os.environ.get("DSH_EXPRESS_COOLDOWN", "180")))
# 小时配额：每群每小时最多几次由情绪驱动的多媒体表达。
MAX_PER_HOUR = max(1, int(os.environ.get("DSH_EXPRESS_MAX_PER_HOUR", "2")))
# 触发概率。不是每轮情绪到位了都要出声 —— 0.5 意味着一半轮次忍住。
RATE = max(0.0, min(1.0, float(os.environ.get("DSH_EXPRESS_RATE", "0.5"))))
# 分情绪门槛：与 dsh-voice 同一口径 —— angry 能到 3，其余情绪上限 2。
# 未列出的情绪回落到全局阈值。
THRESHOLD = max(1, min(3, int(os.environ.get("DSH_EXPRESS_THRESHOLD", "3"))))
THRESHOLD_BY_NAME: dict[str, int] = {}
for _item in os.environ.get(
    "DSH_EXPRESS_THRESHOLD_BY_NAME",
    "angry:3,happy:2,curious:2,awkward:2,excited:2,sad:2,surprised:2,worried:2,proud:2",
).split(","):
    _item = _item.strip()
    if not _item or ":" not in _item:
        continue
    _name, _, _val = _item.partition(":")
    try:
        THRESHOLD_BY_NAME[_name.strip()] = max(1, min(3, int(_val)))
    except ValueError:
        continue

# 注入文本长度上限。太长了模型会当背景噪音忽略，太短了看不出许可。
MAX_INJECT_CHARS = int(os.environ.get("DSH_EXPRESS_MAX_INJECT_CHARS", "220"))

# 只在这些情绪上主动表达。calm 不用提醒模型做多余的事。
EMOTION_NAMES = {
    x.strip() for x in os.environ.get(
        "DSH_EXPRESS_EMOTION_NAMES",
        "angry,happy,curious,awkward,excited,sad,surprised,worried,proud",
    ).split(",") if x.strip()
}

# ---------------------------------------------------------------- 状态

# gid -> 最近一次表达触发的时间
_last: dict[str, float] = {}
# gid -> 最近一小时内的触发时间（滑动窗口）
_hits: dict[str, list[float]] = {}

# 注入文本池：随机挑一句，避免每轮一模一样被模型/群友当成口头禅。
# 语气要像一个「提醒自己可以出声」的内部提示，不是命令。
_INJECT_POOL = (
    "【主动表达】你现在心里有情绪。如果你觉得合适，可以主动用语音、"
    "一张图、一段视频或者一个表情包把它表达出来 —— 挑最贴切的一个，"
    "一轮最多一个，不要勉强，不合适就照常文字聊。",
    "【主动表达】你现在有情绪。想发点什么来表达它吗？语音、图片、视频、"
    "表情包都行，选一个最有感觉的就好；不想发就当没看见，正常聊天。",
    "【主动表达】你现在有情绪。如果文字不够味，可以补一条语音/图片/视频/"
    "表情包来表达，挑一个最贴切的；一轮一个，别硬凑。",
)


# ---------------------------------------------------------------- 情绪读取

def _read_emotion(gid: str, now: float | None = None) -> tuple[str, int, str]:
    """读取 dsh-emotion 落盘的单一主情绪；坏文件/过期状态一律视为平静。"""
    try:
        with open(EMOTION_STATE_PATH, encoding="utf-8") as handle:
            states = json.load(handle)
        state = states.get(str(gid)) if isinstance(states, dict) else None
        if not isinstance(state, dict):
            return "calm", 0, "missing"
        emotion = str(state.get("emotion") or "calm")
        intensity = max(0, min(3, int(state.get("intensity") or 0)))
        expires = float(state.get("expires_at") or 0.0)
        ts = time.time() if now is None else now
        if emotion == "calm" or (expires and ts >= expires):
            return "calm", 0, "expired" if expires else "calm"
        return emotion, intensity, "active"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return "calm", 0, "unreadable"


def _should_express(gid: str, now: float | None = None) -> tuple[bool, str]:
    """表达级总闸门（无副作用），便于离线测试。"""
    if not ENABLED:
        return False, "关闭"
    if GROUPS and str(gid) not in GROUPS:
        return False, "群未启用"
    emotion, intensity, status = _read_emotion(gid, now)
    if status != "active" or emotion not in EMOTION_NAMES:
        return False, "情绪不匹配"
    need = THRESHOLD_BY_NAME.get(emotion, THRESHOLD)
    if intensity < need:
        return False, "强度不足"
    ts = time.time() if now is None else now
    if ts - _last.get(gid, 0.0) < COOLDOWN:
        return False, "冷却中"
    hits = _hits.setdefault(gid, [])
    hits[:] = [t for t in hits if ts - t < 3600]
    if len(hits) >= MAX_PER_HOUR:
        return False, "小时配额满"
    if random.random() >= RATE:
        return False, "概率未中"
    return True, "%s(%d)" % (emotion, intensity)


# ---------------------------------------------------------------- 插件本体

class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        super().__init__(context)
        self._textpart_warned = False
        try:
            from astrbot.core.agent.message import TextPart  # type: ignore
            self._TextPart = TextPart
        except ImportError:  # 老版本没有 TextPart，退回纯字符串
            self._TextPart = None
        logger.info(
            "[express] 已加载：阈值(分情绪)=%s 冷却=%ds 小时上限=%d 概率=%.2f",
            ",".join("%s≥%d" % (k, THRESHOLD_BY_NAME[k])
                     for k in sorted(THRESHOLD_BY_NAME)),
            COOLDOWN, MAX_PER_HOUR, RATE,
        )

    # ------------------------------------------------ 路径：注入许可

    @filter.on_llm_request()
    async def inject_express_permission(self, event: AstrMessageEvent, req) -> None:
        """在模型生成前，若当前情绪到位，注入一条「可以主动用多媒体表达」的提示。

        这是唯一的入口。模型收到后自己决定做不做、做什么 —— 本插件不调用任何
        生成函数，所有安全机制都在 send_voice / generate_image / generate_video /
        [贴纸:] 各自的插件里。
        """
        if not ENABLED:
            return
        try:
            if event.get_message_type() != 2:  # MessageType.GROUP_MESSAGE
                return
            gid = str(event.get_group_id() or "")
            if not gid:
                return
            ok, why = _should_express(gid)
            if not ok:
                return
            # 占位：冷却与配额先落，再注入。注入是异步管道，崩了也不能让配额白漏。
            now = time.time()
            _last[gid] = now
            _hits.setdefault(gid, []).append(now)

            text = random.choice(_INJECT_POOL)
            if len(text) > MAX_INJECT_CHARS:
                text = text[:MAX_INJECT_CHARS]

            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                # 框架版本不支持 extra_user_content_parts：宁可不注入，也不塞裸字符串。
                if not self._textpart_warned:
                    self._textpart_warned = True
                    logger.warning("[express] 拿不到 extra_user_content_parts，不注入")
                return

            if self._TextPart is not None:
                parts.append(self._TextPart(text=text))
            else:
                parts.append(text)
            logger.info("[express] 已注入主动表达许可（%s）：%.60s", why, text)
        except BaseException as exc:  # noqa: BLE001
            logger.error("[express] 注入失败：%s", exc)

    # ------------------------------------------------ 群主命令

    @filter.command("自主表达")
    async def cmd_status(self, event: AstrMessageEvent) -> None:
        """群主查询/切换主动表达状态。"""
        owner = os.environ.get("DSH_EMOTION_OWNER", "")
        if owner and str(event.get_sender_id()) != str(owner):
            return
        gid = str(event.get_group_id() or "")
        now = time.time()
        emotion, intensity, status = _read_emotion(gid, now)
        ok, why = _should_express(gid, now)
        hits = _hits.get(gid, [])
        hits = [t for t in hits if now - t < 3600]
        yield event.plain_result(
            "自主表达：%s\n当前情绪：%s（强度 %d，状态 %s）\n"
            "本次能否触发：%s\n过去一小时已触发 %d 次（上限 %d）\n"
            "阈值：%s ｜ 冷却 %ds ｜ 概率 %.2f"
            % (
                "开" if ENABLED else "关",
                emotion, intensity, status,
                ("能 → " + why) if ok else ("否：" + why),
                len(hits), MAX_PER_HOUR,
                ",".join("%s≥%d" % (k, THRESHOLD_BY_NAME[k])
                         for k in sorted(THRESHOLD_BY_NAME)),
                COOLDOWN, RATE,
            )
        )