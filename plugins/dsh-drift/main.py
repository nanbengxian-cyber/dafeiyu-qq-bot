# -*- coding: utf-8 -*-
"""dsh-drift -- 允许它像真人一样被支线勾走一下（注意力漂移）。

---------------------------------------------------------------------------
为什么要这个

真人在群里不是一问一答的机器：别人提到一个细节，他会顺着那个细节歪一句，
再绕回来。AI 的典型形状恰恰相反 —— 永远严格贴着上一条消息回答，一句多余的
联想都没有。这种「过度切题」本身就是一种 AI 味。

抄的是 MaiBot（github.com/Mai-with-u/MaiBot，5882 星，设计原则「最像而不是
好」）的 src/maisaka/attention_drift.py：它把漂移写成**分档的提示词**，
而不是让模型自由发散。

---------------------------------------------------------------------------
两条硬边界（这个插件最重要的部分）

本项目专门修过「答非所问」这个 bug（详见 dsh-ctxclean：根因是注入块堆进
历史，不是模型想跑题）。现在主动加漂移，必须防止把那个抱怨重新引回来。
所以定了两条**结构性**边界，不是靠提示词自觉：

  1. **只在「自己插话」的轮次漂移，被 @ 时绝不漂移。**
     被点名了还去接支线，那就是字面意义上的答非所问。
  2. **当前消息像在提问时不漂移。**
     哪怕没 @ 谁，群里抛出来的问题也是等人答的；用联想去回答问题
     跟第 1 条是同一类错。

再加两道量上的保险：按 RATE 概率触发（不是每轮都塞，否则「每次都联想」
成了新的机器规律），以及只在主群生效（两个语料群只收不说，不碰）。

档位默认最轻的 subtle。要更野可以调 DSH_DRIFT_LEVEL，但 wild 那档没做 ——
在真群里放开到「明显跑题」不值得赌。

---------------------------------------------------------------------------
注入形状（沿用项目既有约定，别自创）

  * 走 req.extra_user_content_parts.append(TextPart(...))，不碰 system_prompt
    （人格已 3644 字，且 system_prompt 每会话缓存）。
  * 块名 <attention_drift>，全小写+下划线 —— dsh-ctxclean 的
    _TAG_RE = ^\\s*<([a-z][a-z0-9_]*)> 会把历史里的旧块结构性清掉，
    不会重演「注入块堆进 conversations.content」那个坑。
  * 不触发时打日志（六个布尔），否则排查只能靠猜 —— 这条教训在
    dsh-imagegen 上踩了三次。
"""

import os
import random
import re

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_DRIFT")
# 只在主群。两个语料群只收不说，任何新插件都不该改变它们的行为。
GROUPS = _set("DSH_DRIFT_GROUPS", "100000001")
# 触发概率。不设成 1 是因为「每轮都联想」本身就是新的机器规律。
RATE = min(1.0, max(0.0, float(os.environ.get("DSH_DRIFT_RATE", "0.35"))))
LEVEL = os.environ.get("DSH_DRIFT_LEVEL", "subtle").strip().lower()
# 太短的消息没有可漂移的信息量（「典」「666」），塞了也白塞。
MIN_LEN = max(1, int(os.environ.get("DSH_DRIFT_MIN_LEN", "6")))
OWNERS = _set("DSH_DRIFT_OWNER", "2774000001")

# 提问判据：问号、疑问词、句末疑问助词。宁可多认几句是提问（漏漂移无害），
# 也不要把真提问当成可以联想的闲聊（那就是答非所问）。
# 句末只收「吗/呢」不收「吧」：吗/呢 是疑问助词，而「吧」多数时候只是软化或
# 表推测（「就这样吧」「真的离谱了吧」），把它当提问就会误杀正常闲聊 ——
# 拿主群 1964 条真语料实测，收「吧」时误判成提问的多了 60 条（18.3%→15.2%），
# 而那些句子没有一句是在问人。
_QUESTION_RE = re.compile(
    r"[?？]"
    r"|(?:怎么|怎样|咋|如何|为什么|为啥|干嘛|啥时候|多少|多久|哪个|哪里|哪儿"
    r"|是不是|有没有|能不能|可不可以)"
    r"|(?:吗|呢)\s*$"
)

# 每档都自带 MaiBot 那句约束：漂移可以，但回复仍要短、要能被最近消息解释。
_GUARD = "回复仍然要短、要清楚，要能从最近的消息里看出你为什么这么说。不要为了跑题而跑题，也不要解释自己在联想。"
LEVELS = {
    "subtle": "这轮可以顺着最近消息里一个很自然的引子，轻轻联想一句；"
              "大多数时候还是接着当前话题说。" + _GUARD,
    "active": "这轮可以主动抓住最近消息里新鲜、好笑或反差强的细节接话，"
              "不用死守当前话题。" + _GUARD,
    "scattered": "这轮允许被支线明显勾走：先接住那个细节，再回到正题。"
                 "可以出现一次能被最近消息解释的突然拐弯。" + _GUARD,
}
if LEVEL not in LEVELS:
    LEVEL = "subtle"


def render(level: str = None) -> str:
    return "\n".join((
        "<attention_drift>",
        LEVELS[level or LEVEL],
        "</attention_drift>",
    ))


def should_drift(msg: str, addressed: bool, roll: float,
                 rate: float = None, min_len: int = None) -> tuple[bool, str]:
    """要不要漂移。纯函数，可离线回测；返回 (结论, 不漂移的原因)。"""
    rate = RATE if rate is None else rate
    min_len = MIN_LEN if min_len is None else min_len
    text = (msg or "").strip()
    if addressed:
        return False, "被点名"
    if len(text) < min_len:
        return False, "消息太短(%d<%d)" % (len(text), min_len)
    if _QUESTION_RE.search(text):
        return False, "像在提问"
    if roll >= rate:
        return False, "没摇中(%.2f>=%.2f)" % (roll, rate)
    return True, ""


_stat = {"seen": 0, "drifted": 0, "addressed": 0, "short": 0,
         "question": 0, "unlucky": 0, "skip_group": 0}


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[drift] 已加载：%s 群=%s 档位=%s 概率=%.0f%% 最短%d字"
            "（被@不漂移、像提问不漂移）",
            "开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无",
            LEVEL, RATE * 100, MIN_LEN,
        )

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or gid not in GROUPS:
                _stat["skip_group"] += 1
                return
            _stat["seen"] += 1
            addressed = bool(getattr(event, "is_at_or_wake_command", False))
            msg = event.message_str or ""
            ok, why = should_drift(msg, addressed, random.random())
            if not ok:
                # 不触发也要能看见，否则排查只能靠猜
                key = {"被点名": "addressed", "像在提问": "question"}.get(why)
                if key is None:
                    key = "short" if why.startswith("消息太短") else "unlucky"
                _stat[key] += 1
                logger.debug("[drift] 不漂移（%s）gid=%s", why, gid)
                return
            req.extra_user_content_parts.append(TextPart(text=render()))
            _stat["drifted"] += 1
            logger.info("[drift] 注入漂移档位 %s gid=%s", LEVEL, gid)
        except BaseException as exc:
            # 漂移只是锦上添花，出任何问题都不许影响正常回复
            logger.warning("[drift] 注入失败，跳过: %r", exc)

    @filter.command("漂移状态")
    async def status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        s = _stat
        rate = s["drifted"] / max(1, s["seen"]) * 100
        yield event.plain_result(
            "注意力漂移：%s｜群：%s｜档位 %s｜概率 %.0f%%\n"
            "本次启动后过了 %d 轮，漂移 %d 轮（%.0f%%）\n"
            "没漂移的原因：被点名 %d｜像提问 %d｜太短 %d｜没摇中 %d"
            % ("开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无",
               LEVEL, RATE * 100, s["seen"], s["drifted"], rate,
               s["addressed"], s["question"], s["short"], s["unlucky"])
        )
