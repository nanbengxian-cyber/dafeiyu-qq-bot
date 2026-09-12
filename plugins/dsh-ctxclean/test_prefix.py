"""dsh-ctxclean 历史截断回归：持久前缀不能被删。"""
import ast
from pathlib import Path

src = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
tree = ast.parse(src)
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_keep_recent_turns")
node.returns = None
for arg in node.args.args:
    arg.annotation = None
ns = {}
exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), ns)
keep = ns["_keep_recent_turns"]

msgs = [
    {"role": "system", "content": "persistent-system"},
    {"role": "developer", "content": "persistent-developer"},
    {"role": "user", "content": "u1"},
    {"role": "assistant", "content": "a1"},
    {"role": "user", "content": "u2"},
    {"role": "assistant", "tool_calls": [{"id": "x"}], "content": ""},
    {"role": "tool", "tool_call_id": "x", "content": "r"},
    {"role": "assistant", "content": "a2"},
    {"role": "user", "content": "u3"},
]
out = keep(msgs, 2)
assert [m["role"] for m in out[:2]] == ["system", "developer"]
assert [m["content"] for m in out if m.get("role") == "user"] == ["u2", "u3"]
assert out[2]["role"] == "user"
assert out == keep(out, 2)
# 保留的 tool 前面仍有发起 tool_calls 的 assistant。
for i, msg in enumerate(out):
    if msg.get("role") == "tool":
        assert i > 0 and out[i - 1].get("role") == "assistant"
        assert out[i - 1].get("tool_calls")
print("CTXCLEAN_PREFIX_TEST_OK")
