# -*- coding: utf-8 -*-
"""dsh-initiate —— 群里冷下来之后，自己开个口。

和 dsh-decide 的关系（两把方向相反的尺子，别混）
------------------------------------------------
dsh-decide 判的是「**有人在说话**，我要不要插进去」——默认开口，只找否决信号。
本插件判的是「**没人在说话**，我要不要起个头」——默认闭嘴，必须有正向理由。
所以 dsh-decide 见到本插件造的事件会直接让路（那边打了 initiate-compat 补丁），
否则它会用「要不要插话」那把尺子把整次开口掐死。

为什么走合成事件（B 路线）而不是直接 llm_generate
------------------------------------------------
dsh-welcome 当初直接调 llm_generate，绕过了管道，结果 dsh-sticker 的
on_llm_response 钩子不触发，模型输出的 `[贴纸:探头]` 原样漏进群聊。
凡是绕过管道的地方，贴纸剥离、分段回复、@ 策略都得自己复制一份。
所以这里造一个 AstrBotMessage 丢进事件队列，走完整管道，那些东西全部照常生效。

代价是管道上的插件会把它当成一条真消息，逐站核对过，三处已打兼容补丁
（decide 让路、ctxclean 给第三种上下文、mention 不给哨兵号加 @）。

三个不显眼但会直接搞死功能的坑（都已实测确认）
--------------------------------------------
1) **上下文不能只读 buffer**。dsh-memory 的 buffer 是滚动抽取队列，抽完就删；
   archive 才是全量留存。实测同一个群 buffer 只剩 240 条、archive 有 1320 条。
   而本插件恰恰在**冷场很久**时才工作 —— 那正是 buffer 最可能被抽空的时候，
   只读 buffer 会让它永远停在「没有可用上下文」。所以两张表按 ts 归并去重。

2) **self_id 不能拿 platform.client_self_id**。那是 `uuid4().hex`
   （platform.py:45），不是 QQ 号；CQHttp 实例也没有 `self_id` 属性。
   而 aiocqhttp 的发送路径会把 raw payload 里的 self_id 当**多号路由键**
   （aiocqhttp_message_event.py:103）传给 NapCat，填错就是发送失败或串号。
   正确来源：反向 ws 客户端字典的 key，兜底 get_login_info。解析不到就不发。

3) **extra_user_content_parts 里只能放 ContentPart，不能放裸 str**。
   openai_source.py:1407 对未知类型直接
   `raise ValueError(f"不支持的额外内容块类型: {type(part)}")`，
   放 str 会让整次请求炸掉。必须包成 TextPart。

判据形状：模型只报事实，阈值和组合写在代码里
------------------------------------------
和 dsh-decide、dsh-guard 一致：模型只回布尔，代码决定。
这样改倾向就是改 should_initiate 那几行，不用重新调提示词，也好单测。

冷场时长会改变**判据本身**（不是只改阈值）：
- 15~45 分钟：想接的是**上文**，所以要求有没接的话头，且话题没收尾；
- 超过 45 分钟：旧话头早凉了，这时开口本质是**起新话题**，
  于是不再要求话头，也不再让「话题已收尾」否决 —— 收尾了正是该起新话题的理由。
  但「气氛沉重」和「像两个人私聊」两条否决在任何时长下都有效。
"""

import asyncio
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiocqhttp import Event
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.custom_filter import CustomFilter


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


ENABLED = _flag("DSH_INITIATE")
# 影子模式：自动路径只判定和记录，不发消息。默认开着，先看几天判定质量再放开。
SHADOW = _flag("DSH_INITIATE_SHADOW")
GROUPS = {
    g.strip()
    for g in os.environ.get("DSH_INITIATE_GROUPS", "100000001").split(",")
    if g.strip()
}
IDLE_MIN = max(60.0, float(os.environ.get("DSH_INITIATE_IDLE", "900")))
IDLE_MAX = max(IDLE_MIN, float(os.environ.get("DSH_INITIATE_IDLE_MAX", "21600")))
# 超过这个冷场时长，判据从「接上文」切换成「起新话题」（见 header）
COLD_AFTER = float(os.environ.get("DSH_INITIATE_COLD_AFTER", "2700"))
COLD_OPEN = _flag("DSH_INITIATE_COLD_OPEN")
COOLDOWN = max(60.0, float(os.environ.get("DSH_INITIATE_COOLDOWN", "3600")))
DAY_MAX = max(1, int(os.environ.get("DSH_INITIATE_DAY_MAX", "3")))
TICK = max(20.0, float(os.environ.get("DSH_INITIATE_TICK", "60")))
# 两次感知之间的最小间隔。tick 是 60 秒，但**不能每个 tick 都问模型**：
# 一段 15~360 分钟的冷场会问出几百次，既烧 token，又会放大下面那个问题。
PERCEIVE_EVERY = max(TICK, float(os.environ.get("DSH_INITIATE_PERCEIVE_EVERY", "300")))
# 需要连续几次判「开口」才真开口。
#
# 这条是实测抓出来的：同一段上下文、temperature=0，相邻两次感知给了**相反**结论
# （一次「有话头、没收尾、不是私聊」，下一次「已收尾、是私聊」）。
# 只要还在冷场，代码就会一直问；见到第一个「开口」就发 = 对噪声取最大值。
# 一段长冷场有几百次机会，哪怕每次只有 5% 说开口，累积下来也接近必然发生 ——
# 那样判定就是装饰品，真正决定频率的只有每日额度。
# 要求连续 N 次同向，噪声要连中 N 次才能过，代价只是多等一个感知间隔。
CONFIRM = max(1, int(os.environ.get("DSH_INITIATE_CONFIRM", "2")))
TIMEZONE = os.environ.get("DSH_INITIATE_TZ", "Asia/Shanghai")
HOURS = os.environ.get("DSH_INITIATE_HOURS", "10-23,0-2")
LOOKBACK = max(3, min(20, int(os.environ.get("DSH_INITIATE_LOOKBACK", "8"))))
TIMEOUT = max(5.0, float(os.environ.get("DSH_INITIATE_TIMEOUT", "25")))
OWNER = os.environ.get("DSH_INITIATE_OWNER", "2774000001").strip()
DB_PATH = os.environ.get("DSH_INITIATE_DB", "/AstrBot/data/dsh_memory.db")
STATE_PATH = Path(
    os.environ.get("DSH_INITIATE_STATE", "/AstrBot/data/dsh_initiate_state.json")
)
PLATFORM_ID = os.environ.get("DSH_INITIATE_PLATFORM", "default")
# 手动兜底：万一两条自动解析都失效，可以用 env 钉死机器人 QQ
SELF_ID_ENV = os.environ.get("DSH_INITIATE_SELF_ID", "").strip()
# 合成事件的发送者。**不能填机器人自己的 QQ**：
# platform_settings.ignore_bot_self_message=True，WakingCheckStage 见到
# sender_id == self_id 会直接 stop_event，事件根本走不到插件。
SENTINEL_UID = "0"
SENTINEL_NAME = "（系统·主动开口）"


def parse_hours(spec: str) -> set[int]:
    """"10-23,0-2" -> {10..23, 0,1,2}。跨零点靠模 24 递增，不用特判。"""
    out: set[int] = set()
    for part in spec.split(","):
        bits = part.strip().split("-", 1)
        try:
            start, end = int(bits[0]), int(bits[-1])
        except (ValueError, IndexError):
            continue
        if not (0 <= start <= 23 and 0 <= end <= 23):
            continue
        hour = start
        for _ in range(24):
            out.add(hour)
            if hour == end:
                break
            hour = (hour + 1) % 24
    return out


ACTIVE_HOURS = parse_hours(HOURS)


def _now_tz(now: float) -> datetime:
    try:
        return datetime.fromtimestamp(now, ZoneInfo(TIMEZONE))
    except Exception:
        # 容器里 tzdata 缺失时不要炸，退回本地时间（实测容器内 CST 可用）
        return datetime.fromtimestamp(now)


def in_active_hours(now: float | None = None) -> bool:
    return _now_tz(time.time() if now is None else now).hour in ACTIVE_HOURS


def parse_judgment(raw: str) -> dict | None:
    """抠出布尔字段。一个布尔都没有 => 模型没回答问题，返回 None（fail-closed）。

    不能把「解析不出」当成「全 False」：前者是没结果，后者是有结论。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        text = text[i : j + 1]
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        value = None
    if not isinstance(value, dict):
        return None
    bools = ("unfinished", "hook", "ended", "private_pair", "heavy")
    if not any(k in value for k in bools):
        return None
    out = {k: bool(value.get(k, False)) for k in bools}
    out["topic"] = str(value.get("topic") or "").strip()[:30]
    out["angle"] = str(value.get("angle") or "").strip()[:50]
    return out


def should_initiate(facts: dict, idle: float) -> tuple[bool, str]:
    """纯函数，无副作用。改开口倾向就改这里，好单测。

    冷场时长会切换判据形状，理由见文件头。
    """
    if facts.get("heavy"):
        return False, "在争吵或诉苦，插不进去"
    if facts.get("private_pair"):
        return False, "看着是两个人的事"
    if COLD_OPEN and idle >= COLD_AFTER:
        # 冷了这么久，旧话头已经没有「接」的意义了，这一路是起新话题。
        # 所以刻意**不看** ended/unfinished/hook —— 话题收尾了正是起新话题的理由。
        return True, "冷场%.0f分钟，起个新话题" % (idle / 60.0)
    if facts.get("ended"):
        return False, "上一轮已经收尾了"
    if facts.get("unfinished"):
        return True, "有话没说完"
    if facts.get("hook"):
        return True, "有能接的话头"
    return False, "没有可接的话头"


def _load_state() -> dict:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(value: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_PATH)  # 原子替换，别让断电留下半个文件
    except OSError as exc:
        logger.warning("[initiate] 保存状态失败: %s", exc)


def read_context(gid: str, limit: int = LOOKBACK, db: str = DB_PATH) -> list[tuple]:
    """buffer ∪ archive 按 ts 归并去重，返回按时间正序的 (uid, name, ts, text)。

    必须两张表都读：buffer 是 dsh-memory 的滚动抽取队列（抽完即删），
    archive 才是全量。本插件专在冷场时工作，那正是 buffer 最可能空的时候。
    """
    rows: list[tuple] = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error as exc:
        logger.debug("[initiate] 打不开记忆库: %s", exc)
        return rows
    try:
        for table in ("buffer", "archive"):
            try:
                rows.extend(
                    con.execute(
                        f"SELECT user_id, name, ts, text FROM {table} "  # noqa: S608
                        "WHERE group_id=? ORDER BY ts DESC LIMIT ?",
                        (gid, limit * 2),
                    ).fetchall()
                )
            except sqlite3.Error as exc:
                logger.debug("[initiate] 读 %s 失败: %s", table, exc)
    finally:
        con.close()
    seen: set[tuple] = set()
    merged: list[tuple] = []
    for uid, name, ts, text in rows:
        text = str(text or "")
        key = (round(float(ts), 3), text)
        if key in seen:
            continue
        seen.add(key)
        merged.append((str(uid), str(name or uid), float(ts), text))
    merged.sort(key=lambda r: r[2])
    return merged[-limit:]


def render(rows: list[tuple]) -> str:
    return "\n".join(f"{r[1]}：{r[3][:80]}" for r in rows if r[3].strip())


SYS = "你是群聊事实抽取器。只报事实，不做决定，不写回复。"

PROMPT = """群里已经安静了 {mins:.0f} 分钟。下面是安静之前最后几条真实消息：

{transcript}

只输出 JSON，不要解释：
{{"unfinished":false,"hook":false,"ended":false,"private_pair":false,"heavy":false,"topic":"","angle":""}}

unfinished：有没有谁的问题没人答、或有事说了一半没下文
hook：有没有一个自然能接上的话头（新东西、可追问的点、还没聊透的事）
ended：是不是已经互相道别、或明确收尾了
private_pair：是不是明显只是两个人之间的事，第三个人插进去很怪
heavy：是不是在吵架、诉苦，或者严肃敏感的话题
topic：他们最后在聊什么，10 字以内
angle：如果要开口，可以从哪切入，20 字以内"""


class InitiateFilter(CustomFilter):
    """只放行本插件自己造的事件。靠 extra 标记认，不靠猜消息形状。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        return bool(event.get_extra("dsh_initiate"))


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._task: asyncio.Task | None = None
        self._busy: set[str] = set()
        self._state = _load_state()
        self._quiet: dict[str, str] = {}  # 日志去重，避免每分钟刷同一行
        self._last_perceive: dict[str, float] = {}  # 上次问模型的时间
        self._streak: dict[str, int] = {}  # 连续判「开口」的次数
        self._self_id: str = SELF_ID_ENV
        logger.info(
            "[initiate] 已加载：%s 影子=%s 冷场%.0f~%.0f分钟 冷启动>%.0f分钟=%s "
            "每日%d次 冷却%.0f分钟 每%.0f分钟看一次 连续%d次才开口 时段=%s(%s) 群=%s",
            "开" if ENABLED else "关", "开" if SHADOW else "关",
            IDLE_MIN / 60, IDLE_MAX / 60, COLD_AFTER / 60, "开" if COLD_OPEN else "关",
            DAY_MAX, COOLDOWN / 60, PERCEIVE_EVERY / 60, CONFIRM,
            HOURS, TIMEZONE, ",".join(sorted(GROUPS)) or "无",
        )

    @filter.on_astrbot_loaded()
    async def start(self) -> None:
        if not ENABLED or not GROUPS:
            return
        self._task = asyncio.create_task(self._loop())

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(TICK)
                for gid in sorted(GROUPS):
                    try:
                        await self._consider(gid)
                    except BaseException as exc:  # 单群出错不能带崩整个循环
                        logger.error("[initiate] gid=%s tick 异常: %r", gid, exc)
        except asyncio.CancelledError:
            return
        except BaseException:
            logger.exception("[initiate] 后台循环退出")

    # ---------------------------------------------------------------- 闸门
    def _day(self, gid: str, now: float) -> dict:
        """按 Asia/Shanghai 的日期分桶。用 UTC 分桶会在早上 8 点换日，不合直觉。"""
        day = _now_tz(now).date().isoformat()
        entry = self._state.get(gid)
        if not isinstance(entry, dict) or entry.get("day") != day:
            entry = {"day": day, "count": 0, "last": 0.0}
            self._state[gid] = entry
        return entry

    def _skip(self, gid: str, reason: str, key: str | None = None) -> bool:
        """不开口也要打日志（imagegen 那三次坑的教训），但同一类原因只打一次。

        去重必须按**原因类别**去重，不能拿整句话去重：句子里带着
        「才安静 11 分钟」这种每分钟都在变的数字，拿整句当键等于没去重 ——
        实测就是安静期间每分钟刷一行。key=None 表示这条不去重
        （异常类原因，每次都该看见）。
        """
        if key is None:
            self._quiet.pop(gid, None)
        elif self._quiet.get(gid) == key:
            return False
        else:
            self._quiet[gid] = key
        logger.info("[initiate] gid=%s 不开口：%s", gid, reason)
        return False

    async def _consider(self, gid: str, force: bool = False) -> bool:
        if gid in self._busy:
            return False
        now = time.time()
        rows = read_context(gid)
        if not rows:
            return self._skip(gid, "读不到群里的上下文", "nocontext")
        last_uid, _last_name, last_ts, _last_text = rows[-1]
        idle = now - last_ts
        if last_uid == SENTINEL_UID:
            return self._skip(gid, "上一条就是我自己起的头", "selflast")
        state = self._day(gid, now)
        if not force:
            if not in_active_hours(now):
                return self._skip(gid, "不在活跃时段（%s时）" % _now_tz(now).hour, "hours")
            if idle < IDLE_MIN:
                return self._skip(gid, "才安静 %.0f 分钟，不够" % (idle / 60), "tooearly")
            if idle > IDLE_MAX:
                return self._skip(gid, "已经凉了 %.1f 小时，不追了" % (idle / 3600), "toolate")
            if state.get("count", 0) >= DAY_MAX:
                return self._skip(gid, "今天已经开口 %d 次" % state["count"], "quota")
            if now - float(state.get("last", 0)) < COOLDOWN:
                left = (COOLDOWN - (now - float(state["last"]))) / 60
                return self._skip(gid, "冷却中，还剩 %.0f 分钟" % left, "cooldown")
            # 闸门全过了才考虑问模型，但别每个 tick 都问：
            # 一段几小时的冷场按 60 秒 tick 会问几百次（见 PERCEIVE_EVERY 注释）
            since = now - self._last_perceive.get(gid, 0.0)
            if since < PERCEIVE_EVERY:
                return self._skip(
                    gid, "等下一次感知（还有 %.0f 秒）" % (PERCEIVE_EVERY - since), "throttle"
                )
        self._busy.add(gid)
        try:
            if not force:
                self._last_perceive[gid] = now
            facts = await self._perceive(gid, idle, render(rows))
            if facts is None:
                # 感知失败不去重日志：这是异常，每次都该看见
                return self._skip(gid, "感知没结果，保持沉默")
            ok, why = should_initiate(facts, idle)
            streak = self._streak.get(gid, 0) + 1 if ok else 0
            self._streak[gid] = streak
            logger.info(
                "[initiate] gid=%s 安静%.0f分钟 判定=%s(%s) 连续%d/%d 话题=%s 切入=%s 事实=%s",
                gid, idle / 60, "开口" if ok else "闭嘴", why, streak, CONFIRM,
                facts["topic"] or "-", facts["angle"] or "-",
                {k: facts[k] for k in ("unfinished", "hook", "ended", "private_pair", "heavy")},
            )
            if not ok:
                return False
            # 同一段上下文实测会给出相反结论，所以要连续同向才算数（见 CONFIRM 注释）。
            # force 是人工触发，本来就只跑一次，不套这条。
            if not force and streak < CONFIRM:
                logger.info("[initiate] gid=%s 先记着，等下一次确认", gid)
                return False
            if SHADOW and not force:
                logger.info("[initiate] gid=%s 影子模式，只记不发", gid)
                self._streak[gid] = 0
                return False
            self._streak[gid] = 0
            return await self._fire(gid, now, idle, facts, state)
        finally:
            self._busy.discard(gid)

    # ---------------------------------------------------------------- 感知
    async def _perceive(self, gid: str, idle: float, transcript: str) -> dict | None:
        if not transcript.strip():
            return None
        umo = f"{PLATFORM_ID}:GroupMessage:{gid}"
        try:
            # get_current_chat_provider_id 是协程，必须 await。
            # 不 await 会把 coroutine 一路传下去，报「Provider <coroutine ...> not found」
            provider = await self.context.get_current_chat_provider_id(umo)
            if not provider:
                return None
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider,
                    prompt=PROMPT.format(mins=idle / 60.0, transcript=transcript[-1800:]),
                    system_prompt=SYS,
                    temperature=0,  # 事实抽取，不要随机性
                ),
                timeout=TIMEOUT,
            )
        except BaseException as exc:
            logger.info("[initiate] gid=%s 感知异常 %s: %r", gid, type(exc).__name__, exc)
            return None
        raw = (getattr(resp, "completion_text", "") or "").strip()
        if not raw:
            # 有的模型把正文全放 reasoning_content
            raw = (getattr(resp, "reasoning_content", "") or "").strip()
        facts = parse_judgment(raw)
        if facts is None:
            logger.info("[initiate] gid=%s 解析不出感知结果，原文=%r", gid, raw[:200])
        return facts

    # ---------------------------------------------------------------- 身份
    async def _resolve_self_id(self, platform) -> str:
        """拿机器人真实 QQ。见文件头第 2 条：填错会污染 NapCat 的多号路由。"""
        if self._self_id.isdigit():
            return self._self_id
        bot = getattr(platform, "bot", None)
        # ① 反向 ws 客户端字典的 key 就是各个号的 self_id，纯本地、零成本
        for attr in ("_wsr_api_clients", "_wsr_event_clients"):
            book = getattr(bot, attr, None)
            if isinstance(book, dict):
                for key in book:
                    if str(key).isdigit():
                        self._self_id = str(key)
                        logger.info("[initiate] self_id=%s（来自 %s）", self._self_id, attr)
                        return self._self_id
        # ② 兜底问一次 NapCat，成功后缓存，不会每次都问
        try:
            info = await asyncio.wait_for(bot.call_action("get_login_info"), timeout=8)
            uid = str((info or {}).get("user_id") or "")
            if uid.isdigit():
                self._self_id = uid
                logger.info("[initiate] self_id=%s（来自 get_login_info）", uid)
                return uid
        except BaseException as exc:
            logger.warning("[initiate] 取 self_id 失败: %r", exc)
        return ""

    # ---------------------------------------------------------------- 开口
    async def _fire(self, gid: str, now: float, idle: float, facts: dict, state: dict) -> bool:
        platform = self.context.get_platform_inst(PLATFORM_ID)
        if platform is None or not hasattr(platform, "create_event"):
            logger.error("[initiate] 找不到平台实例 %s", PLATFORM_ID)
            return False
        self_id = await self._resolve_self_id(platform)
        if not self_id:
            # 宁可不发：错的 self_id 会让 NapCat 路由到别的号或直接失败
            logger.error("[initiate] 解析不到机器人 QQ，放弃这次开口")
            return False
        # raw_message 必须是 aiocqhttp.Event：发送路径要从它里面取 self_id 作路由键。
        # Event.from_payload 会保留自定义键，所以标记也塞进去一份，方便别的插件认。
        raw = Event.from_payload({
            "time": int(now),
            "self_id": int(self_id),
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": uuid.uuid4().int % (2**31),
            "group_id": int(gid) if gid.isdigit() else gid,
            "user_id": int(SENTINEL_UID),
            "message": [],
            "raw_message": "",
            "font": 0,
            "sender": {"user_id": int(SENTINEL_UID), "nickname": SENTINEL_NAME, "role": "member"},
            "dsh_initiate": True,
        })
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE
        msg.self_id = self_id
        msg.session_id = gid
        msg.message_id = str(raw["message_id"])
        msg.group = Group(group_id=gid)
        msg.sender = MessageMember(user_id=SENTINEL_UID, nickname=SENTINEL_NAME)
        msg.message = []      # 空消息链：dsh-memory / dsh-guard 见到空文本直接返回，不污染
        msg.message_str = ""
        msg.timestamp = int(now)
        msg.raw_message = raw
        event = platform.create_event(msg)
        event.set_extra("dsh_initiate", True)
        event.set_extra("dsh_initiate_idle", idle)
        event.set_extra("dsh_initiate_facts", facts)
        # 配额先落盘再提交：提交后是异步管道，崩了也不能让配额白漏
        state["count"] = int(state.get("count", 0)) + 1
        state["last"] = now
        _save_state(self._state)
        platform.commit_event(event)
        logger.info(
            "[initiate] gid=%s 已开口（今天第%d次）安静%.0f分钟 话题=%s",
            gid, state["count"], idle / 60, facts.get("topic") or "-",
        )
        return True

    @filter.custom_filter(InitiateFilter)
    async def speak(self, event: AstrMessageEvent):
        """合成事件走到这里：给出一个非空 prompt，让 ProcessStage 跑完整 agent。

        prompt 不能为空 —— astr_main_agent 对空请求直接返回 None，什么都不会发。
        """
        gid = str(event.get_group_id() or "")
        facts = event.get_extra("dsh_initiate_facts") or {}
        idle = float(event.get_extra("dsh_initiate_idle") or 0)
        conv = None
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if cid:
                conv = await self.context.conversation_manager.get_conversation(
                    event.unified_msg_origin, cid
                )
        except BaseException as exc:
            logger.warning("[initiate] gid=%s 取会话失败: %r", gid, exc)
        if conv is None:
            # 没有会话就没有历史，这时候开口等于凭空说话，不如不说
            logger.error("[initiate] gid=%s 没有现成会话，放弃（需要 /new 建会话）", gid)
            return
        req = event.request_llm(
            prompt="（群里安静下来了，你自己起个头，说一句就好）",
            session_id=event.session_id,
            conversation=conv,
        )
        # 必须是 TextPart，放裸 str 会让 openai_source 抛 ValueError（见文件头第 3 条）。
        # 小写标签开头 => 以后 dsh-ctxclean 会按结构把它从历史里清掉，不会累积。
        req.extra_user_content_parts.append(TextPart(text=(
            "<initiate_context>群里已经安静了 %.0f 分钟，没有人在跟你说话，"
            "是你自己决定开口的。安静前他们在聊：%s。可以从这里切入：%s。"
            "只说一句短话；不要问「大家在吗」「有人吗」，也不要说你在看群。"
            "</initiate_context>"
        ) % (idle / 60.0, facts.get("topic") or "没什么明确的", facts.get("angle") or "随你")))
        yield req

    # ---------------------------------------------------------------- 指令
    @filter.command("主动开口状态")
    async def status(self, event: AstrMessageEvent):
        if str(event.get_sender_id()) != OWNER:
            return
        gid = str(event.get_group_id() or "")
        now = time.time()
        state = self._day(gid, now)
        rows = read_context(gid)
        idle = (now - rows[-1][2]) / 60 if rows else -1
        cool = max(0.0, COOLDOWN - (now - float(state.get("last", 0)))) / 60
        yield event.plain_result(
            "主动开口 %s／影子 %s\n"
            "今天 %d/%d 次，冷却还剩 %.0f 分钟\n"
            "现在安静 %.0f 分钟（要 %.0f~%.0f 分钟才动，超过 %.0f 分钟改起新话题）\n"
            "活跃时段 %s（现在 %s时，%s）\n"
            "每 %.0f 分钟看一次，连续 %d 次说该开口才真开口（现在 %d 次）\n"
            "上下文 %d 条"
            % (
                "开" if ENABLED else "关", "开" if SHADOW else "关",
                state.get("count", 0), DAY_MAX, cool,
                idle, IDLE_MIN / 60, IDLE_MAX / 60, COLD_AFTER / 60,
                HOURS, _now_tz(now).hour, "在" if in_active_hours(now) else "不在",
                PERCEIVE_EVERY / 60, CONFIRM, self._streak.get(gid, 0),
                len(rows),
            )
        )

    @filter.command("主动开口测试")
    async def test(self, event: AstrMessageEvent):
        """人工触发：绕过时段/冷场/配额，但**判据照走**，影子模式下也会真发。"""
        if str(event.get_sender_id()) != OWNER:
            return
        gid = str(event.get_group_id() or "")
        if gid not in GROUPS:
            yield event.plain_result("这个群不在主动开口名单里")
            return
        ok = await self._consider(gid, force=True)
        yield event.plain_result(
            "判定开口，稍后会说话" if ok else "判定这会儿不该开口（原因看日志）"
        )
