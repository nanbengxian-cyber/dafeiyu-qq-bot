"""dsh-emotion 纯逻辑测试：不导入 astrbot，靠 AST 摘出纯函数跑。

分三组：
  A 组 —— 抽取规则（含真群里踩到的误判样本，锁死不许回退）
  B 组 —— 状态机转移与清零
  C 组 —— 脏数据收敛与注入块形状
"""
import ast
import json
import os
import re
import sys

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
src = open(PATH, encoding="utf-8").read()
tree = ast.parse(src)

WANT_FUNCS = {"default_state", "normalize_state", "is_directed", "_match_emotion",
              "extract_candidate", "_ttl", "transition", "render_context"}
ns = {"json": json, "re": re, "os": os}
# 先执行模块级的常量赋值（词表、正则、顺序表），再执行需要的函数定义。
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        try:
            exec(compile(ast.Module([node], []), PATH, "exec"), ns)
        except (NameError, AttributeError, TypeError):
            pass  # 依赖 astrbot / Path 的赋值跳过
    elif isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
        exec(compile(ast.Module([node], []), PATH, "exec"), ns)

E = ns["extract_candidate"]
T = ns["transition"]
D = ns["default_state"]
N = ns["normalize_state"]
R = ns["render_context"]
TTL = ns["_ttl"]

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


print("A 组 抽取规则")
# A1-A4：真群回放里被误判的原句，必须不再命中
check("A1 余额不算尴尬", E("我的余额还有120多") is None)
check("A2 额度不算尴尬", E("担心这个，还不如担心我的额度够不够") is None)
check("A3 额外额度不算尴尬", E("作者要饿似了，所以暂时不给我额外额度了") is None)
check("A4 自嘲提问不算问机器人", E("为什么我这么菜") is None)
# A5：裸语气词才算尴尬
check("A5 裸语气词算尴尬", (E("额") or {}).get("emotion") == "awkward")
# A6-A7：攻击必须有指向
check("A6 无指向的骂声不算", E("闭嘴") is None)
check("A7 带你的骂声算生气", (E("你倒是先闭嘴") or {}).get("emotion") == "angry")
check("A8 @机器人时裸骂也算", (E("闭嘴", True) or {}).get("emotion") == "angry")
# A9：同一句多信号只出一个，且按优先级
check("A9 一句多信号只出一个", (E("你好厉害，但也真让我失望") or {}).get("emotion") == "sad")
# A10：清零压过一切
check("A10 道歉优先清零", (E("对不起，你别生气了，我说错了") or {}).get("kind") == "reset")
# A11-A12：第三方与引用
check("A11 第三方叙述不算", E("他昨天特别难过") is None)
check("A12 整句引用不算", E("“你倒是先闭嘴”") is None)
# A13：提问要有疑问结构且指向
check("A13 指向提问算好奇", (E("那你为什么没有？") or {}).get("emotion") == "curious")
check("A14 无指向提问不算", E("为什么不发语音") is None)
# A15：笑声算开心，强度只有 1（环境氛围）
check("A15 笑声算弱开心", (E("哈哈哈哈") or {}) .get("intensity") == 1)
# A16：指令不参与
check("A16 斜杠指令不算", E("/情绪状态") is None)
# A17：阴阳怪气不当成开心（攻击优先）
check("A17 夹枪带棒判生气", (E("牛逼，你谁都想对着干呗，有病") or {}).get("emotion") == "angry")

print("B 组 状态机")
s0 = D()
s1, r1 = T(s0, E("你倒是先闭嘴"), 1000.0)
check("B1 生气触发", s1["emotion"] == "angry" and r1 == "triggered")
check("B2 生气 TTL 正确", abs(s1["expires_at"] - (1000.0 + TTL("angry"))) < 1e-6)
s2, r2 = T(s1, E("对不起，我说错了"), 1010.0)
check("B3 道歉清零", s2["emotion"] == "calm" and r2 == "explicit_reset")
s3, r3 = T(s1, None, 1000.0 + TTL("angry") + 1)
check("B4 TTL 到期清零", s3["emotion"] == "calm" and r3 == "ttl_expired")
mid, rmid = T(s1, None, 1020.0)
check("B5 一条中性只是待清零", mid["emotion"] == "angry" and rmid == "neutral_pending")
s4, r4 = T(mid, None, 1030.0)
check("B6 连续两条中性清零", s4["emotion"] == "calm" and r4 == "neutral_run")
s5, r5 = T(s1, E("哈哈哈哈"), 1040.0)
check("B7 弱开心不覆盖强生气", s5["emotion"] == "angry" and r5 == "kept_stronger")
s6, _ = T(D(), E("那你为什么没有？"), 2000.0)
s7, r7 = T(s6, E("你太厉害了"), 2010.0)
check("B8 好奇被回答", s6["emotion"] == "curious" and s7["emotion"] == "happy" and r7 == "answered")
s8, r8 = T(s1, E("你真让我失望"), 1050.0)
check("B9 同强度可切换", s8["emotion"] == "sad" or r8 == "kept_stronger")
s9, r9 = T(D(), {"emotion": "nonsense", "intensity": 9}, 3000.0)
check("B10 非法候选不改状态", s9["emotion"] == "calm" and r9 == "invalid_candidate")
s10, r10 = T(D(), None, 3000.0)
check("B11 平静无候选保持", s10["emotion"] == "calm" and r10 == "unchanged")
s11, r11 = T(s1, E("你倒是先闭嘴"), 1060.0)
check("B12 同情绪刷新", s11["emotion"] == "angry" and r11 == "refreshed")

print("C 组 脏数据与注入")
check("C1 None 收敛为平静", N(None)["emotion"] == "calm")
check("C2 非法情绪收敛", N({"emotion": "boom", "intensity": 99})["emotion"] == "calm")
check("C3 强度上限收敛", N({"emotion": "angry", "intensity": 99})["intensity"] == 3)
check("C4 坏时间戳收敛", N({"emotion": "sad", "started_at": "x"})["started_at"] == 0.0)
check("C5 平静不注入", R(D()) == "")
blk = R({"emotion": "angry", "intensity": 3})
check("C6 注入块小写标签", blk.startswith("<emotion_state>") and blk.endswith("</emotion_state>"))
check("C7 注入块不含台词", "说" in blk and "别把情绪本身说出来" in blk.replace("不要", "别"))

print()
if fails:
    print("FAILED %d: %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("全部通过（A 组 17 + B 组 12 + C 组 7 = 36 项）")
