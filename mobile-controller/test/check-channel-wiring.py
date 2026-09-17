# -*- coding: utf-8 -*-
"""静态检查：修复「不回话」的接线是否真的接上了。

为什么需要它：逻辑单测全绿但生产代码**根本没调用**，是这个项目踩过的
真坑（「修 APK 不显示二维码」时，WebProxyPath 测得好好的，
实际四个接口没剥 data 外壳）。View 类不进单测面，只能静态扫。

这次要防的是同类风险：服务器加了 /instance/repair-channel，
App 加了按钮，但按钮**没接到那个接口**、或者自检结果没显示出来 ——
用户看到的和没修一样（依然不回话，也没有任何提示）。
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

# ① 路由存在，且在 POST 分支（它改状态）
ck("有 /instance/repair-channel 路由",
   '"/instance/repair-channel"' in mgr)
ck("repair_channel 函数存在", re.search(r"^def repair_channel\(", mgr, re.M)
   is not None)

# ② repair_channel 必须真的做三件事，少一件就修不好
rc = mgr.split("def repair_channel(", 1)[1].split("\ndef ", 1)[0] \
    if "def repair_channel(" in mgr else ""
ck("修复时写了配对配置", "ensure_pairing(" in rc)
ck("修复时热加载 NapCat（不重启容器，保住登录态）",
   "_napcat_hot_reload(" in rc)
ck("修复时重启了 AstrBot（它是服务端，只在启动时开端口）",
   re.search(r'compose\(name,\s*"restart",\s*"astrbot"', rc) is not None)

# ③ 配对逻辑本身：两端都要写，token 要一致
ep = mgr.split("def ensure_pairing(", 1)[1].split("\ndef ", 1)[0] \
    if "def ensure_pairing(" in mgr else ""
ck("配对写了 NapCat 侧", "_patch_napcat_onebot(" in ep)
ck("配对写了 AstrBot 侧", "ws_reverse_token" in ep and "aiocqhttp" in ep)
ck("两端用同一个 token", ep.count("token") >= 3)

# ④ 三个调用点都要有 —— 缺一个就有用户会踩坑
ck("create_instance 里配了（首登前放好模板）",
   "ensure_pairing(name, meta)" in
   mgr.split("def create_instance(", 1)[1].split("\ndef ", 1)[0])
ck("start_instance 里配了（首启后配置才生成）",
   "ensure_pairing(name, meta)" in
   mgr.split("def start_instance(", 1)[1].split("\ndef ", 1)[0])
ac = mgr.split("def apply_config(", 1)[1].split("\ndef ", 1)[0]
ck("apply_config 里配了", "ensure_pairing(" in ac)

# ⑤ ★ 顺序坑：apply_config 里 ensure_pairing 必须在读 cfg 之前
if "ensure_pairing(" in ac and "read_json_maybe_bom(path)" in ac:
    ck("apply_config 里先配对、后读 cfg（否则私聊白名单键会拼错）",
       ac.index("ensure_pairing(") < ac.index("read_json_maybe_bom(path)"))

# ⑥ 配对状态要回读校验（否则「提示成功但依然不回话」）
ck("apply_config 回读校验了配对状态",
   "pairing_state(name)" in ac and "paired" in ac)

print("App 侧")

# ⑦ 客户端方法存在且打对了接口
ck("ManagerClient 有 repairChannel", "repairChannel(" in cli)
ck("repairChannel 打的是 /instance/repair-channel",
   '"/instance/repair-channel"' in cli)
ck("repairChannel 用的是 POST", re.search(
    r'repairChannel.*?request\("POST"', cli, re.S) is not None)

# ⑧ 界面真的调用了它（不是只加了个没人用的方法）
ck("RobotsView 调用了 repairChannel", "repairChannel(" in rv)
ck("有修复按钮", "修复消息通道" in rv)
ck("按钮默认隐藏（正常用户不该看到）",
   re.search(r"repair\.setVisibility\(View\.GONE\)", rv) is not None)
ck("按钮注册了点击事件", "repair.setOnClickListener" in rv)

# ⑨ 自检结果显示出来了（读了 pairing 字段）
ck("读了服务器返回的 pairing 字段", '"pairing"' in rv)
ck("没配对时会提示用户", "消息通道" in rv and "收不到消息" in rv)

# ⑩ 额度接口也接上了
ck("ManagerClient 有 quota()", "quota()" in cli)
ck("ManagerClient 有 cleanupPreview()", "cleanupPreview()" in cli)

print()
if fails:
    print("失败 %d 项：%s" % (len(fails), fails))
    print("这些接线断了的话，用户看到的现象和「没修」一模一样。")
    sys.exit(1)
print("消息通道接线检查通过：%d 项" % 20)
