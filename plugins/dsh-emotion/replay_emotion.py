"""在容器里回放真实归档，量出情绪规则的命中分布与可疑样本。

用法（容器内）：
    python3 /tmp/replay_emotion.py [插件路径]
"""
import collections
import importlib.util
import json
import sqlite3
import sys

PLUGIN = sys.argv[1] if len(sys.argv) > 1 else "/AstrBot/data/plugins/dsh-emotion/main.py"
DB = "/AstrBot/data/dsh_memory.db"

spec = importlib.util.spec_from_file_location("emotion_replay", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = con.execute(
    "SELECT group_id,user_id,name,text,ts FROM archive UNION ALL "
    "SELECT group_id,user_id,name,text,ts FROM buffer ORDER BY ts"
).fetchall()
con.close()

state = mod.default_state()
hits = collections.Counter()
reasons = collections.Counter()
samples = collections.defaultdict(list)
directed = collections.Counter()
# 情绪连续停留时长：上线前要确认没有「一直生气下不来」
holds = collections.Counter()
current = state["emotion"]
run_len = 0
max_run = collections.Counter()

for gid, uid, name, text, ts in rows:
    text = str(text or "")
    try:
        candidate = mod.extract_candidate(text, False)
    except TypeError:
        candidate = mod.extract_candidate(text)
    state, reason = mod.transition(state, candidate, float(ts))
    reasons[reason] += 1
    if candidate:
        emotion = candidate["emotion"]
        hits[emotion] += 1
        directed["有指向" if candidate.get("directed") else "无指向"] += 1
        if len(samples[emotion]) < 5:
            samples[emotion].append(text[:70])
    if state["emotion"] == current:
        run_len += 1
    else:
        max_run[current] = max(max_run[current], run_len)
        current, run_len = state["emotion"], 1
    holds[state["emotion"]] += 1
max_run[current] = max(max_run[current], run_len)

print(json.dumps({
    "rows": len(rows),
    "triggers": sum(hits.values()),
    "by_emotion": dict(hits),
    "reasons": dict(reasons),
    "directed": dict(directed),
    "state_occupancy": dict(holds),
    "longest_consecutive_state": {k: v for k, v in max_run.items() if v},
    "final_state": state["emotion"],
    "samples": dict(samples),
}, ensure_ascii=False, indent=2))
