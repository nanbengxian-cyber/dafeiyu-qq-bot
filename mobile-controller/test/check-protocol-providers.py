# -*- coding: utf-8 -*-
"""核对：我们写进配置的 provider 名，必须和 AstrBot 官方模板一致。

为什么需要这一条（2026-09-18 实测抓到的真 bug）：
  我原先给 Gemini 写的 provider 是 "googlegenai"，
  而 AstrBot 官方模板（core/config/default.py 的 "Google Gemini"）写的是 "google"。
  Kimi 也一样：官方是 "kimi-code"，我写的是 "anthropic"。

  这个字段**不参与适配器查找**（查找用 type，见 provider/manager.py:700），
  所以写错**不会**加载失败、不会有任何报错 —— 它只决定
  「厂商专属的请求改写」是否生效（openai_source.py:412 按它判 nvidia/ollama，
  openai_responses_source.py:77 判 deepseek）。

  也就是说：写错的后果是**静默少了一层兼容修正**，
  用户看到的是「有时候模型行为不太对」，而没有任何线索指向这里。
  这类「不报错但错了」的字段，只能靠和官方基准比对来钉住。

基准文件 test/astrbot-provider-schema.txt 是从生产 AstrBot 容器里
只读导出的官方模板（type|provider 配对），不是手抄的。
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


# ── 官方基准：type -> {provider, ...} ──
#
# ★ 必须是「一个 type 对应**一组** provider」，不能是单值映射。
#   实测踩到：官方模板里 openai_chat_completion 同时被 Moonshot、DeepSeek、
#   NVIDIA、Azure、Ollama 等**十几个**模板复用，各自 provider 不同。
#   用单值 dict 存会把前面的覆盖掉，只剩最后一个 ——
#   于是我们的 "openai" 会被判成「和官方 'google' 不一致」，
#   这是**测试自己的 bug**（假失败），不是产品的问题。
#   假失败比不测更糟：它会训练人忽略这个检查。
official = {}
for line in read("test/astrbot-provider-schema.txt").splitlines():
    parts = line.split("|")
    if len(parts) == 4:
        official.setdefault(parts[3], set()).add(parts[2])

ck("读到了官方基准（不为空）", len(official) >= 10, len(official))

# ── 我们的表 ──
mgr = read("server/dafeiyu-manager.py")
blk = mgr.split("PROTOCOLS = {", 1)[1].split("\n}", 1)[0]
ours = {}
for mt in re.finditer(r'"([a-z0-9_]+)":\s*\{(.*?)\n    \}', blk, re.S):
    body = mt.group(2)
    pm = re.search(r'"provider":\s*"([a-z0-9\-]+)"', body)
    if pm:
        ours[mt.group(1)] = pm.group(1)

ck("解析出了我们的协议表", len(ours) >= 10, len(ours))

print()
print("逐个核对 provider 名（只核对官方也有的 type）：")
for t in sorted(ours):
    if t not in official:
        continue
    allowed = official[t]
    if ours[t] in allowed:
        print("  ✓ %-32s %s" % (t, ours[t]))
    else:
        print("  ✗ %-32s 我们=%r 官方有=%s" % (t, ours[t], sorted(allowed)))
        fails.append("provider 名和官方不一致：%s（我们 %r / 官方 %s）"
                     % (t, ours[t], sorted(allowed)))

print()
print("我们提供但官方模板里没有的 type（需要人工判断，不算失败）：")
extra = sorted(set(ours) - set(official))
print("  %s" % (extra or "无"))

print()
print("FAILS: %d" % len(fails))
for f in fails:
    print("  !! %s" % f)
if fails:
    print()
    print("provider 名写错不会报错，只会静默少一层厂商专属修正 ——")
    print("所以必须和官方模板一字不差。")
sys.exit(1 if fails else 0)
