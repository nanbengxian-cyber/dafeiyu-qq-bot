# -*- coding: utf-8 -*-
"""
dsh-homophone —— 同音字/谐音识别（2026-09-07）。

群聊真人机器人的两个真实缺口，这里补上：
1. 有人不打 @，打字用谐音喊机器人（大肥鱼 → 大废鱼/大飞鱼/大肥狱…），
   机器人当没被叫、不会应。-> 命中机器人名称谐音 → 把本条标记为「被喊」
   （event.is_at_or_wake_command = True），走正常回复管道，机器人会应，并在
   上下文里注入「这是有人用谐音喊你」让回复接得住。
2. 群里的谐音梗（虾仁猪心=杀人诛心、栓Q=thank you、蚌埠住了=绷不住了…）
   机器人看不懂就接不对。-> on_llm_request 命中已知谐音梗 → 注入本意
   （extra_user_content_parts，与 dsh-slang 同款），帮理解不强迫。

词库在 homophone_data.json（可自行增删；分「名称谐音」和「谐音梗」两部分）。
纯匹配逻辑在 homophone_logic.py（不依赖 astrbot，可单测）。

影子模式：DSH_HOMOPHONE_SHADOW=1 时命中只打日志，不改「被喊」也不注入。
命令：/同音字状态（群主，QQ 由 DSH_HOMOPHONE_OWNER 配置）
"""

import json
import os

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.platform.message_type import MessageType
from astrbot.core.agent.message import TextPart
from .homophone_logic import matched_names, matched_puns

ENABLED = os.environ.get("DSH_HOMOPHONE", "1") != "0"
SHADOW = os.environ.get("DSH_HOMOPHONE_SHADOW", "0") != "0"
OWNER = os.environ.get("DSH_HOMOPHONE_OWNER", "2774000001")
GROUPS = set(
    g.strip()
    for g in os.environ.get("DSH_HOMOPHONE_GROUPS", "100000001").split(",")
    if g.strip()
)

_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "homophone_data.json")


def _load_data():
    try:
        with open(_DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except BaseException as e:
        logger.warning("[homophone] 词库加载失败: %r", e)
        return {"names": {}, "puns": []}


DATA = _load_data()
NAME_MAP = DATA.get("names") or {}
PUNS = DATA.get("puns") or []

_stat = {"wake": 0, "puns": 0, "inject": 0}


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        super().__init__(context)
        self.context = context
        logger.info(
            "[homophone] 已加载：%s 群=%s 名称谐音%d组 谐音梗%d条%s",
            "开" if ENABLED else "关",
            ",".join(sorted(GROUPS)) or "无",
            len(NAME_MAP),
            len(PUNS),
            "（影子）" if SHADOW else "",
        )

    # ---- 阶段1：谐音称呼当「被喊」----
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def wake(self, event: AstrMessageEvent):
        if not ENABLED:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            gid = str(event.get_group_id() or "")
            if not gid or (GROUPS and gid not in GROUPS):
                return
            text = (event.message_str or "").strip()
            if not text or not NAME_MAP:
                return
            nm = matched_names(text, NAME_MAP)
            if not nm:
                return
            if SHADOW:
                logger.info("[homophone] 影子：gid=%s 被喊「%s」（未当被喊）",
                            gid, nm[0][1])
                return
            # 把本消息转成「被喊」→ 走正常回复管道，机器人会应
            event.is_at_or_wake_command = True
            event.is_wake = True
            _stat["wake"] += 1
            logger.info("[homophone] gid=%s 被喊「%s」（当被喊对待）",
                        gid, nm[0][1])
        except BaseException as e:
            logger.debug("[homophone] 唤醒判定异常: %s", e)

    # ---- 阶段2：看懂谐音梗 / 确认被喊 -> 注入上下文 ----
    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req):
        if not ENABLED:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            gid = str(event.get_group_id() or "")
            if not gid or (GROUPS and gid not in GROUPS):
                return
            text = (event.message_str or "").strip()
            if not text:
                return
            lines = []
            nm = matched_names(text, NAME_MAP)
            if nm:
                for canon, v in nm:
                    if v == canon:
                        lines.append("·有人在群里喊你的名字「%s」，这是被叫，应它。" % canon)
                    else:
                        lines.append("·「%s」是「%s」的谐音——有人在群里用谐音喊你，这是被叫，应它。"
                                     % (v, canon))
            puns = matched_puns(text, PUNS)
            if puns:
                _stat["puns"] += 1
                for p in puns:
                    lines.append("·群友说了谐音梗「%s」= %s，接梗时按本意理解。"
                                 % (p.get("text"), p.get("mean") or "？"))
            if not lines:
                return
            if SHADOW:
                logger.info("[homophone] 影子：gid=%s 命中%d条（未注入）", gid, len(lines))
                return
            req.extra_user_content_parts.append(TextPart(text="\n".join(lines)))
            _stat["inject"] += 1
            logger.info("[homophone] gid=%s 注入%d条", gid, len(lines))
        except BaseException as e:
            logger.debug("[homophone] 注入异常(不影响回复): %s", e)

    # ---- 状态 ----
    @filter.command("同音字状态")
    async def status(self, event: AstrMessageEvent):
        if str(event.get_sender_id()) != OWNER:
            return
        yield event.plain_result(
            "同音字 %s\n"
            "名称谐音：%d 组（大肥鱼→大废鱼/大飞鱼…）\n"
            "谐音梗：%d 条\n"
            "累计：谐音被喊%d次／命中梗%d次／注入%d次"
            % ("开" if ENABLED else "关",
               len(NAME_MAP), len(PUNS),
               _stat["wake"], _stat["puns"], _stat["inject"])
        )
