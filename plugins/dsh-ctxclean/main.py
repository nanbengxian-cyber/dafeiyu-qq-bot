# dsh-ctxclean —— 清理会话历史里的「陈旧注入块」。
#
# ============================== 为什么需要它 ==============================
#
# 症状：机器人答非所问。有人问 A，它回的是群里另一个人刚才说的 B。
#
# 最干净的一个证据（真实记录，2026-09-02 09:5x）：
#   群友说：「@群友E听见没有？人家都叫你帮忙升级一下眼睛了」
#   它回的：「**富人？那必须的！**群友E大佬出马，一个顶俩。」
# 「富人」这个词根本不在那句话里 —— 它在**上一条别人说的话**里。
# 也就是说它抓错了「当前该回哪一句」。
#
# 根因不在人格提示词，在会话历史的形状。量出来的现状（真实群 100000001）：
#
#   历史 293 轮 / 588 条消息 / 25.7 万字 / token_usage 5.2 万
#   其中「陈旧注入块」487 个：
#     · 群聊上下文块      138 个 —— 每个都写着「以下是你**上次回复之后**的群聊」
#     · Current datetime 289 个 —— 289 个互相矛盾的「现在几点」
#     · 群友档案块         19 个 —— 同一批人的资料重复 19 遍
#     · 搜索结果/网页正文/图片描述 若干 —— 全是几十分钟前那一轮的
#
# 这些块**只对写它的那一轮有意义**，但框架把整个 user 轮原样存进了
# conversations.content（`_save_to_history` 存的是 run_context.messages，
# 注入块本来就在里面）。而 provider_settings.max_context_length = -1
# 表示不截断（truncator.py:119 `if keep_most_recent_turns == -1: return messages`），
# 于是**每次请求都把 138 份「这是最新群聊」一起发出去**。
#
# 模型看到的是：138 段都自称最新的群聊记录 + 289 个不同的当前时间，
# 最后才是这一轮真正的问题。光最近 20 轮就携带了 66 条时间跨度 40 分钟的
# 旧群消息。它从里面挑错一句，是完全可以预期的结果。
#
# 顺带一个放大器：assistant 轮的 think（内心戏）也存了 393 个、8.5 万字，
# 而 openai_source.py:1027 会把它们回传成 reasoning_content。等于每次还要
# 复读一遍自己几十轮前的思考过程。
#
# ============================== 怎么修 ==============================
#
# 在 on_llm_request 钩子里，只动 req.contexts（**历史**），绝不碰
# req.extra_user_content_parts（**当轮**的新鲜注入）。三件事：
#
#   ① 历史 user 轮：删掉 index>=1 的注入块，只留真正的用户原话
#   ② 历史 assistant 轮：删掉 think
#   ③ 只保留最近 N 轮（默认 30），且必须从 user 开头切
#
# 效果（同一份真实历史模拟）：25.7 万字 -> 4742 字，占原来的 2%。
#
# 为什么敢砍历史：长期记忆的活已经交给 dsh-memory 了（群友档案 + 群共同
# 记忆，每轮新鲜注入）。原始逐字历史留 30 轮足够接话，再多只是噪音。
#
# 顺带说明另一半修法（不在本插件里）：provider_settings.identifier 打开后，
# 框架会给每轮加一行 `User ID: x, Nickname: y`，模型才知道「现在说话的是谁」。
# 之前 292 轮里带说话人标签的是 **0 轮** —— 上下文块里的旧消息全都标了
# `[昵称/时间]`，唯独当前这句是光的，模型只能靠猜。本插件对
# <system_reminder> 块做的是**改写而不是删除**：留下 User ID 那一行
# （历史里也就有了说话人），删掉 Current datetime 那一行（那才是矛盾源）。
#
# ============================== 安全性 ==============================
#
# · 钩子在 reset() 之前跑（internal.py:269 call_event_hook，之后才
#   `await reset_coro`），所以改 req.contexts 能真正生效；也因此清理结果
#   会随 `_save_to_history` 落盘 —— 已经堆在库里的 487 个陈旧块会在下一次
#   请求时被自动清掉，不需要单独写迁移脚本。
# · 不认识的东西一律原样放过：_checkpoint 行、tool 轮、content 是字符串的
#   老格式消息、没有 content 的消息。
# · 截断只在 user 轮边界切，避免出现「tool 结果没有对应的 assistant
#   tool_calls」这种 OpenAI 会直接 400 的序列。
# · 任何异常都吞掉并原样返回，宁可不清理也不能让消息发不出去。

# [patch:initiate-compat-v1 认识 dsh-initiate 的合成事件]
import os
import re

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core import logger
from astrbot.core.agent.message import TextPart

# 保留最近多少轮原始对话（一轮 = 一个 user + 一个 assistant）。0=不限
KEEP_TURNS = int(os.environ.get("DSH_CTX_KEEP_TURNS", "30"))
# 是否删掉历史里的注入块
CLEAN_BLOCKS = os.environ.get("DSH_CTX_CLEAN_BLOCKS", "1") != "0"
# 是否删掉历史 assistant 轮的 think（内心戏）
DROP_THINK = os.environ.get("DSH_CTX_DROP_THINK", "1") != "0"
# 是否告诉模型「这句话是不是对你说的」
MARK_ADDRESSED = os.environ.get("DSH_CTX_MARK_ADDRESSED", "1") != "0"

# ---- 第二件事：让模型知道「这句话是不是对我说的」 ----
#
# 随机主动回复（provider_ltm_settings.active_reply）触发时，框架走的是
# group_chat_context.need_active_reply -> event.request_llm(prompt=event.message_str)，
# 送进模型的 user 轮和「有人 @ 我」时**长得一模一样**。模型没有任何线索
# 能区分这两种情况，只能当成「有人在跟我说话」，于是本来是群友之间的对话，
# 它也一本正经地正面回答 —— 这正是「答非所问」体感的另一半来源。
#
# 概率从 10% 提到 25% 之后这个问题会放大 2.5 倍，所以必须同时修。
#
# 人格里本来就写了「群里不是每句话都是对你说的，先判断这句话指向谁」，
# 但它判断不了。这里就是把它缺的那个事实补上，两行字，不讲道理。
_ADDRESSED = (
    "<reply_context>这句话是直接对你说的（@了你或喊了你的名字），正面回。"
    "</reply_context>"
)
_SPONTANEOUS = (
    "<reply_context>没人喊你，这是你自己看到群里在聊就顺口接一句。"
    "所以：别把它当成对你提的问题，别解释、别应答式回复；"
    "先看清这句话是谁对谁说的，只挑你真能接的点，用一句短话插进去；"
    "接不上就说句最短的废话带过，别硬编内容。</reply_context>"
)
# 第三档：dsh-initiate 的主动开口。前两档都在描述「有一句话」，
# 而这一档根本没有那句话 —— 群里静着，是它自己决定开口的。
# 落到 _SPONTANEOUS 会让模型去「看清这句话是谁对谁说的」，
# 那句话不存在，于是它会接一句凭空想象出来的话。
# 具体安静了多久、刚才在聊什么，由 dsh-initiate 的 <initiate_context> 块给。
_INITIATED = (
    "<reply_context>没人在跟你说话，群里这会儿是静着的，是你自己决定开个口。"
    "所以：别回答什么、别应答、别问「大家在吗」这种废话；"
    "就像人翻到一个冷掉的群随口丢一句那样，说一句你真想说的短话。</reply_context>"
)
# 第四档：dsh-proactive 的兴趣探头。群里**正在聊**，聊到了你感兴趣的内容，
# 所以和 _SPONTANEOUS（自己插话）更像，但触发原因更强：不是「顺口接一句」，
# 而是「聊到我馋的东西/我老本行/点名我了」。和 _INITIATED 的区别：
# 那一档群里是静的，这一档群里正热闹。
_PROACTIVE = (
    "<reply_context>群里正在聊，有人聊到了你很感兴趣的东西（白米饭、吃的、"
    "深海小鲸鱼、DeepSeek/AI、或者点名大肥鱼）。是你自己决定探头接一句的："
    "别假装有人@了你，别解释你在主动说话，别应答式回复；"
    "像真人听到感兴趣的话题时自然插一句那样，只看最戳你的那一处，短话接上；"
    "接不上就发个表情或语气词带过，别硬编内容。</reply_context>"
)

# 注入块的形状：整段以 <小写标签> 开头。用结构判断而不是枚举插件名，
# 这样以后任何插件新增 <xxx_context> 块都会被自动清掉，不用改这里。
_TAG_RE = re.compile(r"^\s*<([a-z][a-z0-9_]*)>")

# <system_reminder> 块里逐行保留的内容：说话人身份有长期价值，
# 「当前时间」只对当轮有效，留着就是 289 个互相矛盾的「现在」。
_KEEP_LINE_RE = re.compile(r"^\s*(User ID:|Group name:)")

# 框架给「只@了机器人、没说任何内容」那种消息造的一段 130 字指令
# （builtin_stars/astrbot/main.py:101）。它是**当轮**指令，但会被
# _save_to_history 原样存进历史的 part0 —— 于是后面每一轮都看到
# 「用户只是通过@来唤醒你，但并未在这条消息中输入内容」，还不止一条。
# 真实群里 6 条消息内就撞见 2 条。这是同一种病，只是它长在 part0，
# 不带 <标签>，所以要单独认。
# 保留「有人戳了我一下」这个对话事实，去掉那段指令。
_EMPTY_AT_PREFIX = "注意，你正在社交媒体上中与用户进行聊天，用户只是通过@来唤醒你"
_EMPTY_AT_REPLACEMENT = "（只@了你，没说内容）"

# 这些标签即使出现在 index>=1 也不删 —— 它们描述的是**这条消息本身**
# 带的内容（图片说明），不是外部注入的一轮性背景。
# quoted_message 由 dsh-quote 注入，描述的是**这条消息本身**引用了谁的哪句话，
# 和框架原来的 <Quoted Message> 同语义，所以和 image_caption 一样留在历史里。
_NEVER_DROP = {"image_caption", "quoted_message"}
# reply_context 由本插件在当轮注入；它进历史后就过期了，所以**不**加白名单，
# 让下一轮的清理把它删掉。这里写下来是为了防止以后有人误加。


def _clean_system_reminder(text: str) -> str:
    """<system_reminder> 块：只留说话人那几行，去掉当前时间。

    返回空字符串表示整块都可以删。
    """
    inner = re.sub(r"^\s*<system_reminder>", "", text)
    inner = re.sub(r"</system_reminder>\s*$", "", inner)
    kept = [ln for ln in inner.splitlines() if _KEEP_LINE_RE.match(ln)]
    if not kept:
        return ""
    return "<system_reminder>" + "\n".join(kept) + "</system_reminder>"


def _clean_user(msg: dict) -> tuple[dict, int]:
    """历史 user 轮：留 part0（真正的原话），清掉后面的注入块。"""
    parts = msg.get("content")
    if not isinstance(parts, list) or not parts:
        return msg, 0

    dropped = 0

    # part0 特例：框架的「只@没说话」指令。压成一句事实。
    head = parts[0]
    if (isinstance(head, dict) and head.get("type") == "text"
            and str(head.get("text") or "").startswith(_EMPTY_AT_PREFIX)):
        head = dict(head)
        head["text"] = _EMPTY_AT_REPLACEMENT
        dropped += 1

    if len(parts) == 1:
        if not dropped:
            return msg, 0
        out = dict(msg)
        out["content"] = [head]
        return out, dropped

    kept = [head]
    for p in parts[1:]:
        if not isinstance(p, dict) or p.get("type") != "text":
            kept.append(p)          # 图片、音频等非文本 part 一律保留
            continue
        text = str(p.get("text") or "")
        m = _TAG_RE.match(text)
        if not m:
            kept.append(p)          # 不是注入块，不动
            continue
        tag = m.group(1)
        if tag in _NEVER_DROP:
            kept.append(p)
            continue
        if tag == "system_reminder":
            slim = _clean_system_reminder(text)
            if slim and slim != text:
                q = dict(p)
                q["text"] = slim
                kept.append(q)
                dropped += 1        # 算作「瘦身了一处」
                continue
            if slim:
                kept.append(p)
                continue
        dropped += 1                # 整块丢弃

    if not dropped:
        return msg, 0
    out = dict(msg)
    out["content"] = kept
    return out, dropped


def _clean_assistant(msg: dict) -> tuple[dict, int]:
    """历史 assistant 轮：去掉 think。"""
    parts = msg.get("content")
    if not isinstance(parts, list):
        return msg, 0
    kept = [p for p in parts
            if not (isinstance(p, dict) and p.get("type") == "think")]
    n = len(parts) - len(kept)
    if not n:
        return msg, 0
    if not kept:
        # 整条都是 think（极少见）。留原样，别造出 content 为空的消息 ——
        # 有些渠道会拒收空 content 的 assistant 消息。
        return msg, 0
    out = dict(msg)
    out["content"] = kept
    return out, n


def _is_plain(msg) -> bool:
    """是不是一条能安全处理的普通消息（不是 _checkpoint 之类的内部行）。"""
    return isinstance(msg, dict) and msg.get("role") in ("user", "assistant", "tool")


def _keep_recent_turns(msgs: list, turns: int) -> list:
    """只留最近 turns 轮，且从 user 开头切。

    从 user 切是硬要求：OpenAI 规定 tool 消息必须跟在带 tool_calls 的
    assistant 之后，从中间切会造出孤儿 tool 消息，直接 400。
    """
    if turns <= 0:
        return msgs
    body = [m for m in msgs if _is_plain(m) or isinstance(m, dict)]
    n_user = sum(1 for m in body if isinstance(m, dict) and m.get("role") == "user")
    if n_user <= turns:
        return msgs

    # 从后往前数 turns 个 user，记下位置
    seen = 0
    cut = 0
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if isinstance(m, dict) and m.get(ole") == "user":
            seen += 1
            if seen == turns:
                cut = i
                break
    return msgs[cut:]


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._last_log = 0.0
        logger.info(
            "[ctxclean] 已加载：留最近%s轮 清注入块=%s 去内心戏=%s 标注是否被喊=%s",
            KEEP_TURNS or "全部",
            "开" if CLEAN_BLOCKS else "关",
            "开" if DROP_THINK else "关",
            "开" if MARK_ADDRESSED else "关",
        )

    @filter.on_llm_request()
    async def clean(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        try:
            ctxs = getattr(req, "contexts", None)
            if not isinstance(ctxs, list) or not ctxs:
                return

            before_msgs = len(ctxs)
            blocks = thinks = 0
            out = []
            for m in ctxs:
                if not isinstance(m, dict):
                    out.append(m)
                    continue
                role = m.get("role")
                if role == "user" and CLEAN_BLOCKS:
                    m, n = _clean_user(m)
                    blocks += n
                elif role == "assistant" and DROP_THINK:
                    m, n = _clean_assistant(m)
                    thinks += n
                out.append(m)

            out = _keep_recent_turns(out, KEEP_TURNS)
            dropped_msgs = before_msgs - len(out)

            if blocks or thinks or dropped_msgs:
                req.contexts = out
                # 用 info：这条是判断「答非所问是否已修好」的唯一依据，
                # 上一次把关键判断放 debug 的教训还热着（log_level=INFO）。
                logger.info(
                    "[ctxclean] 历史 %d->%d 条（砍 %d），清注入块 %d 个，去内心戏 %d 段",
                    before_msgs, len(out), dropped_msgs, blocks, thinks,
                )
        except BaseException as e:
            logger.error("[ctxclean] 清理失败，保持原样: %s", e)

    @filter.on_llm_request()
    async def mark_addressed(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """告诉模型这轮是「有人喊我」还是「我自己插话」。

        分开成第二个钩子而不是塞进 clean()：清历史失败时这个提示仍要加上，
        两件事互不拖累。
        """
        if not MARK_ADDRESSED:
            return
        try:
            from astrbot.core.platform.message_type import MessageType

            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            if event.get_extra("dsh_initiate"):
                kind, block = "主动开口", _INITIATED
            elif event.get_extra("dsh_proactive"):
                kind, block = "兴趣探头", _PROACTIVE
            elif bool(getattr(event, "is_at_or_wake_command", False)):
                kind, block = "被喊的", _ADDRESSED
            else:
                kind, block = "自己插话", _SPONTANEOUS
            req.extra_user_content_parts.append(TextPart(text=block))
            logger.info("[ctxclean] 这轮是%s", kind)
        except BaseException as e:
            logger.error("[ctxclean] 标注失败: %s", e)

    @filter.command("上下文状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """/上下文状态 —— 看这个会话的历史有多大、清理策略是什么。"""
        try:
            umo = event.unified_msg_origin
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            n = raw = 0
            if cid:
                conv = await self.context.conversation_manager.get_conversation(umo, cid)
                if conv and conv.history:
                    import json
                    h = json.loads(conv.history)
                    n = len(h)
                    raw = len(json.dumps(h, ensure_ascii=False))
            yield event.plain_result(
                "上下文清理：留最近 %s 轮｜清注入块 %s｜去内心戏 %s｜标注是否被喊 %s\n"
                "当前会话历史 %d 条、约 %.1f 万字\n"
                "（长期记忆走 dsh-memory 的群友档案，不靠逐字历史）"
                % (KEEP_TURNS or "全部",
                   "开" if CLEAN_BLOCKS else "关",
                   "开" if DROP_THINK else "关",
                   "开" if MARK_ADDRESSED else "关",
                   n, raw / 10000)
            )
        # 不用 BaseException：CancelledError/GeneratorExit 属于「这轮被放弃了」，
        # 吞掉它等于骗框架说自己正常跑完，可能留下半截状态或
        # `async generator ignored GeneratorExit`。真正的异常仍然全部兜住。
        except Exception as e:
            yield event.plain_result("查不到：%s" % e)
