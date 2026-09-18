# -*- coding: utf-8 -*-
"""核对：我们提供给用户的每一种协议，在真实 AstrBot 里**都真的存在**。

为什么这条最重要（2026-09-18 实测抓到的真 bug）：
  协议表里原来有一项 "mirarouter_chat_completion"。它在生产 AstrBot 里
  **根本不存在** —— 全库 grep "mirarouter" 零命中。它是从一份过时的
  适配器清单里抄进来的（那份清单还多算了它一个，43 vs 真实 42）。

  后果为什么严重：
    AstrBot 用 type 去 provider_cls_map 里查适配器
    （provider/manager.py:700 `if provider_config["type"] not in provider_cls_map`），
    查不到就**加载失败**，报错只有一行 traceback。而用户在 App 上看到的
    是「保存成功」，机器人却再也不回话 —— 他会去反复检查 API Key、
    地址、模型名，永远查不到问题在「协议选了个不存在的」。

  也就是说：**提供一个不存在的协议 = 给用户埋一个「选了就坏」的选项。**
  这类错误编译不报、单测不报（因为 App 和服务器两边都「一致地」写着它），
  只能对着真实 AstrBot 的注册表比对。

基准文件 test/astrbot-adapters.txt 是从生产 AstrBot 容器里只读导出的
真实注册表（type|描述|文件名），不是手抄的。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as fh:
        return fh.read()


def ck(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  <- " + str(extra)) if not cond and extra else ""))
    if not cond:
        fails.append(name)


# ── 真实注册表 ──
real = set()
for line in read("test/astrbot-adapters.txt").splitlines():
    parts = line.split("|")
    if parts and parts[0]:
        real.add(parts[0])

ck("读到了真实适配器注册表", len(real) >= 30, len(real))

# ── 我们提供的协议（服务器 + App 两边）──
mgr = read("server/dafeiyu-manager.py")
blk = mgr.split("PROTOCOLS = {", 1)[1].split("\n}", 1)[0]
srv = set(re.findall(r'^\s{4}"([a-z0-9_]+)":\s*\{', blk, re.M))

proto = read("app/src/com/dafeiyu/controller/Protocols.java")
app = set(re.findall(r'add\(\s*"([a-z0-9_]+)"\s*,', proto))

ck("解析出服务器协议表", len(srv) >= 10, len(srv))
ck("解析出 App 协议表", len(app) >= 10, len(app))
ck("★ 两份表完全一致", srv == app,
   "只在服务器=%s 只在App=%s" % (sorted(srv - app), sorted(app - srv)))

print()
print("逐个核对：我们提供的协议在真实 AstrBot 里存在吗")
for t in sorted(srv):
    if t in real:
        print("  ✓ %-32s 真实存在" % t)
    else:
        print("  ✗ %-32s ★ 真实 AstrBot 里**没有**这个适配器！" % t)
        fails.append("协议 %s 在真实 AstrBot 里不存在（选了会让 AstrBot 加载失败，"
                     "而 App 显示「保存成功」）" % t)

print()
print("覆盖情况：真实 AstrBot 的聊天适配器，我们提供了多少")
chat = sorted(t for t in real if t.endswith("_chat_completion") or t == "openai_responses")
missing = [t for t in chat if t not in srv]
print("  真实的聊天适配器 %d 个" % len(chat))
print("  我们没提供的：%s" % (missing or "无（全覆盖）"))
if missing:
    print("  （这些不算失败 —— 但用户如果想用它们，只能选 OpenAI 兼容去撞）")

print()
print("FAILS: %d" % len(fails))
for f in fails:
    print("  !! %s" % f)
if fails:
    print()
    print("协议表里混进不存在的适配器，等于给用户一个「选了就坏」的选项：")
    print("App 显示保存成功，机器人却再也不回话，而报错只有服务器上一行 traceback。")
sys.exit(1 if fails else 0)
