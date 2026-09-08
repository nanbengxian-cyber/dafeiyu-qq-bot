# -*- coding: utf-8 -*-
"""dsh-interest —— 兴趣是会变的：让「鲸鱼娘的馋」有自己的一条时间线。

=====================================================================
为什么要这个

dsh-proactive 的兴趣正则是一张静态表：白米饭 8 分、火锅 6 分……
它没有「最近」的概念，所以机器人的兴趣永远一样厚。真人的兴趣
是会漂移的：这两周狂炫白米饭，过阵子又开始馋别的；群里连着几天
聊深夜食堂，它也会被带得想接话。本插件补的就是这一层 ——
**兴趣热度有生命周期**。

抄的是 MaiBot 的 attention_drift 同源思路（dsh-drift 也是抄它）：
把它拆成可控的、可回测的小机制，而不是让模型自己「有感觉」。

三层机制（全部纯代码 + 可配环境变量，不动一个 token）：

  A. 时效热度（HOT_WINDOW 秒内命中次数的指数衰减）
     最近群里老聊「火锅」，火锅那类兴趣分就该暂时涨。
     5 分钟内聊到 3 次 -> 该兴趣 24 小时内权重 +30%。
     超过 48 小时没再聊 -> 权重回落到基线。真人也是这么变的。

  B. 口味周期（每 MOOD_HOURS 小时随机轮换 1~2 个「今日馋」）
     大胃袋不可能永远只馋白米饭。每几小时从食物池里随机抽
     1~2 个当前「正在上头」的口味，写进状态 + 注入 prompt，
     让说话带上「最近有点馋火锅」的自然变化。不设抽空记忆：
     状态文件一直记着，重启也不丢。

  C. 注入（on_llm_request）
     命中群聊/被 @ 的正常回复里，塞一小块 <interest_state>，
     说明当前兴趣热度最高的话题和今日馋，让人格语气更「活」。
     块名小写标签，dsh-ctxclean 会自动清理历史，不累积。

和 dsh-proactive 的协作：
  本插件把 state 写到 DSH_INTEREST_STATE_PATH（默认
  /AstrBot/data/dsh_interest_state.json），dsh-proactive 每次评分时
  读这个文件给热度高的兴趣加权。—— 两个插件可以独立开关：
  只开 interest 不带 proactive，就是「说话带点今日馋」；
  只开 proactive 不带 interest，就是固定兴趣表。

硬边界：
  * 注入块一句话内，不展开、不列表、不解释「这是插件给我的」。
  * 热度事件只从群聊文本里数，不数刷屏的重复消息（去重后计数）。
  * 睡眠时段（DSH_INTEREST_HOURS 之外）不注入 —— 犯困的大胃袋
    还会馋白米饭，但没人会在凌晨 3 点聊美食。
"""

import json
import os
import random
import re
import time
from collections import deque
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


ENABLED = _flag("DSH_INTEREST")
# 影子模式：照样统计热度、照样轮换口味，但**不**注入 prompt。
# 先看热度数字合不合理，再放开口味注入。
SHADOW = _flag("DSH_INTEREST_SHADOW")
GROUPS = {
    g.strip() for g in os.environ.get("DSH_INTEREST_GROUPS", "100000001").split(",") if g.strip()
}
# 热度统计窗口：这条消息之前的多少秒内算「最近」。
HOT_WINDOW = max(60, int(os.environ.get("DSH_INTEREST_HOT_WINDOW", str(5 * 60))))
# 热度升级需要的命中次数（在窗口内）。
HOT_HITS = max(2, int(os.environ.get("DSH_INTEREST_HOT_HITS", "3")))
# 热度权重最高升到基线的几倍。
HOT_CAP = float(os.environ.get("DSH_INTEREST_HOT_CAP", "1.6"))
# 口味周期：每隔多少小时轮换一次「今日馋」。
MOOD_HOURS = max(1, float(os.environ.get("DSH_INTEREST_MOOD_HOURS", "4")))
# 注入开关（睡眠时段等）。
HOURS = os.environ.get("DSH_INTEREST_HOURS", "10-23,0-2")
TIMEZONE = os.environ.get("DSH_INTEREST_TZ", "Asia/Shanghai")
STATE_PATH = Path(os.environ.get(
    "DSH_INTEREST_STATE_PATH", "/AstrBot/data/dsh_interest_state.json"))
OWNER = os.environ.get("DSH_INTEREST_OWNER", "2774000001").strip()


def parse_hours(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        bits = part.strip().split("-", 1)
        try:
            start, end = int(bits[0]), int(bits[-1])
        except (ValueError, IndexError):
            continue
        hour = start
        for _ in range(24):
            out.add(hour)
            if hour == end:
                break
            hour = (hour + 1) % 24
    return out


ACTIVE_HOURS = parse_hours(HOURS)


def _now_tz(now=None):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = time.time() if now is None else now
    try:
        return datetime.fromtimestamp(now, ZoneInfo(TIMEZONE))
    except Exception:
        return datetime.fromtimestamp(now)


def in_active_hours(now: float | None = None) -> bool:
    return _now_tz(now).hour in ACTIVE_HOURS


# ------------------------------------------------------------------ 食材池
# 轮换口味从这个池子里抽。名字就是注入 prompt 里说的话——
# 留的是「大胃袋会馋」的具体名词，不要放抽象分类。
_MOOD_POOL = [
    "白米饭", "蛋炒饭", "火锅", "烧烤", "麻辣烫", "螺蛳粉", "炸鸡",
    "奶茶", "冰淇淋", "泡面", "深夜食堂", "蛋糕", "零食", "糖炒栗子",
    "老干妈拌饭", "烤肉", "甜品", "夜宵", "干捞面", "红枣糯米糕",
]

# 生效中的热度条目：{ "group_id:兴趣名": deque[(ts, text)] }
# 每条消息只数一次；窗口过期自动丢。
_hot: dict[str, deque] = {}

# 上次写状态/换口味的时间戳（跨重启可恢复）。
_state = {"mood": {}, "hot": {}, "last_swap": 0.0}


def _load_state() -> dict:
    try:
        saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            return saved
    except (OSError, ValueError):
        pass
    return {}


def _save_state() -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except OSError as exc:
        logger.warning("[interest] 保存状态失败: %s", exc)


def _load_from_disk_and_merge() -> None:
    global _state
    saved = _load_state()
    if not saved:
        return
    saved.setdefault("mood", {})
    saved.setdefault("hot", {})
    saved.setdefault("last_swap", 0.0)
    _state = saved


# ------------------------------------------------------------------ 热度
_GROUP_PARTS = {
    "干饭": re.compile(r"(白米饭|大白饭|吃白饭|米饭|干饭|吃饭|没吃饭|去吃饭|干饭人|干饭魂|快吃饭|吃饭了|吃了吗|白饭|饿|馋|想吃|好吃|美食|夜宵|宵夜|加餐|好饿|饿了|吃啥|吃点|吃口|吃顿|开饭)"),
    "美食": re.compile(r"(火锅|烧烤|奶茶|外卖|食堂|零食|甜点|蛋糕|冰淇淋|冰激凌|炸鸡|烤串|麻辣烫|螺蛳粉|面条|饺子|包子|馒头|炒饭|盖饭|泡面|螺蛳|卤味|甜品|夜宵摊|饭量|胃口|加饭|难吃)"),
    "深海": re.compile(r"(鲸鱼|深海|大海|海洋|海底|小鲸鱼|鲸|海豚|水母)"),
    "AI": re.compile(r"(deepseek|DeepSeek|大模型|模型|本地部署|token|API|prompt|代码|bug|报错|显卡|服务器|训练|微调)"),
}


def note_group_heat(gid: str, text: str, now: float) -> None:
    """把一条群消息按兴趣类别计入热度窗口。纯数据累积，无副作用。"""
    if not text or gid not in GROUPS:
        return
    for name, pat in _GROUP_PARTS.items():
        if pat.search(text):
            key = f"{gid}:{name}"
            q = _hot.get(key)
            if q is None:
                q = _hot[key] = deque(maxlen=32)
            # 窗口外的旧记录只管丢，不参与计数。
            while q and now - q[0][0] > HOT_WINDOW * 3:
                q.popleft()
            q.append((now, text[:60]))


def hot_weight(gid: str, name: str, now: float | None = None) -> float:
    """当前某兴趣的热度权重（1.0 = 基线，最高 HOT_CAP）。

    规则：HOT_WINDOW 内命中次数 >= HOT_HITS 才升权；
    用指数衰减（距最近命中的时间越久，越接近基线）。
    """
    now = time.time() if now is None else now
    q = _hot.get(f"{gid}:{name}")
    if not q:
        return 1.0
    recent = [ts for ts, _ in q if now - ts <= HOT_WINDOW]
    if len(recent) < HOT_HITS:
        return 1.0
    newest = max(recent)
    age = now - newest
    # age=0 时 boost=HOT_CAP；每过 HOT_WINDOW 衰减一半（约 30 分钟回 1.0）。
    boost = 1.0 + (HOT_CAP - 1.0) * 2 ** (-age / HOT_WINDOW)
    return min(HOT_CAP, max(1.0, boost))


def heat_snapshot(now: float | None = None) -> list[str]:
    """当前热度最高的 3 个兴趣名（用于注入/状态，不区分群）。"""
    now = time.time() if now is None else now
    rows = []
    for key, q in _hot.items():
        name = key.split(":", 1)[1]
        recent = [ts for ts, _ in q if now - ts <= HOT_WINDOW]
        if not recent:
            continue
        rows.append((len(recent), max(recent), name))
    if not rows:
        return []
    rows.sort(key=lambda r: (-r[0], -r[1]))
    return [name for _, _, name in rows[:3]]


# 热度快照落盘节流（秒）：给 dsh-proactive 读的权重表不必每条消息都写盘。
_PERSIST_EVERY = float(os.environ.get("DSH_INTEREST_PERSIST_EVERY", "30"))
_last_persist = 0.0


def hot_weight_table(now: float | None = None) -> dict:
    """给 dsh-proactive 用的权重表：{gid: {兴趣名: 权重}}。

    只暴露升权中的条目；没热度的兴趣由 proactive 取基线 1.0。
    **_hot 是唯一真相源**：过期条目在 hot_weight 里按时间筛选，
    不依赖状态文件回填，所以表永远反映「此刻」的实时热度。
    """
    now = time.time() if now is None else now
    out: dict[str, dict[str, float]] = {}
    for key, q in _hot.items():
        gid, name = key.split(":", 1)
        w = hot_weight(gid, name, now)
        if w <= 1.0:
            continue
        out.setdefault(gid, {})[name] = round(w, 3)
    return out


def persist_hot_snapshot(now: float | None = None) -> None:
    """把当前热度权重表写进状态文件（节流）。"""
    global _last_persist
    now = time.time() if now is None else now
    if now - _last_persist < _PERSIST_EVERY:
        return
    _last_persist = now
    _state["hot"] = hot_weight_table(now)
    _save_state()


# ------------------------------------------------------------------ 口味轮换
def maybe_swap_mood(now: float | None = None) -> None:
    """每 MOOD_HOURS 小时从池子里随机抽 1~2 个「今日馋」。"""
    now = time.time() if now is None else now
    if now - float(_state.get("last_swap", 0)) < MOOD_HOURS * 3600:
        return
    bk = list(_MOOD_POOL)
    random.shuffle(bk)
    count = 1 if len(bk) < 4 else 2 if random.random() < 0.4 else 1
    picked = bk[:count]
    _state["mood"] = {"items": picked, "since": now, "until": now + MOOD_HOURS * 3600}
    _state["last_swap"] = now
    _save_state()
    logger.info("[interest] 换今日馋：%s", "、".join(picked))


def current_mood() -> list[str]:
    items = _state.get("mood", {}).get("items", [])
    return items if isinstance(items, list) else []


def render_interest_block(now: float | None = None) -> str:
    """生成注入块的小段文字。返回空串 = 不注入。"""
    now = time.time() if now is None else now
    if not in_active_hours(now):
        return ""
    hot = heat_snapshot(now)
    mood = current_mood()
    if not hot and not mood:
        return ""
    parts = []
    if hot:
        parts.append("最近群里聊得你心痒的话题：" + "、".join(hot))
    if mood:
        parts.append("你这两天有点馋：" + "、".join(mood))
    return "<interest_state>" + "；".join(parts) + "。</interest_state>"


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        _load_from_disk_and_merge()
        maybe_swap_mood()
        logger.info(
            "[interest] 已加载：%s 影子=%s 热度窗口%.0f分钟(≥%d次升权) 口味每%.1f小时换 群=%s",
            "开" if ENABLED else "关", "开" if SHADOW else "关",
            HOT_WINDOW / 60, HOT_HITS, MOOD_HOURS,
            ",".join(sorted(GROUPS)) or "无",
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def collect_heat(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            from astrbot.core.platform.message_type import MessageType
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
            text = str(event.get_message_str() or "").strip()
            if not text or text.startswith("/"):
                return
            note_group_heat(gid, text, time.time())
            persist_hot_snapshot()
            maybe_swap_mood()
        except BaseException as exc:
            logger.debug("[interest] 热度统计失败: %s", exc)

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED or SHADOW:
            return
        try:
            block = render_interest_block()
            if not block:
                return
            req.extra_user_content_parts.append(TextPart(text=block))
            logger.info("[interest] 注入今日馋 gid=%s", event.get_group_id())
        except BaseException as exc:
            logger.warning("[interest] 注入失败，跳过: %r", exc)

    @filter.command("馋什么")
    async def status(self, event: AstrMessageEvent) -> None:
        if str(event.get_sender_id()) != OWNER:
            return
        mood = current_mood()
        hot = heat_snapshot()
        yield event.plain_result(
            "今日馋：%s\n"
            "热度榜：%s\n"
            "影子 %s｜状态文件 %s"
            % (
                "、".join(mood) if mood else "（还没换过）",
                "、".join(hot) if hot else "（暂无热度）",
                "开" if SHADOW else "关", STATE_PATH,
            )
        )