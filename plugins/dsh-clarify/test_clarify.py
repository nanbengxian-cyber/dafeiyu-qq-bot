# -*- coding: utf-8 -*-
"""dsh-clarify 纯逻辑回归测试。"""
import ast
import json
from pathlib import Path

src = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
tree = ast.parse(src)
want = {"sanitize_question", "parse_result", "render", "_signature", "decide_action"}
nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in want]
ns = {"re": __import__("re"), "json": json}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "logic", "exec"), ns)

sanitize = ns["sanitize_question"]
parse = ns["parse_result"]
render = ns["render"]
sig = ns["_signature"]
decide = ns["decide_action"]

assert decide("connected", True) == "pass"
assert decide("new_clear", False) == "pass"
assert decide("unclear", True) == "ask"
assert decide("unclear", False) == "silent"
assert decide(None, True) == "ask"
assert decide(None, False) == "silent"
assert decide("unclear", True, duplicate=True) == "pass"
assert decide("unclear", False, duplicate=True) == "silent"

assert parse('{"state":"connected","missing":"","question":"","topic":"语音叫名字","revisit":false}') == {
    "state": "connected", "missing": "", "question": "", "topic": "语音叫名字", "revisit": False}
assert parse('{"state":"new_clear","missing":"","question":""}')["state"] == "new_clear"
r = parse('```json\n{"state":"unclear","missing":"对象","question":"谁又来了","topic":"来的人","revisit":true}\n```')
assert r == {"state": "unclear", "missing": "对象", "question": "谁又来了？",
             "topic": "来的人", "revisit": True}
assert parse('{"state":"guess","missing":"","question":""}') is None
assert parse('废话') is None
assert sanitize("请提供更多上下文") == ""
assert sanitize("啥不行") == "啥不行？"
assert sanitize("这是一个非常非常非常非常非常非常长的客服问题") == ""
block = render("谁又来了", "对象")
assert block.startswith("<clarification>") and block.endswith("</clarification>")
assert "谁又来了？" in block and "不能靠猜" in block and "客服腔" in block
assert sig("1", " 他又来了？！ ") == sig("1", "他又来了")
assert sig("1", "他又来了") != sig("2", "他又来了")

# Prompt 的示例 JSON 必须转义为双花括号，否则 format 会把它当占位符。
start = src.index('PROMPT = """')
end = src.index('"""', start + len('PROMPT = """')) + 3
ns2 = {}
exec(src[start:end], {}, ns2)
out = ns2["PROMPT"].format(transcript="甲：他又来了")
assert "甲：他又来了" in out
assert '{"state":"connected"' in out

# 结构约束：未点名 unclear 必须可 stop，点名 unclear 只注入追问。
assert 'if addressed:' in src
assert 'event.stop_event()' in src
assert 'TextPart(text=render(' in src
assert 'event.set_extra("dsh_topic_key"' in src
assert 'event.set_extra("dsh_topic_revisit"' in src
assert 'event.get_extra("dsh_initiate")' in src
assert 'event.get_extra("dsh_proactive")' in src
assert 'for row in rows:' in src and 'reversed(prior[-LOOKBACK:])' in src

# 2026-09-13：「不知道谁艾特它」根因是分类器不知道最后一条是点名。
# 被点名时 transcript 必须把最后一条标成「@ 机器人」，prompt 也要有对应规则。
assert "_recent(gid, current, addressed: bool = False)" in src or "_recent(gid, text, addressed)" in src
assert "（这条是 @ 机器人 才说的）%s" % "" in src or "（这条是 @ 机器人 才说的）" in src
assert "特意 @ 机器人（点名）才说的" in src and "判 connected" in src
# 调用点要把 addressed 传进去
assert "transcript, context_count = _recent(gid, text, addressed)" in src

print("CLARIFY_TEST_OK")
