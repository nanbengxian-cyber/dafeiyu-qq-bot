# -*- coding: utf-8 -*-
"""dsh-noise -- 换单字 / 说一半 / 叠词：给回复加一点真人「噪点」。

---------------------------------------------------------------------------
为什么

dsh-human 把句子形状改成人样（拆句、去句号），dsh-humanizer 把 AI 腔剥掉
（strong/weak 特征），dsh-style 让长度贴本群真人分布。但真人聊天还有一层
「噪声」是这些插件都没碰的：**不是每条接话都接得漂亮**。

拿真群拉回来的消息对照，机器人跟真人最扎眼的差距不是用词，是**稳定度**：
真人偶尔会：
  · 突然只回一个单字（「乐」「确实」「？」，1~2 字占真人 11.1%）
  · 话说到一半就不说了（被别的事勾走/觉得没意思，直接丢半句）
  · 顺手叠个词（「笑死 笑死」「懂了懂了」，打字比思维快的手癖）

机器人从来不会这样 —— 每条都完整、工整、有始有终，这本身就是「AI 味」。
本插件在发送前按**低概率**随机注入这三种噪点，让它的接话偶尔走神。

原则（宁缺毋滥）：
  · 只改模型生成的纯文本，指令回显/图片/语音一律不碰；
  · 只发生在「没人叫、自己接话」的回复上 —— 被 @ 的正经回答绝不加噪点
    （有人点名问你事，还给半句就太不礼貌了）；
  · 每个噪点都是低概率、可独立开关；触发后发一条日志，防不可解释；
  · 任何异常放行（绝不因自己出错挡掉正常回复）。

旋钮（env）：
  DSH_NOISE                总开关（默认 1=开）
  DSH_NOISE_GROUPS         作用群（默认 100000001），逗号分隔
  DSH_NOISE_SWAP_RATE      应答词换单字概率（默认 0.12）
  DSH_NOISE_TRUNC_RATE     截断说一半概率（默认 0.07）
  DSH_NOISE_DOUBLE_RATE    叠词概率（默认 0.06）

命令：/噪点状态
"""

import os
import random
import re

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger

ENABLED = os.environ.get("DSH_NOISE", "1").lower() not in {"0", "false", "off"}
GROUPS = {
    g.strip()
    for g in os.environ.get("DSH_NOISE_GROUPS", "100000001").split(",")
    if g.strip()
}
SWAP_RATE = min(1.0, max(0.0, float(os.environ.get("DSH_NOISE_SWAP_RATE", "0.12"))))
TRUNC_RATE = min(1.0, max(0.0, float(os.environ.get("DSH_NOISE_TRUNC_RATE", "0.07"))))
DOUBLE_RATE = min(1.0, max(0.0, float(os.environ.get("DSH_NOISE_DOUBLE_RATE", "0.06"))))

# ---- 噪点 1：应答词换单字 ----------------------------------------------
# 仅当**整条**就是一个应答词/短应时才有资格换 —— 语义等价、只换随性程度，
# 绝不把「明天爬山吗」这种真问题糊成「确实」。
_ACK_RE = re.compile(
    r"^(?:嗯|嗯嗯|哦|哦哦|噢|好|好好|好嘞|行|行吧|成|可以|对|对哦|确实|"
    r"哈哈|哈哈哈|笑死|乐|6|草|典|难绷|真的|啊这|懂了|明白|收到|"
    r"不是|没有|没事|算了|得了|好吧|牛|绝了|离谱|绷不住了|有道理|支持)[！!~～…。]?$"
)
# 单字/短应池：同一意思的不同随性说法。换的时候不保证「严格同义」，
# 因为应答词之间本来就暧昧（「行」≈「好」≈「可以」），换错一个也不伤人。
_ACK_POOL = [
    "嗯", "哦", "好", "行", "可以", "对", "确实", "哈哈", "笑死",
    "乐", "6", "草", "典", "难绷", "啊这", "懂了", "不是", "离谱",
    "牛", "绝了", "绷不住了", "支持", "有道理", "真的",
]

# ---- 噪点 2：截断说一半 ------------------------------------------------
# 长句在第一个「。！？…」或「，」处截断，只发前半（后半丢掉），
# 像真人聊着聊着被别的勾走、或者觉得话没意思直接不说完。
# 前半至少要 ≥6 字、且不落在半拉字词上，否则不动。
_TRUNC_CUT = re.compile(r"[。！？…~～]|，|,")
_TRUNC_MIN_LEFT = 6
_TRUNC_MIN_LEN = 14

# ---- 噪点 3：整条叠词 ----------------------------------------------------
# 只对 ≤6 字的中短句做（见 maybe_double），叠完 ≤14 字。
_DOUBLE_MAX_LEN = 6

# 出现这些就整条不动：链接/贴纸标记/CQ 码/换行/千分位数字 ——
# 噪点绝不能被当成对内容的破坏。
_SKIP_RE = re.compile(
    r"https?://|www\.|\[贴纸[:：]|\[CQ:"
    r"|```|\n"
    r"|\d[,，]\d"
)

_stat = {"seen": 0, "swap": 0, "trunc": 0, "double": 0, "skip_at": 0,
         "skip_shape": 0, "skip_roll": 0}


def maybe_swap(text: str, roll: float) -> str | None:
    """噪点 1：应答词换单字。能换就返回新文本，不能换返回 None。纯函数。"""
    if not _ACK_RE.match(text.strip()):
        return None
    if roll >= SWAP_RATE:
        return None
    picks = [w for w in _ACK_POOL if w != text.strip().rstrip("！!~～…。")]
    if not picks:
        return None
    return random.choice(picks)


def maybe_truncate(text: str, roll: float) -> str | None:
    """噪点 2：截断说一半。能截返回前半，不能返回 None。纯函数。"""
    t = text.rstrip("。！!～~… ")
    if len(t) < _TRUNC_MIN_LEN:
        return None
    if roll >= TRUNC_RATE:
        return None
    m = _TRUNC_CUT.search(t)
    if not m or m.start() < _TRUNC_MIN_LEFT:
        return None
    left = t[: m.start()].strip()
    if len(left) < _TRUNC_MIN_LEFT:
        return None
    return left


def maybe_double(text: str, roll: float) -> str | None:
    """噪点 3：整条短句直接重复 —— 真人「卡了/太激动」的手癖重发。

    「笑死」→「笑死笑死」「懂了」→「懂了懂了」「笑死我了」→「笑死我了笑死我了」。
    只对整条 2~6 字的中短句做，重复后 ≤12 字；长句重复不像人、反而像 bug。
    """
    t = text.strip()
    core = t.rstrip("！!~～…。")
    if not (2 <= len(core) <= 6):
        return None
    if roll >= DOUBLE_RATE:
        return None
    if core == t:
        return core + core
    # 带一个感叹/语气标点的话，把标点留在最后：「笑死了！」→「笑死了笑死了！」
    tail = t[len(core):]
    out = core + core + tail
    if len(out) > 14:
        return None
    return out


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[noise] 已加载：%s 群=%s｜换单字%.0f%% 截断%.0f%% 叠词%.0f%%（只对没被@的接话生效）",
            "开" if ENABLED else "关",
            "，".join(sorted(GROUPS)) or "无",
            SWAP_RATE * 100, TRUNC_RATE * 100, DOUBLE_RATE * 100,
        )

    def _in_group(self, event: AstrMessageEvent) -> bool:
        if not GROUPS:
            return True
        try:
            g = str(event.get_group_id() or "")
            return not g or g in GROUPS
        except BaseException:
            return True

    # priority=100：统一去AI味管线最后一步 —— 加噪点（最后一哆嗦）。
    # 固定排在最末（aiflavour 400 → humanizer 300 → typo 200 → noise 100）。
    @filter.on_decorating_result(priority=100)
    async def add_noise(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            if not self._in_group(event):
                return
            # 被 @ 了就是有人点名要正经回答，绝不给噪点
            if bool(getattr(event, "is_at_or_wake_command", False)):
                _stat["skip_at"] += 1
                return
            result = event.get_result()
            if result is None or not result.chain:
                return
            try:
                if not result.is_model_result():
                    return
            except BaseException:
                pass

            new_chain = []
            changed = False
            for comp in result.chain:
                if not isinstance(comp, Plain):
                    new_chain.append(comp)
                    continue
                text = comp.text or ""
                if not text.strip():
                    new_chain.append(comp)
                    continue
                if _SKIP_RE.search(text):
                    new_chain.append(comp)
                    continue
                _stat["seen"] += 1
                lead = text[: len(text) - len(text.lstrip())]
                core = text.strip()
                out, kind = None, None
                r = random.random()
                s = maybe_swap(core, r)
                if s is not None:
                    out, kind = s, "swap"
                else:
                    t2 = maybe_truncate(core, r)
                    if t2 is not None:
                        out, kind = t2, "trunc"
                    else:
                        d = maybe_double(core, r)
                        if d is not None:
                            out, kind = d, "double"
                if out is None or out == core:
                    new_chain.append(comp)
                    continue
                _stat[kind] = _stat.get(kind, 0) + 1
                logger.info("[noise] %s：%r -> %r", kind, core, out)
                new_chain.append(Plain(lead + out))
            if changed:
                result.chain[:] = new_chain
        except BaseException as exc:  # 噪点自己出错绝不能挡消息
            logger.error("[noise] 注入失败，保持原样: %s", exc)

    @filter.command("噪点状态")
    async def cmd_status(self, event: AstrMessageEvent):
        yield event.plain_result(
            "噪点注入：%s（群=%s）\n"
            "看过 %d 条｜换单字 %d｜截断 %d｜叠词 %d｜被@跳过 %d\n"
            "旋钮：换单字 %.0f%% 截断 %.0f%% 叠词 %.0f%%"
            % ("开" if ENABLED else "关",
               "，".join(sorted(GROUPS)) or "-",
               _stat["seen"], _stat.get("swap", 0), _stat.get("trunc", 0),
               _stat.get("double", 0), _stat["skip_at"],
               SWAP_RATE * 100, TRUNC_RATE * 100, DOUBLE_RATE * 100)
        )