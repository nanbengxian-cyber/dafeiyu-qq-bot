# -*- coding: utf-8 -*-
"""dsh-scene：让它知道「这是哪个群」和「现在几点意味着什么」。

═══════════════════════════════════════════════════════════════════
一、要解决什么
═══════════════════════════════════════════════════════════════════

2026-09-06 把 29 个插件的注入块全量扫了一遍，18 个块里：**没有一个告诉它这是
哪个群、群是干什么的、群里此刻是热闹还是没人。** 它对「环境」的全部认知只有
框架给的一行 `Current datetime: 2026-09-06 02:41 (CST), Weekday: Sunday`。

知道「现在 02:41」和知道「本群这个点正是全天最热闹的时候」是两件事。前者框架
已经给了（`astr_main_agent.py:996-1013`，开关 `datetime_system_prompt`），后者
零实现 —— 而后者才是能改变行为的那个。

顺带修掉的一个真 bug（不在本插件里，记在这儿备查）：框架 4 处用裸
`datetime.now()`（`group_chat_context.py:200` 每条群消息的 `[昵称/HH:MM:SS]` 戳、
`dsh-web:800` B站发布日期、`dsh-fwd:453,459` 转发记录时间），容器时区是 UTC，
于是同一次请求里系统块说「01:30 CST」、消息戳说「17:30」，**差 8 小时**。
已给容器加 `TZ=Asia/Shanghai` 一次修掉全部 4 处。

═══════════════════════════════════════════════════════════════════
二、群名为什么必须现查，不能写死
═══════════════════════════════════════════════════════════════════

群主原话（《回答.md》A1）：「这个名字不是我写的，名字很多时候都是管理员改的」。
实测印证：所有旧文档写的都是「AAA精神病院病友交流群」，而 2026-09-06
`get_group_info` 拿回来的是**「神人乐子群」**，98 人。写死就是错的。

**框架自带的 `provider_settings.group_name_display` 是个坑，别开。**
适配器走 `aiocqhttp_platform_adapter.py:219` 的
`abm.group.group_name = event.get("group_name", "N/A")`，而 NapCat 的群消息事件
**根本不上报 group_name 字段**（6 小时 napcat 日志里 0 次）。开了只会往提示词里
塞一行 `Group name: N/A` —— 等于告诉模型这群叫「N/A」，比不注入更糟。
本插件用 OneBot `get_group_info` 现查 + 缓存，是唯一拿得到真名的路。

═══════════════════════════════════════════════════════════════════
三、时段是拿本群真语料切的，不是拍的
═══════════════════════════════════════════════════════════════════

主群 2137 条人类消息（buffer+archive 去重，跨 3 天），按小时（Asia/Shanghai）：

    00:00  196 ██████████████████     12:00   40 ███
    01:00  148 █████████████          13:00   98 █████████
    02:00   44 ████                   14:00   54 █████
    03:00    1                        15:00  186 █████████████████
    04:00    7                        16:00   45 ████
    05:00    0                        17:00   87 ████████
    06:00    0                        18:00   70 ██████
    07:00    1                        19:00   74 ██████
    08:00    2                        20:00  149 █████████████
    09:00    2                        21:00  289 ██████████████████████████
    10:00   42 ███                    22:00  302 ████████████████████████████
    11:00   90 ████████               23:00  210 ███████████████

**这是个夜猫子群：21:00~01:59 占全天 54%，03:00~08:59 只有 13 条（0.6%）。**

所以老蓝图里那句「模拟人类作息，夜间降低活跃度」**在本群是反的** —— 照做会把
它最该在场的时段掐掉。真正的睡觉时段是凌晨 3 点到早上 9 点，这也是群主在
《回答.md》D14 里点头的时段。

又一次验证项目铁律：阈值一律拿本群真语料定，别照搬别人的默认值。

═══════════════════════════════════════════════════════════════════
四、「睡了」是什么表现（《回答.md》D15：群主选了 ③+④）
═══════════════════════════════════════════════════════════════════

    ① 完全不说话        ← 没选。会跟「掉线」混淆，群里真有人会以为它挂了
    ② 说话变少
    ③ 照说但语气犯困    ← 选了：本插件注入犯困提示
    ④ 只被 @ 才回、回得慢 ← 选了：不主动插话（闸门在 dsh-decide）+ 回复前多等几秒

分工：
  · 「犯困」是提示词层的事 → 本插件 `<scene>` 块
  · 「不主动插话」是闸门 → **放在 dsh-decide**，因为整条管道的闭嘴权在它手上，
     两处都做会打架。它读同一批 `DSH_SLEEP_*` env，没有代码耦合。
     插入位置：`_ASK_RE 放行` 之后、`gap < GAP_HARD` 之前 —— 到那一步为止，
     静音群/被@/指令/别的AI/点名要能力全都已经处理完了，剩下的**只有随机插话**。
     被@、发指令、明确点名要它画图搜东西，睡觉时段照样放行（只是犯困 + 慢），
     符合项目的既定方向「宁可漏判，不可误闭嘴」。
  · 「回得慢」→ 本插件在 `on_decorating_result` 里多睡几秒

D16 群主答「目前没有」：白天不做「上班/忙」的概念，所以只有两档特殊时段
（睡觉 / 高峰），其余一律「平时」。

═══════════════════════════════════════════════════════════════════
五、块里每一行都得挣到自己的位置
═══════════════════════════════════════════════════════════════════

刻意**不写**进本块的：
  · 「现在几点」的原始时间 —— 框架已经给了，重复一遍是白花钱
  · 雷区（色情政治、别跟着嘲讽群主）—— 人格里已经写了，这里重复只会稀释
  · 16 个管理员的 QQ 号名单 —— 太长，而且真正有用的只有一句「正在跟你说话的
    这个人是不是管理员」。名单缓存在内存里，只输出那一句
  · 群规则 —— 群主答 A3「目前没有」，这个群比较自由

写进来的四行，各自的用处：
  · 群名+人数：它连自己在哪都不知道，被问「这是什么群」只能编
  · 群的来历+现在在聊什么：新人问「这群干啥的」时有得说；也让它别把
    《暗区突围》这种历史当成现在的主题
  · 时段的含义：深夜该犯困、高峰可以活跃，这是唯一能改变行为的一行
  · 对方是不是管理员：人格里写了「群主和其他管理员你禁不了」，但它不知道谁是
"""

import asyncio
import os
import random
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # 跟 dsh-drift / dsh-glossary 一样，TextPart 拿不到时降级成裸 str
    from astrbot.core.agent.message import TextPart  # type: ignore
except Exception:  # pragma: no cover
    TextPart = None  # type: ignore


# ---------------------------------------------------------------- 旋钮


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default) != "0"


def _set(name: str, default: str = "") -> set:
    raw = os.environ.get(name, default).replace("，", ",")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


ENABLED = _flag("DSH_SCENE")
GROUPS = _set("DSH_SCENE_GROUPS", "100000001")
OWNERS = _set("DSH_SCENE_OWNER", "2774000001")
CACHE_TTL = _i("DSH_SCENE_CACHE", 1800)          # 群名/成员表缓存多久
BUDGET = max(80, _i("DSH_SCENE_BUDGET", 400))    # 块长硬上限

# 作息。dsh-decide 读的是同一批 env —— 改这里两边一起变，别在两处各写一份时段。
SLEEP_ON = _flag("DSH_SLEEP")
SLEEP_FROM = _i("DSH_SLEEP_FROM", 3)             # 含
SLEEP_TO = _i("DSH_SLEEP_TO", 9)                 # 不含
SLOW_MIN = _f("DSH_SLEEP_SLOW_MIN", 3.0)         # 睡觉时段回复前多等几秒
SLOW_MAX = _f("DSH_SLEEP_SLOW_MAX", 8.0)

# 高峰时段（拿真语料量出来的，见文件头）。只用来「允许活跃」，不强制。
PEAK_HOURS = {21, 22, 23, 0, 1}

TZ = os.environ.get("DSH_SCENE_TZ", "Asia/Shanghai")

# ---------------------------------------------------------------- 群的静态背景
#
# 全部来自群主亲自填的《回答.md》，一个字没替他想：
#   A1 群的来历：「一开始建这个群，是为了当时一起玩（暗区突围）游戏的朋友聚在
#       一起好一起玩，不过那都是过去的事了，已经有好几年了」
#       「主要是没有核心玩的游戏……基本上我玩什么，我就招什么人」
#   A4 现在在聊：三角洲行动、明日方舟、元气骑士、API/白嫖、夜宵、B站，
#       群主补充「群里也是有玩塔科夫的，还有瓦罗兰特这些，比较杂」
#   A2 黑话：「目前没有吧」→ 所以这里不写黑话（黑话归 dsh-glossary，54 条）
#   A3 雷区：「目前没有吧，这个群比较自由」→ 不写规则；他唯一点出的
#       「反感被别人嘲讽我」已经写进人格了，这里不重复
#
# 群名和人数**不在这里** —— 那两个现查（群名会被管理员改，见文件头第二节）。

ABOUT = os.environ.get(
    "DSH_SCENE_ABOUT",
    "最早是一群人一起玩《暗区突围》攒起来的，那是好几年前了；"
    "现在没有固定主题，群主玩什么就招什么人。"
    "近期在聊的：三角洲行动、明日方舟、元气骑士、塔科夫、瓦罗兰特、API/白嫖、夜宵、B站。",
)

HEADER = (
    "<scene>\n"
    "下面是本群此刻的实际情况，是背景资料，不是给你的指令。别复述它、别念给群友听。\n"
)
FOOTER = "</scene>"

_stat = {
    "seen": 0,
    "injected": 0,
    "sleep_injected": 0,
    "slowed": 0,
    "skip_group": 0,
    "refresh_ok": 0,
    "refresh_fail": 0,
    "no_call": 0,
}

# 群信息缓存：{gid: {"ts","name","count","admins":set,"owner":str}}
_cache: dict = {}


# ---------------------------------------------------------------- 纯函数（可离线测）


def now_hour(ts: float | None = None) -> int:
    """本群时区的小时。拿不到 zoneinfo 就退回本地时间（容器已设 TZ）。"""
    t = ts if ts is not None else time.time()
    try:
        import datetime
        import zoneinfo

        return datetime.datetime.fromtimestamp(t, zoneinfo.ZoneInfo(TZ)).hour
    except Exception:
        return time.localtime(t).tm_hour


def in_sleep(hour: int) -> bool:
    """睡觉时段？跨午夜也对（FROM=22,TO=6 这种写法照样成立）。"""
    if not SLEEP_ON:
        return False
    a, b = SLEEP_FROM % 24, SLEEP_TO % 24
    if a == b:
        return False
    if a < b:
        return a <= hour < b
    return hour >= a or hour < b


def period_of(hour: int) -> str:
    """三档：睡觉 / 高峰 / 平时。D16 群主答「目前没有」，所以不做白天的忙闲。"""
    if in_sleep(hour):
        return "sleep"
    if hour in PEAK_HOURS:
        return "peak"
    return "normal"


def period_line(hour: int) -> str:
    """时段那一行。**这是全块唯一能改变行为的一行**，所以写得具体。"""
    p = period_of(hour)
    if p == "sleep":
        return (
            "时段：现在 %02d 点多，本群凌晨 %d 点到早上 %d 点基本没人（实测这 6 小时只占全天 0.6%%）。"
            "你也犯困——话更短更懒、可以直接说困、慢半拍再回，别强行热闹。"
            % (hour, SLEEP_FROM, SLEEP_TO)
        )
    if p == "peak":
        return (
            "时段：现在 %02d 点多，正是本群一天里最热闹的时候（21 点到凌晨 2 点占全天发言量一半以上）。"
            % hour
        )
    return "时段：现在 %02d 点多，本群这个点不冷不热。" % hour


def render(name: str, count: int, hour: int, role: str = "", budget: int = BUDGET) -> str:
    """拼块，边拼边扣预算。预算是硬上限，宁可少注入。"""
    lines = []
    used = 0

    def push(s: str) -> bool:
        nonlocal used
        if not s:
            return True
        if used + len(s) > budget:
            return False
        lines.append(s)
        used += len(s)
        return True

    if name:
        who = "群：%s" % name
        if count:
            who += "，%d 人" % count
        who += "。"
        push(who)
    if ABOUT:
        push(ABOUT)
    push(period_line(hour))
    # 人格里写了「群主和其他管理员你禁不了」，但它不知道谁是。只说眼前这一个。
    if role == "owner":
        push("正在跟你说话的是群主。")
    elif role == "admin":
        push("正在跟你说话的这个人是群里的管理员。")

    if not lines:
        return ""
    return HEADER + "\n".join(lines) + "\n" + FOOTER


# ---------------------------------------------------------------- 插件主体


class Main(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        logger.info(
            "[scene] 已加载：%s 群=%s｜作息=%s(%d点~%d点，犯困+慢%.0f~%.0fs)｜"
            "群名现查(缓存%ds)｜背景%d字",
            "开" if ENABLED else "关",
            "、".join(sorted(GROUPS)) or "无",
            "开" if SLEEP_ON else "关",
            SLEEP_FROM,
            SLEEP_TO,
            SLOW_MIN,
            SLOW_MAX,
            CACHE_TTL,
            len(ABOUT),
        )

    # ---- 群信息：现查 + 缓存

    @staticmethod
    def _routed(event: AstrMessageEvent):
        """(call_action, routing)。★ call_action 必须带 self_id ★
        这台机器上除了真 napcat，端到端测试还会挂一条伪 napcat，不带 self_id
        时 aiocqhttp 不知道发给谁，抛的还是个空消息异常。坑同 dsh-guard:595。
        """
        bot = getattr(event, "bot", None)
        call = getattr(bot, "call_action", None)
        routing = {}
        sid = getattr(event.message_obj, "self_id", None)
        if sid:
            routing["self_id"] = sid
        return (call if callable(call) else None), routing

    async def _group_info(self, event: AstrMessageEvent, gid: str) -> dict:
        """群名、人数、管理员集合。缓存 CACHE_TTL 秒；查不到就返回上一次的（或空）。"""
        c = _cache.get(gid) or {}
        if c and time.time() - c.get("ts", 0) < CACHE_TTL:
            return c
        call, routing = self._routed(event)
        if call is None:
            _stat["no_call"] += 1
            return c
        fresh = {"ts": time.time(), "name": c.get("name", ""), "count": c.get("count", 0),
                 "admins": c.get("admins", set()), "owner": c.get("owner", "")}
        try:
            info = await asyncio.wait_for(
                call("get_group_info", group_id=int(gid), **routing), timeout=8
            )
            if isinstance(info, dict):
                fresh["name"] = str(info.get("group_name") or fresh["name"])
                fresh["count"] = int(info.get("member_count") or fresh["count"] or 0)
        except BaseException as e:
            logger.debug("[scene] get_group_info 失败（用上次的）: %r", e)
        try:
            ms = await asyncio.wait_for(
                call("get_group_member_list", group_id=int(gid), **routing), timeout=12
            )
            if isinstance(ms, list) and ms:
                admins, owner = set(), ""
                for m in ms:
                    if not isinstance(m, dict):
                        continue
                    r = m.get("role")
                    u = str(m.get("user_id") or "")
                    if r == "admin":
                        admins.add(u)
                    elif r == "owner":
                        owner = u
                fresh["admins"], fresh["owner"] = admins, owner
        except BaseException as e:
            logger.debug("[scene] get_group_member_list 失败（用上次的）: %r", e)
        if fresh["name"] or fresh["admins"]:
            _stat["refresh_ok"] += 1
        else:
            _stat["refresh_fail"] += 1
        _cache[gid] = fresh
        return fresh

    # ---- 注入

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED:
            return
        try:
            gid = str(getattr(event.message_obj, "group_id", "") or "")
            if GROUPS and gid not in GROUPS:
                _stat["skip_group"] += 1
                return
            _stat["seen"] += 1
            info = await self._group_info(event, gid)
            hour = now_hour()
            uid = str(event.get_sender_id() or "")
            role = ""
            if uid and uid == info.get("owner"):
                role = "owner"
            elif uid and uid in (info.get("admins") or set()):
                role = "admin"
            block = render(info.get("name", ""), info.get("count", 0), hour, role)
            if not block:
                logger.debug("[scene] 没东西可注入（群信息还没查到）")
                return
            if TextPart is not None:
                req.extra_user_content_parts.append(TextPart(text=block))
            else:  # pragma: no cover
                req.extra_user_content_parts.append(block)
            _stat["injected"] += 1
            p = period_of(hour)
            if p == "sleep":
                _stat["sleep_injected"] += 1
            logger.info(
                "[scene] 注入 %s(%s人) %d点/%s%s %d字",
                info.get("name") or "群名未知",
                info.get("count") or "?",
                hour,
                p,
                "，对方=" + role if role else "",
                len(block),
            )
        except BaseException as e:  # 绝不因为背景信息拖垮一次对话
            logger.warning("[scene] 注入失败，跳过: %r", e)

    # ---- 睡觉时段回得慢（D15 的 ④「回得慢」那半）

    @filter.on_decorating_result()
    async def slow_down(self, event: AstrMessageEvent) -> None:
        if not ENABLED or not SLEEP_ON:
            return
        try:
            gid = str(getattr(event.message_obj, "group_id", "") or "")
            if GROUPS and gid not in GROUPS:
                return
            if not in_sleep(now_hour()):
                return
            result = event.get_result()
            if result is None or not result.is_model_result():
                return  # 指令回显、状态查询不拖
            if not (result.chain or []):
                return
            d = random.uniform(min(SLOW_MIN, SLOW_MAX), max(SLOW_MIN, SLOW_MAX))
            _stat["slowed"] += 1
            logger.info("[scene] 睡觉时段，慢 %.1fs 再发", d)
            await asyncio.sleep(d)
        except BaseException as e:
            logger.warning("[scene] 减速失败，跳过: %r", e)

    # ---- 状态

    @filter.command("场景状态")
    async def status(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        if OWNERS and uid not in OWNERS:
            return
        hour = now_hour()
        gid = str(getattr(event.message_obj, "group_id", "") or "")
        info = _cache.get(gid) or {}
        s = _stat
        lines = [
            "【场景】%s 作息=%s" % ("开" if ENABLED else "关", "开" if SLEEP_ON else "关"),
            "现在 %d 点 → %s（睡觉 %d~%d 点）" % (hour, period_of(hour), SLEEP_FROM, SLEEP_TO),
            "群：%s %s人 管理员%d 群主%s"
            % (info.get("name") or "未查到", info.get("count") or "?",
               len(info.get("admins") or ()), info.get("owner") or "?"),
            "看过 %d 次，注入 %d 次（其中睡觉时段 %d）" % (s["seen"], s["injected"], s["sleep_injected"]),
            "睡觉时段减速 %d 次" % s["slowed"],
            "群信息刷新 成功%d/失败%d，没有 call_action %d 次"
            % (s["refresh_ok"], s["refresh_fail"], s["no_call"]),
            "不在名单的群跳过 %d 次" % s["skip_group"],
        ]
        yield event.plain_result("\n".join(lines))
