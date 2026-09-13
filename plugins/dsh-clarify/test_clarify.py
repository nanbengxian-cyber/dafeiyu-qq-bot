# -*- coding: utf-8 -*-
"""dsh-clarify 纯逻辑回归测试。"""
import ast
import json
import re
from pathlib import Path

src = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
tree = ast.parse(src)
want = {"sanitize_question", "parse_result", "render", "_signature", "decide_action",
        "short_and_addressed"}
nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in want]
ns = {"re": __import__("re"), "json": json}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "logic", "exec"), ns)

# 模块级常量：纯函数 short_and_addressed 依赖 _AMBIGUOUS，沙箱里要一起注入。
_amb = re.search(r'^_AMBIGUOUS = .*$', src, re.M)
assert _amb, "未找到 _AMBIGUOUS 定义"
exec(_amb.group(0), ns)

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

# 生产==镜像 一致；被点名的短寒暄必须放行（0913 群主反馈「不知道谁艾特它」）
sa = ns["short_and_addressed"]
assert sa("爱你", True) is True
assert sa("想你了", True) is True
assert sa("踩踩背", True) is True
assert sa("顶你", True) is True
assert sa("戳你", True) is True
# 没点名不算（未点名的短句另有 unclear/沉默逻辑管）
assert sa("爱你", False) is False
# 短但含指代/疑问指向的，仍交给分类器
assert sa("这啥", True) is False
assert sa("谁", True) is False
assert sa("干嘛", True) is False
assert sa("弄疼谁", True) is False
# 长句不受影响
assert sa("你是在跟谁说爱你", True) is False
assert "short_and_addressed(text, addressed)" in src

print("CLARIFY_TEST_OK")
