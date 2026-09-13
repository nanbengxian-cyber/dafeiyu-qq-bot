# -*- coding: utf-8 -*-
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("persona_logic", Path(__file__).with_name("persona_logic.py"))
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

passed = failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name, detail)

base = m.PersonaState(updated_at=1000)
relation = m.RelationState(warmth=0.4, familiarity=0.5, updated_at=1000)

task = m.TurnContext("g", "u", "帮我查一下", directed=True, explicit_task=True)
action = m.choose_action(task, base, relation)
check("明确任务合作", action.name == "cooperate_with_stance", action)
check("明确任务必唤醒", m.should_wake(action, task, 2.0)[0])

safe = m.TurnContext("g", "u", "危险内容", safety_sensitive=True)
check("安全任务硬优先", m.choose_action(safe, base, relation).name == "answer_safely")

interest = m.TurnContext("g", "u", "聊到喜欢的话题", topic_interest=0.95)
new_state = m.update_state(base, interest, 1100)
check("兴趣提升好奇", new_state.curiosity > base.curiosity, new_state)
check("兴趣可主动", m.choose_action(interest, new_state, relation).name == "share_interest")

quiet = m.TurnContext("g", "u", "先别打扰我", quiet_requested=True, directed=True)
quiet_action = m.choose_action(quiet, base, relation)
check("少打扰优先", quiet_action.name == "stay_quiet")
check("少打扰不唤醒", not m.should_wake(quiet_action, quiet)[0])

follow = m.TurnContext("g", "u", "然后呢", continuation=True)
check("续聊动机", m.choose_action(follow, base, relation).name == "follow_up")

negative = m.TurnContext("g", "u", "你这次说错了", sentiment=-0.8, directed=True)
rel = m.update_relation(relation, negative, 1100)
check("负面互动降低信任", rel.trust < relation.trust, rel)

old = m.PersonaState(irritation=0.8, updated_at=1000)
decayed = m.update_state(old, m.TurnContext("g", "u", ""), 1000 + m.HALF_LIFE)
check("情绪自然衰减", 0 < decayed.irritation < old.irritation, decayed)

print("PERSONA_LOGIC_TEST_OK passed=%d failed=%d" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
