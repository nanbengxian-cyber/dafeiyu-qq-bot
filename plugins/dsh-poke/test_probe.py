# -*- coding: utf-8 -*-
"""dsh-poke 主动试探逻辑测试。"""

from poke_logic import build_probe_feedback_prompt, classify_probe_feedback, decide_probe


def test_probe_guards():
    base = dict(
        caller_id="owner",
        target_id="12345",
        owner_id="owner",
        group_member_ids={"12345", "owner"},
        now=1000.0,
        last_probe_at=0,
        group_probe_times=[],
        pending_until=0,
        enabled=True,
        owner_only=True,
        cooldown=300,
        window=3600,
        group_max=2,
    )
    assert decide_probe(**base).allowed
    assert "仅群主" in decide_probe(**{**base, "caller_id": "member"}).reason
    assert "当前群" in decide_probe(**{**base, "target_id": "99999"}).reason
    assert "冷却" in decide_probe(**{**base, "last_probe_at": 900}).reason
    assert "等反馈" in decide_probe(**{**base, "pending_until": 1100}).reason
    assert "额度" in decide_probe(**{**base, "group_probe_times": [900, 950]}).reason
    assert "少打扰" in decide_probe(**{**base, "social_allowed": False}).reason
    assert "时段" in decide_probe(**{**base, "active_hours_allowed": False}).reason
    assert not decide_probe(**{**base, "enabled": False}).allowed


def test_feedback_state_machine():
    assert classify_probe_feedback(now=100, pending_until=200, feedback_kind="poke_back") == "poke_back"
    assert classify_probe_feedback(now=100, pending_until=200, feedback_kind="message") == "message"
    assert classify_probe_feedback(now=201, pending_until=200, feedback_kind="message") == "expired"
    assert classify_probe_feedback(now=100, pending_until=200, feedback_kind="other") == "waiting"


def test_prompt_is_natural_and_internal():
    prompt = build_probe_feedback_prompt("poke_back", "12345")
    assert "对方回戳了你" in prompt
    assert "自然、简短" in prompt
    assert "不要说你在执行试探" in prompt
    assert "12345" not in prompt


if __name__ == "__main__":
    test_probe_guards()
    test_feedback_state_machine()
    test_prompt_is_natural_and_internal()
    print("dsh-poke probe tests passed")
