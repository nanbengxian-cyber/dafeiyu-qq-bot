# -*- coding: utf-8 -*-
"""源码树里的 Preset.java 必须是**空模板**。

背景（2026-09-18，我自己踩的坑）：
  build-preset.sh 的流程是「把真实连接信息注入 Preset.java → 构建 → 立刻还原」。
  还原靠的是 `trap restore EXIT`。但为了让构建日志可读，我在脚本后半段
  又写了一个 `trap 'rm -f $LOG' EXIT` —— bash 的 EXIT trap 只有一个，
  谁最后写谁赢，于是 restore **再也不会执行**。

  后果：服务器地址、SSH 私钥、管理口令全留在 app/src/.../Preset.java 里。
  这个文件是被 git 跟踪的，下一次 `git add -A && commit && push`
  就会把这些推到**公开仓库**。而屏幕上一切正常，没有任何提示。

  当时是 test/run-tests.sh 报出 11 项失败才发现的，不是人看出来的。

为什么这条必须独立存在：
  上面的泄漏**不会**让 App 出任何问题（内置版反而更「能用」），
  所以功能测试全是绿的。它是纯粹的「静默安全事件」，
  只有专门盯这个文件的检查才能发现。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "app/src/com/dafeiyu/controller/Preset.java")
fails = []


def ck(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  <- %s" % extra) if not cond and extra else ""))
    if not cond:
        fails.append(name)


print("① 文件存在且是空模板")
if not os.path.isfile(P):
    print("  ✗ 找不到 %s" % P)
    sys.exit(1)
src = open(P, encoding="utf-8").read()
ck("Preset.java 存在", True)
ck("★ HAS_PRESET = false", re.search(r"HAS_PRESET\s*=\s*false", src) is not None,
   "内置版的值被提交上来了")
for field in ("HOST", "SSH_USER", "SSH_KEY", "HOST_FINGERPRINT", "MANAGER_TOKEN"):
    m = re.search(r'String\s+%s\s*=\s*"([^"]*)"' % field, src)
    ck("★ %s 是空串" % field, m is not None and m.group(1) == "",
       m.group(1)[:60] if m else "找不到该字段")
m = re.search(r"int\s+SSH_PORT\s*=\s*(\d+)", src)
ck("★ SSH_PORT 是 0", m is not None and m.group(1) == "0",
   m.group(1) if m else "找不到")

print()
print("② 不出现任何真实凭据的形态（按形状查，不依赖具体值）")
# 私钥：PEM 头是最硬的信号
ck("★ 不含 BEGIN RSA PRIVATE KEY", "BEGIN RSA PRIVATE KEY" not in src)
ck("不含任何 BEGIN ... PRIVATE KEY", "PRIVATE KEY" not in src)
# 长 base64 串（私钥正文、加密后的 token 都长这样）
long_b64 = re.findall(r'"[A-Za-z0-9+/]{60,}={0,2}"', src)
ck("★ 不含长 base64 串（私钥/密文正文）", not long_b64,
   "%d 处，例如 %s" % (len(long_b64), long_b64[0][:50]) if long_b64 else "")
# IPv4
ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", src)
ck("★ 不含 IP 地址", not ips, ips)
# http(s) 地址
urls = re.findall(r"https?://[^\s\"]+", src)
ck("★ 不含网址", not urls, urls)
# 域名形态（xxx.yyy 且 tld 是常见字母）
doms = re.findall(r'"[a-z0-9-]+(?:\.[a-z0-9-]+)+\.(?:com|org|net|cn|cc|io|org|top|xyz|dpdns)"', src)
ck("★ 不含域名", not doms, doms)

print()
print("③ 构建脚本的还原防线还在（这才是根治）")
bp = open(os.path.join(ROOT, "build-preset.sh"), encoding="utf-8").read()
ck("★ build-preset.sh 里 restore 函数还在",
   re.search(r"^restore\(\)\s*\{", bp, re.M) is not None)
# ★ 关键：EXIT trap 只能有一个。若出现两个 trap ... EXIT，后者会覆盖前者，
#   还原就静默失效 —— 这正是当初出事的机制。
# ★ `trap - EXIT` 是**清除** trap，不是注册新的，必须排除掉。
#   不排除的话它会被算成「第二个 EXIT trap」→ 检查恒假失败（假失败）。
#   而假失败比不测更糟：它会训练人忽略这个检查，最后把检查关掉。
exit_traps = [t for t in re.findall(r"^\s*trap\s+[^\n]*\bEXIT\b", bp, re.M)
              if not re.match(r"^\s*trap\s+-\s", t)]
ck("★ 全文只有一个 EXIT trap（多个会互相覆盖，还原会静默失效）",
   len(exit_traps) == 1, exit_traps)
ck("★ 那个 EXIT trap 就是还原用的（cleanup_all 或 restore）",
   any("cleanup_all" in t or "restore" in t for t in exit_traps), exit_traps)
# 不依赖 trap 的显式还原
ck("★ 结尾有显式 restore（不依赖 trap 语义）",
   len(re.findall(r"^restore\s*$", bp, re.M)) >= 1)
ck("★ 还原后有「残留凭据」核对（还原了不等于还原对了）",
   "仍残留真实凭据" in bp or "残留" in bp and "PRIVATE KEY" in bp)

print()
print("FAILS: %d" % len(fails))
for f in fails:
    print("  !! %s" % f)
if fails:
    print()
    print("这条坏了意味着：服务器地址 / SSH 私钥 / 管理口令 可能正躺在源码树里，")
    print("下一次 commit 推到公开仓库，任何人都能拿到你服务器的管理权。")
    print("功能测试**不会**发现这件事 —— 内置版反而更「能用」。")
sys.exit(1 if fails else 0)
