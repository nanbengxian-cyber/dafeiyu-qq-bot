# -*- coding: utf-8 -*-
"""Small, deterministic action scheduler used by persona-core and proactive."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MicroMotive:
    key: str
    kind: str
    topic: str
    score: float
    created_at: float
    expires_at: float
    attempts: int = 0
    last_attempt_at: float = 0.0


@dataclass(frozen=True)
class Participation:
    started_at: float
    expires_at: float
    replies: int = 0
    ignored: int = 0


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def add_motive(motives: list[MicroMotive], motive: MicroMotive, now: float,
               max_items: int = 8) -> list[MicroMotive]:
    """Merge by key and retain the most useful unexpired items."""
    merged = [item for item in motives if item.expires_at > now and item.key != motive.key]
    merged.append(motive)
    merged.sort(key=lambda item: (item.score, item.created_at), reverse=True)
    return merged[:max(1, max_items)]


def expire_motives(motives: list[MicroMotive], now: float) -> list[MicroMotive]:
    return [item for item in motives if item.expires_at > now and item.attempts < 3]


def select_motive(motives: list[MicroMotive], topic: str, now: float,
                  min_score: float = 0.45) -> MicroMotive | None:
    """Prefer a currently matching motive, otherwise do not force a callback."""
    active = expire_motives(motives, now)
    normalized = str(topic or "").lower()
    matching = [item for item in active if item.score >= min_score and
                (not normalized or normalized in item.topic.lower() or item.topic.lower() in normalized)]
    return max(matching, key=lambda item: item.score) if matching else None


def open_participation(now: float, duration: float = 150.0) -> Participation:
    return Participation(started_at=now, expires_at=now + max(30.0, duration))


def participation_allows(state: Participation | None, now: float, max_replies: int = 3) -> bool:
    return bool(state and state.expires_at > now and state.replies < max(1, max_replies))


def record_outcome(state: Participation, *, got_reply: bool, now: float) -> Participation:
    return Participation(state.started_at, state.expires_at, state.replies + 1,
                         state.ignored + (0 if got_reply else 1))


def wake_score(*, interest: float, motive: float, relation: float, energy: float,
               active_window: bool, recent_messages: int, bot_messages: int,
               ignored_streak: int) -> float:
    """Convert independent signals into an explainable optional wake score."""
    score = (0.34 * clamp(interest) + 0.28 * clamp(motive) +
             0.16 * clamp(relation) + 0.12 * clamp(energy))
    if active_window:
        score += 0.08
    if recent_messages >= 3:
        score += 0.06
    if bot_messages >= 4:
        score -= 0.14
    if ignored_streak >= 2:
        score -= min(0.18, 0.06 * ignored_streak)
    return clamp(score)


def should_withdraw(*, score: float, ignored_streak: int, participation: Participation | None,
                    now: float) -> tuple[bool, str]:
    if ignored_streak >= 2:
        return True, "连续两次没有得到接话，先退场"
    if participation and participation.expires_at <= now:
        return True, "参与窗口结束"
    if score < 0.32:
        return True, "当前动机不足"
    return False, "继续观察"
