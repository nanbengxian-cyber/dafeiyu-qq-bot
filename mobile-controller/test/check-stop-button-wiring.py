# -*- coding: utf-8 -*-
"""静态检查：「机器人运行中停止不了」的修复有没有被改回去。

背景（2026-09-18 用户报障原话：「机器人运行中停止不了」）：
  界面上原来只按 it.running()（**两个容器都 running**）在
  「启动」和「停止」之间二选一。而 NapCat 单独挂掉（QQ 掉线/被顶号）
  但 AstrBot 还活着，是最常见的半死状态 —— 这时 running()=false，
  界面就只给「启动」，**一个「停止」按钮都没有**。
  用户看着机器人「还在跑」（AstrBot 占着端口、还在回消息），
  却没有任何办法把它停下来（他不会去服务器上敲 docker）。

  线上实测佐证：实例 1 当时就是 astrbot=running + napcat=exited。

为什么这条必须单独静态检查：
  View 类不进单测面（见 run-tests.sh 里的反向检查），
  所以「按钮怎么给」这段逻辑单测碰不到。而这个 bug 的特征是
  **逻辑单测全绿、界面就是不给按钮** —— 正是这个项目反复踩的那类坑。
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
    """去掉 // 和 /* */ 注释（手写扫描，不用正则 —— 正则会把
    "http://..." 里的 // 当注释，既误报又漏报）。"""
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


rv = strip_comments(read("app/src/com/dafeiyu/controller/RobotsView.java"))
cli = strip_comments(read("app/src/com/dafeiyu/controller/ManagerClient.java"))

print("① ManagerClient 的三个判据都在")
ck("有 partiallyRunning()（任何一个容器活着）",
   re.search(r"public boolean partiallyRunning\(\)", cli) is not None)
ck("有 absent()（两个都不在）",
   re.search(r"public boolean absent\(\)", cli) is not None)
pr = cli.split("public boolean partiallyRunning()", 1)[1].split("\n        }", 1)[0] \
    if "public boolean partiallyRunning()" in cli else ""
ck("★ partiallyRunning 用的是「或」不是「与」（用与就等于没修）",
   '"running".equals(napcat) || "running".equals(astrbot)' in pr,
   pr.replace("\n", " ")[:120])

print()
print("② 界面按钮的判据")
# ★ 关键：给「停止」的判据必须是 partiallyRunning，不能是 running。
#   写回 running 就等于退回那个 bug。
rowfor = rv.split("private View rowFor(", 1)[1] if "private View rowFor(" in rv else ""
# 截到下一个方法定义，避免把后面的代码也算进来
m = re.search(r"\n    (?:private|public|protected) .*?\(", rowfor[10:])
if m:
    rowfor = rowfor[:10 + m.start()]
ck("切出了 rowFor 方法体", len(rowfor) > 500, len(rowfor))

ck("★ 给「停止」的判据是 partiallyRunning()（不是 running()）",
   re.search(r'if \(it\.partiallyRunning\(\)\) \{\s*btns\.addView\(smallBtn\("停止"',
             rowfor) is not None,
   "找不到「partiallyRunning → 停止按钮」这个组合")
ck("★ 给「启动」的判据是 !running()（没在完整运行就给）",
   re.search(r'if \(!it\.running\(\)\) \{\s*btns\.addView\(smallBtn\("启动"', rowfor)
   is not None)
# 反例：绝不能出现「running() → 停止」这种退回写法
ck("★ 没有退回成「只有两个都 running 才给停止」",
   not re.search(r'if \(it\.running\(\)\) \{\s*btns\.addView\(smallBtn\("停止"', rowfor))
ck("停止按钮真的调了 stop 动作",
   re.search(r'addView\(smallBtn\("停止".*?act\("stop"', rowfor, re.S) is not None)
ck("启动按钮真的调了 start 动作",
   re.search(r'addView\(smallBtn\("启动".*?act\("start"', rowfor, re.S) is not None)

print()
print("③ 半死状态必须说清是哪一个死了（否则用户不知道去修哪边）")
st = cli.split("public String stateText()", 1)[1].split("\n        }", 1)[0] \
    if "public String stateText()" in cli else ""
ck("★ 说明「聊天服务还在跑，QQ 已掉线」",
   "聊天服务还在跑" in st and "QQ 已掉线" in st)
ck("★ 说明「QQ 还在，聊天服务已停」",
   "QQ 还在" in st and "聊天服务已停" in st)
# ★ 不能写成「st 里不出现『运行中』」—— 那个串本来就该出现一次，
#   在 running() 那个分支里。原来我写的 split 取的是 `if (running())` 之后
#   的全部内容，而紧跟其后的 `return "运行中";` 也在里面，
#   于是**正确的代码被判成失败**（假失败）。
#   假失败比不测更糟：它会训练人忽略这个检查，最后把检查关掉。
#
# 正确的判据：把每个分支**分别**切开，只允许 running() 分支返回「运行中」。
# 允许 if (...) { 换行再 return —— 源码就是这个格式，
# 不允许多行的话会切不出任何分支，检查直接失效（假失败）。
# ★ 条件里本身带括号（"running".equals(napcat) && ...），
#   所以 `[^)]+` 会在第一个 ')' 就截断，一条都匹配不到 ——
#   检查于是**静默失效**（假失败/假通过都可能）。
#   改成非贪婪匹配到 " ) {" 为止，才吃得下带括号的条件。
_branches = re.findall(r'if \((.*?)\)\s*\{\s*\n?\s*return "([^"]+)"', st, re.S)
ck("切出了 stateText 的分支表", len(_branches) >= 3, _branches)
_lies = [b for b in _branches if "运行中" in b[1] and "running()" != b[0].strip()]
ck("★ 只有 running() 分支能说「运行中」（其它分支不能谎报正常）",
   not _lies, _lies)
ck("running() 分支确实返回「运行中」",
   any(b[0].strip() == "running()" and b[1] == "运行中" for b in _branches),
   _branches)
ck("两个都 exited 时是「已停止」", '"已停止"' in st)
ck("两个都 absent 时是「未启动」", '"未启动"' in st)

print()
print("FAILS: %d" % len(fails))
for f in fails:
    print("  !! %s" % f)
if fails:
    print()
    print("这条坏了的表现：机器人明明还在跑，界面上却没有「停止」按钮，")
    print("用户只能干看着 —— 而他没有任何别的办法把它停下来。")
sys.exit(1 if fails else 0)
