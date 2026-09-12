# -*- coding: utf-8 -*-
"""
dsh-merge —— 艾特风暴停歇后重扫上下文再回复。

行为不是把收到的艾特逐条拼起来，而是：艾特过密时立即暂停逐条回复；每次新艾特
都重置静默计时；确认没人继续喊后，重新读取最新真实群聊上下文，让主聊天模型只
挑当前仍在继续的核心话头自然接一句。不同话题宁可舍弃，也不硬拼成清单。

状态机：idle -> collecting -> settling -> flush。MAX_WAIT 仅告警，不会在艾特仍
持续时强行回复；必须真正安静 SILENCE 秒才 flush。

命令：/整合回复状态（群主）
"""

import asyncio
import os
import random
import time

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.platform.message_type import MessageType
from astrbot.core.message.message_event_result import MessageChain
from .merge_logic import (
    bare_allowed,
    bare_choose,
    build_context_prompt,
    combine_context,
    read_recent_context,
)

# ---------------------------------------------------------------- 配置
ENABLED = os.environ.get("DSH_MERGE", "1") != "0"
SHADOW = os.environ.get("DSH_MERGE_SHADOW", "0") != "0"
OWNER = os.environ.get("DSH_MERGE_OWNER", "2774000001")
GROUPS = set(
    g.strip() for g in os.environ.get("DSH_MERGE_GROUPS", "100000001").split(",") if g.strip()
)
# 瞬间：20 秒内 3 条
BURST_N = float(os.environ.get("DSH_MERGE_BURST_N", "20"))
BURST_K = int(os.environ.get("DSH_MERGE_BURST_K", "3"))
# 累积：120 秒内 5 条
ACCUM_N = float(os.environ.get("DSH_MERGE_ACCUM_N", "120"))
ACCUM_K = int(os.environ.get("DSH_MERGE_ACCUM_K", "5"))
# 静默多久才认定“他们喊完了”。MAX_WAIT 只用于日志和状态，不再强制打断持续艾特。
SILENCE = float(os.environ.get("DSH_MERGE_SILENCE", "8"))
MAX_WAIT = float(os.environ.get("DSH_MERGE_MAX_WAIT", "45"))
# 停歇后重读多少条真实群聊上下文
CONTEXT_N = max(8, int(os.environ.get("DSH_MERGE_CONTEXT_N", "24")))
DB_PATH = os.environ.get("DSH_MERGE_DB", "/AstrBot/data/dsh_memory.db")
# 整合调用的超时
TIMEOUT = float(os.environ.get("DSH_MERGE_TIMEOUT", "90"))
# 单条消息文本最长收多少（防注入长文刷屏）
MAX_MSG = int(os.environ.get("DSH_MERGE_MAX_MSG", "120"))

# 空艾特偶尔回应（有人只点了 @ 没写字）——「不能回复太多」的闸：
#   概率 RATE 才回、同人冷却 UID_CD 秒、每群 WIN 秒内至多 CAP 条。
BARE_ON = os.environ.get("DSH_MERGE_BARE", "1") != "0"
BARE_RATE = float(os.environ.get("DSH_MERGE_BARE_RATE", "0.35"))
BARE_UID_CD = float(os.environ.get("DSH_MERGE_BARE_UID_CD", "180"))
BARE_GROUP_CAP = int(os.environ.get("DSH_MERGE_BARE_GROUP_CAP", "4"))
BARE_GROUP_WIN = float(os.environ.get("DSH_MERGE_BARE_GROUP_WIN", "900"))

# 每群的@历史（计数用）：gid -> deque[(uid, ts)]
_recent: dict[str, object] = {}
_RECENT_MAX = 120

# 每群收集状态
class _Win:
    __slots__ = ("gid", "origin", "items", "start", "last", "task", "flushed")

    def __init__(self, gid: str):
        self.gid = gid
        self.origin = ""  # 统一消息来源（platform:type:session），发送要用
        self.items: list[tuple[str, str, float, str]] = []  # (uid, name, ts, text)
        self.start = time.time()
        self.last = time.time()
        self.task: asyncio.Task | None = None
        self.flushed = False


_windows: dict[str, _Win] = {}

# 统计（/整合回复状态 用）
_stat = {
    "seen": 0,      # 见过的被@消息
    "opened": 0,    # 开过多少次窗口
    "flushed": 0,   # 整合发了几条
    "swallowed": 0, # 吞掉的逐条回复
    "bare_replied": 0,  # 空艾特偶尔回应过几次
}

# 空艾特限流状态
_bare_hist: dict[str, list[float]] = {}   # gid -> 回应的裸@时间戳（窗口内）
_bare_last: dict[str, float] = {}         # uid -> 上次回应的时间戳


def _bump(gid: str) -> None:
    """把 gid 的窗口 last 时间刷新，并由调用方安排定时器。"""
    w = _windows.get(gid)
    if w is not None and not w.flushed:
        w.last = time.time()


def _in_window(gid: str) -> bool:
    w = _windows.get(gid)
    return w is not None and not w.flushed


def _should_open(gid: str, now: float) -> bool:
    """风暴判定：瞬间 or 累积。"""
    q = _recent.get(gid)
    if not q:
        return False
    items = list(q)
    # 瞬间：BURST_N 秒内 ≥ BURST_K 条
    burst = sum(1 for rec in items if now - rec[3] <= BURST_N)
    if burst >= BURST_K:
        return True
    # 累积
    accum = sum(1 for rec in items if now - rec[3] <= ACCUM_N)
    if accum >= ACCUM_K:
        return True
    return False


async def _flush(gid: str, star_ctx) -> bool:
    """重扫并尝试回复；生成期间又有人喊则丢弃草稿，返回 False 继续等。"""
    w = _windows.get(gid)
    if w is None or w.flushed:
        return True
    snapshot_last = w.last
    # 清掉已计入这一波的历史；生成期间的新艾特会重新写入，并刷新 w.last。
    _recent.pop(gid, None)

    # 窗口内容只是“哪些消息明确在喊机器人”的标记；真正回复前重新读全群上下文。
    mention_rows = list(w.items)
    if not mention_rows:
        logger.info("[merge] gid=%s 窗口到期但没攒到问题，不发", gid)
        w.flushed = True
        return True
    context_rows = await asyncio.to_thread(read_recent_context, gid, CONTEXT_N, DB_PATH)
    rows = combine_context(context_rows, mention_rows, CONTEXT_N)

    if SHADOW:
        logger.info("[merge] 影子模式：本应在停歇后重扫 %d 条上下文（%d 条艾特），未发送",
                    len(rows), len(mention_rows))
        w.flushed = True
        _stat["flushed"] += 1
        return True

    prompt = build_context_prompt(rows, MAX_MSG)

    try:
        # provider 要按完整会话 origin 解析；只传 gid 在部分 AstrBot 版本会取不到。
        provider_key = w.origin or gid
        pid = await star_ctx.get_current_chat_provider_id(provider_key)
    except BaseException as e:
        logger.error("[merge] 取 provider 失败: %r", e)
        w.flushed = True
        return True
    if not pid:
        logger.error("[merge] gid=%s 没有可用 provider，放弃整合", gid)
        w.flushed = True
        return True

    try:
        # llm_generate 不会自动装载会话人格；显式取当前会话的有效人格提示词，
        # 否则“主模型”也会退化成通用助手口吻。
        system_prompt = None
        try:
            cid = await star_ctx.conversation_manager.get_curr_conversation_id(provider_key)
            conv = await star_ctx.conversation_manager.get_conversation(provider_key, cid) if cid else None
            settings = star_ctx.get_config(umo=provider_key).get("provider_settings", {})
            _persona_id, persona, _forced, _web_default = (
                await star_ctx.persona_manager.resolve_selected_persona(
                    umo=provider_key,
                    conversation_persona_id=getattr(conv, "persona_id", None),
                    platform_name="default",
                    provider_settings=settings,
                )
            )
            if persona is None:
                persona = await star_ctx.persona_manager.get_default_persona_v3(provider_key)
            system_prompt = getattr(persona, "prompt", None)
            if system_prompt is None and isinstance(persona, dict):
                system_prompt = persona.get("prompt")
        except BaseException as e:
            logger.warning("[merge] 读取会话人格失败，使用 provider 默认行为: %r", e)
        resp = await asyncio.wait_for(
            star_ctx.llm_generate(
                chat_provider_id=pid,
                prompt=prompt,
                system_prompt=system_prompt,
            ),
            timeout=TIMEOUT,
        )
    except BaseException as e:
        logger.error("[merge] 整合生成失败: %r", e)
        w.flushed = True
        return True
    # 生成期间如果又有人喊，当前草稿已经过时：不发，回到静默等待再重扫一次。
    if w.last > snapshot_last:
        logger.info("[merge] gid=%s 生成期间收到新艾特，丢弃旧草稿并继续等待", gid)
        return False
    raw = (getattr(resp, "completion_text", "") or "").strip()
    if not raw:
        logger.warning("[merge] 整合生成空文本，放弃")
        w.flushed = True
        return True

    # 组装 MessageChain：只发整合文本，不@任何人
    chain = MessageChain().message(raw)

    # 构造正确的 session（统一消息来源），否则框架 send_message 会报
    # "不合法的 session 字符串: not enough values to unpack (expected 3, got 1)"
    session = w.origin or (
        "aiocqhttp:%s:%s" % (MessageType.GROUP_MESSAGE.value, gid))
    try:
        await star_ctx.send_message(session, chain)
        w.flushed = True
        _stat["flushed"] += 1
        logger.info("[merge] gid=%s 停歇后重扫并发送：上下文%d条／其中艾特%d条",
                    gid, len(rows), len(mention_rows))
        return True
    except BaseException as e:
        logger.error("[merge] 发送失败: %r", e)
        w.flushed = True
        return True


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        super().__init__(context)
        self.context = context
        self._lock = asyncio.Lock()
        logger.info(
            "[merge] 已加载：%s 群=%s 瞬间%d条/%ds 累积%d条/%ds 停喊%.0fs 重扫%d条%s",
            "开" if ENABLED else "关",
            ",".join(sorted(GROUPS)) or "无",
            BURST_K, int(BURST_N), ACCUM_K, int(ACCUM_N),
            SILENCE, CONTEXT_N,
            "（影子）" if SHADOW else "",
        )

    # ---- 空艾特偶尔回应：有人只点了 @ 没写字 ----
    # 用事件类型过滤器，赶在内置 astrbot.main 把空文本那条（line 119）丢弃之前截住。
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def bare_at(self, event: AstrMessageEvent):
        if not ENABLED or not BARE_ON:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
            if not gid or not uid or uid == self_id:
                return
            if GROUPS and gid not in GROUPS:
                return
            if (event.get_extra("dsh_initiate") or event.get_extra("dsh_proactive")
                    or event.get_extra("dsh_poke_probe") or event.get_extra("dsh_poke_probe_feedback")):
                return
            # 带字的消息不归空艾特管（走正常/整合路径）
            if (event.message_str or "").strip():
                return
            # 这句必须是被唤醒（被 @）才接；别人回话被带入的不算
            if not bool(getattr(event, "is_at_or_wake_command", False)):
                return
            now = time.time()
            allowed, new_hist, reason = bare_allowed(
                now, gid, uid, _bare_hist.get(gid, []), _bare_last.get(uid, 0.0),
                BARE_UID_CD, BARE_GROUP_CAP, BARE_GROUP_WIN,
            )
            if not allowed:
                event.stop_event()  # 掐掉后续处理，但也不回
                return
            reply = bare_choose(random.random(), BARE_RATE)
            if reply is None:
                event.stop_event()  # 概率没中：同样不回
                return
            _bare_hist[gid] = new_hist + [now]
            _bare_last[uid] = now
            _stat["bare_replied"] += 1
            event.stop_event()
            try:
                chain = MessageChain().message(reply)
                await self.context.send_message(
                    getattr(event, "unified_msg_origin", gid), chain)
                logger.info("[merge] 空艾特偶尔回应 uid=%s: %s", uid, reply)
            except BaseException as e:
                logger.error("[merge] 空艾特回应发送失败: %r", e)
        except BaseException as e:
            logger.error("[merge] 空艾特处理异常: %s", e)

    # ---- 被动计数：每群记录被@/唤醒的消息 ----
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def watch(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            if not bool(getattr(event, "is_at_or_wake_command", False)):
                return  # 只有被@/唤醒才算「有人在喊我」
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            if not gid or not uid:
                return
            if uid == str(event.get_self_id() or ""):
                return
            if (event.get_extra("dsh_initiate") or event.get_extra("dsh_proactive")
                    or event.get_extra("dsh_poke_probe") or event.get_extra("dsh_poke_probe_feedback")):
                return
            if GROUPS and gid not in GROUPS:
                return
            q = _recent.get(gid)
            if q is None:
                q = _recent[gid] = __import__("collections").deque(maxlen=_RECENT_MAX)
            try:
                ts = float(event.message_obj.timestamp or time.time())
            except (TypeError, ValueError, AttributeError):
                ts = time.time()
            # 存 (uid, name, text, ts)：开窗时要回收「窗口内最近几条」的内容
            name = str(event.get_sender_name() or uid)
            text = (event.message_str or "").strip()
            q.append((uid, name, text, ts))
        except BaseException as e:
            logger.debug("[merge] 计数失败: %s", e)

    # ---- 吞掉风暴里的逐条回复，攒问题 ----
    # priority=2000：输入侧拦截组，同 armor/decide。吞消息时 stop_event
    # 令同批后续 handler 全跳过，不让 effect/emotion 在未攒齐时注入。
    @filter.on_llm_request(priority=2000)
    async def collect(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            gid = str(event.get_group_id() or "")
            if not gid:
                return
            if GROUPS and gid not in GROUPS:
                return
            targeted = bool(getattr(event, "is_at_or_wake_command", False))
            if (event.get_extra("dsh_initiate") or event.get_extra("dsh_proactive")
                    or event.get_extra("dsh_poke_probe") or event.get_extra("dsh_poke_probe_feedback")):
                return
            # 窗口外只处理被喊；窗口内则连普通触发的 LLM 回复也暂停，避免机器人
            # 一边说“先等喊完”一边又随机插话。普通消息仍会进记忆库，flush 时重扫。
            if not targeted and not _in_window(gid):
                return

            async with self._lock:
                now = time.time()
                opened_now = False

                if not _in_window(gid):
                    if _should_open(gid, now):
                        _windows[gid] = _Win(gid)
                        _windows[gid].origin = getattr(
                            event, "unified_msg_origin", "")
                        _stat["opened"] += 1
                        opened_now = True
                        logger.info("[merge] gid=%s 触发艾特风暴，开窗收集", gid)
                        # 开窗时回收阈值窗口中的艾特，把它们标成“明确喊过机器人”。
                        # 最终生成不直接拼这些条目，而会重新读取完整的最新群聊上下文。
                        for rec in list(_recent.get(gid, ())):
                            r_uid, r_name, r_text, r_ts = rec[0], rec[1], rec[2], rec[3]
                            if now - r_ts <= ACCUM_N:
                                _windows[gid].items.append(
                                    (r_uid, r_name, float(r_ts), (r_text or "")[:MAX_MSG]))
                        _stat["swallowed"] += len(_windows[gid].items)
                    else:
                        return  # 没风暴，正常逐条回

                w = _windows[gid]
                if w.flushed:
                    return
                # 吞掉本次逐条回复；普通随机插话只吞，不算成艾特，也不延长停喊计时。
                event.stop_event()
                if targeted:
                    uid = str(event.get_sender_id() or "")
                    name = str(event.get_sender_name() or uid)
                    text = (event.message_str or "").strip()
                    # 本条已在开窗回收里（若刚开窗）；后续窗口内的艾特到这里追加
                    if not any(it[0] == uid and it[3] == (text or "")[:MAX_MSG] for it in w.items):
                        w.items.append((uid, name, now, text[:MAX_MSG]))
                        _stat["swallowed"] += 1
                    w.last = now

                if w.task is None or w.task.done():
                    w.task = asyncio.ensure_future(
                        self._window_loop(gid, opened_now)
                    )
        except BaseException as e:
            logger.error("[merge] collect 异常: %s", e)

    async def _window_loop(self, gid: str, just_opened: bool) -> None:
        """每次新艾特都续命；只有持续安静 SILENCE 秒后才 flush。"""
        w = _windows.get(gid)
        if w is None:
            return
        warned = False
        try:
            while not w.flushed:
                while not w.flushed:
                    await asyncio.sleep(min(1.0, max(0.1, SILENCE / 4)))
                    now = time.time()
                    quiet_for = now - w.last
                    if quiet_for >= SILENCE:
                        break
                    if not warned and now - w.start >= MAX_WAIT:
                        warned = True
                        logger.info(
                            "[merge] gid=%s 艾特仍在继续，已等待%.0fs；保持静默直到停喊",
                            gid, now - w.start,
                        )
                if w.flushed or await _flush(gid, self.context):
                    break
        except BaseException as e:
            logger.error("[merge] 窗口循环异常: %s", e)
        finally:
            # 清理窗口
            if _windows.get(gid) is w:
                _windows.pop(gid, None)

    # ---- 状态指令 ----
    @filter.command("整合回复状态")
    async def status(self, event: AstrMessageEvent):
        if str(event.get_sender_id()) != OWNER:
            return
        gid = str(event.get_group_id() or "")
        now = time.time()
        q = _recent.get(gid)
        burst = accum = 0
        if q:
            burst = sum(1 for rec in q if now - rec[3] <= BURST_N)
            accum = sum(1 for rec in q if now - rec[3] <= ACCUM_N)
        w = _windows.get(gid)
        wstate = "收集中(%d条,已%.0fs)" % (len(w.items), now - w.start) if (
            w and not w.flushed) else "无"
        yield event.plain_result(
            "整合回复 %s\n"
            "最近%s秒内被@ %d/%d 条（瞬间阈值）\n"
            "最近%s秒内被@ %d/%d 条（累积阈值）\n"
            "当前窗口：%s\n"
            "策略：连续停喊%s秒后重扫最近%d条上下文；持续艾特时一直等\n"
            "累计：开窗%d 次／整合%d 条／吞掉逐条%d 次／空艾特回应%d 次"
            % (
                "开" if ENABLED else "关",
                int(BURST_N), burst, BURST_K,
                int(ACCUM_N), accum, ACCUM_K,
                wstate,
                int(SILENCE), CONTEXT_N,
                _stat["opened"], _stat["flushed"], _stat["swallowed"],
                _stat["bare_replied"],
            )
        )
