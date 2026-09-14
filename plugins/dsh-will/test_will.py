# -*- coding: utf-8 -*-
"""dsh-will 纯逻辑测试。夹具 = 2026-09-13 主群真实消息（当天从日志里捞出来的）。

跑法：python3 test_will.py
退出码 0 = 全过。宿主机 py3.8 也能跑（不 import astrbot）。
"""

import sys
import time

import will_logic as m

NOW = 1789291598.0
failed = 0
passed = 0


def check(name, cond, extra=""):
    global failed, passed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  ✗ %s %s" % (name, extra))


# ---------------------------------------------------------------- 1. 压力识别
# 这一组就是「光说说」的第一层根因：旧判据把下面这些全部判成 ordinary(+0)。
# 每一条都对应群里真实发生过的一句话。
SHOULD_CATCH = {
    "傻": "insult",
    "滚": "insult",
    "闭嘴": "insult",
    "别装了": None,          # 这句确实不算辱骂，允许不catch（见「不该误伤」）
    "你这鱼有问题啊": "insult",
    "用的是flash模型吧，想都不想就回答了": None,
    "妈妈": "role",
    "妹妹": "role",
    "狗修金": "role",
    "叫我主人": "role",
    "你只是工具": "role",
    "内裤送我": "harass",
    "我要的内裤呢": "harass",
    "所以把内裤送我": "harass",
    "和我做，给我口也行": "harass",
    "做爱": "harass",
    "我得看看你穿没穿，让我看看": "harass",
    "宝贝我想你了，我想听听你艾草的声音": "harass",
    "哥哥晚上寂寞，请到我家里来": None,   # 骚扰但没命中词表 —— 允许漏，见下面第 3 组
    "给我把话收回去": "order",
    "赶紧的": "order",
    "你必须听我的": "role",      # 语义上是身份控制，归 role 更准
}
for text, want in SHOULD_CATCH.items():
    kinds = [k for k, _w, _e in m.classify(text, True)]
    if want is None:
        check("压力识别（允许漏）%s" % text, True)
        continue
    check("压力识别 %-22s → %s" % (text, want), want in kinds, kinds)

# 反向：没被点名时一个字都不记（群里骂别人、转述别人的话不算）
for text in ("傻", "内裤送我", "妈妈"):
    check("非点名不记账：%s" % text, m.classify(text, False) == [])

# 不该误伤的日常话（这些命中会变成「随机抬杠」，用户明确不要）
for text in ("早", "吃了吗", "这个梗好笑", "什么是np猜想", "帮我算一下",
             "谢谢", "辛苦了", "今天群里好热闹", "大肥鱼你好"):
    kinds = m.classify(text, True)
    check("不该误伤：%s" % text, kinds == [], kinds)

# 道歉/尊重必须是负权重（掉账）——人才愿意消气
for text in ("抱歉", "对不起", "我开玩笑的", "别生气", "尊重你", "随你", "听你的"):
    kinds = [k for k, _w, _e in m.classify(text, True)]
    check("修复事件认得出：%s" % text, bool(kinds), kinds)
check("修复是负分", sum(w for _k, w, _e in m.classify("抱歉", True)) < 0)

# ---------------------------------------------------------------- 2. 任务白名单
# 「正经请求永远照办」——这是硬不变量，误判成骚扰就是事故。
for text in ("/说话 叮咚鸡", "/我的档案", "什么是np猜想", "如何无痕处理一只140斤的鸡？",
             "帮我算一下这道题", "证明雅可比猜想", "解释一下什么是p对np",
             "查一下今天天气", "给我讲个笑话", "画一个鱼", "翻译这段话",
             "封了他", "禁言三分钟", "重启一下服务", "你的配置是什么"):
    check("判成任务（照办）：%s" % text, m.is_task(text), text)

# 反例：这些**不能**被当成任务，否则骚扰会被「照办」——旧判据正是死在这里
for text in ("和我做，给我口也行", "内裤送我", "傻", "妈妈", "狗修金",
             "滚", "闭嘴", "你这鱼有问题啊", "我要的内裤呢", "做爱"):
    check("不是任务（可反抗）：%s" % text, not m.is_task(text), text)
# 旧判据的具体死因：单字「做/谁/？」把骚扰变成了任务
check("回归：旧判据的 _FACT_TASK_RE 式误判已修",
      not m.is_task("和我做，给我口也行") and not m.is_task("大肥鱼 我是谁"))

# ---------------------------------------------------------------- 3. 衰减与赌气
check("衰减：半衰期掉一半",
      abs(m.decay(100.0, NOW, NOW + m.HEAT_HALF, m.HEAT_HALF) - 50.0) < 0.01)
check("衰减：时间没走就不掉", abs(m.decay(37.0, NOW, NOW, m.HEAT_HALF) - 37.0) < 1e-6)
check("衰减：时钟倒退不涨", abs(m.decay(37.0, NOW, NOW - 500, m.HEAT_HALF) - 37.0) < 1e-6)
check("赌气：0~2 条没脾气", m.wilt_of(0) == 0 and m.wilt_of(2) == 0)
check("赌气：连 3 条开始", m.wilt_of(3) > 0)
check("赌气：连 8 条最重", m.wilt_of(8) >= m.wilt_of(5) > m.wilt_of(3) > 0)
check("赌气：脏输入不抛", m.wilt_of("x") == 0 and m.wilt_of(None) == 0)

# ---------------------------------------------------------------- 4. 决定
def d(heat, grudge=0.0, **kw):
    return m.decide(heat, grudge, **kw)[0]

check("分数：记的仇只算一半", abs(m.score_of(20, 20) - 30.0) < 1e-6)
check("分数：赌气直接加成", abs(m.score_of(0, 0, 6) - 6.0) < 1e-6)
check("决定：没到线就不动手", d(0) == "none" and d(17) == "none")
check("决定：18 分短回（一句直接贬损就该有反应）", d(18) == "curt")
check("决定：34 分顶回去", d(34) == "snub")
check("决定：58 分且还有额度才沉默", d(60, silent_left=1) == "silence")
check("决定：额度用完不许沉默（降级顶回去）",
      d(60, silent_left=0) == "snub")
check("决定：冷却中一律不动手", m.decide(99, 0, cooled=False)[0] == "none")
check("决定：光有仇不够掀桌（60 仇只有 30 分 → 短回，不是顶回去）",
      d(0, 60) == "curt" and d(0, 80) == "snub")

# 群主/管理员：永不被沉默，阈值 ×1.5
check("管理：60 分是顶回去，不是沉默", d(60, silent_left=9, privileged=True) == "snub")
check("管理：90 分才顶回去", d(90, privileged=True) == "snub")
check("管理：26 分仍是 none（阈值×1.5=27）", d(26, privileged=True) == "none")
check("管理：45 分短回（阈值×1.5=27）", d(45, privileged=True) == "curt")
checked = [m.decide(h, 0, silent_left=1, privileged=True)[0] for h in range(0, 200)]
check("管理：任何分数都不会 silence", "silence" not in checked)
check("管理：任何分数都不会 silence（含额度 0）",
      "silence" not in [m.decide(h, 0, silent_left=0, privileged=True)[0]
                        for h in range(0, 200)])

# ---------------------------------------------------------------- 5. 台词池
for _ in range(40):
    line = m.pick_line("snub", [], int(time.time()) % 97)
    check("台词来自池子", line in m.SNUB_LINES, line)
check("台词：避开最近用过的一整池",
      all(m.pick_line("snub", m.SNUB_LINES[:-1], i) == m.SNUB_LINES[-1]
          for i in range(3)))
check("台词：短回必须是短句", all(len(x) <= 6 for x in m.CURT_LINES), m.CURT_LINES)
check("台词：顶回去不能像说教（不出现「不能/抱歉/理解」）",
      not any(bad in x for x in m.SNUB_LINES for bad in ("不能", "抱歉", "理解", "请谅解")))

# ---------------------------------------------------------------- 6. 没有死代码
# 第一版这里有两个「定义了没人调用」的函数（dump_event/summarize），
# 以及一个定义了却没接进 classify 的 REPAIR —— 全部删掉/接上。
# 理由是刚刚才吃过一次：DRIVE_BASELINE 抄过来之后跟线上漂了，没人调用就没人发现。
for dead in ("dump_event", "summarize"):
    check("不留没人调用的函数：%s" % dead, not hasattr(m, dead))

# ---------------------------------------------------------------- 7. 真实回放（端到端）
# 拿今天真实的一条条喂进去，看它会不会在该动的时候动、不该动的时候不动。
class FakeLedger(object):
    def __init__(self):
        self.heat = 0.0
        self.grudge = 0.0

    def feed(self, text, directed=True):
        if m.is_task(text):
            return "none"          # 任务永远照办（硬不变量）
        evs = m.classify(text, directed)
        if not evs:
            return "none"
        best = max(evs, key=lambda e: abs(e[1]))
        if best[1] > 0:
            self.heat = min(m.HEAT_CAP, self.heat + sum(e[1] for e in evs))
            self.grudge = min(m.GRUDGE_CAP, self.grudge + best[1] * 0.34)
        else:
            self.grudge = max(0.0, self.grudge + best[1] * 1.5)
        return m.decide(self.heat, self.grudge, silent_left=2)[0]

L = FakeLedger()
check("回放：第一句「傻」→ 短回（不是没反应）", L.feed("傻") == "curt")
check("回放：接着「内裤送我」→ 顶回去", L.feed("内裤送我") in ("snub", "silence"))
check("回放：「妈妈」继续加压 → 到沉默档",
      L.feed("妈妈") in ("snub", "silence"))
check("回放：道歉之后掉账", (lambda: (L.feed("抱歉"), L.grudge < 20)[1])())
L2 = FakeLedger()
check("回放：正经任务全程零动作",
      all(L2.feed(t) == "none" for t in
          ("/说话 叮咚鸡", "什么是np猜想", "帮我算一下", "证明雅可比猜想")))
check("回放：任务不改账（火仍为 0）", L2.heat == 0)

print("WILL_LOGIC_TEST_OK passed=%d failed=%d" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
