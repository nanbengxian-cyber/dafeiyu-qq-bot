# -*- coding: utf-8 -*-
"""只读回放 dsh-agency 分类器；不写数据库、不改变生产状态。"""

import collections
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

PLUGIN = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("agency_logic.py")
DB = sys.argv[2] if len(sys.argv) > 2 else "/AstrBot/data/dsh_memory.db"
GROUP = sys.argv[3] if len(sys.argv) > 3 else ""
BOT_NAMES = ("大肥鱼", "肥鱼", "小鲸鱼", "鲸鱼娘")

spec = importlib.util.spec_from_file_location("agency_replay_logic", PLUGIN)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
where = " WHERE group_id=?" if GROUP else ""
params = (GROUP,) if GROUP else ()
rows = con.execute(
    "SELECT group_id,user_id,name,text,ts FROM archive%s UNION ALL "
    "SELECT group_id,user_id,name,text,ts FROM buffer%s ORDER BY ts" % (where, where),
    params + params,
).fetchall()
con.close()

counts = collections.Counter()
samples = collections.defaultdict(list)
by_user_last = {}
pressure = {}
protected_reject = 0
for gid, uid, name, raw, ts in rows:
    text = str(raw or "").strip()
    directed = any(n in text for n in BOT_NAMES)
    fp = m.fingerprint(text)
    key = (str(gid), str(uid))
    previous = by_user_last.get(key)
    same = bool(previous and previous[0] == fp and float(ts) - previous[1] <= 300)
    repeat = previous[2] + 1 if same else 0
    a = m.assess(text, directed=directed, same_fingerprint=same, repeat_count=repeat)
    before = pressure.get(key, {"reactance": m.BASELINE, "updated_at": float(ts)})
    state = m.transition(before, a, float(ts))
    mode = m.behavior_mode(state["reactance"], a, live=True)
    pressure[key] = state
    by_user_last[key] = (fp, float(ts), repeat)
    counts[a.reason] += 1
    counts["mode:" + mode] += 1
    if a.delta > 0 and len(samples[a.reason]) < 12:
        samples[a.reason].append({"name": str(name or ""), "text": text[:90], "delta": a.delta,
                                  "reactance": round(state["reactance"], 1), "mode": mode})
    if a.protected_task and mode == "refuse_boundary":
        protected_reject += 1

print(json.dumps({
    "rows": len(rows),
    "groups": len({str(r[0]) for r in rows}),
    "users": len({(str(r[0]), str(r[1])) for r in rows}),
    "counts": dict(counts),
    "positive_rate": round(100 * sum(v for k, v in counts.items() if not k.startswith("mode:") and k not in {"ordinary", "not_directed", "ignored", "polite", "boundary_respected"}) / max(1, len(rows)), 3),
    "protected_rejects": protected_reject,
    "samples": dict(samples),
}, ensure_ascii=False, indent=2))
