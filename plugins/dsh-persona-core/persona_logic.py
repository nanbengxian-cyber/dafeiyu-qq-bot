# -*- coding: utf-8 -*-
"""dsh-persona-core: deterministic state, motive and action coordination.

This module is deliberately dependency-free. AstrBot adapters can translate an event into
TurnContext, call the pure functions, and use the returned action as one coordination signal.
It does not send messages and it never overrides explicit tasks or safety decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import math

BASELINE = 0.0
HALF_LIFE = 6 * 3600.0


@dataclass(frozen=True)
class TurnContext:
    group_id: str
    speaker_id: str
    text: str
    directed: bool = False
    explicit_task: bool = False
    safety_sensitive: bool = False
    topic_interest: float = 0.0
    sentiment: float = 0.0
    relationship_importance: float = 0.0
    continuation: bool = False
    recent_bot_reply: bool = False
    active_conflict: bool = False
    quiet_requested: bool = False


@dataclass(frozen=True)
class Motive:
    name: str
    score: float
    reason: str


@dataclass(frozen=True)
class Action:
    name: str
    score: float
    motive: str
    reason: str
    optional: bool = True


@dataclass
class PersonaState:
    mood: float = 0.0
    energy: float = 0.55
    curiosity: float = 0.15
    irritation: float = 0.0
    loneliness: float = 0.0
    confidence: float = 0.2
    updated_at: float = 0.0


@dataclass
class RelationState:
    familiarity: float = 0.0
    trust: float = 0.0
    warmth: float = 0.0
    teasing_tolerance: float = 0.3
    last_interaction: str = ""
    updated_at: float = 0.0


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def decay(value: float, updated_at: float, now: float, half_life: float = HALF_LIFE,
          baseline: float = BASELINE) -> float:
    if not updated_at or now <= updated_at:
        return clamp(value)
    factor = math.pow(0.5, max(0.0, now - updated_at) / max(1.0, half_life))
    return clamp(baseline + (value - baseline) * factor)


def _state_value(state: PersonaState, name: str, now: float) -> float:
    value = getattr(state, name)
    if name == "energy":
        return clamp(0.55 + (value - 0.55) * math.pow(0.5, max(0, now - state.updated_at) / (4 * 3600.0)))
    return decay(value, state.updated_at, now)


def update_state(previous: PersonaState, ctx: TurnContext, now: float) -> PersonaState:
    """Apply one event while first decaying stale transient state."""
    values = {name: _state_value(previous, name, now) for name in
              ("mood", "energy", "curiosity", "irritation", "loneliness", "confidence")}
    if ctx.directed:
        values["loneliness"] -= 0.08
        values["energy"] += 0.02
    if ctx.explicit_task:
        values["confidence"] += 0.03
    values["mood"] += 0.16 * ctx.sentiment
    values["curiosity"] += 0.22 * clamp(ctx.topic_interest)
    if ctx.active_conflict:
        values["irritation"] += 0.12
        values["energy"] -= 0.05
    if ctx.quiet_requested:
        values["irritation"] -= 0.08
    return PersonaState(**{key: clamp(value) for key, value in values.items()}, updated_at=float(now))


def update_relation(previous: RelationState, ctx: TurnContext, now: float) -> RelationState:
    """Small bounded relationship changes; one event cannot rewrite a relationship."""
    age_factor = 1.0 if not previous.updated_at else math.pow(
        0.5, max(0.0, now - previous.updated_at) / (30 * 86400.0)
    )
    familiarity = clamp(previous.familiarity * age_factor + (0.025 if ctx.text.strip() else 0.0))
    trust = clamp(previous.trust * age_factor)
    warmth = clamp(previous.warmth * age_factor)
    if ctx.sentiment > 0.35:
        warmth += 0.04
        trust += 0.02
    elif ctx.sentiment < -0.55:
        warmth -= 0.04
        trust -= 0.02
    if ctx.explicit_task and ctx.directed:
        familiarity += 0.015
    interaction = "positive" if ctx.sentiment > 0.35 else "negative" if ctx.sentiment < -0.35 else "neutral"
    return RelationState(familiarity=familiarity, trust=trust, warmth=warmth,
                         teasing_tolerance=clamp(previous.teasing_tolerance * age_factor),
                         last_interaction=interaction, updated_at=float(now))


def motives(ctx: TurnContext, state: PersonaState, relation: RelationState) -> list[Motive]:
    """Return motives in descending order without making the final send decision."""
    out: list[Motive] = []
    if ctx.explicit_task:
        out.append(Motive("help", 1.0, "明确任务优先"))
    if ctx.continuation:
        out.append(Motive("continuity", 0.72 + 0.18 * relation.familiarity, "存在未完成的上下文"))
    if ctx.directed:
        out.append(Motive("social", 0.52 + 0.28 * relation.warmth, "消息明确指向机器人"))
    interest = 0.45 * clamp(ctx.topic_interest) + 0.25 * clamp(state.curiosity)
    if interest > 0.18:
        out.append(Motive("interest", interest, "话题符合当前兴趣"))
    care = 0.35 * relation.warmth + 0.25 * state.loneliness
    if care > 0.12:
        out.append(Motive("relationship", care, "关系维护值得投入"))
    if ctx.active_conflict:
        out.append(Motive("observe", 0.45 + 0.2 * state.irritation, "群内存在冲突，先观察"))
    if not out:
        out.append(Motive("rest", 0.4 + 0.3 * (1.0 - state.energy), "没有足够理由主动介入"))
    return sorted(out, key=lambda item: item.score, reverse=True)


def choose_action(ctx: TurnContext, state: PersonaState, relation: RelationState) -> Action:
    """Choose exactly one coordination result. Optional actions may be suppressed by caller."""
    if ctx.safety_sensitive:
        return Action("answer_safely", 1.0, "help", "安全相关问题必须走可靠路径", optional=False)
    if ctx.explicit_task:
        return Action("cooperate_with_stance", 1.0, "help", "明确任务不可被人格状态阻断", optional=False)
    if ctx.quiet_requested:
        return Action("stay_quiet", 0.95, "rest", "对方请求少打扰")
    ranked = motives(ctx, state, relation)
    top = ranked[0]
    if top.name == "continuity":
        return Action("follow_up", top.score, top.name, top.reason)
    if top.name == "social":
        return Action("reply_socially", top.score, top.name, top.reason)
    if top.name == "interest":
        return Action("share_interest", top.score, top.name, top.reason)
    if top.name == "relationship":
        return Action("check_in", top.score, top.name, top.reason)
    if top.name == "observe":
        return Action("observe", top.score, top.name, top.reason)
    return Action("stay_quiet", top.score, top.name, top.reason)


def should_wake(action: Action, ctx: TurnContext, min_score: float = 0.58) -> tuple[bool, str]:
    """Final optional wake gate. Explicit replies bypass the optional threshold."""
    if not action.optional:
        return True, "硬优先级"
    if ctx.quiet_requested:
        return False, "少打扰边界"
    if action.name in {"stay_quiet", "observe"}:
        return False, "没有足够的发言理由"
    if action.score < min_score:
        return False, "动机分不足"
    return True, action.reason


def state_from_dict(value: dict[str, Any] | None) -> PersonaState:
    value = value if isinstance(value, dict) else {}
    return PersonaState(**{key: float(value.get(key, default)) for key, default in {
        "mood": 0.0, "energy": 0.55, "curiosity": 0.15, "irritation": 0.0,
        "loneliness": 0.0, "confidence": 0.2, "updated_at": 0.0}.items()})


def relation_from_dict(value: dict[str, Any] | None) -> RelationState:
    value = value if isinstance(value, dict) else {}
    return RelationState(**{key: value.get(key, default) for key, default in {
        "familiarity": 0.0, "trust": 0.0, "warmth": 0.0,
        "teasing_tolerance": 0.3, "last_interaction": "", "updated_at": 0.0}.items()})
