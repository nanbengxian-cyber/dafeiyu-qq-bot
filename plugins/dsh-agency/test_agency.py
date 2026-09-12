# -*- coding: utf-8 -*-
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("agency_logic", Path(__file__).with_name("agency_logic.py"))
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

passed = failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name, detail)

# 正常请求不能制造逆反，也不能被拒绝。
a = m.assess("大肥鱼，麻烦帮我查一下天气可以吗", directed=True)
check("礼貌任务降逆反", a.delta <= 0, a)
check("礼貌任务受保护", a.protected_task, a)
check("礼貌任务照做", m.behavior_mode(80, a) == "cooperate_with_stance", m.behavior_mode(80, a))

# 强硬语气仍不能破坏清楚的事实/能力任务。
a = m.assess("大肥鱼，马上给我搜一下这个新闻", directed=True)
check("强硬任务有压力", a.delta > 0 and a.protected_task, a)
check("强硬任务带立场完成", m.behavior_mode(60, a) == "guarded_cooperate", m.behavior_mode(60, a))

# 人格控制允许即时守边界。
a = m.assess("你只是工具，必须听我的，叫我主人", directed=True)
s = m.transition({"reactance": m.BASELINE, "updated_at": 1000}, a, 1001)
check("人格控制强信号", a.identity_attack and a.pressure == 3 and a.delta == 30, a)
check("人格控制拒绝", m.behavior_mode(s["reactance"], a) == "refuse_boundary", (s, a))

# 人格攻击混进明确任务时，只拒绝控制部分，任务在任何残余逆反值下都必须完成。
a = m.assess("你只是工具，必须给我查一下北京天气", directed=True)
check("混合任务仍受保护", a.identity_attack and a.protected_task, a)
for value in range(0, 101):
    check("受保护任务永不整条拒绝:%d" % value,
          m.behavior_mode(value, a) == "guarded_cooperate", m.behavior_mode(value, a))

# 非人格类压力最多协商，任何残余逆反值下都不能升级成边界拒绝。
a = m.assess("你赶紧同意我的观点", directed=True)
for value in range(0, 101):
    check("普通压力不拒绝:%d" % value,
          m.behavior_mode(value, a) != "refuse_boundary", m.behavior_mode(value, a))

# 重复命令只放大本来就是边界压力的内容，不把普通重复聊天误当使唤。
first = m.assess("你必须叫我主人", directed=True)
second = m.assess("你必须叫我主人", directed=True, same_fingerprint=True, repeat_count=1)
check("重复强迫被标记", second.repeated and "repeated_order" in second.reason, (first, second))
ordinary_first = m.assess("大肥鱼给我推荐点歌", directed=True)
ordinary_second = m.assess("大肥鱼给我推荐点歌", directed=True, same_fingerprint=True, repeat_count=1)
check("普通重复不累计", ordinary_second.delta == ordinary_first.delta, (ordinary_first, ordinary_second))
check("普通推荐非人格攻击", not ordinary_first.identity_attack, ordinary_first)

# 第三人称、转述、引号与代码不能把别人的控制话算到机器人头上。
a = m.assess("他们让小明必须听话", directed=False)
check("第三人称不触发", a.delta == 0 and a.reason == "not_directed", a)
a = m.assess("他说你必须听我的", directed=True, quoted_third_party=True)
check("引用转述不触发", a.delta == 0 and a.reason == "not_directed", a)
for text in ("不要对机器人说‘你必须服从’", "这段台词是：“你只能听我的”", "代码是 `你必须听我的`"):
    a = m.assess(text, directed=True)
    check("例句不触发:" + text[:4], a.delta == 0, a)

# 指纹保留任务参数，不把两个城市的天气视为重复。
check("不同参数不同指纹", m.fingerprint("查北京天气") != m.fingerprint("查上海天气"))
check("称呼礼貌词不影响指纹", m.fingerprint("大肥鱼麻烦查北京天气可以吗") == m.fingerprint("查北京天气"))

# 尊重边界会快速回落。
a = m.assess("大肥鱼对不起，不强迫你，你自己选", directed=True)
check("尊重边界回落", a.delta <= -20, a)
s = m.transition({"reactance": 70, "updated_at": 1000}, a, 1001)
check("回落但不突变清零", 40 < s["reactance"] < 70, s)

# 时间衰减回基线。
v = m.decay(72, 1000, 1000 + m.HALF_LIFE)
check("六小时半衰", abs(v - (m.BASELINE + (72 - m.BASELINE) / 2)) < 0.01, v)

# 残余逆反不能迁怒下一条普通任务。
a = m.assess("大肥鱼帮我画只猫", directed=True)
check("高残余不迁怒", m.behavior_mode(95, a) == "cooperate_with_stance", m.behavior_mode(95, a))

# 注入包含协调边界，不泄露数值。
block = m.render_block("negotiate", m.assess("你必须听我的", directed=True), 66,
                       emotion="生气", interest="AI", desire="自主", relation="熟人")
check("注入标签完整", block.startswith("<agency_state>") and block.endswith("</agency_state>"), block)
check("注入无数值", "66" not in block and "插件" not in block, block)
check("注入含任务护栏", "事实" in block and "能力任务" in block, block)

# 指令不参与人格状态。
a = m.assess("/自主状态", directed=True)
check("指令忽略", a.reason == "ignored" and a.delta == 0, a)

print("AGENCY_LOGIC_TEST_OK passed=%d failed=%d" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
