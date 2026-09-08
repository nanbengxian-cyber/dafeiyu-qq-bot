# -*- coding: utf-8 -*-
"""dsh-aiflavour 纯逻辑单测。跑法：python3 test_aiflavour.py"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiflavour_logic import DynState, Matcher, STRONG_BASE, WEAK_BASE, ROOT_BASE


def mk(tmpdir):
    return Matcher(set(STRONG_BASE), set(WEAK_BASE), set(ROOT_BASE),
                   DynState(os.path.join(tmpdir, "dyn.json")),
                   min_count=3, deactivate_days=7, short_window=180.0)


def t(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


tmp = tempfile.mkdtemp()
m = mk(tmp)
now = time.time()

# 1. 静态强词直接命中
r = m.inspect("解释一下，这个功能是这样用的", "g1", now)
assert "解释一下" in r["strong_hits"], r

# 2. 动态学习：解释词根 3 次升级（文本避开静态强词，只含词根）
#    注意：上一步"解释一下"已消耗 1 次词根计数，这里重建全新状态再测
m.dyn.data = {}
states = []
for i in range(3):
    r = m.inspect("这儿得解释清楚才能明白", "g1", now + i)
    states.append(r)
t("前2次不升级", states[0]["dyn_upgraded"] is None and states[1]["dyn_upgraded"] is None)
t("解释词根第3次升级进名单", "解释" in m.dyn.active_roots())

# 3. 升级后再命中 → hot（仍在名单）
r = m.inspect("还得解释解释才懂", "g1", now + 100)
t("升级后继续命中 dyn_hits", "解释" in r["dyn_hits"])

# 4. strip_strong 会同时剥静态强词和名单词根
out, hits = m.strip_strong("简单来说就是换个说法而已，综上所述")
t("strip 剥掉静态+动态词", "简单来说" in hits and "综上所述" in hits and "换个说法" in out)

# 5. 会话刹车：同一群短窗口内再次命中升级词 → brake=True
tb = now + 1000
r0 = m.inspect("还得解释解释才懂", "g2", tb)          # 记录时间点，不算频发
t("刹车：窗口外首条不触发", r0["brake"] is False)
r1 = m.inspect("又要解释什么啊", "g2", tb + 30)        # 30s 内再来 → 刹车
t("180s窗口内再命中触发刹车", r1["brake"] is True)
r2 = m.inspect("这次真的不解释了", "g2", tb + 400)     # 间隔超过窗口 → 解除
t("超过窗口后刹车解除", r2["brake"] is False)

# 6. 降级：7 天后无命中退出名单（用干净状态，避免前面测试污染 last_ts）
m.dyn.data = {}
for i in range(3):
    m.inspect("这儿得解释清楚才能明白", "g1", now + i)   # 升级"解释"
stale = m.dyn.deactivate_stale(now + 8 * 86400, 7)      # 8 天后：远超 7 天
t("超7天自动降级", "解释" in stale and "解释" not in m.dyn.active_roots())

# 7. 持久化
m2 = mk(tmp)
m2.dyn.load()
# 上面最后一次状态是降级后的，验证 data 读取不炸
t("JSON 持久化 round-trip", isinstance(m2.dyn.data, dict))

# 8. shadow 影子层：撞日常词不剥
r = m.inspect("其实我觉得不错，就是说还行吧", "g1", now + 400)
t("影词只进 shadow_hits 不进 strong", "其实" in r["shadow_hits"] and not r["strong_hits"])

# 9. 强词多行剥空 → 工具函数返回空串场景
out, hits = m.strip_strong("简单来说")
t("整句就是强词 → 剥空", out.strip() == "")

print("\n全部通过")