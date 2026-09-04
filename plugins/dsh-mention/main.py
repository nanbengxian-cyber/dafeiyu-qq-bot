# dsh-mention —— 智能 @ 控制。
#
# 问题：AstrBot 的 platform_settings.reply_with_mention 是个全局开关，开了就
# 每条回复都在开头插 @发送者。在一个基本只有一两个人跟机器人聊的群里，这是纯
# 噪音——对方明明知道你在回他，还要被 @ 一次。
#
# ============================== v2（2026-09-02）==============================
#
# v1 的判断规则里有一条「回复延迟 ≥ 8s 就 @」，本意是「这条消息可能已经被别的
# 消息刷走了，@ 一下才找得回来」。这条**从上线起就一直命中**：
#
#   delay = now - event.message_obj.timestamp
#   而适配器是在**收到消息那一刻**填 timestamp 的（abm.timestamp = int(time.time())）
#
# 所以 delay 量的根本不是「群里有多吵」，而是**机器人自己想了多久**。一次
# 带人格 + 20 条历史的 LLM 调用普遍就 5~15 秒，带工具动辄几十秒。于是这条
# 规则等价于「无条件 @」，整个智能判断形同虚设。
# 实测：近 24 小时 8 条回复，8 条都带 At —— 100%。
#
# v2 的思路：**@ 的唯一用途是「人已经不在这条消息附近了，需要一个通知把他叫
# 回来」**。据此只留三种情况：
#
#   ① 用了工具        —— 生图 5~15s、生视频 212~287s、语音 2s+上传。这类回复
#                        回来时人早翻页了，必须 @。这也是「调用工具时才艾特」
#                        这个要求的落点。
#   ② 中间有人插话    —— 从他发问到现在，**别人**又说了 ≥ AT_INTERLEAVE 条。
#                        这才是真正的歧义信号。v1 用的「上次回复的不是这个人」
#                        是错的代理量：两个人轮流跟机器人聊，每条都会命中。
#   ③ 真的慢得离谱    —— ≥ AT_SLOW 秒。只兜住渠道抽风、排队这类异常。
#
# 删掉的规则：「本群首次回复」「换人」「距上次回复同一人 ≥180s 算新话题」。
# 这三条都不是「人不在附近」的证据，只是让 @ 变多。
#
# ---- 两个阈值是量出来的，不是拍的 ----
#
# 拿近 24 小时真实日志，把「收到消息(core.event_bus)」和「准备发送
# (Prepare to send)」按用户配对，24 个样本：
#
#   回复耗时：最小 1.1s  P25 12.5s  中位 22.1s  P75 34.9s  P90 39.5s  最大 63.0s
#   插话条数：0 条 46%，1 条 33%，2 条 12%，4 条 8%
#
# 中位数 22 秒 —— 这直接说明 v1 那个 8 秒阈值为什么等于无条件 @。
# 我第一版把 AT_SLOW 定在 25s，量完发现仍会 @ 掉 33% 的回复，还是太多。
# 各组合的实测命中率（含工具规则）：
#
#   插话≥2 慢≥25s -> 54%      插话≥3 慢≥30s -> 46%
#   插话≥3 慢≥45s -> 33%      插话≥3 慢≥60s -> 29%   ← 采用
#   关掉插话 慢≥60s -> 21%（但会漏掉真正吵的场合）
#
# 选 (3, 60)：29% 里工具贡献 4 条、插话 2 条、异常慢 1 条 —— 工具是主力，
# 另两条退成罕见兜底，正是「平时别喊人、干活了才喊」。对比 v1 的 24/24 = 100%。
# 这两个数随渠道速度变化，量的脚本留在 data/tests/{delaystat,interstat,combostat}.py，
# 以后觉得 @ 多了或少了先跑一遍再改，别凭感觉调。
#
# 判断「用了工具」用两个信号，缺一不可靠：
#   · @filter.on_using_llm_tool() —— 框架在真正执行函数工具前触发（源码
#     astr_agent_hooks.py: MainAgentHooks.on_tool_start），能拿到工具名。
#   · imagegen_done / voice_done / video_done 三个 extra —— 那三个插件的
#     **兜底钩子**路径不经过函数工具（模型嘴上答应但没调工具时插件自己动手），
#     只看工具事件会漏。这三个 extra 本来就是它们防重复用的，顺手复用。
# extras 在 clear_result() 里不会被清（源码只置 self._result = None），而
# on_tool_end 会调 clear_result —— 所以必须用 extras 而不是往 result 上做记号。
#
# 配套改动：platform_settings.reply_with_mention 必须设为 false，否则框架会
# 先插一个 At，本插件再判断就变成「双 @」。本插件是唯一的 At 来源。
#
# 时机：on_decorating_result 钩子在 ResultDecorateStage 第 160 行触发，而框架
# 插 At 的代码在第 429 行——钩子跑在前面，所以这里插入的 At 位置和框架一致，
# 后续的分段回复、t2i 等逻辑都能正常处理。

# [patch:initiate-compat-v1 认识 dsh-initiate 的合成事件]
import os
import time
from collections import deque

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.core import logger
from astrbot.core.platform.message_type import MessageType

# 总开关：0=完全不 @，1=智能判断，2=总是 @（等价于框架原行为）
AT_MODE = os.environ.get("DSH_AT_MODE", "1")
# 用了工具（生图/生视频/语音/搜索…）就 @。1=开 0=关
AT_TOOL = os.environ.get("DSH_AT_TOOL", "1") != "0"
# 从他发问到现在，别人又说了这么多条 -> @。0=关掉这条规则。
# 3 是量出来的：真实分布里 0~1 条占 79%，≥3 只占 8%（见文件头）
AT_INTERLEAVE = int(os.environ.get("DSH_AT_INTERLEAVE", "3"))
# 慢到这个程度才算异常 -> @。60s 是量出来的：中位 22s、P90 39.5s，
# 60s 只兜住渠道抽风那一档，不会把正常思考时间算成异常
AT_SLOW = float(os.environ.get("DSH_AT_SLOW", "60"))

# 一次「用了工具」的记号，写在 event extras 上
TOOL_FLAG = "dsh_at_used_tool"
# 三个插件兜底路径留下的记号，语义等价于「刚发了个慢媒体」
FALLBACK_FLAGS = ("imagegen_done", "voice_done", "video_done")

# group_id -> deque[(uid, 收到时间)]，只用于数「中间插了几条别人的话」。
# 纯内存、定长，重启即空；数不准的代价只是少 @ 一次，不值得落盘。
_recent: dict[str, deque] = {}
_RECENT_MAX = 40

# group_id -> (上次回复的用户 id, 时间戳)，只给 /艾特模式 展示用
_last_reply: dict[str, tuple[str, float]] = {}


def _used_tool(event: AstrMessageEvent) -> str:
    """这轮有没有动过工具/慢媒体。返回工具名（空字符串=没有）。"""
    try:
        name = event.get_extra(TOOL_FLAG)
        if name:
            return str(name)
        for flag in FALLBACK_FLAGS:
            if event.get_extra(flag):
                return flag.replace("_done", "")
    except BaseException:
        pass
    return ""


def _interleaved(gid: str, uid: str, since: float) -> int:
    """他发问之后，别人又说了几条。"""
    q = _recent.get(gid)
    if not q:
        return 0
    n = 0
    for ouid, ts in q:
        if ts > since and ouid != uid:
            n += 1
    return n


def _decide(event: AstrMessageEvent) -> tuple[bool, str]:
    """判断这条回复要不要 @，返回 (要不要, 原因)。原因会进日志。"""
    if AT_MODE == "0":
        return False, "模式=从不"
    if AT_MODE == "2":
        return True, "模式=总是"

    if event.get_message_type() == MessageType.FRIEND_MESSAGE:
        return False, "私聊"

    gid = str(event.get_group_id() or event.unified_msg_origin or "?")
    uid = str(event.get_sender_id() or "?")
    now = time.time()

    # 收到这条消息到现在过了多久。注意这个值主要反映**机器人想了多久**，
    # 不是群里多吵 —— v1 拿它当「消息被刷走」的证据是错的，见文件头。
    try:
        msg_ts = float(event.message_obj.timestamp or now)
    except (TypeError, ValueError, AttributeError):
        msg_ts = now
    delay = now - msg_ts

    tool = _used_tool(event)
    inter = _interleaved(gid, uid, msg_ts)

    # 三条规则，按「证据强度」排，谁先命中就用谁的理由
    if AT_TOOL and tool:
        return True, "用了工具(%s，等了%.0fs)" % (tool, delay)
    if AT_INTERLEAVE > 0 and inter >= AT_INTERLEAVE:
        return True, "中间插了%d条别人的话" % inter
    if delay >= AT_SLOW:
        return True, "慢得异常(%.0fs)" % delay

    # 不 @ 的时候也要把三个信号全打出来，否则「为什么没 @」只能靠猜。
    return False, "不用@(工具=无 插话=%d/%d 延迟=%.0f/%.0fs)" % (
        inter, AT_INTERLEAVE, delay, AT_SLOW,
    )


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        mode_name = {"0": "从不", "1": "智能", "2": "总是"}.get(AT_MODE, AT_MODE)
        logger.info(
            "[mention] 已加载 v2：模式=%s 用工具就@=%s 插话阈值=%d条 异常慢阈值=%.0fs",
            mode_name,
            "开" if AT_TOOL else "关",
            AT_INTERLEAVE,
            AT_SLOW,
        )

    # ---- 记录群里谁在什么时候说话，只为「中间插了几条别人的话」服务。
    #
    # 用 platform_adapter_type(ALL) 挂一个被动处理器。这**不会**让机器人对每条
    # 消息都回话：ProcessStage 里 LLM 调用另有 event.is_at_or_wake_command 把关，
    # 这里只是搭个便车看一眼消息就返回。dsh-memory 用的同一套路子，已验证。
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def watch(self, event: AstrMessageEvent) -> None:
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            if not gid or not uid:
                return
            # 机器人自己的话不算插话，否则它一开口就把自己算进去了
            if uid == str(event.get_self_id() or ""):
                return
            # dsh-initiate 的合成事件不是真人发言，记进来会污染
            # 「他发问后别人又说了几条」这个计数（那是另外两条 @ 规则的判据）。
            if event.get_extra("dsh_initiate"):
                return
            q = _recent.get(gid)
            if q is None:
                q = _recent[gid] = deque(maxlen=_RECENT_MAX)
            try:
                ts = float(event.message_obj.timestamp or time.time())
            except (TypeError, ValueError, AttributeError):
                ts = time.time()
            q.append((uid, ts))
        except BaseException as e:
            logger.debug("[mention] 记录消息失败: %s", e)

    # ---- 框架真正要执行某个函数工具了，留个记号给后面的 @ 判断
    @filter.on_using_llm_tool()
    async def note_tool(self, event: AstrMessageEvent, tool, tool_args) -> None:
        try:
            name = getattr(tool, "name", None) or str(tool)
            event.set_extra(TOOL_FLAG, name)
            logger.debug("[mention] 记下工具调用: %s", name)
        except BaseException as e:
            logger.debug("[mention] 记录工具失败: %s", e)

    @filter.on_decorating_result()
    async def smart_at(self, event: AstrMessageEvent) -> None:
        try:
            result = event.get_result()
            if result is None or not result.chain:
                return

            # 主动开口没有「发送者」可 @：dsh-initiate 的合成事件用的是哨兵号
            # （不能用机器人自己的号，ignore_bot_self_message=True 会把事件掐掉），
            # 插进去就是一个点不动的 @。
            if event.get_extra("dsh_initiate"):
                logger.info("[mention] at=False 主动开口，没有对象可@")
                return

            # 和框架保持一致：只给纯文本/图文消息加 @，别去动转发、语音等复杂链
            if not all(isinstance(c, (Plain, Image)) for c in result.chain):
                return
            # 已经有 At 了（别的插件或框架加的），不重复插
            if any(isinstance(c, At) for c in result.chain):
                return

            gid = str(event.get_group_id() or event.unified_msg_origin or "?")
            uid = str(event.get_sender_id() or "?")

            need_at, reason = _decide(event)

            if need_at:
                result.chain.insert(
                    0,
                    At(qq=event.get_sender_id(), name=event.get_sender_name()),
                )
                # At 后面紧跟文字时补一个空格，避免 @昵称 和正文粘在一起
                if len(result.chain) > 1 and isinstance(result.chain[1], Plain):
                    text = result.chain[1].text or ""
                    if text and not text[0].isspace():
                        result.chain[1].text = " " + text

            if event.get_message_type() != MessageType.FRIEND_MESSAGE:
                _last_reply[gid] = (uid, time.time())

            # 用 info 而不是 debug：日志级别是 INFO，debug 根本不落盘，
            # 上一版就是因为这条看不见，才让「无条件 @」藏了一整天。
            logger.info("[mention] at=%s %s", need_at, reason)
        except BaseException as e:
            # @ 只是锦上添花，任何异常都不该影响消息发出
            logger.error("[mention] 处理失败: %s", e)

    @filter.command("艾特模式")
    async def cmd_mode(self, event: AstrMessageEvent):
        """/艾特模式 —— 查看当前 @ 策略。"""
        mode_name = {"0": "从不 @", "1": "智能判断", "2": "每条都 @"}.get(
            AT_MODE, AT_MODE
        )
        gid = str(event.get_group_id() or "?")
        prev = _last_reply.get(gid)
        last = "%s（%.0fs 前）" % (prev[0], time.time() - prev[1]) if prev else "无记录"
        seen = len(_recent.get(gid) or ())
        yield event.plain_result(
            "@ 策略：%s\n"
            "· 用了工具（生图/生视频/语音/搜网页）就 @：%s\n"
            "· 他发问后别人又说了 ≥%d 条就 @\n"
            "· 回复慢过 %.0fs（异常）就 @\n"
            "· 其余情况都不 @ —— 就在他上一句下面回，不用喊他\n"
            "本群上次回复：%s（缓存 %d 条发言记录）"
            % (mode_name, "开" if AT_TOOL else "关", AT_INTERLEAVE, AT_SLOW, last, seen)
        )
