# dsh-poke —— 别人戳它，它戳回去。
#
# ---------------------------------------------------------------------------
# 一、poke 事件在管道里长什么样（探针实测，不是推断）
#
# OneBot v11 群内戳一戳是 notice 事件：
#   {"post_type":"notice","notice_type":"notify","sub_type":"poke",
#    "group_id":..., "user_id":<戳人的>, "target_id":<被戳的>}
#
# aiocqhttp 适配器把它转成 message_str="" 的 AstrBotMessage
# （aiocqhttp_platform_adapter.py:168-196），并且因为带 group_id 而被标成
# GROUP_MESSAGE，照样进完整管道。message 列表里会有一个 Poke 组件，
# 它的 target_id() 返回被戳的人（components.py:629-637）。
#
# 装了个临时只读探针实测确认：
#   有bot属性=True  bot类型=CQHttp  有call_action=True
#   raw_target_id=3999999997  组件=['Poke']  target_id()=['3999999997']
# 并且**整条管道一个 action 都没发出** —— 也就是说现在被戳完全没反应，
# 不是被谁拦了，是空链走到 RespondStage 自然什么都不发。
#
# 二、为什么回话不问 LLM
#
# llm_generate 不经过消息管道，dsh-sticker 的剥离钩子不会触发，模型输出的
# [贴纸:x] 会原样漏进群聊 —— dsh-welcome 已经踩过一次这个坑（实测漏出
# [贴纸:探头]）。为一句「干嘛」去花 9400 input tokens、还要自己复制一遍
# 剥离逻辑，不值得。预置短句写成小鲸鱼的口吻就够了。
#
# 三、防对戳循环（两道按群隔离的闸门）
#
# 最大的风险是无限对戳：机器人回戳 → 对方回戳 → 机器人再回戳。
# QQ 对高频动作有风控，而这个账号**已经被打标**（换大陆 IP 实测无效），
# 不能再喂它理由。所以：
#   * 同人冷却 COOLDOWN 秒；群主用 OWNER_COOLDOWN，避免正常连点像没反应
#   * 同群 60s 内最多 GROUP_MAX 次：一群人一起戳时不刷屏
# 配额严格按 gid 分桶，新群活动不会压掉主群回戳。
#
# 四、group_poke 这个 action 实测可用
#   docker exec napcat curl -X POST .../group_poke -d '{"group_id":G,"user_id":U}'
#   -> {"status":"ok","retcode":0}
# 注意它不是 OneBot v11 标准动作，是 napcat 扩展；调用失败一律吞掉不影响群聊。

import asyncio
import json
import os
import random
import re
import sqlite3
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiocqhttp import Event
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.custom_filter import CustomFilter
try:
    from .poke_logic import (
        build_probe_feedback_prompt,
        classify_probe_feedback,
        decide_probe,
    )
except ImportError:  # AstrBot 的连字符插件目录不一定建立包名，兼容直接模块加载
    from poke_logic import (
        build_probe_feedback_prompt,
        classify_probe_feedback,
        decide_probe,
    )

ENABLED = os.environ.get("DSH_POKE", "1") != "0"
# 回戳概率。1.0 = 每次都回（被戳还不理更奇怪）
BACK_RATE = float(os.environ.get("DSH_POKE_BACK_RATE", "1.0"))
# 附一句短话的概率。默认 25%：每次都说话很吵，一句不说又太机械
TALK_RATE = float(os.environ.get("DSH_POKE_TALK_RATE", "0.25"))
# 同一个人多久内只回一次；群主可单独设更短冷却，避免正常连点被误判没反应
COOLDOWN = float(os.environ.get("DSH_POKE_COOLDOWN", "20"))
OWNER = os.environ.get("DSH_POKE_OWNER", "").strip()
OWNER_COOLDOWN = float(os.environ.get("DSH_POKE_OWNER_COOLDOWN", "3"))
# 限流窗口（秒）与窗口内上限。配额只按群分桶，避免新群戳一戳占掉主群额度。
WINDOW = float(os.environ.get("DSH_POKE_WINDOW", "60"))
GROUP_MAX = max(1, int(os.environ.get("DSH_POKE_GROUP_MAX", "6")))
# 回戳前的随机延迟：真人不会 0ms 反射
DELAY_MIN = float(os.environ.get("DSH_POKE_DELAY_MIN", "0.6"))
DELAY_MAX = float(os.environ.get("DSH_POKE_DELAY_MAX", "2.2"))
# 只在这些群生效，空 = 全部
GROUPS = {
    g.strip() for g in os.environ.get("DSH_POKE_GROUPS", "").split(",") if g.strip()
}

# 主动戳是“先轻轻敲门，再看对方反应”，不是另一种刷屏。安全试运行默认仅允许
# 群主显式要求，机器人虽然知道自己能做，但普通群友不能借此让它骚扰第三人。
PROBE_ON = os.environ.get("DSH_POKE_PROBE", "1") != "0"
PROBE_OWNER_ONLY = os.environ.get("DSH_POKE_PROBE_OWNER_ONLY", "1") != "0"
PROBE_COOLDOWN = max(60.0, float(os.environ.get("DSH_POKE_PROBE_COOLDOWN", "1800")))
PROBE_WINDOW = max(60.0, float(os.environ.get("DSH_POKE_PROBE_WINDOW", "3600")))
PROBE_GROUP_MAX = max(1, int(os.environ.get("DSH_POKE_PROBE_GROUP_MAX", "2")))
PROBE_WAIT = max(30.0, float(os.environ.get("DSH_POKE_PROBE_WAIT", "180")))
PROBE_TZ = os.environ.get("DSH_POKE_PROBE_TZ", "Asia/Shanghai")
PROBE_HOURS = os.environ.get("DSH_POKE_PROBE_HOURS", "10-23,0-2")
PROBE_STATE = Path(os.environ.get("DSH_POKE_PROBE_STATE", "/AstrBot/data/dsh_poke_probe_state.json"))
SOCIAL_DB = os.environ.get("DSH_SOCIAL_DB", "/AstrBot/data/dsh_social.db")
PROBE_PLATFORM = os.environ.get("DSH_POKE_PLATFORM", "default")
PROBE_SELF_ID = os.environ.get("DSH_POKE_SELF_ID", "").strip()
PROBE_SENTINEL_UID = "0"
PROBE_SENTINEL_NAME = "（系统·戳一戳反馈）"
_TARGET_RE = re.compile(r"\d{5,20}")

# 预置短句。写成小鲸鱼的口吻：傲娇、短、不客服腔。
# 分两组：一般情况 / 被同一个人反复戳（有点烦了）
TALK = [
    "干嘛",
    "别戳",
    "？",
    "戳回去了",
    "手别抖",
    "再戳咬你",
    "有事说事",
    "戳我干啥",
]
TALK_ANNOYED = [
    "还戳？",
    "戳够了没",
    "手是不是闲",
    "行吧你厉害",
    "再戳我就装死",
]

_last_poke: dict[str, float] = {}          # "gid:uid" -> 上次回戳时间
_repeat: dict[str, int] = {}               # "gid:uid" -> 连续戳次数
_group_hits: dict[str, deque] = {}         # gid -> 时间戳窗口；严格按群隔离

_stat = {
    "seen": 0, "not_me": 0, "cooldown": 0, "group_limit": 0,
    "dice": 0, "poked": 0, "talked": 0, "fail": 0, "self_poke": 0,
    "probe_requested": 0, "probe_sent": 0, "probe_denied": 0,
    "probe_poke_back": 0, "probe_message": 0, "probe_followup": 0,
}
_last: list[str] = []


def _load_probe_state() -> dict:
    try:
        value = json.loads(PROBE_STATE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_probe_state(value: dict) -> None:
    try:
        PROBE_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROBE_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(PROBE_STATE)
    except OSError as exc:
        logger.warning("[poke] 保存主动试探状态失败: %s", exc)


def _probe_key(gid: str, uid: str) -> str:
    return "%s:%s" % (gid, uid)


def _parse_hours(spec: str) -> set[int]:
    hours: set[int] = set()
    for part in str(spec or "").split(","):
        bits = part.strip().split("-", 1)
        try:
            start, end = int(bits[0]), int(bits[-1])
        except (ValueError, IndexError):
            continue
        if not (0 <= start <= 23 and 0 <= end <= 23):
            continue
        hour = start
        for _ in range(24):
            hours.add(hour)
            if hour == end:
                break
            hour = (hour + 1) % 24
    return hours


PROBE_ACTIVE_HOURS = _parse_hours(PROBE_HOURS)


def _in_probe_hours(now: float) -> bool:
    try:
        hour = datetime.fromtimestamp(now, ZoneInfo(PROBE_TZ)).hour
    except Exception:
        hour = datetime.fromtimestamp(now).hour
    return hour in PROBE_ACTIVE_HOURS


def _social_allows_probe(gid: str, uid: str, now: float) -> bool:
    """尊重 dsh-social 的少打扰边界；数据库缺失/锁定时不阻断显式群主动作。"""
    if not gid or not uid:
        return True
    try:
        con = sqlite3.connect("file:%s?mode=ro" % SOCIAL_DB, uri=True, timeout=0.5)
        try:
            row = con.execute(
                "SELECT avoid_until FROM relations WHERE group_id=? AND user_id=?", (gid, uid)
            ).fetchone()
        finally:
            con.close()
        return not (row and float(row[0] or 0) > now)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return True


class ProbeFeedbackFilter(CustomFilter):
    """只放行主动戳得到反馈后构造的续聊事件。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        return bool(event.get_extra("dsh_poke_probe"))


def _rg(raw, key, default=None):
    """notice 事件的 raw_message 可能是 dict 也可能是 aiocqhttp.Event。

    dsh-welcome 里同一个坑：Event 支持 [] 但不是 dict，getattr 也可能有值，
    所以三种取法都试一遍。
    """
    if isinstance(raw, dict):
        return raw.get(key, default)
    try:
        return raw[key]
    except BaseException:
        pass
    return getattr(raw, key, default)


class PokeFilter(CustomFilter):
    """只放行 OneBot 戳一戳通知。

    照抄 dsh-welcome 的 GroupIncreaseFilter：CustomFilter 注册进
    EventType.AdapterMessageEvent，插件 handler 被激活时 WakingCheckStage
    会置 is_wake=True，所以 message_str 为空也能走到 handler。
    """

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        raw = getattr(event.message_obj, "raw_message", None)
        return (
            _rg(raw, "post_type") == "notice"
            and _rg(raw, "sub_type") == "poke"
        )


def _prune(dq: deque, now: float) -> None:
    while dq and now - dq[0] > WINDOW:
        dq.popleft()


def _quota(gid: str) -> tuple[bool, str]:
    """单群限流。不同群互不占额度，返回 (是否允许, 原因)。"""
    now = time.time()
    g = _group_hits.setdefault(gid, deque())
    _prune(g, now)
    if len(g) >= GROUP_MAX:
        return False, "同群 %.0fs 内已回 %d 次" % (WINDOW, len(g))
    return True, ""


def _note_hit(gid: str) -> None:
    _group_hits.setdefault(gid, deque()).append(time.time())


def _target_of(event: AstrMessageEvent) -> str | None:
    """谁被戳了。优先读 Poke 组件，回落到 raw 的 target_id。"""
    for c in getattr(event.message_obj, "message", []) or []:
        fn = getattr(c, "target_id", None)
        if callable(fn):
            t = fn()
            if t:
                return str(t)
    raw = getattr(event.message_obj, "raw_message", None)
    t = _rg(raw, "target_id")
    return str(t) if t else None


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._probe_state = _load_probe_state()
        self._probe_following: set[str] = set()
        self._self_id = PROBE_SELF_ID
        logger.info(
            "[poke] 已加载：%s 回戳率%.0f%% 说话率%.0f%% 同人冷却%.0fs "
            "群主冷却%.0fs 限流%.0fs内每群%d 延迟%.1f~%.1fs；主动试探=%s/%s "
            "冷却%.0f分钟 等反馈%.0fs 限定群=%s",
            "开" if ENABLED else "关", BACK_RATE * 100, TALK_RATE * 100,
            COOLDOWN, OWNER_COOLDOWN, WINDOW, GROUP_MAX, DELAY_MIN, DELAY_MAX,
            "开" if PROBE_ON else "关", "仅群主" if PROBE_OWNER_ONLY else "全员可触发",
            PROBE_COOLDOWN / 60, PROBE_WAIT,
            "、".join(sorted(GROUPS)) if GROUPS else "全部",
        )

    @filter.custom_filter(PokeFilter)
    async def on_poke(self, event: AstrMessageEvent):
        try:
            if not ENABLED:
                return
            gid = str(event.get_group_id() or "")
            me = str(event.get_self_id() or "")
            who = str(event.get_sender_id() or "")
            target = _target_of(event)
            _stat["seen"] += 1

            if GROUPS and gid and gid not in GROUPS:
                return
            # 戳的不是我 —— 群友互戳，不关我事
            if not target or target != me:
                _stat["not_me"] += 1
                logger.info("[poke] 戳的不是我(target=%s me=%s)，不管", target, me)
                return
            # 自己戳自己（理论上不该出现，但真出现会变成死循环）
            if who == me:
                _stat["self_poke"] += 1
                logger.info("[poke] 我戳我自己？忽略")
                return

            # 主动戳之后对方回戳：这不是普通“机械回戳”，而是明确的试探反馈。
            # 消耗 pending，走一次完整聊天管道，自然接一句；不再 group_poke 回去，
            # 避免演化成无限乒乓。
            probe_key = _probe_key(gid, who)
            probe = self._probe_state.get("pending", {}).get(probe_key, {})
            feedback = classify_probe_feedback(
                now=time.time(),
                pending_until=float(probe.get("until", 0) or 0),
                feedback_kind="poke_back",
            )
            if feedback == "poke_back":
                self._consume_probe(probe_key)
                _stat["probe_poke_back"] += 1
                logger.info("[poke] 主动试探得到回戳反馈 gid=%s uid=%s，准备自然续聊", gid, who)
                await self._fire_probe_followup(gid, who, "poke_back")
                return

            key = "%s:%s" % (gid, who)
            now = time.time()
            gap = now - _last_poke.get(key, 0.0)
            cooldown = OWNER_COOLDOWN if OWNER and who == OWNER else COOLDOWN
            if gap < cooldown:
                _stat["cooldown"] += 1
                _repeat[key] = _repeat.get(key, 0) + 1
                logger.info("[poke] %s 在 %.0fs 内又戳（第%d下），不回",
                            who, gap, _repeat[key] + 1)
                return

            allow, why = _quota(gid)
            if not allow:
                _stat["group_limit"] += 1
                logger.info("[poke] 限流：%s，不回", why)
                return

            if random.random() > BACK_RATE:
                _stat["dice"] += 1
                logger.info("[poke] 掷骰子没中(%.2f)，这次不回", BACK_RATE)
                return

            # 冷却窗过去了就把连击数清掉
            annoyed = _repeat.get(key, 0) >= 2
            _repeat[key] = 0
            _last_poke[key] = now
            _note_hit(gid)

            await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

            ok = await self._poke_back(event, gid, who)
            said = ""
            if ok and random.random() < TALK_RATE:
                said = random.choice(TALK_ANNOYED if annoyed else TALK)
                try:
                    # event.send 要 MessageChain。plain_result() 返回的是
                    # MessageEventResult，没有 get_result —— 实测报
                    # 'MessageEventResult' object has no attribute 'get_result'。
                    # dsh-welcome 里的正确写法就是 MessageChain(chain=[...])。
                    await event.send(MessageChain(chain=[Plain(said)]))
                    _stat["talked"] += 1
                except BaseException as e:
                    logger.warning("[poke] 附言发送失败: %s", e)
                    said = ""
            brief = "回戳 %s%s%s" % (
                who, "（烦了）" if annoyed else "",
                ("＋「%s」" % said) if said else "",
            )
            _last.append(time.strftime("%H:%M:%S ") + brief)
            del _last[:-10]
            logger.info("[poke] %s", brief)
        except BaseException as e:
            logger.warning("[poke] 处理失败: %s", e)
        finally:
            # 空消息绝不能继续往下走去问 LLM（dsh-welcome 同一处理）
            try:
                event.stop_event()
            except BaseException:
                pass

    async def _poke_action(self, bot, gid: str, who: str, self_id: str = "") -> bool:
        """统一执行 NapCat group_poke；主动/被动两条链共用同一风控出口。"""
        call = getattr(bot, "call_action", None)
        if not callable(call):
            _stat["fail"] += 1
            logger.warning("[poke] 这个平台没有 call_action，戳不了")
            return False
        routing = {"self_id": self_id} if self_id else {}
        try:
            await call("group_poke", group_id=int(gid), user_id=int(who), **routing)
            _stat["poked"] += 1
            return True
        except BaseException as e:
            _stat["fail"] += 1
            logger.warning("[poke] group_poke 失败(%s): %r", type(e).__name__, e)
            return False

    async def _poke_back(self, event: AstrMessageEvent, gid: str, who: str) -> bool:
        """被戳后的回戳；失败不影响群聊。"""
        sid = str(getattr(event.message_obj, "self_id", None) or "")
        return await self._poke_action(getattr(event, "bot", None), gid, who, sid)

    def _consume_probe(self, key: str) -> None:
        pending = self._probe_state.setdefault("pending", {})
        pending.pop(key, None)
        _save_probe_state(self._probe_state)

    async def _group_member_ids(self, bot, gid: str, self_id: str = "") -> set[str]:
        call = getattr(bot, "call_action", None)
        if not callable(call):
            return set()
        routing = {"self_id": self_id} if self_id else {}
        try:
            rows = await asyncio.wait_for(
                call("get_group_member_list", group_id=int(gid), **routing), timeout=8
            )
            if isinstance(rows, dict):
                rows = rows.get("data") or rows.get("members") or []
            return {str(row.get("user_id")) for row in (rows or []) if isinstance(row, dict)}
        except BaseException as exc:
            logger.warning("[poke] 读取群成员失败 gid=%s: %r", gid, exc)
            return set()

    async def _platform_bot(self):
        platform = self.context.get_platform_inst(PROBE_PLATFORM)
        if platform is None:
            return None, ""
        bot = getattr(platform, "bot", None)
        if self._self_id.isdigit():
            return bot, self._self_id
        for attr in ("_wsr_api_clients", "_wsr_event_clients"):
            book = getattr(bot, attr, None)
            if isinstance(book, dict):
                for key in book:
                    if str(key).isdigit():
                        self._self_id = str(key)
                        return bot, self._self_id
        try:
            info = await asyncio.wait_for(bot.call_action("get_login_info"), timeout=8)
            uid = str((info or {}).get("user_id") or "")
            if uid.isdigit():
                self._self_id = uid
        except BaseException as exc:
            logger.warning("[poke] 读取机器人 QQ 失败: %r", exc)
        return bot, self._self_id

    @filter.llm_tool(name="probe_with_poke")
    async def probe_with_poke(self, event: AstrMessageEvent, target_qq: str):
        """在群里想试探某位群友是否愿意互动时，先轻轻戳一下并等待反应。

        安全试运行期间，仅当群主明确要求你戳某个群成员时调用；不得自行骚扰别人，
        不得连续调用。调用后不要立刻再发消息，插件会在对方回戳或开口时续聊。

        Args:
            target_qq(string): 当前群成员的 QQ 数字账号
        """
        _stat["probe_requested"] += 1
        gid = str(event.get_group_id() or "")
        caller = str(event.get_sender_id() or "")
        target = str(target_qq or "").strip()
        if not _TARGET_RE.fullmatch(target):
            _stat["probe_denied"] += 1
            return "没有执行：目标 QQ 必须是当前群成员的纯数字账号。"
        if not gid or (GROUPS and gid not in GROUPS):
            _stat["probe_denied"] += 1
            return "没有执行：当前群不在戳一戳试运行范围。"
        bot, self_id = await self._platform_bot()
        members = await self._group_member_ids(bot, gid, self_id)
        now = time.time()
        history = self._probe_state.setdefault("history", {}).setdefault(gid, [])
        history[:] = [float(ts) for ts in history if now - float(ts) <= PROBE_WINDOW]
        key = _probe_key(gid, target)
        pending = self._probe_state.setdefault("pending", {}).get(key, {})
        decision = decide_probe(
            caller_id=caller,
            target_id=target,
            owner_id=OWNER,
            group_member_ids=members,
            now=now,
            last_probe_at=float(self._probe_state.setdefault("last", {}).get(key, 0) or 0),
            group_probe_times=history,
            pending_until=float(pending.get("until", 0) or 0),
            social_allowed=_social_allows_probe(gid, target, now),
            active_hours_allowed=_in_probe_hours(now),
            enabled=PROBE_ON,
            owner_only=PROBE_OWNER_ONLY,
            cooldown=PROBE_COOLDOWN,
            window=PROBE_WINDOW,
            group_max=PROBE_GROUP_MAX,
        )
        if not decision.allowed:
            _stat["probe_denied"] += 1
            logger.info("[poke] 主动试探被闸门拒绝 gid=%s target=%s: %s", gid, target, decision.reason)
            return "没有执行：%s。" % decision.reason
        if not await self._poke_action(bot, gid, target, self_id):
            return "主动戳发送失败，不要声称已经戳到。"
        history.append(now)
        self._probe_state["last"][key] = now
        self._probe_state["pending"][key] = {
            "until": now + PROBE_WAIT, "caller": caller,
            "request_marker": str(event.message_obj.message_id or ""),
        }
        _save_probe_state(self._probe_state)
        _stat["probe_sent"] += 1
        logger.info("[poke] 主动试探已发送 gid=%s target=%s，等待%.0fs反馈", gid, target, PROBE_WAIT)
        return "已轻轻戳了一下，正在等对方反应。现在不要立刻追发消息。"

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=1500)
    async def observe_probe_message(self, event: AstrMessageEvent) -> None:
        """对方被主动戳后在等待窗内开口：给当前真实对话补充“试探有反馈”语义。"""
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            if event.get_extra("dsh_poke_probe"):
                return
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            if not gid or not uid or uid == str(event.get_self_id() or ""):
                return
            key = _probe_key(gid, uid)
            probe = self._probe_state.get("pending", {}).get(key, {})
            feedback = classify_probe_feedback(
                now=time.time(), pending_until=float(probe.get("until", 0) or 0), feedback_kind="message"
            )
            if feedback != "message":
                return
            self._consume_probe(key)
            _stat["probe_message"] += 1
            event.set_extra("dsh_poke_probe_feedback", "message")
            event.set_extra("dsh_poke_probe_target", uid)
            # 主动戳后的真实发言就是一次明确回应，允许这轮进入正常聊天管道；
            # decide/ctxclean/mention 会根据专用 extra 识别它，不会误当普通强制 @。
            event.is_at_or_wake_command = True
            logger.info("[poke] 主动试探得到消息反馈 gid=%s uid=%s，交给当前对话承接", gid, uid)
        except BaseException as exc:
            logger.warning("[poke] 观察主动试探反馈失败: %r", exc)

    async def _fire_probe_followup(self, gid: str, who: str, kind: str) -> bool:
        platform = self.context.get_platform_inst(PROBE_PLATFORM)
        if platform is None or not hasattr(platform, "create_event"):
            logger.warning("[poke] 主动试探续聊找不到平台 %s", PROBE_PLATFORM)
            return False
        bot, self_id = await self._platform_bot()
        if not self_id:
            return False
        key = _probe_key(gid, who)
        if key in self._probe_following:
            return False
        self._probe_following.add(key)
        try:
            now = time.time()
            raw = Event.from_payload({
                "time": int(now), "self_id": int(self_id), "post_type": "message",
                "message_type": "group", "sub_type": "normal", "message_id": uuid.uuid4().int % (2**31),
                "group_id": int(gid), "user_id": int(PROBE_SENTINEL_UID), "message": [],
                "raw_message": "", "font": 0,
                "sender": {"user_id": int(PROBE_SENTINEL_UID), "nickname": PROBE_SENTINEL_NAME, "role": "member"},
                "dsh_poke_probe": True,
            })
            msg = AstrBotMessage()
            msg.type = MessageType.GROUP_MESSAGE
            msg.self_id = self_id
            msg.session_id = gid
            msg.message_id = str(raw["message_id"])
            msg.group = Group(group_id=gid)
            msg.sender = MessageMember(user_id=PROBE_SENTINEL_UID, nickname=PROBE_SENTINEL_NAME)
            msg.message = []
            msg.message_str = ""
            msg.timestamp = int(now)
            msg.raw_message = raw
            follow = platform.create_event(msg)
            follow.set_extra("dsh_poke_probe", True)
            follow.set_extra("dsh_poke_probe_kind", kind)
            follow.set_extra("dsh_poke_probe_target", who)
            platform.commit_event(follow)
            _stat["probe_followup"] += 1
            return True
        finally:
            self._probe_following.discard(key)

    @filter.custom_filter(ProbeFeedbackFilter)
    async def speak_after_probe(self, event: AstrMessageEvent):
        gid = str(event.get_group_id() or "")
        conv = None
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            if cid:
                conv = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, cid)
        except BaseException as exc:
            logger.warning("[poke] gid=%s 读取会话失败: %r", gid, exc)
        if conv is None:
            logger.info("[poke] gid=%s 没有现成会话，不凭空续聊", gid)
            return
        req = event.request_llm(
            prompt="（你先轻轻戳了对方一下，对方现在给了反应；顺势接一句）",
            session_id=event.session_id,
            conversation=conv,
        )
        req.extra_user_content_parts.append(TextPart(text=build_probe_feedback_prompt(
            str(event.get_extra("dsh_poke_probe_kind") or "poke_back"),
            str(event.get_extra("dsh_poke_probe_target") or ""),
        )))
        yield req

    @filter.on_llm_request(priority=1200)
    async def inject_message_feedback(self, event: AstrMessageEvent, req) -> None:
        """若对方用文字回应，把试探反馈交给既有对话，而不是另发机械话术。"""
        kind = str(event.get_extra("dsh_poke_probe_feedback") or "")
        if kind != "message":
            return
        req.extra_user_content_parts.append(TextPart(text=build_probe_feedback_prompt(
            kind, str(event.get_extra("dsh_poke_probe_target") or "")
        )))

    @filter.on_using_llm_tool()
    async def mark_probe_tool(self, event: AstrMessageEvent, tool, tool_args) -> None:
        """标记本轮确实选择了主动戳工具，供回复出口精确静默。"""
        name = getattr(tool, "name", None) or str(tool)
        if name == "probe_with_poke":
            event.set_extra("dsh_poke_probe_tool", True)

    @filter.on_llm_response(priority=1200)
    async def silence_after_probe_tool(self, event: AstrMessageEvent, response) -> None:
        """工具成功主动戳后不再同时发文字；真正的下一句留给对方反馈。"""
        if not event.get_extra("dsh_poke_probe_tool"):
            return
        # pending 只会在工具成功后写入，按群和调用者可精确识别本轮。
        gid = str(event.get_group_id() or "")
        caller = str(event.get_sender_id() or "")
        marker = str(event.message_obj.message_id or "")
        pending = self._probe_state.get("pending", {})
        if any(key.startswith(gid + ":") and str(val.get("caller", "")) == caller
               and str(val.get("request_marker", "")) == marker
               for key, val in pending.items() if isinstance(val, dict)):
            response.completion_text = ""
            logger.info("[poke] 主动戳已发出，本轮不附文字，等待对方反应")

    @filter.command("戳一戳状态")
    async def cmd_status(self, event: AstrMessageEvent):
        s = _stat
        lines = [
            "戳一戳：%s 回戳率%.0f%% 说话率%.0f%%"
            % ("开" if ENABLED else "关", BACK_RATE * 100, TALK_RATE * 100),
            "收到戳 %d 次：戳的不是我 %d｜戳我自己 %d"
            % (s["seen"], s["not_me"], s["self_poke"]),
            "没回的原因：同人冷却 %d｜同群限流 %d｜掷骰子 %d"
            % (s["cooldown"], s["group_limit"], s["dice"]),
            "回戳成功 %d 次，其中附话 %d 次；调用失败 %d"
            % (s["poked"], s["talked"], s["fail"]),
            "主动试探：%s/%s 请求%d｜已戳%d｜拒绝%d｜回戳反馈%d｜消息反馈%d｜续聊%d"
            % ("开" if PROBE_ON else "关", "仅群主" if PROBE_OWNER_ONLY else "全员",
               s["probe_requested"], s["probe_sent"], s["probe_denied"],
               s["probe_poke_back"], s["probe_message"], s["probe_followup"]),
            "主动闸门：同人%.0f分钟｜%.0f分钟每群≤%d｜反馈窗%.0fs｜时段%s"
            % (PROBE_COOLDOWN / 60, PROBE_WINDOW / 60, PROBE_GROUP_MAX, PROBE_WAIT,
               PROBE_HOURS),
            "被动闸门：普通同人 %.0fs｜群主 %.0fs｜%.0fs 内每群≤%d"
            % (COOLDOWN, OWNER_COOLDOWN, WINDOW, GROUP_MAX),
        ]
        if _last:
            lines.append("最近几次：")
            lines += ["  " + x for x in _last[-5:]]
        yield event.plain_result("\n".join(lines))
