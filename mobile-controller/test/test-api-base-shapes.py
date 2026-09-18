# -*- coding: utf-8 -*-
"""地址规范化：用户填的各种形状都必须拼出**能用的** URL。

为什么这条值得单独测：
  用户填地址时是从**别家**的配置复制过来的。OpenAI 兼容那套的习惯是
  `https://xxx/v1`，所以切到 Anthropic / Gemini 时，那个 /v1 常常留着。
  各家适配器对 api_base 的处理**各不相同**（Anthropic 自己剥 /v1、
  Gemini 只剥结尾 /），所以「用户多填一个 /v1」会拼出 /v1/v1/... 这种 404。

  而 404 的报错会指向「地址写错了」—— 用户会去反复改地址，永远改不好。
  这正是这次要修的那类「报错指错方向」的问题。
"""
import importlib.util, sys, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location('mgr', os.path.join(ROOT,'server','dafeiyu-manager.py'))
m = importlib.util.module_from_spec(spec); sys.modules['mgr']=m; spec.loader.exec_module(m)

fails = []
def ck(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  <- %s" % extra) if not cond and extra else ""))
    if not cond:
        fails.append(name)

print("① Anthropic：用户填 /v1 时必须剥掉（适配器自己也剥，两边要一致）")
# 依据 anthropic_source.py:97-98：先 rstrip("/")，再 removesuffix("/v1")
for inp in ["https://api.anthropic.com", "https://api.anthropic.com/",
            "https://api.anthropic.com/v1", "https://api.anthropic.com/v1/"]:
    got = m.normalize_api_base(inp, "anthropic_chat_completion")
    ck("%-38s -> %s" % (inp, got), got == "https://api.anthropic.com", got)
url = m.protocol_endpoint("https://api.anthropic.com/v1", "anthropic_chat_completion", "chat")
ck("★ 拼出来是 /v1/messages（不是 /v1/v1/messages）",
   url == "https://api.anthropic.com/v1/messages", url)

print()
print("② Gemini：用户填 /v1 或 /v1beta 时都必须剥掉")
for inp, want in [
    ("https://generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com"),
    ("https://generativelanguage.googleapis.com/", "https://generativelanguage.googleapis.com"),
    ("https://generativelanguage.googleapis.com/v1", "https://generativelanguage.googleapis.com"),
    ("https://generativelanguage.googleapis.com/v1/", "https://generativelanguage.googleapis.com"),
    ("https://generativelanguage.googleapis.com/v1beta", "https://generativelanguage.googleapis.com"),
]:
    got = m.normalize_api_base(inp, "googlegenai_chat_completion")
    ck("%-48s -> %s" % (inp, got), got == want, got)
# ★ 这条是实测抓到的 bug：修复前会拼出 /v1/v1beta/...
for inp in ["https://generativelanguage.googleapis.com/v1",
            "https://generativelanguage.googleapis.com/v1beta"]:
    u = m.protocol_endpoint(inp, "googlegenai_chat_completion", "chat", "gemini-2.0-flash")
    ck("★ %s 拼出的 URL 里没有重复版本段" % inp.split("/")[-1],
       "/v1/v1beta" not in u and u.count("/v1beta") == 1, u)
u = m.protocol_endpoint("https://x.example", "googlegenai_chat_completion", "chat", "gem")
ck("★ 模型名在路径里（Gemini 的规定）",
   u == "https://x.example/v1beta/models/gem:generateContent", u)
ck("Gemini 没有模型名时返回 None（拼不出来，不能瞎拼）",
   m.protocol_endpoint("https://x.example", "googlegenai_chat_completion", "chat") is None)

print()
print("③ OpenAI 兼容：**不能**动用户的地址（那是最常见的情况）")
for inp in ["https://api.deepseek.com/v1", "https://api.openai.com/v1",
            "http://1.2.3.4:8080/v1"]:
    got = m.normalize_api_base(inp, "openai_chat_completion")
    ck("%-32s 原样保留" % inp, got == inp, got)
ck("OpenAI 兼容拼出 /chat/completions",
   m.protocol_endpoint("https://api.deepseek.com/v1", "openai_chat_completion", "chat")
   == "https://api.deepseek.com/v1/chat/completions")

print()
print("④ Responses：没有 /models 端点（返回 None，不能瞎拼一个）")
ck("Responses 的 models 端点返回 None",
   m.protocol_endpoint("https://api.openai.com/v1", "openai_responses", "models") is None)
ck("Responses 的 chat 端点是 /responses",
   m.protocol_endpoint("https://api.openai.com/v1", "openai_responses", "chat")
   == "https://api.openai.com/v1/responses")

print()
print("⑤ 空地址：原样返回空（不能崩、也不能拼出奇怪的串）")
ck("空串返回空", m.normalize_api_base("", "openai_chat_completion") == "")
ck("None 返回空", m.normalize_api_base(None, "openai_chat_completion") == "")

print()
print("FAILS: %d" % len(fails))
for f in fails:
    print("  !! %s" % f)
if fails:
    print()
    print("地址拼错的后果：报错会说「地址写错了」，于是用户反复改地址，")
    print("永远改不好 —— 而真正的问题在程序拼路径的方式上。")
sys.exit(1 if fails else 0)
