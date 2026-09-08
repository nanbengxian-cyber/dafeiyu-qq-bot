# -*- coding: utf-8 -*-
"""dsh-proactive —— 群友聊到我感兴趣的内容时，主动探头接一句。

=====================================================================
这和 dsh-decide / dsh-initiate 是三条不同的尺子，别混：

  dsh-decide      有人正在说话，我该不该插 -> 小模型否决式（默认开口）
  dsh-initiate    没人在说话，我该不该起头 -> 小模型感知 + 代码闸门
  dsh-proactive   有人聊到了「我感兴趣的内容」，主动探头接一句
                  -> 纯正则兴趣评分（零 LLM 成本），命中就走合成事件

抄的是 Codex-Remote-Contact 的 QQ-Enhancer（github.com/Epic0522/QQ-Enhancer）
src/qq-enhancer/qq-chat-style.js 的 shouldProactivelyReplyToQq：
它用部署者定义的正则 + 分数分类 + 冷却时间判断要不要主动插话，
完全不需要问模型 —— 这条链路零 token 成本，可以挂在每条群消息上。

移植要点（对比原版）：
  评分表    原版五类 + 锐评 + 抖动，这里按「小鲸鱼/大肥鱼」人设重写
  触发方式  原版直接回消息；这里走 dsh-initiate 已验证的合成事件路线
            （CustomFilter 认 dsh_proactive 标记 -> 完整消息管道 ->
              贴纸剥离/分段回复/@策略全部照常生效）
            —— 这是本机 astrbot 体系里唯一靠谱的主动说话通道。
  冷却      每群 lastGroupReplyAt，minIntervalMs 可配（默认 10 分钟）
  影子模式  默认开：只判定、只记日志、不真发。观察几天再放开。
  群主权重  群主（2774000001）说话兴趣分 +2 —— 主人说话天然更值得接
  每日额度  每群每天最多主动探头 N 次，防话痨（initiate 同款闸门）

三条硬边界（从 decide/drift 的教训里搬过来）：
  1. 被 @ / 被回复时不走这条 —— 那是正常应答路径，不是主动探头。
  2. 上一句是机器人自己说的不触发 —— 别自己接自己的话（自问自答）。
  3. 冷场时不触发 —— 那是 dsh-initiate 的活；本插件只处理热场插话。

合成事件兼容（和 dsh-initiate 同一套，需三处补丁，见最下方）：
  decide  见到 dsh_proactive 直接让路（兴趣是代码判的正向证据，不用再问）
  ctxclean 给 <proactive_context> 块（这段是「一句话都没被问就插话」档）
  mention 见到 dsh_proactive 跳过 @（哨兵号没有可 @ 的对象）

兴趣评分怎么改：score_interest(text, event) 是纯函数，改正则或分数
只动那几行，测试照着 test_proactive.py 跑。分数体系：
  主人说话     +2
  命中的兴趣类 见 _INTERESTS 表，每类一份正则 + 分数
  疑问词/标点  +1（吗/呢/咋/怎么/？）—— 有人抛话题
  长度 6~80    +1（太短没信息量，太长是正事）
  稳定抖动     0~3（同消息每次分数一致，但天然带随机性，防同分）

阈值默认 8（对齐 Codex 原版）：一类强兴趣 + 群主分 或 两类弱兴趣 可触发。
"""

import asyncio
import json
import os
import re
import sqlite3
import time
import uuid
from collections import deque
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

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


ENABLED = _flag("DSH_PROACTIVE")
# 影子模式默认开：自动路径只判定和记录，不发消息。先看几天判定质量再放开。
SHADOW = _flag("DSH_PROACTIVE_SHADOW")
GROUPS = {
    g.strip() for g in os.environ.get("DSH_PROACTIVE_GROUPS", "100000001").split(",") if g.strip()
}
# 每群两次主动探头之间的最小间隔（毫秒，对齐 Codex minIntervalMs 语义）。
MIN_INTERVAL_MS = max(60_000, int(os.environ.get("DSH_PROACTIVE_MIN_INTERVAL_MS", "600000")))
DAY_MAX = max(1, int(os.environ.get("DSH_PROACTIVE_DAY_MAX", "3")))
THRESHOLD = max(1, int(os.environ.get("DSH_PROACTIVE_THRESHOLD", "8")))
OWNER = os.environ.get("DSH_PROACTIVE_OWNER", "2774000001").strip()
TIMEZONE = os.environ.get("DSH_PROACTIVE_TZ", "Asia/Shanghai")
HOURS = os.environ.get("DSH_PROACTIVE_HOURS", "10-23,0-2")
DB_PATH = os.environ.get("DSH_PROACTIVE_DB", "/AstrBot/data/dsh_memory.db")
STATE_PATH = Path(os.environ.get("DSH_PROACTIVE_STATE", "/AstrBot/data/dsh_proactive_state.json"))
PLATFORM_ID = os.environ.get("DSH_PROACTIVE_PLATFORM", "default")
SELF_ID_ENV = os.environ.get("DSH_PROACTIVE_SELF_ID", "").strip()
# 合成事件的发送者。不能填机器人自己的 QQ：ignore_bot_self_message=True
# 会让 WakingCheckStage 直接 stop_event，事件根本走不到插件。
SENTINEL_UID = "0"
SENTINEL_NAME = "（系统·兴趣探头）"


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
        return datetime.fromtimestamp(now)


def in_active_hours(now: float | None = None) -> bool:
    return _now_tz(time.time() if now is None else now).hour in ACTIVE_HOURS


# ------------------------------------------------------------------ 兴趣表
# 每份兴趣 = (正则, 分数, 名字)。
# 分数对齐 Codex 原版量级（单一强兴趣 ≈ 阈值，弱信号叠几个也能过）。
# 正则全部取「群里真实说话会出现的词」，至少留一条活路，避免全不命中
# 变成永远不主动（那样还不如删掉本插件）。
_LOW_VALUE_RE = re.compile(
    r"^(\?|？|。|\.|,|，|哈+|啊+|哦+|嗯+|1|6|66|666|草|艹|笑死|哈哈哈*|典|乐|难绷|"
    r"对|是|好|行|可以|收到|在|嗯嗯|欧克|ok|OK)$"
)
_SERVICE_RE = re.compile(
    r"(帮我|请问|怎么|如何|为什么|能不能|可以吗|求|查一下|搜一下|写|做|修|装|配置|教程|解释|分析|总结|发给我|告诉我)"
)
# 冷场判据：最近一条消息距现在超过这个秒数，就归 dsh-initiate，不归本插件。
COLD_AFTER = float(os.environ.get("DSH_PROACTIVE_COLD_AFTER", "600"))

_INTERESTS = [
    # 核心食欲词 —— 大胃袋的生理反应。白米饭/饿/馋/想吃 都该单档必触发，
    # 群里真的在喊「不准吃大白饭」「夜宵给我吃」「饿啊」。
    (re.compile(r"(白米饭|大白饭|吃白饭|米饭|干饭|吃饭|没吃饭|去吃饭|干饭人|干饭魂|快吃饭|吃饭了|吃了吗|白饭|饿|馋|想吃|好吃|美食|夜宵|宵夜|加餐|好饿|饿了|吃啥|吃点|吃口|吃顿|开饭)"), 8, "干饭"),
    # 具体食物词 —— 在聊具体吃什么的，比「饿」低半档。
    (re.compile(r"(火锅|烧烤|奶茶|外卖|食堂|零食|甜点|蛋糕|冰淇淋|冰激凌|炸鸡|烤串|麻辣烫|螺蛳粉|面条|饺子|包子|馒头|炒饭|盖饭|泡面|螺蛳|卤味|甜品|夜宵摊|饭量|胃口|加饭|好吃|难吃)"), 6, "美食"),
    # 深海 / 鲸 —— 本体是小鲸鱼，聊到自己的来处会更想接。
    (re.compile(r"(鲸鱼|深海|大海|海洋|海底|小鲸鱼|鲸|海豚|水母)"), 5, "深海"),
    # DeepSeek/模型/技术 —— 老本行，别人聊 AI 会有「这题我会」冲动。
    (re.compile(r"(deepseek|DeepSeek|大模型|模型|本地部署|token|API|prompt|代码|bug|报错|显卡|服务器|训练|微调)"), 4, "AI"),
    # 群里点名 bot —— 有人提「大肥鱼 / 鱼哥 / 小鲸鱼」就是在说它。
    (re.compile(r"(大肥鱼|肥鱼|鱼哥|小鲸鱼|这鱼|那个鱼)"), 6, "点名"),
]
_QUESTION_RE = re.compile(r"[?？]|(吗|呢)\s*$|(怎么|咋|啥|为什么|为啥|哪个|谁|什么时候|多少)")


def stable_modulo(seed: str, modulo: int) -> int:
    """同一条消息的种子恒定 -> 分数恒定；不同消息天然带 0~3 随机性。"""
    return sum(ord(c) for c in seed) % modulo


def load_interest_weights() -> dict:
    """读 dsh-interest 写出的热度权重表 {gid: {兴趣名: 权重}}。

    文件不存在、损坏、或没有升权条目都返回 {}（= 全按基线 1.0）。
    失败绝不炸 —— 兴趣热度是锦上添花，静态兴趣表才是主干。
    """
    path = os.environ.get(
        "DSH_PROACTIVE_INTEREST_STATE", "/AstrBot/data/dsh_interest_state.json"
    )
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        table = value.get("hot") if isinstance(value, dict) else None
        return table if isinstance(table, dict) else {}
    except (OSError, ValueError):
        return {}


def score_interest(text: str, event: dict | None = None,
                   weights: dict | None = None) -> tuple[int, list[str]]:
    """给一条消息打兴趣分。纯函数，无副作用，可离线回测。

    weights：dsh-interest 写出的热度权重表 {兴趣名: 系数}，
    命中该兴趣时把分数乘上系数（>1 表示最近群里老聊它，更想接）。
    返回 (分数, 命中的兴趣名列表)。event 可选，用于群主加权。
    """
    raw = str(text or "").strip()
    if not raw:
        return 0, []
    event = event or {}
    weights = weights or {}
    score = 0
    hits: list[str] = []
    for pattern, points, name in _INTERESTS:
        if pattern.search(raw):
            # 同一类只算一次，别一条消息里出现五顿「吃饭」就把分刷爆。
            w = weights.get(name)
            score += int(points * w) if w else points
            hits.append(name)
    if str(event.get("senderId") or "") == OWNER:
        score += 2
        hits.append("群主")
    if _QUESTION_RE.search(raw):
        score += 1
    if 6 <= len(raw) <= 80:
        score += 1
    # 稳定抖动：同消息同分（可复现），不同消息天然有 0~3 的差别，
    # 避免两条都刚好卡在阈值上的消息永远同时触发或同时错过。
    gid = str(event.get("groupId") or "")
    uid = str(event.get("senderId") or "")
    mid = str(event.get("raw", {}).get("message_id", "") if isinstance(event.get("raw"), dict) else "")
    score += stable_modulo(f"{gid}:{uid}:{mid}:{raw}", 4)
    return score, hits


def should_proactively_reply(
    text: str,
    event: dict | None = None,
    last_reply_ms: int = 0,
    now_ms: int = 0,
    count: int = 0,
    weights: dict | None = None,
) -> tuple[bool, str, int, list[str]]:
    """总闸门（纯函数）。返回 (是否触发, 原因, 分数, 命中兴趣)。"""
    now_ms = now_ms or int(time.time() * 1000)
    raw = str(text or "").strip()
    if not raw:
        return False, "空消息", 0, []
    if _LOW_VALUE_RE.match(raw):
        return False, "低价值消息", 0, []
    # 服务式请求（帮我/怎么/能不能）—— 有人在求帮忙，不是闲聊，
    # 除非里面本身带兴趣词（「求推荐好吃的」带「好吃」照样触发）。
    if _SERVICE_RE.search(raw):
        has_interest = any(p.search(raw) for p, _, _ in _INTERESTS)
        if not has_interest:
            return False, "纯服务请求", 0, []
    score, hits = score_interest(raw, event, weights)
    if now_ms - last_reply_ms < MIN_INTERVAL_MS:
        return False, "冷却中", score, hits
    if count >= DAY_MAX:
        return False, "今日额度用完", score, hits
    if score < THRESHOLD:
        return False, f"兴趣分不够({score}<{THRESHOLD})", score, hits
    return True, f"兴趣分{score}", score, hits


# ------------------------------------------------------------------ 状态
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
        tmp.replace(STATE_PATH)
    except OSError as exc:
        logger.warning("[proactive] 保存状态失败: %s", exc)


def read_context(gid: str, limit: int = 8, db: str = DB_PATH) -> list[tuple]:
    """buffer ∪ archive 按 ts 归并去重，返回按时间正序的 (uid, name, ts, text)。

    只读最近活跃段的上下文（探头要在「刚才发生过什么」里找由头），
    冷场很久时该由 dsh-initiate 管，本插件不读太深的历史。
    """
    rows: list[tuple] = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
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
            except sqlite3.Error:
                pass
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


class ProactiveFilter(CustomFilter):
    """只放行本插件自己造的事件。靠 extra 标记认，不靠猜消息形状。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        return bool(event.get_extra("dsh_proactive"))


def _render_context(rows: list[tuple]) -> str:
    return "\n".join(f"{r[1]}：{r[3][:80]}" for r in rows if r[3].strip())


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._state = _load_state()
        self._last_event: dict[str, float] = {}  # 同一条消息去重（消息 id -> ts）
        self._quiet: dict[str, str] = {}  # 日志去重，避免每条消息刷一行
        self._self_id: str = SELF_ID_ENV
        logger.info(
            "[proactive] 已加载：%s 影子=%s 阈值%d 间隔%.0f分钟 每日%d次 群=%s",
            "开" if ENABLED else "关", "开" if SHADOW else "关",
            THRESHOLD, MIN_INTERVAL_MS / 60000, DAY_MAX,
            ",".join(sorted(GROUPS)) or "无",
        )

    # ---------------------------------------------------------- 消息入口
    # 用 platform_adapter_type(ALL) 像 dsh-memory 一样收每条群消息，
    # 这样群友之间的闲聊也进评分（主动探头要抓的恰恰是没人喊它的时候）。
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
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
            if not gid or not uid:
                return
            if gid not in GROUPS:
                return
            if uid == str(event.get_self_id() or ""):
                return
            # 被 @ / 被回复 —— 正常应答路径，不是主动探头。
            if bool(getattr(event, "is_at_or_wake_command", False)):
                return
            # 上一句是自己说的 —— 别自己接自己（decide 的 MIN_GAP 类比）。
            recent = read_context(gid, 3)
            if recent and str(recent[-1][0]) == SENTINEL_UID:
                return self._quiet_skip(gid, "上一句就是自己说的，不探头", "selflast")
            # 冷场不触发 —— 那是 dsh-initiate 的活。
            if recent:
                idle = time.time() - recent[-1][2]
                if idle >= COLD_AFTER:
                    return self._quiet_skip(gid, "冷场%.0f分钟，归 initiate 管" % (idle / 60), "cold")

            text = str(event.get_message_str() or "").strip()
            if not text:
                return
            if text.startswith("/"):
                return

            now_ms = int(time.time() * 1000)
            state = self._day(gid, now_ms)
            # 读 dsh-interest 的热度权重（存在才加权，文件读失败当没有）。
            weights = load_interest_weights().get(gid)
            decision = should_proactively_reply(
                text,
                {
                    "senderId": uid,
                    "groupId": gid,
                    "raw": getattr(event, "raw_message", None) or {},
                },
                last_reply_ms=int(state.get("last", 0) or 0),
                now_ms=now_ms,
                count=int(state.get("count", 0) or 0),
                weights=weights if isinstance(weights, dict) else None,
            )
            ok, why, score, hits = decision
            if not ok:
                return self._quiet_skip(gid, "不探头：%s" % why, why[:16])
            # 同一条消息可能被多个渠道/适配器重复送进来，按消息 id 去重。
            mid = str(getattr(getattr(event, "raw_message", None), "message_id", "") or "#%s:%s" % (gid, text[:12]))
            last = self._last_event.get(gid, 0.0)
            if time.time() - last < 2.0:
                return
            self._last_event[gid] = time.time()
            logger.info(
                "[proactive] gid=%s 命中兴趣(%s) 分%d %s 原文=%r",
                gid, "+".join(hits), score, why, text[:60],
            )
            if SHADOW:
                self._quiet.pop(gid, None)
                logger.info("[proactive] 影子模式，只记不发 gid=%s", gid)
                return
            await self._fire(gid, now_ms, score, hits, text, state)
        except BaseException as exc:
            logger.warning("[proactive] 评分失败，跳过: %r", exc)

    def _quiet_skip(self, gid: str, reason: str, key: str | None) -> None:
        """不触发也要打日志，但同一类原因只打一次（decide/initiate 同款做法）。"""
        if key is None:
            self._quiet.pop(gid, None)
        elif self._quiet.get(gid) == key:
            return
        else:
            self._quiet[gid] = key
        logger.info("[proactive] gid=%s %s", gid, reason)

    def _day(self, gid: str, now_ms: int) -> dict:
        day = _now_tz(now_ms / 1000).date().isoformat()
        entry = self._state.get(gid)
        if not isinstance(entry, dict) or entry.get("day") != day:
            entry = {"day": day, "count": 0, "last": 0.0}
            self._state[gid] = entry
        return entry

    # ------------------------------------------------------------ 探头
    async def _fire(self, gid: str, now_ms: int, score: int, hits: list,
                    trigger_text: str, state: dict) -> bool:
        platform = self.context.get_platform_inst(PLATFORM_ID)
        if platform is None or not hasattr(platform, "create_event"):
            logger.error("[proactive] 找不到平台实例 %s", PLATFORM_ID)
            return False
        self_id = await self._resolve_self_id(platform)
        if not self_id:
            logger.error("[proactive] 解析不到机器人 QQ，放弃这次探头")
            return False
        now = now_ms / 1000.0
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
            "dsh_proactive": True,
        })
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE
        msg.self_id = self_id
        msg.session_id = gid
        msg.message_id = str(raw["message_id"])
        msg.group = Group(group_id=gid)
        msg.sender = MessageMember(user_id=SENTINEL_UID, nickname=SENTINEL_NAME)
        msg.message = []
        msg.message_str = ""
        msg.timestamp = int(now)
        msg.raw_message = raw
        event = platform.create_event(msg)
        event.set_extra("dsh_proactive", True)
        event.set_extra("dsh_proactive_score", score)
        event.set_extra("dsh_proactive_hits", hits)
        event.set_extra("dsh_proactive_text", trigger_text)
        event.set_extra("dsh_proactive_ctx", _render_context(read_context(gid, 8)))
        # 配额先落盘再提交：提交后是异步管道，崩了也不能让配额白漏。
        state["count"] = int(state.get("count", 0)) + 1
        state["last"] = now_ms
        _save_state(self._state)
        platform.commit_event(event)
        logger.info("[proactive] gid=%s 已探头（今天第%d次）兴趣=%s 分%d",
                    gid, state["count"], "+".join(hits), score)
        return True

    @filter.custom_filter(ProactiveFilter)
    async def speak(self, event: AstrMessageEvent):
        """合成事件走到这里：给出非空 prompt，让 ProcessStage 跑完整 agent。

        prompt 不能为空 —— astr_main_agent 对空请求直接返回 None。
        由 dsh-ctxclean 的 _SPONTANEOUS 档和本插件的 <proactive_context> 块
        一起告诉模型「这是你主动探头，不是被提问」。
        """
        gid = str(event.get_group_id() or "")
        hits = event.get_extra("dsh_proactive_hits") or []
        score = int(event.get_extra("dsh_proactive_score") or 0)
        ctx = event.get_extra("dsh_proactive_ctx") or ""
        trigger = event.get_extra("dsh_proactive_text") or ""
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
            logger.warning("[proactive] gid=%s 取会话失败: %r", gid, exc)
        if conv is None:
            logger.error("[proactive] gid=%s 没有现成会话，放弃探头", gid)
            return
        req = event.request_llm(
            prompt="（群里有人在聊你感兴趣的内容，你主动探头接一句）",
            session_id=event.session_id,
            conversation=conv,
        )
        # 必须是 TextPart，放裸 str 会让 openai_source 抛 ValueError。
        # 小写标签开头 => dsh-ctxclean 会按结构把它从历史里清掉，不会累积。
        req.extra_user_content_parts.append(TextPart(text=(
            "<proactive_context>群里有人聊到了你感兴趣的东西（%s，兴趣分%d）。"
            "你顺着最自然的一处接一句，像真人听到感兴趣的话题时随口插话一样；"
            "不要假装有人@了你，不要说「你提到我很高兴」，别解释你在主动说话。"
            "这句前面他们聊到：%s。"
            "触发的那一句是：%s。"
            "只说一句短话，接不上就发个表情或语气词。"
            "</proactive_context>"
        ) % ("/".join(hits) if hits else "感兴趣的话题", score,
             ctx[-500:] or "（没有上下文）", trigger[:100]))
        yield req

    # ------------------------------------------------------------ 身份
    async def _resolve_self_id(self, platform) -> str:
        """拿机器人真实 QQ。填错会污染 NapCat 的多号路由（见 initiate 文件头）。"""
        if self._self_id.isdigit():
            return self._self_id
        bot = getattr(platform, "bot", None)
        for attr in ("_wsr_api_clients", "_wsr_event_clients"):
            book = getattr(bot, attr, None)
            if isinstance(book, dict):
                for key in book:
                    if str(key).isdigit():
                        self._self_id = str(key)
                        logger.info("[proactive] self_id=%s（来自 %s）", self._self_id, attr)
                        return self._self_id
        try:
            info = await asyncio.wait_for(bot.call_action("get_login_info"), timeout=8)
            uid = str((info or {}).get("user_id") or "")
            if uid.isdigit():
                self._self_id = uid
                logger.info("[proactive] self_id=%s（来自 get_login_info）", uid)
                return uid
        except BaseException as exc:
            logger.warning("[proactive] 取 self_id 失败: %r", exc)
        return ""

    # ------------------------------------------------------------ 指令
    @filter.command("探头状态")
    async def status(self, event: AstrMessageEvent):
        if str(event.get_sender_id()) != OWNER:
            return
        gid = str(event.get_group_id() or "")
        state = self._day(gid, int(time.time() * 1000))
        last = float(state.get("last", 0) or 0)
        left = max(0.0, (last + MIN_INTERVAL_MS - int(time.time() * 1000)) / 60000)
        yield event.plain_result(
            "兴趣探头 %s／影子 %s\n"
            "今天 %d/%d 次，冷却还剩 %.0f 分钟\n"
            "阈值 %d，兴趣类别：%s\n"
            "群 %s"
            % (
                "开" if ENABLED else "关", "开" if SHADOW else "关",
                state.get("count", 0), DAY_MAX, left, THRESHOLD,
                "/".join(name for _, _, name in _INTERESTS),
                ",".join(sorted(GROUPS)) or "无",
            )
        )
