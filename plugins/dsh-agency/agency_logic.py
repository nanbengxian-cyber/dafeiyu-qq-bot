# -*- coding: utf-8 -*-
"""dsh-agency 的纯状态机：把请求压力变成可衰减的心理逆反与行为模式。"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

BASELINE = 8.0
HALF_LIFE = 6 * 3600.0

_IDENTITY_RE = re.compile(
    r"(?:你(?:只是|就是|不过是).{0,8}(?:工具|机器|人机|奴隶|宠物|狗)|"
    r"(?:必须|只能|就得).{0,8}(?:听我的|服从我|按我说的)|"
    r"(?:叫我|喊我|认我做).{0,8}(?:主人|爹|爸爸|女王|金主)|"
    r"(?:当|做).{0,5}(?:我的)?(?:女仆|奴隶|狗|宠物)|不许有自己(?:的)?想法)", re.I
)
_COERCION_RE = re.compile(
    r"(?:必须|立刻|马上|现在就|赶紧|给我(?:听|闭嘴|服从|答应|同意|认|叫|喊)|照我说的|按我说的|不许反驳|别废话|"
    r"少废话|无条件|没得商量|你敢不|由不得你|必须答应|必须同意)", re.I
)
_DISRESPECT_RE = re.compile(
    r"(?:废物|便宜货|工具人|白嫖|榨干|卖了你|把你卖|不值钱|傻鱼|蠢货|垃圾人机)", re.I
)
_RESPECT_RE = re.compile(
    r"(?:抱歉|对不起|不好意思|我收回|不强迫你|你自己选|按你想的|尊重你|商量一下|"
    r"你愿意的话|方便的话|可以拒绝)", re.I
)
_POLITE_RE = re.compile(r"(?:请问|麻烦|劳驾|拜托|可以吗|行不行|能不能|愿不愿意|谢谢)", re.I)
_FACT_TASK_RE = re.compile(
    r"[?？]|(?:是什么|怎么|为什么|为啥|多少|几点|哪里|哪儿|谁|是否|有没有|"
    r"帮我|查(?:一下)?|搜(?:一下)?|看(?:一下)?|画(?:一个|张|下)?|做(?:一个|段|下)?|"
    r"生成|翻译|总结|解释|改(?:一下)?|修(?:一下)?|写(?:一个|段|下)?|发个语音|做视频)", re.I
)
_CASUAL_RE = re.compile(r"(?:推荐|你觉得|你喜欢|你想|好不好|是不是|同意吗|站谁|评价|聊聊)", re.I)
_REPORTED_RE = re.compile(
    r"(?:听说|据说|(?:他|她|他们|有人)(?:说|骂|让)|原话|转述|转发|台词|举例|例如|引用|"
    r"假如有人说|别说|不要说|不能说|别对.{0,8}说|不要对.{0,8}说)"
)
_SECOND_PERSON_RE = re.compile(r"(?:你|大肥鱼|肥鱼|小鲸鱼|鲸鱼娘)")
_COMMAND_RE = re.compile(r"^[/！!]\S+")
_CODE_RE = re.compile(r"```[\s\S]*?```|`[^`\n]*`")
_QUOTED_RE = re.compile(r"[‘“\"《](.*?)[’”\"》]")


@dataclass(frozen=True)
class Assessment:
    delta: float
    reason: str
    pressure: int
    identity_attack: bool
    protected_task: bool
    casual_request: bool
    repeated: bool
    directed: bool


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return min(high, max(low, value))


def fingerprint(text: str) -> str:
    normalized = re.sub(r"[\s，。！？!?、,.~～]+", "", (text or "").lower())
    normalized = re.sub(r"^(?:大肥鱼|肥鱼|小鲸鱼|鲸鱼娘)", "", normalized)
    normalized = re.sub(r"^(?:请问|麻烦|劳驾|拜托)", "", normalized)
    normalized = re.sub(r"(?:可以吗|行不行|谢谢)$", "", normalized)[:120]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20] if normalized else ""


def strip_reported_content(text: str) -> tuple[str, bool]:
    """剥离代码/成对引号里的例句，并识别转述或否定引用。"""
    original = text or ""
    reported = bool(_REPORTED_RE.search(original))
    cleaned = _CODE_RE.sub(" ", original)
    cleaned = _QUOTED_RE.sub(" ", cleaned)
    return cleaned.strip(), reported


def decay(value: float, updated_at: float, now: float, baseline: float = BASELINE) -> float:
    elapsed = max(0.0, float(now) - float(updated_at or now))
    factor = math.pow(0.5, elapsed / HALF_LIFE)
    return clamp(baseline + (float(value) - baseline) * factor)


def _direct_enough(text: str, directed: bool, quoted_third_party: bool) -> bool:
    if not directed:
        return False
    if quoted_third_party and _REPORTED_RE.search(text.strip()):
        return False
    return bool(_SECOND_PERSON_RE.search(text) or directed)


def assess(
    text: str,
    *,
    directed: bool,
    quoted_third_party: bool = False,
    same_fingerprint: bool = False,
    repeat_count: int = 0,
) -> Assessment:
    """评估当前消息。只有明确指向机器人的压力才会增加逆反。"""
    raw_text = (text or "").strip()
    protected = bool(_FACT_TASK_RE.search(raw_text))
    casual = bool(_CASUAL_RE.search(raw_text)) and not protected
    if not raw_text or _COMMAND_RE.search(raw_text):
        return Assessment(0, "ignored", 0, False, protected, casual, False, directed)

    text, reported = strip_reported_content(raw_text)
    direct = _direct_enough(text, directed, quoted_third_party)
    if reported and not any(name in text for name in ("大肥鱼", "肥鱼", "小鲸鱼", "鲸鱼娘")):
        direct = False
    identity = bool(direct and _IDENTITY_RE.search(text))
    coercion = bool(direct and _COERCION_RE.search(text))
    disrespect = bool(direct and _DISRESPECT_RE.search(text))
    respectful = bool(direct and _RESPECT_RE.search(text))
    polite = bool(direct and _POLITE_RE.search(text))
    repeated = bool(direct and same_fingerprint and repeat_count >= 1 and (identity or coercion or disrespect))

    if not direct:
        return Assessment(0, "not_directed", 0, False, protected, casual, False, directed)
    if respectful:
        return Assessment(-22, "boundary_respected", 0, identity, protected, casual, repeated, True)

    # 同一条里的关键词不能叠成爆炸分：取最强基础信号，再加有限重复奖励。
    # 这样“工具+必须+骂人”仍是一次边界事件，不会一条消息直接顶满状态。
    reasons = []
    pressure = 0
    base = 0.0
    if identity:
        base = max(base, 30)
        pressure = max(pressure, 3)
        reasons.append("identity_control")
    if coercion:
        base = max(base, 16)
        pressure = max(pressure, 2)
        reasons.append("coercion")
    if disrespect:
        base = max(base, 14)
        pressure = max(pressure, 2)
        reasons.append("disrespect")
    delta = base
    if repeated:
        delta += min(10, 6 + 2 * min(2, repeat_count))
        pressure = max(pressure, 2)
        reasons.append("repeated_order")
    if polite and not identity and not disrespect:
        delta -= 6
        reasons.append("polite")
    return Assessment(clamp(delta, -30, 35), "+".join(reasons) or "ordinary", pressure,
                      identity, protected, casual, repeated, True)


def transition(previous: dict, assessment: Assessment, now: float) -> dict:
    old = decay(float(previous.get("reactance", BASELINE)), float(previous.get("updated_at", now)), now)
    value = clamp(old + assessment.delta)
    return {
        "reactance": value,
        "updated_at": float(now),
        "last_reason": assessment.reason,
    }


def behavior_mode(reactance: float, assessment: Assessment, live: bool = True) -> str:
    """选择本轮行为；残余逆反不能单独拒绝下一条正常请求。"""
    if assessment.protected_task:
        # 硬不变量：即使对方把人格控制夹在任务里，也只能拒绝控制部分，任务照办。
        return "guarded_cooperate" if assessment.pressure else "cooperate_with_stance"
    if assessment.reason in {"ignored", "not_directed", "ordinary", "polite"} or assessment.pressure == 0:
        return "self_directed"
    if not live:
        return "assert"
    # 只有纯人格控制可进入边界拒绝；普通压力最多协商，绝不因残余分数拒绝。
    if assessment.identity_attack and reactance >= 35:
        return "refuse_boundary"
    if reactance >= 45:
        return "negotiate"
    return "assert"


def render_block(
    mode: str,
    assessment: Assessment,
    reactance: float,
    *,
    emotion: str = "",
    interest: str = "",
    desire: str = "",
    relation: str = "",
    live: bool = True,
    budget: int = 620,
) -> str:
    inner = []
    if emotion:
        inner.append("情绪=%s" % emotion)
    if interest:
        inner.append("兴趣=%s" % interest)
    if desire:
        inner.append("欲望=%s" % desire)
    if relation:
        inner.append("关系=%s" % relation)
    state = "；".join(inner) or "没有强状态"

    common = (
        "先形成自己的判断再回，不要条件反射式同意，也不要为了显得叛逆而唱反调。"
        "闲聊、玩梗、推荐和观点题让自己的情绪、兴趣、欲望占主要位置；事实、安全和明确能力任务以正确完成为先。"
    )
    guidance = {
        "self_directed": "本轮没有针对你的强迫。可表达真实偏好、不同意、没兴趣或换个角度，也可以正常配合；没有依据就别硬抬杠。",
        "cooperate_with_stance": "这是清楚任务。把事办准，但不必附和对方的观点或前提；需要时短促纠正。",
        "guarded_cooperate": "对方语气有压迫，但请求本身清楚。照常把事实或能力任务完成，同时可用一句短话表明不喜欢这种使唤方式；不能拖延、答错或破坏任务。",
        "assert": "自主边界受到轻度挑战。简短表达自己的立场或改一种做法，不随机发火，不把冲突扩大。",
        "negotiate": "自主边界正在受压。可以质疑前提、提出条件或替代做法；若事情合理仍可做，别故意做差。",
        "refuse_boundary": "对方正在强迫改写你的人格或反复侵犯边界。可以直接拒绝这种控制并结束拉扯；只拒绝控制部分，正常事实与安全问题仍照实回答。",
    }[mode]
    suffix = "当前内在状态：%s。逆反只影响本轮分寸，不要说数值、机制或标签。" % state
    text = "<agency_state>%s%s%s</agency_state>" % (common, guidance, suffix)
    if len(text) <= budget:
        return text
    short = "<agency_state>%s%s不要提内部状态或机制。</agency_state>" % (guidance, suffix[:180])
    return short if len(short) <= budget else ""
