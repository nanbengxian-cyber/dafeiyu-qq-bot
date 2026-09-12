"""dsh-web 安全边界的纯逻辑回归测试。"""
import ast
import ipaddress
import json
from pathlib import Path

src = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
ast.parse(src)

# TLS 验证不能被关闭；短链也不能自动跟随未复验的重定向。
assert "ssl=False" not in src
assert "allow_redirects=True" not in src
assert "_regex_block_hit(info)" not in src
assert "if not ip.is_global:" in src
assert "connector=_public_connector()" in src
assert "use_dns_cache=False" in src

# Python 对 CGNAT shared space 的 private/reserved 判定不可靠，global 准入必须拦。
for value in ("127.0.0.1", "169.254.169.254", "100.64.0.1", "100.100.100.100", "::1"):
    assert not ipaddress.ip_address(value).is_global, value

# 抽出 _mod_parse，隔离 AstrBot 依赖执行严格 schema 测试。
tree = ast.parse(src)
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_mod_parse")
node.returns = None
for arg in node.args.args:
    arg.annotation = None
module = ast.Module(body=[node], type_ignores=[])
ns = {"json": json, "re": __import__("re")}
exec(compile(module, "main.py", "exec"), ns)
parse = ns["_mod_parse"]
valid = {
    "politics": False,
    "sensitive_figure": False,
    "nsfw": False,
    "illegal": False,
    "hate": False,
    "reason": "",
    "can_send": True,
}
assert parse(json.dumps(valid)) == valid
for bad in (
    {**valid, "politics": "true"},
    {**valid, "can_send": "false"},
    {**valid, "can_send": 1},
    {**valid, "reason": None},
    {k: v for k, v in valid.items() if k != "hate"},
):
    assert parse(json.dumps(bad)) is None, bad

# 审核缓存必须绑定完整内容，不能只绑定 URL 或前 500 字。
assert "hashlib.sha256(key_material).hexdigest()" in src
assert "blob[:500]" not in src
assert "blob[:2000]" not in src
assert "prompt=_MOD_PROMPT.format(content=blob)" in src
assert "url.strip() if (url or \"\").strip()" not in src

# 主出口取消必须上传播，普通异常则进入 fail-closed URL 清理。
body = src.split("async def strip_leaks", 1)[1].split("async def _outbound_gate", 1)[0]
assert "except asyncio.CancelledError:" in body and "raise" in body
assert "except BaseException" not in body
assert "BARE_RE.sub" in body and "_REPLY_URL_RE.sub" in body

print("WEB_SECURITY_TEST_OK")
