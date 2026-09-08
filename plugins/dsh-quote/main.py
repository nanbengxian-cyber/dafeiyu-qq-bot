# -*- coding: utf-8 -*-
"""dsh-quote：把「引用消息」改写成机器人真能读懂的形状。

## 为什么需要这个插件

框架在 `astr_main_agent._process_quote_message` 里把引用消息拼成这样一块：

    <Quoted Message>
    (大肥鱼): 30块确实不贵
    </Quoted Message>

这块东西有三个致命的缺失，实测已经造成真群里的误判：

1. **没说这句话是不是机器人自己说的。** 机器人在群里的群名片就是「大肥鱼」，
   而群里还有一个**真人**（QQ 1493202695）也把名字改成了「大肥鱼」。
   于是 `(大肥鱼): 念啥` 到底是「你自己刚说的」还是「另一个人说的」，
   模型没有任何依据可以分辨 —— 这正是「看不懂别人引用的聊天、错以为是自己」
   的根因。凭昵称认人在这个群里本来就是错的，必须用 QQ 号。

2. **没说现在是谁在引用。** 只有被引用者的名字，没有引用者。模型于是把
   「A 引用 B 的话说了句什么」压平成「有人说了 B 的话」，主谓宾就错了。

3. **没说这次的话是对谁说的。** 消息里 @ 了别人（不是机器人）时，
   模型照样当成在对自己说话。

## 做法

在 `on_llm_request` 里（框架已经拼好块、但请求还没发出去，顺序已核对：
`_process_quote_message` 在 build 阶段，`OnLLMRequestEvent` 在其后触发）
找到那个 `<Quoted Message>` 块，整块换成一段**用 QQ 号说清关系**的文字：
谁在说、他引用的是谁的话、那句是不是机器人自己的、这次 @ 的是谁。

标签用小写 `<quoted_message>`，与 dsh-ctxclean 的结构判断对齐；
同时把它加进 ctxclean 的 `_NEVER_DROP`，保持和框架原块一样「留在历史里」的语义
（它描述的是这条消息本身，不是一轮性的外部注入）。

失败一律 fail-open：拿不到引用信息就原样不动，绝不让消息发不出去。
"""

import os
import re

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import At, Reply
from astrbot.core.platform.message_type import MessageType

ENABLED = os.environ.get("DSH_QUOTE", "1").lower() not in {"0", "false", "off"}
# 引用原文最多带多少字。引用只是背景，不该挤掉当前这句话。
MAX_QUOTE = max(20, int(os.environ.get("DSH_QUOTE_MAX", "120")))
# 框架那块的开头，用来定位替换。
FRAMEWORK_HEAD = "<Quoted Message>"


def _strip_at_prefix(text: str) -> str:
    """去掉引用原文开头那串 @：它是上一轮的 @，不是内容。

    **必须保守**：只在 @ 后面跟着 `(QQ号)` 或空白时才切。
    NapCat 上报的 message_str 里 At 和正文常常没有空格（真实样本
    `@群主对`，正文只有「对」一个字），此时昵称边界无法确定，
    贪婪切会把正文一起吃掉、退化成「（空消息）」——那比不切更糟。
    这种情况交给 `_chain_text` 从组件链里精确剥离。
    """
    return re.sub(
        r"^(?:\s*@[^\s(（]*(?:[(（]\d+[)）]|\s+))+", "", text or ""
    ).strip()


def _chain_text(chain) -> str:
    """从被引用消息的组件链里取纯文本，精确跳过 At 组件。

    比正则可靠：At 组件自带 qq 字段，不用猜昵称从哪结束。
    """
    out: list[str] = []
    for comp in chain or ():
        if str(getattr(comp, "qq", "") or "").strip():
            continue  # At 组件
        piece = str(getattr(comp, "text", "") or "").strip()
        if piece:
            out.append(piece)
    return " ".join(out).strip()


def _who(name: str, uid: str) -> str:
    """统一的人物写法：昵称 + QQ 号。群里有重名，只有 QQ 号是可靠凭证。"""
    name = (name or "").strip()
    uid = (uid or "").strip()
    if name and uid:
        return "「%s」（QQ %s）" % (name, uid)
    if uid:
        return "QQ %s" % uid
    return "「%s」" % name if name else "某人"


def build_block(
    speaker_name: str,
    speaker_uid: str,
    quoted_name: str,
    quoted_uid: str,
    quoted_text: str,
    self_id: str,
    at_uids: tuple[str, ...] = (),
    at_names: tuple[str, ...] = (),
) -> str:
    """拼出替换用的引用说明块。纯函数，便于断言。

    只描述**事实关系**，不给态度也不给台词 —— 给台词模型会照念。
    """
    self_id = (self_id or "").strip()
    quoted_uid = (quoted_uid or "").strip()
    speaker_uid = (speaker_uid or "").strip()
    text = _strip_at_prefix(quoted_text)[:MAX_QUOTE] or "（空消息）"

    lines = ["<quoted_message>",
             "这条消息引用了之前的一句话。引用的内容是**旧的**，不是刚刚发生的新消息。",
             "· 现在说话的人是 %s。" % _who(speaker_name, speaker_uid)]

    # 判断被引用的那句是不是机器人自己说的 —— 只认 QQ 号，不认昵称。
    # 群里有真人把名字改成了和机器人一样的「大肥鱼」，靠昵称判断必错。
    if quoted_uid and self_id and quoted_uid == self_id:
        lines.append("· 他引用的那句话是【你自己】之前说的：「%s」" % text)
        lines.append("· 也就是说，他在回应你。顺着你自己那句往下接。")
    elif quoted_uid:
        lines.append("· 他引用的那句话是 %s 说的，【不是你说的】：「%s」"
                     % (_who(quoted_name, quoted_uid), text))
        lines.append("· 也就是说，这是他们之间的对话，你只是看见了。别把它当成你做过的事或你说过的话。")
    else:
        # 拿不到被引用者的 QQ 号时**不许猜**：说不清就只说内容。
        lines.append("· 他引用了一句旧消息（分不清是谁说的）：「%s」" % text)
        lines.append("· 分不清是谁说的，就别假设是你自己说的。")

    others = [(u, n) for u, n in zip(at_uids, at_names) if u and u != self_id]
    if self_id and self_id in at_uids:
        lines.append("· 这次他 @ 了你，所以这句话是对你说的。")
    elif others:
        who = "、".join(_who(n, u) for u, n in others[:3])
        lines.append("· 这次他 @ 的是 %s，不是你。" % who)

    lines.append("</quoted_message>")
    return "\n".join(lines)


def _quote_of(event: AstrMessageEvent):
    for comp in getattr(event.message_obj, "message", None) or ():
        if isinstance(comp, Reply):
            return comp
    return None


def _ats_of(event: AstrMessageEvent) -> tuple[tuple[str, ...], tuple[str, ...]]:
    uids: list[str] = []
    names: list[str] = []
    for comp in getattr(event.message_obj, "message", None) or ():
        if isinstance(comp, At):
            uids.append(str(getattr(comp, "qq", "") or "").strip())
            names.append(str(getattr(comp, "name", "") or "").strip())
    return tuple(uids), tuple(names)


def _quoted_text(quote) -> str:
    """取被引用消息的正文。优先组件链（能精确跳过 At），再退回 message_str。"""
    text = _chain_text(getattr(quote, "chain", None))
    if text:
        return text
    return str(getattr(quote, "message_str", "") or "").strip()


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info("[quote] 已加载：%s 引用原文上限%d字", "开" if ENABLED else "关", MAX_QUOTE)

    @filter.on_llm_request()
    async def rewrite(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not ENABLED:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            quote = _quote_of(event)
            if quote is None:
                return
            at_uids, at_names = _ats_of(event)
            block = build_block(
                speaker_name=str(event.get_sender_name() or ""),
                speaker_uid=str(event.get_sender_id() or ""),
                quoted_name=str(getattr(quote, "sender_nickname", "") or ""),
                quoted_uid=str(getattr(quote, "sender_id", "") or ""),
                quoted_text=_quoted_text(quote),
                self_id=str(event.get_self_id() or ""),
                at_uids=at_uids,
                at_names=at_names,
            )
            parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(parts, list):
                return
            replaced = False
            for i, part in enumerate(parts):
                text = str(getattr(part, "text", "") or "")
                if text.startswith(FRAMEWORK_HEAD):
                    parts[i] = TextPart(text=block)
                    replaced = True
                    break
            if not replaced:
                parts.append(TextPart(text=block))
            quoted_uid = str(getattr(quote, "sender_id", "") or "")
            logger.info(
                "[quote] 改写引用块（%s）：被引用者=%s 是否本人=%s @了=%s",
                "替换" if replaced else "追加", quoted_uid or "未知",
                quoted_uid == str(event.get_self_id() or ""), ",".join(u for u in at_uids if u) or "无",
            )
        # 不用 BaseException：CancelledError/GeneratorExit 属于「这轮被放弃了」，
        # 吞掉它等于骗框架说自己正常跑完，可能留下半截状态或
        # `async generator ignored GeneratorExit`。真正的异常仍然全部兜住。
        except Exception as exc:  # 绝不能让引用处理挡住回复
            logger.error("[quote] 改写失败，保持原样: %s", exc)

    @filter.command("引用状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id()) != os.environ.get("DSH_QUOTE_OWNER", "2774000001"):
            return
        quote = _quote_of(event)
        yield event.plain_result(
            "引用改写：%s｜原文上限 %d 字\n本条%s引用\n机器人自己的 QQ：%s\n"
            "（判断「是不是我自己说的」只用 QQ 号，群里有重名昵称）"
            % ("开" if ENABLED else "关", MAX_QUOTE,
               "带" if quote is not None else "不带", event.get_self_id())
        )
