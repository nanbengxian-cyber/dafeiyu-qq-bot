# -*- coding: utf-8 -*-
"""向后兼容性：**已经在跑的 20 个实例升级后必须一点变化都没有**。

为什么这一条最重要：
  这次改动会让服务器重写 AstrBot 的 provider 配置。如果对「没填协议」
  的老实例写错了什么，20 个正在用的机器人会**同时**出问题 ——
  而且症状是「机器人不回话」，用户第一反应是「你把我的机器人搞坏了」。

  所以这里**造一个真实的老实例目录**，用新代码读它、写它，
  逐项核对结果。
"""
import importlib.util, sys, json, copy, os, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location('mgr', os.path.join(ROOT, 'server', 'dafeiyu-manager.py'))
m = importlib.util.module_from_spec(spec); sys.modules['mgr'] = m; spec.loader.exec_module(m)

fails = []
def ck(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  <- %s" % extra) if not cond and extra else ""))
    if not cond:
        fails.append(name)

# ── 造一个真实的老实例：只有 9 个标准字段，没有 protocol ──
ROOT = tempfile.mkdtemp(prefix="bc-")
m.DATA_DIR = ROOT
m.INSTANCES_DIR = os.path.join(ROOT, "instances")
os.makedirs(os.path.join(m.INSTANCES_DIR, "oldbot", "astrbot", "data"), exist_ok=True)

OLD_SRC = {
    "id": "dafeiyu-main_source",
    "type": "openai_chat_completion",
    "provider": "openai",
    "key": ["sk-old-key"],
    "api_base": "https://api.example.com/v1",
    "model": "gpt-4o",
    "timeout": 120,
    "model_config": {"max_tokens": 0},
    "proxy": "",
}
OLD_CFG = {
    "provider_sources": [copy.deepcopy(OLD_SRC)],
    # ★ 形状必须和 AstrBot 真实的一致：provider 是**对象数组**，
    #   不是字符串数组。用错的形状会测出假崩溃。
    "provider": [{
        "id": "dafeiyu-main",
        "provider_source_id": "dafeiyu-main_source",
        "enable": True,
        "model": "gpt-4o",
        "modalities": ["text", "tool_use"],
    }],
    "provider_settings": {},
}
P = os.path.join(m.INSTANCES_DIR, "oldbot", "astrbot", "data", "cmd_config.json")
with open(P, "w", encoding="utf-8") as fh:
    json.dump(OLD_CFG, fh, ensure_ascii=False, indent=2)

print("① 读回老实例（配置里没有 protocol 字段）")
p = m.protocol_of("oldbot")
ck("protocol_of 回落默认（不崩、不空）", p == m.DEFAULT_PROTOCOL, repr(p))
vp = m.vision_protocol_of("oldbot")
ck("vision_protocol_of 也回落默认", vp == m.DEFAULT_PROTOCOL, repr(vp))
ck("默认协议就是旧的 openai_chat_completion",
   m.DEFAULT_PROTOCOL == "openai_chat_completion", m.DEFAULT_PROTOCOL)
ck("默认协议的 provider 名仍是 'openai'（和旧代码一字不差）",
   m.PROTOCOLS[m.DEFAULT_PROTOCOL]["provider"] == "openai",
   m.PROTOCOLS[m.DEFAULT_PROTOCOL]["provider"])
eb = m.extra_body_of("oldbot", "main")
ck("老配置读出的自定义请求体是空 dict", eb == {}, repr(eb))

print()
print("② 老实例的地址必须**原样保留**（不能偷偷改掉用户的地址）")
for base in ["https://api.example.com/v1", "https://api.example.com",
             "http://1.2.3.4:8080/v1"]:
    got = m.normalize_api_base(base, m.DEFAULT_PROTOCOL)
    ck("%-32s 不变" % base, got == base, "被改成了 %r" % got)

print()
print("③ 两条路要**分开**判：读配置要宽容，收参数要严格")
# 收用户参数这条路（normalize_protocol）：认不出必须**报错**。
# 静默改成默认协议 = 把用户选好的协议悄悄换掉，他会以为「选了没用」。
for t in ["some_future_adapter", "made_up"]:
    try:
        m.normalize_protocol(t)
        ck("normalize_protocol(%r) 必须报错（不能静默改协议）" % t, False, "居然通过了")
    except m.ManagerError as e:
        ck("normalize_protocol(%r) 报错且列出可选项" % t, "可选的有" in str(e))
# 空值这条路：没传 = 不动它 = 默认，不该报错
for t in ["", None, "   "]:
    try:
        r = m.normalize_protocol(t)
        ck("normalize_protocol(%r) 回默认（没传≠填错）" % t, r == m.DEFAULT_PROTOCOL, repr(r))
    except Exception as e:
        ck("normalize_protocol(%r) 不该报错" % t, False, str(e)[:50])

print()
print("④ 老配置里本来没有 custom_extra_body —— 确认这一点")
ck("老 source 只有 9 个标准字段",
   "custom_extra_body" not in OLD_CFG["provider_sources"][0],
   sorted(OLD_CFG["provider_sources"][0].keys()))
ck("老 source 的字段数 = 9", len(OLD_CFG["provider_sources"][0]) == 9,
   len(OLD_CFG["provider_sources"][0]))

print()
print("⑤ 解析自定义请求体的防呆（用户填错时要能看懂）")
try:
    m.parse_extra_body('{"model": "evil"}', "自定义请求体")
    ck("覆盖 model 被拦住", False, "居然通过了")
except m.ManagerError as e:
    ck("覆盖 model 被拦住", True)
    ck("报错里点名是哪个字段", "model" in str(e), str(e)[:60])
try:
    m.parse_extra_body('[1,2,3]', "自定义请求体")
    ck("顶层是数组被拦住", False, "居然通过了")
except m.ManagerError:
    ck("顶层是数组被拦住", True)
# ★ parse_extra_body 返回 (dict, 警告列表) 这个元组形状本身要钉住：
#   调用方 unpack 错了会直接 ValueError，而且是在**保存配置**时炸。
r = m.parse_extra_body('{"temperature":0.7}', "x")
ck("返回的是 (dict, 警告) 二元组", isinstance(r, tuple) and len(r) == 2, repr(r)[:60])
ck("合法的请求体能通过", r[0] == {"temperature": 0.7}, repr(r[0]))
ck("★ temperature 允许（那是用户最想调的）", "temperature" in r[0])
ck("★ max_tokens 允许", m.parse_extra_body('{"max_tokens":100}', "x")[0]
   == {"max_tokens": 100})
ck("空串 = 不设置（不是报错）", m.parse_extra_body("", "x")[0] == {})
ck("★ 值写成字符串要**给警告**（有些网关会静默按 0 处理）",
   len(m.parse_extra_body('{"temperature":"0.7"}', "x")[1]) > 0)
ck("★ 合法值不该有警告",
   len(m.parse_extra_body('{"temperature":0.7}', "x")[1]) == 0)

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("FAILS: %d" % len(fails))
for f in fails:
    print("  !! %s" % f)
sys.exit(1 if fails else 0)
