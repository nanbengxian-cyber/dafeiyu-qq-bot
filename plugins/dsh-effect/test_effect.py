"""dsh-effect 离线回测。

不联网、不碰真库：建临时库验证表结构和写入，用构造的模型输出验证解析
（重点是**脏输出绝不能写进库**：取值不在枚举里要退回安全值）。
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

if "astrbot" not in sys.modules:
    try:
        import astrbot  # noqa: F401
    except ImportError:
        for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.core"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
        sys.modules["astrbot.api.event"].AstrMessageEvent = object
        sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
            after_message_sent=lambda: (lambda f: f),
            on_astrbot_loaded=lambda: (lambda f: f),
            command=lambda *a, **k: (lambda f: f))
        sys.modules["astrbot.core"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            error=lambda *a, **k: None, debug=lambda *a, **k: None,
            exception=lambda *a, **k: None)

tmp = tempfile.mkdtemp()
os.environ["DSH_EFFECT_DB"] = os.path.join(tmp, "t.db")
spec = importlib.util.spec_from_file_location("effect", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# ------------------------------------------------------------ 建库 / 幂等
m.init_db()
m.init_db()                                  # 再来一次不许报错
con = sqlite3.connect(m.DB)
cols = [r[1] for r in con.execute("pragma table_info(reply)")]
for need in ("id", "group_id", "ts", "due", "text", "addressed", "status",
             "reactions", "strategy", "stance", "target", "contribution", "why"):
    assert need in cols, need
idx = [r[0] for r in con.execute(
    "select name from sqlite_master where type='index' and tbl_name='reply'")]
assert any("due" in i for i in idx), idx
print("  建库 OK，字段 %d 个，索引 %s" % (len(cols), idx))

# ------------------------------------------------------------ 解析：正常
v = m.parse_verdict(
    '{"strategy":"humor","stance":"playful","target":"bot_persona",'
    '"contribution":"advance","why":"群友接着玩起来了"}')
assert v == {"strategy": "humor", "stance": "playful", "target": "bot_persona",
             "contribution": "advance", "why": "群友接着玩起来了"}, v

# ------------------------------------------------------------ 解析：模型爱加壳
v = m.parse_verdict('```json\n{"strategy":"answer","stance":"neutral",'
                    '"target":"topic","contribution":"maintain","why":"平淡接着说"}\n```')
assert v["strategy"] == "answer" and v["stance"] == "neutral", v
v = m.parse_verdict('好的，结论如下：{"stance":"appreciation"} 希望有帮助')
assert v["stance"] == "appreciation"
assert v["strategy"] == "other" and v["target"] == "none"      # 缺的退默认

# ------------------------------------------------------------ 解析：脏值必须被挡住
v = m.parse_verdict('{"stance":"很棒","target":"随便","contribution":"???",'
                    '"strategy":"讲笑话"}')
assert v["stance"] == "neutral", v          # 不在枚举 -> 安全值
assert v["target"] == "none" and v["contribution"] == "none"
assert v["strategy"] == "other"
# 大小写和空格要能容忍
v = m.parse_verdict('{"stance":" PLAYFUL ","strategy":"Humor"}')
assert v["stance"] == "playful" and v["strategy"] == "humor", v
# why 要截断，别让模型灌一整段进库
v = m.parse_verdict('{"stance":"neutral","why":"%s"}' % ("啊" * 200))
assert len(v["why"]) <= 60

# ------------------------------------------------------------ 解析：完全解析不出
for bad in ("", "   ", "抱歉我无法完成", "[1,2,3]", "null", "{坏掉的"):
    assert m.parse_verdict(bad) is None, bad

# ------------------------------------------------------------ 解析：输出被截断（上线第一天的真实失败样本）
# 模型把 why 写太长导致 completion 被截断，JSON 缺收尾的 } —— 但四个枚举字段都在
# 截断点之前，必须救回来，不能白扔掉一条已经花过钱的评分。
truncated = ('{"strategy":"humor","stance":"rejection","target":"bot_persona",'
             '"contribution":"wrong_push","why":"某群友骂人，另一个说绷不住了，')
v = m.parse_verdict(truncated)
assert v is not None, "截断的 JSON 必须能救回来"
assert v["strategy"] == "humor" and v["stance"] == "rejection", v
assert v["target"] == "bot_persona" and v["contribution"] == "wrong_push", v
# 只剩残缺的 why、一个枚举字段都没有时，仍然算解析失败
assert m.parse_verdict('{"why":"只有理由没有结论') is None
# 逐字段捞出来的脏值同样要过枚举校验
assert m.parse_verdict('{"stance":"超级棒","strategy":"讲笑话"')["stance"] == "neutral"

# ------------------------------------------------------------ 枚举本身
assert "ignored" in m.STANCES            # 「没人理」是真群里最常见的结果
assert "bot_persona" in m.TARGETS        # 冲人设 vs 冲内容必须分开
assert "wrong_push" in m.CONTRIBS
for group in (m.STANCES, m.TARGETS, m.CONTRIBS, m.STRATEGIES):
    assert len(set(group)) == len(group)
    for k in group:
        assert k in m._ZH, k             # 每个取值都要有中文，否则状态面板出英文

# ------------------------------------------------------------ 旋钮与边界
# 群名单必须显式配置：**不配 env 就是空集**（fail-safe，不作用于任何群）。
# 这样断言而不是写死某个群号，开源版把默认值清空后同一份测试照样通过。
assert isinstance(m.GROUPS, set)
assert m._set("DSH_EFFECT_GROUPS_DEFINITELY_NOT_SET") == set()
assert m._set("DSH_EFFECT_GROUPS_DEFINITELY_NOT_SET", "a, b") == {"a", "b"}
assert m.WINDOW >= 30 and m.TICK >= 15
assert m.BATCH >= 1 and m.MAX_AFTER >= 2
assert m.DB != m.MEM_DB, "结果库不能和语料库是同一个文件"

# ------------------------------------------------------------ 写入 -> 读出
con.execute("INSERT INTO reply(group_id,ts,due,text,addressed,status)"
            " VALUES('100000001',1000,1180,'测试',0,'pending')")
con.commit()
rid = con.execute("select id from reply").fetchone()[0]
con.execute("UPDATE reply SET status='done',reactions=2,stance='playful' WHERE id=?",
            (rid,))
con.commit()
row = con.execute("select status,reactions,stance from reply where id=?", (rid,)).fetchone()
assert row == ("done", 2, "playful"), row
con.close()

print("EFFECT_TEST_OK stances=%d targets=%d contribs=%d strategies=%d window=%.0fs"
      % (len(m.STANCES), len(m.TARGETS), len(m.CONTRIBS), len(m.STRATEGIES), m.WINDOW))
