# -*- coding: utf-8 -*-
"""静态检查：识图 API 的接线是否真的接上了。

为什么需要它：逻辑单测全绿但生产代码**根本没调用**，是这个项目踩过的
真坑（「修 APK 不显示二维码」时，WebProxyPath 测得好好的，
实际四个接口没剥 data 外壳）。View 类不进单测面，只能静态扫。

这次要防的风险：
  * 服务器加了 /instance/vision/test，App 加了「测试识图」按钮，
    但按钮没接到那个接口 —— 用户点了没反应，或者更糟：以为测过了；
  * 用户填的识图配置**没被提交**（applyConfig 少传了 vision_*），
    界面显示「已保存」，实际服务器上什么都没写；
  * 识图配置读回来了但没回填到输入框 —— 用户以为没配过，
    重复配置或误以为配置丢了；
  * 服务器返回的 vision_capable 没被用来决定提示颜色 ——
    「不能识图」和「能识图」长得一样，防呆就白做了。
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

print("服务器侧")

# ① 探测函数与路由
ck("probe_vision 函数存在",
   re.search(r"^def probe_vision\(", mgr, re.M) is not None)
ck("有 /instance/vision/test 路由", '"/instance/vision/test"' in mgr)
ck("路由在 do_POST 分支（它要收 Key，不能走 GET 查询串）",
   re.search(r'"/instance/vision/test".{0,200}?probe_vision', mgr, re.S)
   is not None)

# ② 探测必须真的发图 —— 只看 /models 是假绿灯
pv = mgr.split("def probe_vision(", 1)[1].split("\ndef ", 1)[0] \
    if "def probe_vision(" in mgr else ""
ck("★ 探测真的构造了图片（image_url）", '"image_url"' in pv)
ck("★ 图片是 base64 内联的（服务器本地造图，不依赖外网图床）",
   "data:image/png;base64," in pv)
ck("有造图函数 _make_test_png",
   re.search(r"^def _make_test_png\(", mgr, re.M) is not None)
ck("测试颜色是随机的（防模型背答案）",
   "secrets.choice(_VISION_COLORS)" in pv)
ck("★ max_tokens 给足（推理模型给少了正文为空，会误判）",
   re.search(r'"max_tokens":\s*(\d+)', pv) is not None
   and int(re.search(r'"max_tokens":\s*(\d+)', pv).group(1)) >= 800,
   re.search(r'"max_tokens":\s*(\d+)', pv).group(1)
   if re.search(r'"max_tokens":\s*(\d+)', pv) else "没找到")
ck("★ 空回复会重试（推理模型偶尔把预算全花在思考上）",
   "for attempt in range(" in pv)
ck("★ 同义词也算答对（答「金色」不该被判成没看图）",
   "names[1]" in pv and "any(syn in" in pv)

# ③ 能力结论必须真的落到 vision_capable 上
ck("返回里有 vision_capable 字段", '"vision_capable"' in pv)
for v in ("True", "False", "None"):
    ck("vision_capable 能取到 %s（三态：能/不能/测不出）" % v,
       "vision_capable\"] = %s" % v in pv)

# ④ 写入侧：识图 provider 必须是另一个 provider，且排在主 provider 后面
ac = mgr.split("def apply_config(", 1)[1].split("\ndef ", 1)[0] \
    if "def apply_config(" in mgr else ""
ck("★ 识图写成独立的 provider（不是替换主 provider）",
   '"dafeiyu-vision"' in ac)
ck("★ 识图 provider 声明了 image 模态（AstrBot 靠它决定回退）",
   re.search(r'"modalities":\s*\["text",\s*"image"\]', ac) is not None)
ck("★ 识图 provider 排在主 provider 后面（排前面会变成主聊天模型）",
   re.search(r'cfg\["provider"\] = \[p for p in .*?\] \+ \[vprov\]', ac, re.S)
   is not None)
ck("图片描述 provider 也指过去了（引用图片那条路径也要能识图）",
   "default_image_caption_provider_id" in ac)
ck("不填 vision_* 就不动多模态配置（老用户升级不受影响）",
   re.search(r"if vision_any:", ac) is not None)

# ⑤ 重启后回读校验三件事
ck("★ 回读校验：provider 还在", "识图 API 丢了" in ac)
ck("★ 回读校验：modalities 含 image", "少了 image 标记" in ac)
ck("★ 回读校验：没抢主聊天的位置", "抢到主聊天的位置" in ac)

# ⑥ 读取侧要回传识图配置（否则界面回填不了）
rc = mgr.split("def read_config(", 1)[1].split("\ndef ", 1)[0] \
    if "def read_config(" in mgr else ""
ck("read_config 返回 vision_base", '"vision_base"' in rc)
ck("read_config 返回 vision_model", '"vision_model"' in rc)
ck("★ read_config 只回传「有没有配 Key」，不回传 Key 本身",
   '"vision_key_set"' in rc and "vision_key\"]" not in rc)

print("App 侧")

# ⑦ 客户端方法存在且打对了接口
ck("ManagerClient 有 testVision", "testVision(" in cli)
ck("testVision 打的是 /instance/vision/test",
   '"/instance/vision/test"' in cli)
ck("testVision 用的是 POST",
   re.search(r'testVision.*?request\("POST"', cli, re.S) is not None)

# ⑧ applyConfig 必须把 vision_* 真的提交上去
ck("★ applyConfig 提交了 vision_base", 'b.put("vision_base"' in cli)
ck("★ applyConfig 提交了 vision_key", 'b.put("vision_key"' in cli)
ck("★ applyConfig 提交了 vision_model", 'b.put("vision_model"' in cli)

# ⑨ 界面真的调用了它，而且把参数传下去了
ck("RobotsView 调用了 testVision", "testVision(" in rv)
ck("RobotsView 有识图输入框", "visionBase" in rv and "visionModel" in rv)
ck("识图 Key 输入框是密码样式", re.search(
    r'visionKey = UiKit\.input\(ctx,[^;]*?true\)', rv, re.S) is not None)
ck("★ 保存时把 vision_* 传给了 applyConfig",
   re.search(r"applyConfig\(\s*it\.name,\s*g,\s*f,\s*ab,\s*ak,\s*am,\s*pe,\s*lockPw,\s*vb,\s*vk,\s*vm\)",
             rv, re.S) is not None)

# ⑩ 能力结论要影响界面（否则「不能识图」和「能识图」长得一样）
ck("★ 读了 vision_capable", '"vision_capable"' in rv)
ck("★ 能识图/不能识图用不同颜色提示",
   re.search(r'canSee \? Theme\.GOOD', rv, re.S) is not None)
ck("测不出时不当成失败（用中性色）", "unknown" in rv)

# ⑪ 读回来的配置要回填，并且已配过的自动展开
ck("★ 回填 vision_base", re.search(r'visionBase\.setText\(', rv) is not None)
ck("★ 回填 vision_model", re.search(r'visionModel\.setText\(', rv) is not None)
ck("已配过识图的自动展开那块（否则用户找不到在哪改）",
   re.search(r"visionBox\.setVisibility\(View\.VISIBLE\)", rv) is not None)

# ⑫ 本地先拦「只填一半」，免得白等一次往返
ck("★ 只填一半时本地就拦住", re.search(r"识图 API 要填全", rv) is not None)

print()
if fails:
    print("失败 %d 项：%s" % (len(fails), fails))
    print("这些接线断了的话，用户看到的和「没做防呆」一模一样。")
    sys.exit(1)
print("识图接线检查通过：%d 项" % 40)
