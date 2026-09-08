# -*- coding: utf-8 -*-
"""
dsh-merge —— 艾特风暴聚合回复（2026-09-06）。

问题：群里瞬间或短时间里好几个人轮番 @ 大肥鱼（或同一个人连环问），
机器人逐条回 → 刷屏。真人这时候的做法是等一波问完，**结合大家说过的话，
一条统一回复把这一圈接完（不@、不点名）**。

做法：每群一个状态机 idle -> collecting -> flush。
  · on_llm_request 钩子里，若该群正在收集窗口：
      event.stop_event() 吞掉本次逐条回复（不发），把这条被@消息攒进队列；
      调度窗口定时器（静默 6s 无新@ 就 flush；最多攒 45s 强制 flush）。
  · 不在窗口但触发了「风暴」阈值：进入 collecting 并吞掉本条。
  · flush：把攒到的 (提问者昵称, 消息文本) 列表一次 LLM 调用整合成一条
    回复，用 context.send_message 主动发出；不 @ 任何人、不点名。

阈值（env，DSH_MERGE_*）：
  · 瞬间：BURST_N 秒内 ≥ BURST_K 条被@ -> 开窗
  · 累积：ACCUM_N 秒内 ≥ ACCUM_K 条被@ -> 开窗
  两个都先到先触发。默认 20s/3 条、120s/5 条。

只统计「被 @/唤醒」的消息（event.is_at_or_wake_command），普通闲聊不算——
那是 dsh-decide 的随机插话，不该把机器人的主动回复吞掉。

发送注意：send_message 需要 session（unified_msg_origin）与 MessageChain。
整合回复只发一段纯文本（不拼接 At 段、不点名）——这是「主动发言」，
不是回复某条消息，所以不用框架的 @ 机制。

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
from .merge_logic import bare_allowed, bare_choose

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
# 静默多久 flush；总上限（攒太久用户会以为机器人死了）
SILENCE = float(os.environ.get("DSH_MERGE_SILENCE", "6"))
MAX_WAIT = float(os.environ.get("DSH_MERGE_MAX_WAIT", "45"))
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
        self.items: list[tuple[str, str, str]] = []  # (uid, name, text)
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


async def _flush(gid: str, star_ctx) -> None:
    """窗口到期：把攒的问题整合成一条，主动发出去。"""
    w = _windows.get(gid)
    if w is None or w.flushed:
        return
    w.flushed = True
    _stat["flushed"] += 1
    if w.task is not None:
        w.task = None
    # 清掉这一波的@历史，否则同一波消息会再触发一次开窗
    _recent.pop(gid, None)

    items = w.items
    if not items:
        logger.info("[merge] gid=%s 窗口到期但没攒到问题，不发", gid)
        return

    # 去重：同一 uid 短时间内重复 @（刷屏追问）只留第一条 + 记次数
    seen: dict[str, tuple[str, str, int]] = {}
    for uid, name, text in items:
        if uid in seen:
            nm, tx, cnt = seen[uid]
            seen[uid] = (nm, tx, cnt + 1)
        else:
            seen[uid] = (name, text, 1)

    if SHADOW:
        logger.info("[merge] 影子模式：本应整合 %d 人的 %d 条问题，未发送",
                    len(seen), len(items))
        return

    # 组装 prompt（隐藏名字：不给模型看到谁问的，回复自然就不点名）
    lines = []
    for uid, (name, text, cnt) in seen.items():
        extra = "（连问%d次）" % cnt if cnt > 1 else ""
        lines.append("· %s%s" % ((text or "")[:MAX_MSG], extra))
    prompt = (
        "群里好几个人几乎同时 @ 你，各说各的。别逐条回，也别@、别点名——"
        "先**总结上面这些人说的话**：把相关的点**连着一起回答**（同一个话题"
        "就拎成一条、别拆开重复说）；遇到跟上文不相关、连不到一起的，就"
        "自然地用「另外说」之类的话转一下，再接过去回答。最后整成**一条统一"
        "回复**，简短干脆，像真人把这圈话接完：\n%s\n"
        "要求：不要@任何人，不要出现任何群友的昵称或名字，不要写「@xx」「"
        "xx说」；别用「大家」「各位」这种群发感开头；以总结和串联为主，"
        "把这一整段对话融成一条通顺的话。"
    ) % "\n".join(lines)

    try:
        pid = await star_ctx.get_current_chat_provider_id(gid)
    except BaseException as e:
        logger.error("[merge] 取 provider 失败: %r", e)
        return
    if not pid:
        logger.error("[merge] gid=%s 没有可用 provider，放弃整合", gid)
        return

    try:
        resp = await asyncio.wait_for(
            star_ctx.llm_generate(
                chat_provider_id=pid,
                prompt=prompt,
                system_prompt="你是大肥鱼，一个混在 QQ 群里的人。",
            ),
            timeout=TIMEOUT,
        )
    except BaseException as e:
        logger.error("[merge] 整合生成失败: %r", e)
        return
    raw = (getattr(resp, "completion_text", "") or "").strip()
    if not raw:
        logger.warning("[merge] 整合生成空文本，放弃")
        return

    # 组装 MessageChain：只发整合文本，不@任何人
    chain = MessageChain().message(raw)

    # 构造正确的 session（统一消息来源），否则框架 send_message 会报
    # "不合法的 session 字符串: not enough values to unpack (expected 3, got 1)"
    session = w.origin or (
        "aiocqhttp:%s:%s" % (MessageType.GROUP_MESSAGE.value, gid))
    try:
        await star_ctx.send_message(session, chain)
        logger.info("[merge] gid=%s 已整合发送：%d 人 %d 条",
                    gid, len(seen), len(items))
    except BaseException as e:
        logger.error("[merge] 发送失败: %r", e)


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        super().__init__(context)
        self.context = context
        self._lock = asyncio.Lock()
        logger.info(
            "[merge] 已加载：%s 群=%s 瞬间%d条/%ds 累积%d条/%ds 静默%.0fs 上限%.0fs%s",
            "开" if ENABLED else "关",
            ",".join(sorted(GROUPS)) or "无",
            BURST_K, int(BURST_N), ACCUM_K, int(ACCUM_N),
            SILENCE, MAX_WAIT,
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
            if event.get_extra("dsh_initiate"):
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
            if event.get_extra("dsh_initiate"):
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
            if not bool(getattr(event, "is_at_or_wake_command", False)):
                return  # 普通闲聊不吞（那是 decide 的随机插话）

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
                        # 开窗这一刻，把窗口内最近几条（含本条）回收进队列——
                        # 它们刚触发风暴，其中部分可能已经各自回了，但真人
                        # 也是从这条开始合并；至少本条+后续不再逐条回。
                        # 窗口内最近消息全收（同人连问的每一条都算），去重留给 flush
                        for rec in list(_recent.get(gid, ())):
                            r_uid, r_name, r_text, r_ts = rec[0], rec[1], rec[2], rec[3]
                            if now - r_ts <= ACCUM_N:
                                _windows[gid].items.append(
                                    (r_uid, r_name, (r_text or "")[:MAX_MSG]))
                        _stat["swallowed"] += len(_windows[gid].items)
                    else:
                        return  # 没风暴，正常逐条回

                w = _windows[gid]
                if w.flushed:
                    return
                # 吞掉本次逐条回复
                event.stop_event()
                uid = str(event.get_sender_id() or "")
                name = str(event.get_sender_name() or uid)
                text = (event.message_str or "").strip()
                # 本条已在开窗回收里（若刚开窗）；后续窗口内的消息到这里追加
                if not any(it[0] == uid and it[2] == (text or "")[:MAX_MSG] for it in w.items):
                    w.items.append((uid, name, text[:MAX_MSG]))
                    _stat["swallowed"] += 1
                w.last = now

                if w.task is None or w.task.done():
                    w.task = asyncio.ensure_future(
                        self._window_loop(gid, opened_now)
                    )
        except BaseException as e:
            logger.error("[merge] collect 异常: %s", e)

    async def _window_loop(self, gid: str, just_opened: bool) -> None:
        """窗口定时器：静默 SILENCE 秒或总超 MAX_WAIT 秒后 flush。"""
        w = _windows.get(gid)
        if w is None:
            return
        try:
            while not w.flushed:
                await asyncio.sleep(1.0)
                now = time.time()
                # 刚开始的那条消息也给它一点收集时间
                min_wait = 0 if just_opened else 0
                if now - w.last >= SILENCE or now - w.start >= MAX_WAIT:
                    break
            await _flush(gid, self.context)
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
            "累计：开窗%d 次／整合%d 条／吞掉逐条%d 次／空艾特回应%d 次"
            % (
                "开" if ENABLED else "关",
                int(BURST_N), burst, BURST_K,
                int(ACCUM_N), accum, ACCUM_K,
                wstate,
                _stat["opened"], _stat["flushed"], _stat["swallowed"],
                _stat["bare_replied"],
            )
        )