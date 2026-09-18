# -*- coding: utf-8 -*-
"""静态检查：接口协议（protocol）与自定义请求体的接线是否真的接上了。

为什么需要它：这个项目反复踩同一个坑 —— **逻辑单测全绿，但生产代码
根本没调用**（「修 APK 不显示二维码」时 WebProxyPath 测得好好的，
实际四个接口没剥 data 外壳）。View 类不进单测面，只能静态扫。

这次要防的风险，每一条都会让用户看到「保存成功」但实际是坏的：
  * 服务器加了 protocol 参数，App 的下拉框没把它提交上去 ——
    用户选了 Anthropic，服务器仍然按 OpenAI 兼容写，机器人不回话；
  * 用户填的自定义请求体没被提交 —— 他调了 temperature，实际没生效，
    而他没有任何办法看出这一点；
  * 读回来的协议没回填到下拉框 —— 用户打开面板看到的是第一个选项，
    一保存就把**本来正确的**协议改成了 OpenAI 兼容；
  * 自定义请求体写进去了但没被读回来 —— 用户以为参数丢了，重复填；
  * App 和服务器两份协议表不一致 —— 用户选了服务器不认的协议。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as fh:
        return fh.read()


def strip_comments(s):
    """去掉 // 和 /* */ 注释。

    手写扫描而不是正则：这个项目踩过正则误删代码的坑 ——
    曾经有检查脚本用正则剥注释，把 "http://127.0.0.1" 里的 // 当注释，
    后半行代码被吃掉，于是既误报失败又漏报真问题。
    """
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            out.append(c); i += 1
            while i < n:
                if s[i] == "\\":
                    out.append(s[i:i + 2]); i += 2; continue
                out.append(s[i])
                if s[i] == '"':
                    i += 1; break
                i += 1
            continue
        if c == "'":
            out.append(c); i += 1
            while i < n:
                if s[i] == "\\":
                    out.append(s[i:i + 2]); i += 2; continue
                out.append(s[i])
                if s[i] == "'":
                    i += 1; break
                i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            while i < n and s[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i += 2
            while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c); i += 1
    return "".join(out)



def ck(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  <- " + str(extra)) if not cond and extra else ""))
    if not cond:
        fails.append(name)


mgr = strip_comments(read("server/dafeiyu-manager.py"))
cli = strip_comments(read("app/src/com/dafeiyu/controller/ManagerClient.java"))
rv = strip_comments(read("app/src/com/dafeiyu/controller/RobotsView.java"))
proto = strip_comments(read("app/src/com/dafeiyu/controller/Protocols.java"))

print("服务器侧：协议表")

# ① 协议表存在且结构完整
ck("有 PROTOCOLS 表", re.search(r"^PROTOCOLS = \{", mgr, re.M) is not None)
ck("有 DEFAULT_PROTOCOL", re.search(r"^DEFAULT_PROTOCOL = ", mgr, re.M) is not None)
ck("有 normalize_protocol（不认识的协议要报错，不能静默写进去）",
   re.search(r"^def normalize_protocol\(", mgr, re.M) is not None)
ck("有 protocol_of（读回当前协议）",
   re.search(r"^def protocol_of\(", mgr, re.M) is not None)
ck("有 vision_protocol_of（识图协议独立于主协议）",
   re.search(r"^def vision_protocol_of\(", mgr, re.M) is not None)

# ② 地址规范化必须和适配器真实行为一致 —— 错了会拼出 /v1/v1
ck("有 normalize_api_base", re.search(r"^def normalize_api_base\(", mgr, re.M) is not None)
# ★ 这一条要查**行为**，不能只查「有这段代码」。
#   实测踩到：只查 `if protocol in (...)` 这行在不在，
#   把剥离逻辑本身换成 `pass` 之后检查照样通过 ——
#   那是典型的「测试看着在测、实际没测」。
#   所以这里直接把函数抠出来、拿真实输入跑一遍。
nab = mgr.split("def normalize_api_base(", 1)[1].split("\ndef ", 1)[0] \
    if "def normalize_api_base(" in mgr else ""
_scope = {}
exec("def normalize_api_base(base, protocol):\n" + nab.split("):", 1)[1], _scope)
_nab = _scope["normalize_api_base"]
ck("★ Anthropic 地址剥掉结尾 /v1（不剥会拼出 /v1/v1/messages）",
   _nab("https://api.anthropic.com/v1", "anthropic_chat_completion")
   == "https://api.anthropic.com",
   _nab("https://api.anthropic.com/v1", "anthropic_chat_completion"))
ck("★ Anthropic 裸域名保持原样",
   _nab("https://api.anthropic.com", "anthropic_chat_completion")
   == "https://api.anthropic.com")
ck("★ OpenAI 兼容的地址原样保留（不能误剥别人的 /v1）",
   _nab("https://api.deepseek.com/v1", "openai_chat_completion")
   == "https://api.deepseek.com/v1",
   _nab("https://api.deepseek.com/v1", "openai_chat_completion"))
ck("★ Gemini 地址去掉结尾斜杠",
   _nab("https://g.com/", "googlegenai_chat_completion") == "https://g.com")
ck("★ 端点拼接有 protocol_endpoint（不能到处手拼路径）",
   re.search(r"^def protocol_endpoint\(", mgr, re.M) is not None)

# ③ 鉴权头必须按协议分 —— 一律 Bearer 会让 Anthropic 返回 401，
#    而 401 被我们翻译成「Key 不对」，用户会去反复复制一个正确的 Key
ck("★ 有 protocol_headers", re.search(r"^def protocol_headers\(", mgr, re.M) is not None)
ph = mgr.split("def protocol_headers(", 1)[1].split("\ndef ", 1)[0]
# 同样查**行为**：把函数抠出来真的调一次。
# 只查字符串的话，把 x-api-key 那行换成 Authorization 也能蒙过去。
# 抠出来的函数依赖模块级的 PROTOCOLS 表（auth 字段），
# 所以把表也一起注入，否则会 NameError —— 检查直接崩掉，
# 而「检查崩了」比「检查误报」更糟：整条防线都不跑了。
_pblock = mgr.split("PROTOCOLS = {", 1)[1].split("\n}\n", 1)[0]
_scope2 = {}
exec("PROTOCOLS = {" + _pblock + "\n}", _scope2)
exec("def protocol_headers(protocol, key):\n" + ph.split("):", 1)[1], _scope2)
_ph = _scope2["protocol_headers"]
_ah = _ph("anthropic_chat_completion", "K")
ck("★ Anthropic 用 x-api-key（不是 Bearer）", _ah.get("x-api-key") == "K", _ah)
ck("★ Anthropic 不带 Authorization（带 Bearer 会被判 401）",
   "Authorization" not in _ah)
ck("★ Anthropic 带 anthropic-version（不带给 400）",
   bool(_ah.get("anthropic-version")))
_gh = _ph("googlegenai_chat_completion", "K")
ck("★ Gemini 用 x-goog-api-key", _gh.get("x-goog-api-key") == "K", _gh)
_oh = _ph("openai_chat_completion", "K")
ck("★ OpenAI 兼容用 Bearer", _oh.get("Authorization") == "Bearer K", _oh)

# ④ 探测请求体必须按协议造
ck("有 protocol_probe_payload", re.search(r"^def protocol_probe_payload\(", mgr, re.M) is not None)
pp = mgr.split("def protocol_probe_payload(", 1)[1].split("\ndef ", 1)[0]
ck("★ Anthropic 探测体带 max_tokens（它是必填，不带给 400）",
   'kind == "anthropic"' in pp and "max_tokens" in pp)
ck("★ Responses 用 input 不用 messages", '"input"' in pp)
ck("★ Gemini 用 contents", '"contents"' in pp)

# ⑤ 探测路径必须走协议（不能还写死 /chat/completions）
ck("★ list_api_models 接受 protocol 参数",
   re.search(r"^def list_api_models\([^)]*protocol", mgr, re.M | re.S) is not None)
ck("★ list_api_models 用 protocol_endpoint（不是写死 /models）",
   "protocol_endpoint(use_base, proto" in mgr)
ck("★ _chat_probe 接受 protocol 参数",
   re.search(r"^def _chat_probe\([^)]*protocol", mgr, re.M | re.S) is not None)
ck("★ _chat_probe 用 protocol_endpoint（不是写死 /chat/completions）",
   'protocol_endpoint(base, proto, "chat"' in mgr)
ck("★ probe_api 接受并回传 protocol",
   re.search(r"^def probe_api\([^)]*protocol", mgr, re.M | re.S) is not None
   and '"protocol": proto' in mgr)
ck("probe_api 回传规范化后的地址（用户能看出地址被用成了什么）",
   '"api_base_effective"' in mgr)

# ⑥ 模型列表的返回形状各家不同，必须分开解析
ck("有 _models_from_body（Gemini/Anthropic 的形状和 OpenAI 不同）",
   re.search(r"^def _models_from_body\(", mgr, re.M) is not None)
mb = mgr.split("def _models_from_body(", 1)[1].split("\ndef ", 1)[0]
ck("★ Gemini 的模型名要剥掉 models/ 前缀", 'split("/")[-1]' in mb)

# ⑦ 自定义请求体：必须解析 + 防呆
ck("有 parse_extra_body", re.search(r"^def parse_extra_body\(", mgr, re.M) is not None)
pe = mgr.split("def parse_extra_body(", 1)[1].split("\ndef ", 1)[0]
ck("★ 拦住覆盖 model（否则等于换模型，界面上看不出来）",
   "RESERVED_BODY_KEYS" in pe)
ck("★ 顶层不是对象要报错（update() 会炸，而且是在回复时炸）",
   "isinstance(obj, dict)" in pe)
ck("解析失败时带上原始报错（用户要知道错在第几个字符）", "json.loads" in pe)
ck("有 RESERVED_BODY_KEYS 常量",
   re.search(r"^RESERVED_BODY_KEYS = \{", mgr, re.M) is not None)
rbk = mgr.split("RESERVED_BODY_KEYS = {", 1)[1].split("}", 1)[0]
ck("★ 不能把 temperature 拦掉（那正是用户最想调的）",
   "temperature" not in rbk)
ck("★ 不能把 max_tokens 拦掉（同上）", "max_tokens" not in rbk)
ck("★ 要拦住 messages", "messages" in rbk)
ck("★ 要拦住 model", "model" in rbk)

# ⑧ 写入侧：协议和请求体必须真的落到配置里
ac = mgr.split("def apply_config(", 1)[1].split("\ndef ", 1)[0] \
    if "def apply_config(" in mgr else ""
ck("★ apply_config 接受 protocol 参数",
   re.search(r"^def apply_config\([^)]*protocol", mgr, re.M | re.S) is not None)
ck("★ apply_config 接受 extra_body 参数",
   re.search(r"^def apply_config\([^)]*extra_body", mgr, re.M | re.S) is not None)
ck("★ 主 source 的 type 来自 PROTOCOLS（不是写死的字符串）",
   '"type": use_proto' in ac)
ck("★ 主 source 的 provider 也来自协议表", 'proto_meta["provider"]' in ac)
ck("★ 自定义请求体写进 custom_extra_body", '"custom_extra_body": use_extra' in ac)
ck("★ 识图 source 的 type 用识图协议（不是主协议）", '"type": use_vproto' in ac)
ck("★ 识图请求体写进识图的 custom_extra_body",
   '"custom_extra_body": use_vextra' in ac)
ck("★ 只改请求体、不动 API 三要素时也要能保存",
   re.search(r"if extra_touched and not api_any:", ac) is not None)
ck("识图协议不跟着主协议走（两者可以不同家）",
   "vision_protocol_of(name)" in ac)

# ⑨ 读取侧要回传协议和请求体
rc = mgr.split("def read_config(", 1)[1].split("\ndef ", 1)[0] \
    if "def read_config(" in mgr else ""
ck("★ read_config 回传 protocol", '"protocol":' in rc)
ck("★ read_config 回传 extra_body", '"extra_body":' in rc)
ck("★ read_config 回传 vision_protocol", '"vision_protocol":' in rc)
ck("★ read_config 回传 vision_extra_body", '"vision_extra_body":' in rc)
ck("认不出的协议回落到默认值（服务器比 App 新时不崩）",
   "src.get(\"type\") if src.get(\"type\") in PROTOCOLS" in rc)

# ⑩ 路由要收这些参数
ck("★ /instance/config 路由传了 protocol",
   re.search(r'body\.get\("protocol", ""\)', mgr) is not None)
ck("★ /instance/config 路由传了 extra_body",
   re.search(r'body\.get\("extra_body", ""\)', mgr) is not None)
ck("★ /instance/api/test 路由传了 protocol",
   re.search(r'"/instance/api/test".{0,1500}?body\.get\("protocol"', mgr, re.S) is not None)
ck("★ /instance/api/models 路由收 protocol 查询参数",
   re.search(r'q\.get\("protocol"\)', mgr) is not None)
ck("★ /instance/vision/test 路由传了 protocol",
   re.search(r'"/instance/vision/test".{0,1500}?body\.get\("protocol"', mgr, re.S) is not None)

# ⑪ 识图探测的图片形状必须按协议
pv = mgr.split("def probe_vision(", 1)[1].split("\ndef ", 1)[0] \
    if "def probe_vision(" in mgr else ""
ck("有 _vision_payload（三种协议的图片形状完全不同）",
   re.search(r"^def _vision_payload\(", mgr, re.M) is not None)
vp = mgr.split("def _vision_payload(", 1)[1].split("\ndef ", 1)[0]
ck("★ OpenAI 用 image_url", '"image_url"' in vp)
ck("★ Anthropic 用 image/source/base64", '"type": "image"' in vp and "base64" in vp)
ck("★ Gemini 用 inline_data", "inline_data" in vp)
ck("有 _vision_text_of（三种协议的响应形状也不同）",
   re.search(r"^def _vision_text_of\(", mgr, re.M) is not None)
vt = mgr.split("def _vision_text_of(", 1)[1].split("\ndef ", 1)[0]
ck("★ Anthropic 的正文在 content[] 里", '"content"' in vt)
ck("★ Gemini 的正文在 candidates[] 里", "candidates" in vt)

print("App 侧：接线")

# ⑫ 客户端方法必须把新参数提交上去。
#
# ★ 必须**按方法切开**再查，不能用全局子串搜 ——
#   实测踩到：用 `'b.put("protocol"' in cli` 这种全局搜索时，
#   把 applyConfig 里那一行删掉，检查**照样通过**，
#   因为 testApi 里还有一行同样的 b.put("protocol")。
#   那是「测试为错误的理由通过」，比没有测试更糟：
#   它会在真的断线时给人「已检查」的错觉。
#   所以下面每个方法都先切出方法体，只在方法体内找。
def method_body(src, sig):
    i = src.find(sig)
    if i < 0:
        return ""
    j = src.find("\n    }", i)
    return src[i:j if j > 0 else len(src)]

ac_body = method_body(cli, "public List<String> applyConfig(String name, "
                           "String groups, String friends,\n"
                           "                                    String apiBase, "
                           "String apiKey, String apiModel,\n"
                           "                                    String persona, "
                           "String lockPassword,\n"
                           "                                    String visionBase, "
                           "String visionKey,\n"
                           "                                    String visionModel,\n"
                           "                                    String protocol, "
                           "String extraBody,\n"
                           "                                    String visionProtocol, "
                           "String visionExtraBody)")
ck("切出了 applyConfig 的方法体（切不出来后面的检查都是假的）",
   len(ac_body) > 500, len(ac_body))
ck("★ ManagerClient.applyConfig 提交 protocol", 'b.put("protocol"' in ac_body)
ck("★ ManagerClient.applyConfig 提交 extra_body", 'b.put("extra_body"' in ac_body)
ck("★ ManagerClient.applyConfig 提交 vision_protocol",
   'b.put("vision_protocol"' in ac_body)
ck("★ ManagerClient.applyConfig 提交 vision_extra_body",
   'b.put("vision_extra_body"' in ac_body)
ta_body = method_body(cli, "public Map<String, Object> testApi(String name, "
                           "String apiBase, String apiKey,\n"
                           "                                       String apiModel, "
                           "String protocol,\n"
                           "                                       String extraBody)")
ck("切出了 testApi 的方法体", len(ta_body) > 200, len(ta_body))
ck("★ testApi 提交 protocol", 'b.put("protocol"' in ta_body)
ck("★ testApi 提交 extra_body（否则「测通了但真跑起来 400」）",
   'b.put("extra_body"' in ta_body)

tv_body = method_body(cli, "public Map<String, Object> testVision(String name, "
                           "String base, String key,\n"
                           "                                          String model, "
                           "String protocol,\n"
                           "                                          String extraBody)")
ck("切出了 testVision 的方法体", len(tv_body) > 200, len(tv_body))
ck("★ testVision 提交 protocol（图片形状按协议不同）",
   'b.put("protocol"' in tv_body)
ck("★ testVision 提交 extra_body", 'b.put("extra_body"' in tv_body)
ck("★ listModels 接受并提交 protocol", re.search(
    r'listModels\([^)]*protocol[^)]*\)', cli, re.S) is not None
   and 'protocol=")' in cli)

# ⑬ 界面真的调用了，而且把参数传下去了
ck("RobotsView 有协议下拉框", "UiKit.spinner(" in rv)
ck("RobotsView 有自定义请求体输入框", "extraBody" in rv)
ck("★ 主 API 的 testApi 传了协议和请求体",
   re.search(r"testApi\(it\.name, b, k,[^;]*?protoSpin\.getSelectedItem", rv, re.S)
   is not None)
ck("★ 保存时把 protocol/extra_body 传给了 applyConfig",
   re.search(r"applyConfig\(\s*it\.name,\s*g,\s*f,\s*ab,\s*ak,\s*am,\s*pe,\s*lockPw,\s*"
             r"vb,\s*vk,\s*vm,\s*proto,\s*eb,\s*vproto,\s*veb\)", rv, re.S) is not None)
ck("★ 识图协议是**独立**的下拉框（不能复用主协议那个）",
   "visionProto" in rv and "visionExtra" in rv)
# ★ 光有 visionProto 这个变量名不够 —— 实测踩到：
#   把 testVision 的调用改成传 protoSpin（主协议）之后，
#   「visionProto 存在」照样为真，检查通过，但用户改主 API 的协议
#   会把识图一起改坏（现象是「文字能聊、图看不懂」，极难联想到）。
#   所以要查**实际传的是哪一个变量**。
_tv_call = re.search(r"client\(\)\.testVision\((.*?)\);", rv, re.S)
ck("找得到 testVision 的调用点", _tv_call is not None)
if _tv_call:
    _args = _tv_call.group(1)
    ck("★ testVision 传的是 visionProto（不是主协议 protoSpin）",
       "visionProto" in _args and "protoSpin" not in _args,
       _args.replace("\n", " ")[:160])
    ck("★ testVision 传的是 visionExtra（识图自己的请求体）",
       "visionExtra" in _args and "extraBody.getText" not in _args)

# ⑭ 回填：读回来的协议要真的选到下拉框上
# ★ 不能只查「有 setSelection」—— 实测踩到：把回填整行删掉、
#   或者改成 setSelection(0)（永远选第一个）之后，检查照样通过。
#   而那个 bug 的后果很重：用户打开面板看到的是默认项，
#   一保存就把**本来正确**的协议改成了 OpenAI 兼容，
#   他什么都没动过，根本查不出来。
ck("★ 回填主协议用的是读回来的值（不是写死 0）",
   re.search(r"protoSpin\.setSelection\(Protocols\.indexOfKey\(protoKey\)\)",
             rv) is not None)
ck("★ 回填识图协议用的是读回来的值",
   re.search(r"visionProto\.setSelection\(Protocols\.indexOfKey\(", rv) is not None)
ck("★ 读配置时真的取了 protocol 字段", 'Json.str(cfg, "protocol"' in rv)
ck("★ 读配置时真的取了 vision_protocol 字段",
   'Json.str(cfg, "vision_protocol"' in rv)
ck("★ 回填自定义请求体", re.search(r"extraBody\.setText\(eb\)", rv) is not None)
ck("★ 回填识图请求体",
   re.search(r'visionExtra\.setText\(', rv) is not None)
ck("★ 配过请求体的自动展开（否则用户以为参数丢了）",
   re.search(r"extraBox\.setVisibility\(View\.VISIBLE\)", rv) is not None)

# ⑮ App 认不出的协议要显式警告，不能静默改成默认值
ck("★ 检查 App 认不认识服务器给的协议", "Protocols.knows(" in rv)
ck("★ 认不出时明确警告（静默改协议 = 把好配置改坏）",
   re.search(r"不要直接保存", rv) is not None)
ck("警告不会被后面的提示覆盖（note 被写了多次）",
   "unknownProtoMsg" in rv and re.search(
       r"if \(!unknownProtoMsg\.isEmpty\(\)\)", rv) is not None)

# ⑯ 本地先拦明显的 JSON 格式错
ck("★ 本地粗查 JSON 格式（省一次网络往返）",
   re.search(r"looksLikeJsonObject\(", rv) is not None)
ljo = rv.split("looksLikeJsonObject(String s)", 1)[1].split("\n    }", 1)[0] \
    if "looksLikeJsonObject(String s)" in rv else ""
ck("★ 只查大括号、不自己写半吊子解析器（误判合法 JSON 比不查更糟）",
   "startsWith" in ljo and "endsWith" in ljo and "parse" not in ljo)

# ⑰ 两份协议表必须一致
print("两份协议表一致性")
app_keys = re.findall(r'add\(\s*"([a-z0-9_]+)"\s*,', proto)
mgr_block = mgr.split("PROTOCOLS = {", 1)[1].split("\n}", 1)[0]
mgr_keys = re.findall(r'^\s{4}"([a-z0-9_]+)":\s*\{', mgr_block, re.M)
ck("★ App 和服务器协议数量一致", len(app_keys) == len(mgr_keys),
   "App=%d 服务器=%d" % (len(app_keys), len(mgr_keys)))
ck("★ App 里没有服务器不认的协议",
   set(app_keys) <= set(mgr_keys), sorted(set(app_keys) - set(mgr_keys)))
ck("★ 服务器没有 App 选不到的协议",
   set(mgr_keys) <= set(app_keys), sorted(set(mgr_keys) - set(app_keys)))
ck("★ 顺序一致（最常用的要在第一个）", app_keys == mgr_keys)
ck("App 的 DEFAULT 和服务器一致",
   re.search(r'String DEFAULT = "openai_chat_completion"', proto) is not None
   and re.search(r'^DEFAULT_PROTOCOL = "openai_chat_completion"', mgr, re.M) is not None)

print()
if fails:
    print("失败 %d 项：%s" % (len(fails), fails))
    print("这些接线断了的话，用户看到的是「保存成功」但配置根本没生效。")
    sys.exit(1)
print("协议接线检查通过")
