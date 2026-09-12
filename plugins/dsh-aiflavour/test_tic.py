# -*- coding: utf-8 -*-
"""dsh-aiflavour 口癖层（TIC）测试。

口径全部用**生产日志里的真文本**（/tmp/tic_cases.json，由宿主从 astrbot 日志抽取）：
  · tic   21 条 —— 历史上真发过的含「大半夜」的机器人回复
  · clean 5913 条 —— 同一来源、不含「大半夜」的普通回复（假阳性对照）

跑法（必须在 astrbot 容器里，main.py 要 import astrbot）：
  sudo docker cp 本目录 astrbot:/tmp/aftest && sudo docker exec astrbot python3 /tmp/aftest/test_tic.py

注意：import 插件前先把 loguru 的 Logger.add 换成空操作，否则 AstrBot 会把文件
sink 挂到**生产日志**上（docs/71 结论 7 的坑，事后 logger.remove() 已经太晚）。
"""

import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))   # 让 main.py 的 `from .x import` 能成立

from loguru import logger as _lg          # noqa: E402

_LGR = type(_lg)
_ADD = _LGR.add
_LGR.add = lambda self, *a, **k: 0        # 挡住 AstrBot 往生产日志挂 sink
try:
    import dsh_aiflavour.aiflavour_logic as L
    import dsh_aiflavour.main as M
except ImportError:                       # 直接放在包内跑时的退路
    import aiflavour_logic as L           # type: ignore
    import main as M                      # type: ignore
finally:
    _LGR.add = _ADD
    _lg.remove()

CASES = json.load(open("/tmp/tic_cases.json", encoding="utf-8"))
TIC = CASES["tic"]
CLEAN = CASES["clean"]

SEP_EDGE = re.compile(r"^[,，。；、!！?？\s]|[,，。；、\s]$")


def build(active_roots, tics=None, pre_active=True):
    """造一个 Matcher。pre_active=True 时把 active_roots/tics 预先置为已升级。"""
    dyn = L.DynState("/tmp/aftest_dyn.json")
    dyn.data = {}
    m = L.Matcher(L.STRONG_BASE, L.WEAK_BASE, L.ROOT_BASE, dyn,
                  min_count=3, deactivate_days=7, short_window=180.0,
                  tics=set(tics if tics is not None else L.TIC_BASE))
    if pre_active:
        for r in list(active_roots) + list(tics if tics is not None else L.TIC_BASE):
            dyn.data[r] = {"count": 99, "last_ts": time.time(), "active": True}
    return m, dyn


fail = []


def ck(cond, msg):
    if not cond:
        fail.append(msg)


# ---------------------------------------------------------------- 1. 口癖必须剥干净
m, dyn = build(L.TIC_BASE)
print("=== 1. 21 条真口癖回复的剥除结果 ===")
stripped_empty = 0
for t in TIC:
    out, hits = m.strip_strong(t)
    ck(hits, "没剥到：%r" % t)
    ck("大半夜" not in out, "还留着口癖：%r → %r" % (t, out))
    ck(not SEP_EDGE.search(out) if out else True,
       "剥出了悬空标点/空白：%r → %r" % (t, out))
    if not out.strip():
        stripped_empty += 1
    print("   %-26s → %s" % (t[:26], out or "（空 → 整条不发）"))
print("   其中剥空 %d 条（这些会走 clear_result，不会发空消息）" % stripped_empty)

# ---------------------------------------------------------------- 2. 不许误伤
print("\n=== 2. 对照 %d 条普通回复：口癖层不许改动任何一条 ===" % len(CLEAN))
# 关键：拿"开着口癖层"和"关掉口癖层"的**同一份代码**逐条比。
# 直接比 out!=t 会把 STRONG_BASE 的既有行为（"说白了"/"讲白了"本来就会被剥）
# 算成我的误伤 —— 那是老行为，不是这次改动引入的。
m_off, _ = build([], tics=[], pre_active=False)
bad = []
for t in CLEAN:
    a = m.strip_strong(t)
    b = m_off.strip_strong(t)
    if a != b:
        bad.append((t, a, b))
ck(not bad, "口癖层改动了 %d 条，例：%r" % (len(bad), bad[:2]))
print("   口癖层改动 0 条 ✓（%d 条里连一条都没碰）" % len(CLEAN))

# ---------------------------------------------------------------- 3. 口癖只剥不刹
print("\n=== 3. 口癖不触发会话刹车（AI 腔才刹）===")
mt, _ = build([], tics=["大半夜的", "大半夜"])
r1 = mt.inspect("大半夜的，谁是你宝宝", "476573490", 1000.0)
r2 = mt.inspect("亲什么亲 大半夜的", "476573490", 1010.0)   # 10 秒后，在 180s 窗口内
ck(r1["tic_hits"], "口癖没被记到 tic_hits：%r" % r1)
ck(not r1["dyn_hits"], "口癖串进了 dyn_hits：%r" % r1["dyn_hits"])
ck(not r2["brake"], "口癖触发了整条拦（应该只剥不刹）：%r" % r2)
print("   口癖连击 2 次 brake=%s ✓（该剥的照样剥：%s）"
      % (r2["brake"], r2["tic_hits"]))

mr, _ = build([], tics=[])
mr.dyn.data["解释"] = {"count": 99, "last_ts": 1000.0, "active": True}
mr.roots = set(mr.roots) | {"解释"}
q1 = mr.inspect("我来解释一下这个", "476573490", 2000.0)
q2 = mr.inspect("简单解释就是 A", "476573490", 2010.0)
ck(q1["dyn_hits"], "AI 腔词根没被记到 dyn_hits：%r" % q1)
ck(q2["brake"], "AI 腔词根没触发刹车（原有行为被改坏了）：%r" % q2)
print("   AI 腔词根连击 2 次 brake=%s ✓（原有刹车行为保持不变）" % q2["brake"])

# ---------------------------------------------------------------- 4. 学习门槛还在
print("\n=== 4. 动态学习门槛：累计到 LEARN_MIN 才开剥 ===")
m2, d2 = build([], tics=["大半夜"], pre_active=False)
seq = []
for i in range(4):
    st = d2.observe("大半夜", 3000.0 + i, 3, 7)
    seq.append(st or "观察")
ck(seq[:2] == ["观察", "观察"] and seq[2] == "upgraded",
   "升级节奏不对：%r" % seq)
act = d2.active_roots()
ck("大半夜" not in act or seq[2] == "upgraded", "升级后没进剥除名单：%r" % act)
ck("大半夜" in act, "升级后没进剥除名单：%r" % act)
print("   第 1、2 次=%s，第 3 次=%s，之后进名单 ✓" % (seq[0], seq[2]))

# ---------------------------------------------------------------- 5. 装机自检
print("\n=== 5. 插件装载 + /AI味状态 渲染 ===")


class FakeEvent:
    """filter.command 装饰器会自己查属主/群，所以这些方法都得在。"""

    def __init__(self, uid, gid):
        self._uid, self._gid = uid, gid

    def plain_result(self, text):
        return text

    def get_sender_id(self):
        return self._uid

    def get_group_id(self):
        return self._gid

    def get_self_id(self):
        return "3752949717"

    @property
    def unified_msg_origin(self):
        return "aiocqhttp:GroupMessage:%s" % self._gid


class FakeCtx:
    pass


try:
    plugin = M.Main(FakeCtx())
    txt = None
    import asyncio

    async def run():
        ev = FakeEvent(sorted(M.OWNERS)[0], sorted(M.GROUPS)[0])
        async for r in plugin.cmd_status(ev):
            return r
    txt = asyncio.get_event_loop().run_until_complete(run())
    ck(txt and "口癖" in txt, "状态面板没有口癖字段：%r" % txt)
    print("   " + (txt or "").replace("\n", "\n   "))
except BaseException as exc:
    fail.append("插件装载/状态面板异常：%r" % exc)

# ---------------------------------------------------------------- 结果
print("\n" + "=" * 60)
if fail:
    print("TIC_TEST_FAILED（%d 项）" % len(fail))
    for f in fail:
        print("  ✗ " + f)
    sys.exit(1)
print("TIC_TEST_OK")
