"""主动话题多插件联动纯逻辑测试。"""

import json
import sqlite3
import tempfile
from pathlib import Path

from topic_sources import (
    choose_topic, collect_topics, load_group_fact_topics, load_interest_topics,
    load_self_topics, safe_group_fact, search_query, topic_key,
)

root = Path(tempfile.mkdtemp(prefix="initiate-topic-test-"))
mem = root / "memory.db"
con = sqlite3.connect(mem)
con.execute("CREATE TABLE facts(group_id TEXT,user_id TEXT,kind TEXT,content TEXT,weight REAL,updated_at REAL)")
con.executemany("INSERT INTO facts VALUES(?,?,?,?,?,?)", [
    ("g", "", "梗", "群里常用乐子指代找有趣的事", 3, 100),
    ("g", "", "其他", "群里曾讨论奶汤鱼头怎么做", 2, 101),
    ("g", "u1", "爱好", "某群友喜欢猫", 99, 102),  # 个人档案绝不能进主动候选
    ("g", "", "隐私", "群主住址是某地", 99, 103),
    ("g", "", "其他", "服务器 token 是 abc", 99, 104),
])
con.commit(); con.close()
rows = load_group_fact_topics(str(mem), "g")
assert [r["topic"] for r in rows] == ["用乐子指代找有趣的事", "讨论奶汤鱼头怎么做"]
assert all("某群友" not in r["hint"] and "token" not in r["hint"] for r in rows)
assert rows[0]["search_query"] == "乐子指代找有趣的事"
assert rows[1]["search_query"] == "奶汤鱼头怎么做"
assert search_query("聊天状态") == "聊天状态"
assert search_query("群主视频登上热搜推荐") == ""
assert search_query("服务器 token abc") == ""
assert safe_group_fact("梗", "群里喜欢聊美食")
assert not safe_group_fact("隐私", "住址")
assert not safe_group_fact("其他", "验证码 123456")

interest = root / "interest.json"
interest.write_text(json.dumps({
    "mood": {"items": ["深夜食堂", "甜品"]},
    "hot": {"g": {"AI": 2.4, "美食": 1.8}},
}, ensure_ascii=False), encoding="utf-8")
interests = load_interest_topics(str(interest), "g")
assert {x["topic"] for x in interests} == {"深夜食堂", "甜品", "AI", "美食"}

selfdb = root / "self.db"
con = sqlite3.connect(selfdb)
con.execute("CREATE TABLE sense_event(id INTEGER PRIMARY KEY,capability TEXT,status TEXT,ts REAL)")
con.executemany("INSERT INTO sense_event(capability,status,ts) VALUES(?,?,?)", [
    ("vision", "recovered", 200), ("vision", "ok", 100),
    ("web", "degraded", 150), ("unknown", "ok", 300),
])
con.commit(); con.close()
self_rows = load_self_topics(str(selfdb))
assert [x["topic"] for x in self_rows] == ["看图能力", "联网能力"]
assert all("IP" not in x["hint"] and "磁盘" not in x["hint"] for x in self_rows)

all_rows = collect_topics(str(mem), str(interest), str(selfdb), "g")
assert any(x["source"] == "memory" for x in all_rows)
assert any(x["source"] == "interest" for x in all_rows)
assert any(x["source"] == "self" for x in all_rows)

now = 1_000_000
picked = choose_topic(all_rows, {}, now, 3 * 86400)
assert picked and picked["source"] == "memory"  # 权重高的群共同记忆优先
recent = {picked["key"]: now - 1}
picked2 = choose_topic(all_rows, recent, now, 3 * 86400)
assert picked2 and picked2["key"] != picked["key"]
assert choose_topic([picked], {picked["key"]: now}, now, 3 * 86400) is None
assert topic_key("奶汤鱼头") == topic_key("奶汤 鱼头！")

src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
assert "collect_topics(" in src
assert 'facts["topic_candidate"]' in src
assert 'event.set_extra("dsh_initiate_topic_key"' in src
assert "不要点名或暴露任何人的个人档案" in src
assert "不要机械播报状态" in src
assert "initiative_topic" in src
print("INITIATE_TOPIC_LINK_TEST_OK")
