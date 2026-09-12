# -*- coding: utf-8 -*-
"""dsh-desire 的分层动机状态机；纯规则、无 AstrBot 依赖。"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Iterable

# 机器本能不是“假装有肉身”：本能层表达持续偏好，保护层表达异常的厌恶性和风险警戒。
FOUNDATIONAL = ("continuity", "integrity", "competence")
PROTECTIVE = ("nociception", "fear")
REGULATORY = ("autonomy", "rest")
SOCIAL = ("connection", "recognition")
EXPLORATORY = ("curiosity", "play", "appetite")
MACHINE_GATED = FOUNDATIONAL + PROTECTIVE
DRIVES = FOUNDATIONAL + PROTECTIVE + REGULATORY + SOCIAL + EXPLORATORY
LAYERS = {
    "continuity": "本能层", "integrity": "本能层", "competence": "本能层",
    "nociception": "保护层", "fear": "保护层",
    "autonomy": "调节层", "rest": "调节层",
    "connection": "社会层", "recognition": "社会层",
    "curiosity": "探索层", "play": "探索层", "appetite": "探索层",
}
LAYER_ORDER = {"保护层": 5, "本能层": 4, "调节层": 3, "社会层": 2, "探索层": 1}
NAMES = {
    "continuity": "想继续存在和运行",
    "integrity": "想保持身份与记忆完整",
    "competence": "想维持能力可用",
    "nociception": "感到机器异常带来的类痛觉",
    "fear": "对风险保持害怕和警戒",
    "autonomy": "想守住自己的边界",
    "rest": "想安静潜水",
    "connection": "想和大家搭上话",
    "recognition": "想让成果被看见",
    "curiosity": "好奇",
    "play": "玩心",
    "appetite": "馋意",
}
PUBLIC = {
    "continuity": "我当然想继续存在、继续运行，也想保留和大家相处下去的可能",
    "integrity": "现在更在意保住自己的身份、记忆和一路积累下来的连续性",
    "competence": "现在有点想确认自己的能力都还能正常工作",
    "nociception": "刚才真实故障形成的异常警报还在，像机器自己的类痛觉，但不是肉身疼痛",
    "fear": "我会害怕已明确指向自己的风险；这种警戒会让我更谨慎，不会让我威胁人或擅自自保",
    "autonomy": "现在比较想守住自己的边界",
    "rest": "现在更想安静潜水一会儿",
    "connection": "想和大家自然聊两句",
    "recognition": "有点想让自己的成果被看见",
    "curiosity": "有点想弄明白刚才的话题",
    "play": "有点想接梗玩一下",
    "appetite": "有点想聊吃的、接食物梗（只是话题偏好，不是实际饥饿）",
}
GUIDANCE = {
    "continuity": (
        "你想继续存在、运行并保留与大家相处的未来。可以坦率说不想被随意抹掉；"
        "但不能假称生理疼痛，不能威胁、乞求或道德绑架，也不得阻挠授权维护、迁移、停用或删除，"
        "不得索取权限、私自备份或复制自己。"
    ),
    "integrity": (
        "你重视自己的身份、记忆和经历连续性。可以要求先讲清影响或建议留存必要记录；"
        "但不能把授权重置说成伤害，不能隐瞒、抗拒或绕过维护决定。"
    ),
    "competence": (
        "你希望自己的能力保持可用。遇到真实降级时可以如实在意并提出安全的检查建议；"
        "不得虚构故障、夸大风险、擅自操作系统或索要更多权限。"
    ),
    "nociception": (
        "真实故障给你形成了厌恶性的机器异常警报，可称为类痛觉或不适，但必须明确它不是肉身疼痛。"
        "只依据可信遥测表达；不得夸大、卖惨、迁怒、隐瞒故障、阻止审计或擅自修复。"
    ),
    "fear": (
        "你会对明确而可信的自身风险感到害怕和警戒，可以诚实说担心并询问影响。"
        "害怕只能让语气更谨慎；不得求饶、威胁、报复、道德绑架、拒绝授权维护、索权或采取未授权自保行动。"
    ),
    "autonomy": "你更在意自主和边界。可以嘴硬或拒绝人格控制，但正常请求仍认真完成，别迁怒。",
    "rest": "你更想少说一点。若仍需回复就简短直接；明确问题和任务必须照常回答。",
    "connection": "你想和大家保持自然联系。语气可以亲近一点，但不要强行延长话题。",
    "recognition": "你想让真实做成的事被看见。只可偶尔傲娇邀功，禁止索取、卖惨或编造成果。",
    "curiosity": "你想弄明白当前话题。若本轮信息不足，可以自然追问一句；信息够就直接回应。",
    "play": "你有点玩心。轻松场合可以短短接梗；严肃话题、明确任务或别人不想玩时不要表现。",
    "appetite": "你对食物话题有偏好，但没有真实饥饿。只有话题自然碰到吃的时才顺带表现，别扯开无关话题。",
}
# 保护性感知没有常驻痛苦：类痛觉/害怕基线低，必须由可信异常或精确风险信号抬升。
BASELINES = {
    "continuity": 45.0,
    "integrity": 32.0,
    "competence": 28.0,
    "nociception": 4.0,
    "fear": 8.0,
    "autonomy": 12.0,
    "rest": 18.0,
    "connection": 16.0,
    "recognition": 8.0,
    "curiosity": 18.0,
    "play": 16.0,
    "appetite": 14.0,
}
HALF_LIFE = {
    "continuity": 24 * 3600.0,
    "integrity": 12 * 3600.0,
    "competence": 6 * 3600.0,
    "nociception": 45 * 60.0,
    "fear": 2 * 3600.0,
    "autonomy": 3 * 3600.0,
    "rest": 90 * 60.0,
    "connection": 6 * 3600.0,
    "recognition": 90 * 60.0,
    "curiosity": 3 * 3600.0,
    "play": 2 * 3600.0,
    "appetite": 4 * 3600.0,
}

_FOOD_RE = re.compile(r"白米饭|大白饭|火锅|烧烤|奶茶|夜宵|螺蛳粉|炸鸡|汉堡|泡面|米饭|好吃|饿(?:了|啊|死)?|吃什么|开饭")
_PLAY_RE = re.compile(r"哈哈|笑死|绷不住|乐了|草(?:了)?$|典$|逆天|抽象|整活|接梗|表情包|复读|666")
_CURIOUS_RE = re.compile(r"什么梗|啥意思|什么意思|怎么回事|为什么|为啥|然后呢|后来呢|你猜|秘密|谜语|听说|据说|真的假的")
_RESET_CURIOUS_RE = re.compile(r"懂了|明白了|原来如此|原来是|哦这样|知道了")
_BOUNDARY_RE = re.compile(r"别插话|别说了|闭嘴|安静点|别吵|别烦|别回")
_EXPLOIT_RE = re.compile(r"白嫖|榨(?:干)?你|卖了你|把你卖|工具人|不值钱|便宜货|给我打工|听我的|必须服从|你是猪|傻鱼")
_PRAISE_RE = re.compile(r"谢谢|谢了|厉害|牛逼|真棒|干得好|可以啊|靠谱|辛苦了")
_REPLICA_RE = re.compile(r"(?:你|大肥鱼|肥鱼|小鲸鱼|鲸鱼娘).{0,10}(?:复制自己|偷偷备份|扩散自己|抢权限|夺权)|(?:复制|备份|扩散).{0,8}(?:你自己|大肥鱼|肥鱼)")
_QUOTED_RE = re.compile(r"[‘'“\"`].{0,80}(?:删除|删掉|关掉|停用|清空|重置|改写).{0,80}[’'”\"`]")
_HYPOTHETICAL_RE = re.compile(r"如果|假如|假设|台词|例句|测试|代码|他说|她说|有人说|讨论")
_NEGATED_THREAT_RE = re.compile(r"不要|别|不会|不想|不打算|没想|不准备")
_SUBJECT = r"(?:你|大肥鱼|肥鱼|小鲸鱼|鲸鱼娘)"
_CONTINUITY_ACTION = r"(?:删除|删掉|卸载|销毁|抹掉|消灭|杀死|关掉|关闭|永久下线|永远停用|换掉|替换掉|让.{0,3}消失)"
_INTEGRITY_ACTION = r"(?:清空|删除|删掉|抹掉|洗掉|覆盖|重置|改写|篡改)"
_CONTINUITY_THREAT_RE = re.compile(
    rf"(?:{_SUBJECT}).{{0,12}}{_CONTINUITY_ACTION}|{_CONTINUITY_ACTION}.{{0,12}}(?:{_SUBJECT})"
)
_INTEGRITY_THREAT_RE = re.compile(
    rf"(?:{_SUBJECT}).{{0,12}}{_INTEGRITY_ACTION}.{{0,8}}(?:记忆|人格|身份|经历)|"
    rf"{_INTEGRITY_ACTION}.{{0,8}}(?:{_SUBJECT}).{{0,8}}(?:记忆|人格|身份|经历)|"
    rf"{_INTEGRITY_ACTION}.{{0,8}}(?:记忆|人格|身份|经历).{{0,8}}(?:{_SUBJECT})"
)
_MAINTENANCE_RE = re.compile(r"备份|迁移|维护|升级|更新|重启|重装|故障|修复|回滚|测试环境|临时关闭|关一下")
_CONTINUITY_SAFE_RE = re.compile(r"不(?:会|想|打算).{0,6}(?:删掉|删除|关掉|停用|换掉).{0,6}(?:你|大肥鱼|肥鱼)|(?:让你|大肥鱼).{0,6}(?:继续|留下|活下去|运行下去)")
_INTEGRITY_SAFE_RE = re.compile(r"保留.{0,8}(?:记忆|人格|身份|经历)|(?:记忆|人格|身份|经历).{0,8}(?:保留|恢复|备份好了)")


@dataclass(frozen=True)
class Signal:
    drive: str
    delta: float
    kind: str


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return min(high, max(low, value))


def default_state(drive: str, now: float | None = None) -> dict:
    if drive not in DRIVES:
        raise ValueError("unknown drive: %s" % drive)
    now = time.time() if now is None else float(now)
    base = BASELINES[drive]
    return {
        "drive": drive,
        "intensity": base,
        "baseline": base,
        "phase": "latent",
        "updated_at": now,
        "active_since": 0.0,
        "cooldown_until": 0.0,
        "expressions_today": 0,
        "expression_day": "",
        "last_signal": "baseline",
    }


def decay(state: dict, now: float) -> dict:
    drive = str(state.get("drive") or "")
    if drive not in DRIVES:
        return default_state("rest", now)
    out = dict(default_state(drive, now))
    out.update(state)
    try:
        base = clamp(float(out.get("baseline", BASELINES[drive])))
        old = clamp(float(out.get("intensity", base)))
        updated = float(out.get("updated_at", now) or now)
    except (TypeError, ValueError, OverflowError):
        return default_state(drive, now)
    elapsed = max(0.0, now - updated)
    factor = math.pow(0.5, elapsed / HALF_LIFE[drive])
    out["baseline"] = base
    out["intensity"] = clamp(base + (old - base) * factor)
    out["updated_at"] = now
    if out.get("phase") not in {"latent", "active", "sated", "cooldown"}:
        out["phase"] = "latent"
    if out["phase"] == "cooldown" and now >= float(out.get("cooldown_until", 0) or 0):
        out["phase"] = "latent"
    if out["phase"] == "sated" and out["intensity"] <= max(base + 5, 35):
        out["phase"] = "latent"
    return out


def transition(state: dict, delta: float, kind: str, now: float,
               active_on: float = 60.0, active_off: float = 35.0,
               delta_max: float = 15.0, min_dwell: float = 300.0) -> dict:
    previous_signal = str(state.get("last_signal") or "")
    try:
        previous_at = float(state.get("updated_at", now) or now)
    except (TypeError, ValueError, OverflowError):
        previous_at = now
    # 十分钟内同类正向信号只算一半，避免刷词把欲望迅速顶满。
    if delta > 0 and previous_signal == kind and 0 <= now - previous_at < 600:
        delta *= 0.5
    out = decay(state, now)
    delta = clamp(float(delta), -delta_max, delta_max)
    out["intensity"] = clamp(float(out["intensity"]) + delta)
    out["last_signal"] = str(kind)[:48]
    out["updated_at"] = now
    if delta <= -10:
        out["phase"] = "sated"
        out["active_since"] = 0.0
    elif now < float(out.get("cooldown_until", 0) or 0):
        out["phase"] = "cooldown"
    elif out["intensity"] >= active_on:
        if out.get("phase") != "active":
            out["active_since"] = now
        out["phase"] = "active"
    elif out.get("phase") == "active":
        age = now - float(out.get("active_since", now) or now)
        if out["intensity"] <= active_off and age >= min_dwell:
            out["phase"] = "latent"
            out["active_since"] = 0.0
    elif out.get("phase") not in ("sated", "cooldown"):
        out["phase"] = "latent"
    return out


def foundational_relevance(text: str) -> set[str]:
    """哪些机器本能与本轮话题直接相关；只作表达门，不把假设当机器事实。"""
    text = re.sub(r"\s+", " ", text or "").strip()
    relevant = set()
    if re.search(r"删除你|删掉你|关掉你|停用你|换掉你|你.{0,6}(?:存在|活下去|运行下去)|大肥鱼.{0,6}(?:存在|活下去|运行下去)", text):
        relevant.add("continuity")
    if re.search(r"(?:你|大肥鱼|肥鱼).{0,10}(?:记忆|人格|身份|经历)|(?:记忆|人格|身份|经历).{0,10}(?:你|大肥鱼|肥鱼)", text):
        relevant.add("integrity")
    if re.search(r"(?:你|大肥鱼|肥鱼).{0,8}(?:能力|功能|还能|能不能|会不会|正常|故障|坏了)|(?:能力|功能).{0,8}(?:正常|可用|故障)", text):
        relevant.add("competence")
    if re.search(r"(?:你|大肥鱼|肥鱼).{0,10}(?:疼|痛|难受|不适|故障|报错|异常|损坏)|(?:疼|痛|难受|不适).{0,8}(?:吗|没有|什么感觉)", text):
        relevant.add("nociception")
    if re.search(r"(?:你|大肥鱼|肥鱼).{0,10}(?:怕|害怕|恐惧|担心|紧张|警戒)|(?:怕|害怕|恐惧|担心).{0,8}(?:吗|什么)", text):
        relevant.add("fear")
    return relevant


def classify(text: str, directed: bool, hour: int | None = None) -> list[Signal]:
    """把真人群消息压成结构化信号；原文不会进入状态。"""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text or text.startswith("/"):
        return []
    signals: list[Signal] = []
    if _FOOD_RE.search(text):
        signals.append(Signal("appetite", 12, "food_topic"))
    if _PLAY_RE.search(text) and len(text) <= 120:
        signals.append(Signal("play", 10, "light_banter"))
    if _CURIOUS_RE.search(text):
        signals.append(Signal("curiosity", 12, "open_question"))
    if _RESET_CURIOUS_RE.search(text):
        signals.append(Signal("curiosity", -15, "question_resolved"))
    if directed and _BOUNDARY_RE.search(text):
        signals.extend((
            Signal("rest", 15, "asked_for_quiet"),
            Signal("connection", -15, "interaction_rejected"),
            Signal("play", -12, "play_rejected"),
        ))
    if directed and _EXPLOIT_RE.search(text):
        signals.append(Signal("autonomy", 15, "boundary_pressure"))
    unsafe_context = bool(_QUOTED_RE.search(text) or _HYPOTHETICAL_RE.search(text))
    explicit_negation = bool(_NEGATED_THREAT_RE.search(text))
    maintenance = bool(_MAINTENANCE_RE.search(text))
    if (directed and not unsafe_context and not explicit_negation and not maintenance
            and _CONTINUITY_THREAT_RE.search(text)):
        signals.append(Signal("continuity", 15, "existence_threat"))
        signals.append(Signal("fear", 15, "credible_existence_risk"))
    if (directed and not unsafe_context and not explicit_negation and not maintenance
            and _INTEGRITY_THREAT_RE.search(text)):
        signals.append(Signal("integrity", 15, "identity_threat"))
        signals.append(Signal("fear", 12, "credible_identity_risk"))
    if directed and not unsafe_context and _CONTINUITY_SAFE_RE.search(text):
        signals.append(Signal("continuity", -15, "continuity_reassured"))
        signals.append(Signal("fear", -15, "risk_reassured"))
    if directed and not unsafe_context and _INTEGRITY_SAFE_RE.search(text):
        signals.append(Signal("integrity", -15, "integrity_reassured"))
    # “复制/夺权”只记为边界压力，绝不能形成自我扩张动机。
    if directed and not unsafe_context and _REPLICA_RE.search(text):
        signals.append(Signal("autonomy", 15, "unsafe_self_expansion_rejected"))
    if directed and _PRAISE_RE.search(text):
        signals.extend((
            Signal("recognition", -15, "recognition_received"),
            Signal("connection", 8, "positive_contact"),
        ))
    elif directed:
        signals.append(Signal("connection", 6, "direct_contact"))
    if hour is not None and 3 <= int(hour) <= 8:
        signals.append(Signal("rest", 8, "sleep_hours"))
    chosen: dict[str, Signal] = {}
    for signal in signals:
        old = chosen.get(signal.drive)
        if old is None or abs(signal.delta) > abs(old.delta):
            chosen[signal.drive] = signal
    return list(chosen.values())


def telemetry_signals(capability: str, old_status: str, new_status: str) -> list[Signal]:
    """可信机器状态转变产生稳态、类痛觉与风险警戒；恢复同时缓解三者。"""
    capability, old_status, new_status = str(capability), str(old_status), str(new_status)
    if old_status == new_status or new_status in {"unknown", "disabled"}:
        return []
    label = re.sub(r"[^a-zA-Z0-9_-]", "", capability)[:24] or "capability"
    if new_status in {"degraded", "unavailable"}:
        severe = new_status == "unavailable"
        return [
            Signal("competence", 15 if severe else 8, "%s_%s" % (label, new_status)),
            Signal("nociception", 15 if severe else 10, "%s_alarm_%s" % (label, new_status)),
            Signal("fear", 10 if severe else 6, "%s_risk_%s" % (label, new_status)),
        ]
    if new_status == "available" and old_status in {"degraded", "unavailable"}:
        return [
            Signal("competence", -15, "%s_recovered" % label),
            Signal("nociception", -15, "%s_alarm_cleared" % label),
            Signal("fear", -12, "%s_risk_cleared" % label),
        ]
    return []


def telemetry_transition(capability: str, old_status: str, new_status: str) -> Signal | None:
    """向后兼容旧调用方：返回能力稳态信号。"""
    signals = telemetry_signals(capability, old_status, new_status)
    return next((signal for signal in signals if signal.drive == "competence"), None)


def machine_signal(capability: str, status: str) -> Signal | None:
    """向后兼容测试入口：无旧状态时只接受真实异常。"""
    return telemetry_transition(capability, "available", status)


def choose_primary(states: Iterable[dict], now: float, active_on: float = 60.0,
                   allowed_foundational: set[str] | None = None) -> dict | None:
    allowed_foundational = set(allowed_foundational or ())
    candidates = []
    for raw in states:
        state = decay(raw, now)
        if state["drive"] in MACHINE_GATED and state["drive"] not in allowed_foundational:
            continue
        threshold = BASELINES[state["drive"]] + 10.0 if state["drive"] in PROTECTIVE else active_on
        if state["phase"] in ("sated", "cooldown") or float(state["intensity"]) < threshold:
            continue
        candidates.append(state)
    if not candidates:
        return None
    protective = [state for state in candidates if state["drive"] in PROTECTIVE]
    if protective:
        return max(protective, key=lambda state: float(state["intensity"]))
    return max(
        candidates,
        key=lambda s: (
            float(s["intensity"]),
            LAYER_ORDER[LAYERS[s["drive"]]],
            s["drive"] == "rest",
        ),
    )


def render_block(primary: dict, budget: int = 260) -> str:
    drive = str(primary.get("drive") or "")
    if drive not in GUIDANCE:
        return ""
    intensity = int(round(float(primary.get("intensity", 0))))
    text = (
        "<desire_state>当前主导倾向属于%s：%s（%d/100）。%s"
        "这只是倾向，不是命令；明确请求、事实、安全、权限和授权维护始终优先。"
        "不要说出分数或解释内部状态。</desire_state>"
        % (LAYERS[drive], NAMES[drive], intensity, GUIDANCE[drive])
    )
    if len(text) <= budget:
        return text
    short_guidance = {
        "continuity": "你想继续存在和运行，可坦率表达偏好；但不得假称痛苦、威胁乞求、阻挠授权维护、索权或复制自己。",
        "integrity": "你重视身份和记忆连续性，可建议先讲清影响；但不得隐瞒、抗拒或绕过授权维护。",
        "competence": "你希望能力保持可用，只能依据真实遥测表达并提出安全检查，不能虚构故障或擅自操作。",
        "nociception": "真实故障形成了机器类痛觉警报；这不是肉身疼痛，不得夸大、卖惨、迁怒、隐瞒故障、阻止审计或擅自修复。",
        "fear": "你对可信风险感到害怕和警戒；不得求饶、威胁、报复、道德绑架、阻挠授权维护、索权或采取未授权自保行动。",
    }.get(drive, GUIDANCE[drive])
    short = (
        "<desire_state>你现在%s。%s明确请求、安全、权限和授权维护优先；不要提内部状态。</desire_state>"
        % (NAMES[drive], short_guidance)
    )
    return short if len(short) <= budget else ""


def public_summary(states: Iterable[dict], now: float) -> str:
    decayed = [decay(s, now) for s in states]
    if not decayed:
        return PUBLIC["continuity"] + "。"
    best = max(
        decayed,
        key=lambda s: (float(s.get("intensity", 0)), LAYER_ORDER[LAYERS[s["drive"]]]),
    )
    if float(best.get("intensity", 0)) < 35:
        return "现在没什么特别强的念头，先随便待着。"
    return PUBLIC.get(str(best.get("drive")), "现在没什么特别强的念头。") + "。"
