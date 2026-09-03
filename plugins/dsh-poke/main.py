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
# 三、防对戳循环（三道闸门，缺一不可）
#
# 最大的风险是无限对戳：机器人回戳 → 对方回戳 → 机器人再回戳。
# QQ 对高频动作有风控，而这个账号**已经被打标**（换大陆 IP 实测无效），
# 不能再喂它理由。所以：
#   * 同人冷却 COOLDOWN 秒：同一个人连续戳只回第一下
#   * 同群 60s 内最多 GROUP_MAX 次：一群人一起戳时不刷屏
#   * 全局 60s 内最多 GLOBAL_MAX 次：多群同时被戳的总闸
# 三道闸门都是滑动窗口 deque，与 dsh-sticker 的配额同一套写法。
#
# 四、group_poke 这个 action 实测可用
#   docker exec napcat curl -X POST .../group_poke -d '{"group_id":G,"user_id":U}'
#   -> {"status":"ok","retcode":0}
# 注意它不是 OneBot v11 标准动作，是 napcat 扩展；调用失败一律吞掉不影响群聊。

import asyncio
import os
import random
import time
from collections import deque

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger
from astrbot.core.star.filter.custom_filter import CustomFilter

ENABLED = os.environ.get("DSH_POKE", "1") != "0"
# 回戳概率。1.0 = 每次都回（被戳还不理更奇怪）
BACK_RATE = float(os.environ.get("DSH_POKE_BACK_RATE", "1.0"))
# 附一句短话的概率。默认 25%：每次都说话很吵，一句不说又太机械
TALK_RATE = float(os.environ.get("DSH_POKE_TALK_RATE", "0.25"))
# 同一个人多久内只回一次
COOLDOWN = float(os.environ.get("DSH_POKE_COOLDOWN", "20"))
# 限流窗口（秒）与窗口内上限
WINDOW = float(os.environ.get("DSH_POKE_WINDOW", "60"))
GROUP_MAX = max(1, int(os.environ.get("DSH_POKE_GROUP_MAX", "3")))
GLOBAL_MAX = max(1, int(os.environ.get("DSH_POKE_GLOBAL_MAX", "6")))
# 回戳前的随机延迟：真人不会 0ms 反射
DELAY_MIN = float(os.environ.get("DSH_POKE_DELAY_MIN", "0.6"))
DELAY_MAX = float(os.environ.get("DSH_POKE_DELAY_MAX", "2.2"))
# 只在这些群生效，空 = 全部
GROUPS = {
    g.strip() for g in os.environ.get("DSH_POKE_GROUPS", "").split(",") if g.strip()
}

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
_group_hits: dict[str, deque] = {}         # gid -> 时间戳窗口
_global_hits: deque = deque()

_stat = {
    "seen": 0, "not_me": 0, "cooldown": 0, "group_limit": 0, "global_limit": 0,
    "dice": 0, "poked": 0, "talked": 0, "fail": 0, "self_poke": 0,
}
_last: list[str] = []


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
    """两级限流。返回 (是否允许, 原因)。"""
    now = time.time()
    g = _group_hits.setdefault(gid, deque())
    _prune(g, now)
    _prune(_global_hits, now)
    if len(g) >= GROUP_MAX:
        return False, "同群 %.0fs 内已回 %d 次" % (WINDOW, len(g))
    if len(_global_hits) >= GLOBAL_MAX:
        return False, "全局 %.0fs 内已回 %d 次" % (WINDOW, len(_global_hits))
    return True, ""


def _note_hit(gid: str) -> None:
    now = time.time()
    _group_hits.setdefault(gid, deque()).append(now)
    _global_hits.append(now)


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
        logger.info(
            "[poke] 已加载：%s 回戳率%.0f%% 说话率%.0f%% 同人冷却%.0fs "
            "限流%.0fs内(群%d/全局%d) 延迟%.1f~%.1fs 限定群=%s",
            "开" if ENABLED else "关", BACK_RATE * 100, TALK_RATE * 100,
            COOLDOWN, WINDOW, GROUP_MAX, GLOBAL_MAX, DELAY_MIN, DELAY_MAX,
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

            key = "%s:%s" % (gid, who)
            now = time.time()
            gap = now - _last_poke.get(key, 0.0)
            if gap < COOLDOWN:
                _stat["cooldown"] += 1
                _repeat[key] = _repeat.get(key, 0) + 1
                logger.info("[poke] %s 在 %.0fs 内又戳（第%d下），不回",
                            who, gap, _repeat[key] + 1)
                return

            allow, why = _quota(gid)
            if not allow:
                _stat["group_limit" if "同群" in why else "global_limit"] += 1
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

    async def _poke_back(self, event: AstrMessageEvent, gid: str, who: str) -> bool:
        """调 napcat 扩展动作 group_poke。失败一律吞掉，不影响群聊。

        ★ 必须带 self_id 路由 ★
        aiocqhttp 用 X-Self-ID 维护一个「多客户端」字典
        （CQHttp._add_wsr_api_client）。这台机器上除了真 napcat，端到端测试
        还会挂一条伪 napcat 连接，不带 self_id 时 call_action 不知道该发给谁，
        直接抛一个**空消息**的异常（日志里只有 "group_poke 失败: "，
        连原因都没有，极难查）。
        框架自己的 get_group() 就是这么解决的
        （aiocqhttp_message_event.py:244-252），照抄。
        这个坑在只有一条连接时不会出现 —— 只有端到端测试能发现它。
        """
        bot = getattr(event, "bot", None)
        call = getattr(bot, "call_action", None)
        if not callable(call):
            _stat["fail"] += 1
            logger.warning("[poke] 这个平台没有 call_action，回戳不了")
            return False
        routing = {}
        sid = getattr(event.message_obj, "self_id", None)
        if sid:
            routing["self_id"] = sid
        try:
            await call("group_poke", group_id=int(gid), user_id=int(who), **routing)
            _stat["poked"] += 1
            return True
        except BaseException as e:
            _stat["fail"] += 1
            logger.warning("[poke] group_poke 失败(%s): %r",
                           type(e).__name__, e)
            return False

    @filter.command("戳一戳状态")
    async def cmd_status(self, event: AstrMessageEvent):
        s = _stat
        lines = [
            "戳一戳：%s 回戳率%.0f%% 说话率%.0f%%"
            % ("开" if ENABLED else "关", BACK_RATE * 100, TALK_RATE * 100),
            "收到戳 %d 次：戳的不是我 %d｜戳我自己 %d"
            % (s["seen"], s["not_me"], s["self_poke"]),
            "没回的原因：同人冷却 %d｜同群限流 %d｜全局限流 %d｜掷骰子 %d"
            % (s["cooldown"], s["group_limit"], s["global_limit"], s["dice"]),
            "回戳成功 %d 次，其中附话 %d 次；调用失败 %d"
            % (s["poked"], s["talked"], s["fail"]),
            "闸门：同人 %.0fs 冷却｜%.0fs 内同群≤%d 全局≤%d"
            % (COOLDOWN, WINDOW, GROUP_MAX, GLOBAL_MAX),
        ]
        if _last:
            lines.append("最近几次：")
            lines += ["  " + x for x in _last[-5:]]
        yield event.plain_result("\n".join(lines))
