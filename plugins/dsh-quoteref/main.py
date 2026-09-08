# -*- coding: utf-8 -*-
"""dsh-quoteref -- 偶尔用「引用回复」而不是直接说。

---------------------------------------------------------------------------
为什么

QQ 的引用回复是真人常用但不高频的动作：话题已经翻过去了、或者一条消息同时
有好几个人在问，才会引用着答。机器人一直**一次都没用过** ——
拿本群最近 96 条实测：

    真人消息 67 条，其中带引用的 4 条（6.0%），来自 3 个不同的人
    机器人带引用的 0 条

所以这是一条只缺不多的真人动作。

---------------------------------------------------------------------------
频率怎么定

按「每 N 次**提问式的 @**，才引用一次」计数，不是按概率 —— 概率会扎堆，
而计数天然是均匀的。

默认 **N=4**：主群高峰期大概每小时 5~8 次提问式 @，也就是一小时引用一两次，
占它全部发言的 4~5%，**刚好压在真人 6.0% 下面一点**。
外加 240 秒冷却，防止一串连问把引用挤在一起。

嫌少就把 `DSH_QUOTEREF_EVERY` 调成 3（正好等于真人频率），嫌多调 6。

---------------------------------------------------------------------------
引用了就不要再 @

真人是「引用」或「@」二选一，不会既引用又艾特 —— 那是机器人味。
所以挂上 Reply 的同时把 At 摘掉（`dsh-mention` 可能刚加上），
连它为了防「@昵称正文」粘住而补的那个前导空格也一起收拾干净。

这不是把 `dsh-mention` 的判断作废：它决定「这条需要让对方注意到」，
引用回复把这件事做得更好 —— 引用自带「我在回你哪句」，比一个 @ 信息量大。

---------------------------------------------------------------------------
框架已经替我们处理好的一件事

不用担心「拆成三条会不会每条都带引用」。`respond/stage.py:259` 在分段发送前
会把 `Reply` 和 `At` 抽成 header，`header_comps.clear()` 之后只跟着**第一条**发：

    header_comps = self._extract_comp(chain, {ComponentType.Reply, ComponentType.At}, ...)
    for comp in result.chain:
        await event.send(result.derive([*header_comps, comp]))
        header_comps.clear()

所以 Reply 加在链里哪个位置都行，一定只出现一次。
"""

import os
import re
import time

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.core import logger


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_QUOTEREF")
GROUPS = _set("DSH_QUOTEREF_GROUPS", "100000001")
# 每 N 次提问式的 @ 引用一次。真人频率 6.0%，N=4 约 4~5%，压在下面一点。
EVERY = max(2, int(os.environ.get("DSH_QUOTEREF_EVERY", "4")))
# 两次引用之间至少隔这么久，防止一串连问把引用挤在一起
COOLDOWN = max(0.0, float(os.environ.get("DSH_QUOTEREF_COOLDOWN", "240")))
# 引用时把 At 摘掉（真人不会既引用又艾特）
DROP_AT = _flag("DSH_QUOTEREF_DROP_AT")
OWNERS = _set("DSH_QUOTEREF_OWNER", "2774000001")

# 判「像不像提问」。与 dsh-drift 用同一套，已经拿真语料校准过：
# 句末只把「吗/呢」当疑问助词，**「吧」不算**（多为软化或推测，
# 「这程度真的离谱了吧」不是在问人）；补了「咋」「干嘛」「多久」。
_QUESTION_RE = re.compile(
    r"[?？]"
    r"|(?:怎么|怎样|咋|如何|为什么|为啥|干嘛|啥时候|多少|多久|哪个|哪里|哪儿"
    r"|是不是|有没有|能不能|可不可以)"
    r"|(?:吗|呢)\s*$"
)


def looks_question(text: str) -> bool:
    return bool(_QUESTION_RE.search((text or "").strip()))


def should_quote(count: int, now: float, last: float,
                 every: int = None, cooldown: float = None) -> tuple[bool, str]:
    """第 count 次提问式 @ 该不该引用。纯函数，可离线回测。

    count 从 1 开始。返回 (要不要引用, 原因)。
    """
    every = EVERY if every is None else every
    cooldown = COOLDOWN if cooldown is None else cooldown
    if count % every != 0:
        return False, "第%d次，还没到第%d次" % (count, every)
    if last and now - last < cooldown:
        return False, "距上次引用只过了%.0fs（<%.0fs 冷却）" % (now - last, cooldown)
    return True, "第%d次提问式@，引用" % count


_stat = {"addressed": 0, "question": 0, "quoted": 0, "not_yet": 0, "cooling": 0,
         "no_msgid": 0, "already": 0, "skip_group": 0, "not_model": 0, "dropped_at": 0}
_last: list[str] = []


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._count: dict[str, int] = {}
        self._last_ts: dict[str, float] = {}
        logger.info(
            "[quoteref] 已加载：%s 群=%s 每%d次提问式@引用一次 冷却%.0fs 引用时摘@=%s"
            "（真人实测 6.0%%，这样约 4~5%%）",
            "开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无",
            EVERY, COOLDOWN, "开" if DROP_AT else "关",
        )

    @filter.on_decorating_result()
    async def maybe_quote(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or gid not in GROUPS:
                _stat["skip_group"] += 1
                return
            result = event.get_result()
            if result is None or not result.chain:
                return
            # 只在模型生成的回复上引用。指令回显引用着发很怪。
            try:
                if not result.is_model_result():
                    _stat["not_model"] += 1
                    return
            except BaseException:
                pass

            # 只数「提问式的 @」：被点名了、而且那句确实在问东西
            if not getattr(event, "is_at_or_wake_command", False):
                return
            _stat["addressed"] += 1
            asked = (event.get_message_str() or "").strip()
            if not looks_question(asked):
                logger.debug("[quoteref] 被@但不像提问，不计数：%s", asked[:30])
                return
            _stat["question"] += 1

            n = self._count.get(gid, 0) + 1
            self._count[gid] = n
            now = time.time()
            ok, why = should_quote(n, now, self._last_ts.get(gid, 0.0))
            if not ok:
                _stat["cooling" if "冷却" in why else "not_yet"] += 1
                logger.debug("[quoteref] 不引用：%s", why)
                return

            mid = getattr(getattr(event, "message_obj", None), "message_id", None)
            if not mid:
                _stat["no_msgid"] += 1
                logger.info("[quoteref] 拿不到被引用的消息 id，这次算了")
                return
            if any(isinstance(c, Reply) for c in result.chain):
                _stat["already"] += 1
                return

            # 引用了就不要再 @（真人二选一）。顺手收掉 dsh-mention 为了防粘
            # 补的那个前导空格，否则引用回复会以一个空格开头。
            if DROP_AT:
                had_at = any(isinstance(c, At) for c in result.chain)
                if had_at:
                    result.chain[:] = [c for c in result.chain if not isinstance(c, At)]
                    for c in result.chain:
                        if isinstance(c, Plain) and c.text and c.text[:1] == " ":
                            c.text = c.text.lstrip(" ")
                            break
                    _stat["dropped_at"] += 1

            result.chain.insert(0, Reply(id=mid))
            self._last_ts[gid] = now
            _stat["quoted"] += 1
            brief = "%s｜问的是：%s" % (why, asked[:28])
            _last.append(time.strftime("%H:%M:%S ") + brief)
            del _last[:-8]
            logger.info("[quoteref] 引用回复 msg_id=%s｜%s", mid, brief)
        except BaseException as exc:
            # 引用纯属锦上添花，出任何问题都不许挡住消息
            logger.warning("[quoteref] 加引用失败，按普通回复发: %r", exc)

    @filter.command("引用回复状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        s = _stat
        gid = str(event.get_group_id() or "")
        n = self._count.get(gid, 0)
        yield event.plain_result(
            "引用回复：%s｜群：%s｜每 %d 次提问式@引用一次｜冷却 %.0fs\n"
            "被@ %d 次，其中像提问 %d 次（本群已数到第 %d 次，还差 %d 次）\n"
            "已引用 %d 次｜没到次数 %d｜在冷却 %d｜拿不到消息id %d｜引用时摘掉@ %d 次\n"
            "参考：本群真人带引用的消息占 6.0%%（96 条里 4 条，3 个人在用）\n"
            "最近：%s"
            % ("开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无", EVERY, COOLDOWN,
               s["addressed"], s["question"], n, (EVERY - n % EVERY) % EVERY or EVERY,
               s["quoted"], s["not_yet"], s["cooling"], s["no_msgid"], s["dropped_at"],
               "｜".join(_last[-3:]) or "还没有")
        )
