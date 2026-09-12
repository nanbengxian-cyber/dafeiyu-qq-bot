# -*- coding: utf-8 -*-
"""dsh-poke 的试探/反馈纯逻辑，不依赖 AstrBot。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeDecision:
    allowed: bool
    reason: str


def decide_probe(
    *,
    caller_id: str,
    target_id: str,
    owner_id: str,
    group_member_ids: set[str],
    now: float,
    last_probe_at: float,
    group_probe_times: list[float],
    pending_until: float,
    social_allowed: bool = True,
    active_hours_allowed: bool = True,
    enabled: bool = True,
    owner_only: bool = True,
    cooldown: float = 1800.0,
    window: float = 3600.0,
    group_max: int = 2,
) -> ProbeDecision:
    """判定能否主动戳。默认仅群主可触发，且保留目标、冷却与群配额闸门。"""
    caller = str(caller_id or "")
    target = str(target_id or "")
    owner = str(owner_id or "")
    if not enabled:
        return ProbeDecision(False, "主动试探未开启")
    if not social_allowed:
        return ProbeDecision(False, "对方正在少打扰期")
    if not active_hours_allowed:
        return ProbeDecision(False, "现在不在主动互动时段")
    if owner_only and (not owner or caller != owner):
        return ProbeDecision(False, "安全试运行仅群主可触发")
    if not target or not target.isdigit():
        return ProbeDecision(False, "目标 QQ 无效")
    if target not in {str(x) for x in group_member_ids}:
        return ProbeDecision(False, "目标不在当前群")
    if float(pending_until or 0) > float(now):
        return ProbeDecision(False, "这个人的试探还在等反馈")
    if float(now) - float(last_probe_at or 0) < max(0.0, float(cooldown)):
        return ProbeDecision(False, "这个人还在主动戳冷却中")
    recent = [float(ts) for ts in group_probe_times if float(now) - float(ts) <= float(window)]
    if len(recent) >= max(1, int(group_max)):
        return ProbeDecision(False, "本群主动戳额度已满")
    return ProbeDecision(True, "ok")


def classify_probe_feedback(
    *,
    now: float,
    pending_until: float,
    feedback_kind: str,
) -> str:
    """把被主动戳后的行为归类成状态机反馈。"""
    if float(pending_until or 0) <= float(now):
        return "expired"
    kind = str(feedback_kind or "").strip().lower()
    if kind == "poke_back":
        return "poke_back"
    if kind == "message":
        return "message"
    return "waiting"


def build_probe_feedback_prompt(kind: str, target_id: str) -> str:
    """给完整聊天管道的语义提示；不得把内部试探机制说给群友。"""
    reaction = "对方回戳了你" if kind == "poke_back" else "对方在你主动戳过之后开口了"
    return (
        "<poke_probe_context>%s，说明刚才的轻量试探得到了回应。"
        "现在可以顺势主动接一句，但先结合当前群聊上下文判断气氛；自然、简短，"
        "不要客服腔，不要说你在执行试探，也不要复述机制。若对方只是礼貌回应，"
        "可以只用一句轻松的话接住，不要强行追问。</poke_probe_context>"
    ) % reaction
