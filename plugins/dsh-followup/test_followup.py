# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from followup_logic import (
    is_short_ack, keyword_tokens, reply_probability, should_wake_followup, topical_score,
)

fails = []


def check(name, got, want):
    if got == want:
        print("  PASS", name)
    else:
        print("  FAIL %s got=%r want=%r" % (name, got, want))
        fails.append(name)


print("A. 收尾短句")
for text in ("嗯", "好", "哈哈哈", "收到", "OK", "谢谢！"):
    check(text, is_short_ack(text), True)
for text in ("那你觉得哪个好", "我刚才说的是电脑", "哈哈但是这也太贵了"):
    check(text, is_short_ack(text), False)

print("B. 关键词与相似度")
check("中文话题词", "显卡" in keyword_tokens("这张显卡性能怎么样"), True)
check("英文整词", "deepseek" in keyword_tokens("DeepSeek V4 好用吗"), True)
high, high_hits = topical_score("那显卡温度高吗？", "这张 5070 显卡游戏性能不错，散热也还行")
low, low_hits = topical_score("我晚上准备吃火锅", "这张 5070 显卡游戏性能不错，散热也还行")
medium, _ = topical_score("那散热怎么样？", "显卡性能不错，散热器声音不大")
check("同话题高分", high >= 55, True)
check("无关话题低分", low <= 15, True)
check("同话题高于无关", high > low, True)
check("匹配词可解释", "显卡" in high_hits, True)
check("相关追问有中高分", medium >= 40, True)
check("概率单调", reply_probability(high) > reply_probability(low), True)
check("概率边界0", reply_probability(0), 0.05)
check("概率边界100", reply_probability(100), 0.95)

print("C. 概率续聊")
def D(**kw):
    base = dict(
        sender_id="u1", target_id="u1", text="那显卡温度呢", context_text="5070显卡性能不错",
        now=100, expires_at=200, quotes_self=False, at_other=False, roll=0.0,
        min_score=20, quiet=False,
    )
    base.update(kw)
    return should_wake_followup(**base)

ok = D()
check("同一人低随机数抽中", ok[0], True)
check("命中有分数", ok[2] > 0, True)
check("高随机数可不回", D(roll=0.999)[0], False)
check("引用机器人直回", D(sender_id="u2", target_id="", quotes_self=True),
      (True, "引用了机器人", 100, 1.0, []))
check("零关联不能靠5%撞中", D(text="今晚准备吃火锅", roll=0.0)[:3],
      (False, "上下文关联不足", 0))
check("低于可调语义底线不回", D(text="那散热怎么样", context_text="显卡散热器声音不大", min_score=60, roll=0.0)[0], False)
check("少打扰时不免艾特追聊", D(quiet=True)[:2], (False, "对方要求少打扰"))
check("少打扰仍回应明确引用", D(sender_id="u2", target_id="", quotes_self=True, quiet=True),
      (True, "引用了机器人", 100, 1.0, []))
check("换人不抢", D(sender_id="u2")[:2], (False, "不是上一轮对话对象"))
check("窗口过期", D(now=201)[:2], (False, "续聊窗口已过"))
check("@别人", D(at_other=True)[:2], (False, "明确@了别人"))
check("收尾应答", D(text="好")[:2], (False, "只是收尾应答"))
check("指令", D(text="/帮助")[:2], (False, "是指令"))

print("D. 社交边界联动")
# main.py 带 AstrBot 依赖；框架容器中从同目录显式加载它的只读联动函数。
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("followup_main", Path(__file__).with_name("main.py"))
    followup_main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(followup_main)
except ImportError:
    followup_main = None
if followup_main is not None:
    path = os.path.join(tempfile.mkdtemp(), "social.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE relations(group_id TEXT,user_id TEXT,avoid_until REAL,opted_out INTEGER)"
    )
    con.executemany(
        "INSERT INTO relations VALUES(?,?,?,?)",
        [("g1", "u1", time.time() + 60, 0), ("g1", "u2", 0, 1),
         ("g2", "u1", 0, 0)],
    )
    con.commit()
    con.close()
    check("临时边界按群按人读取", followup_main.social_quiet("g1", "u1", db=path), True)
    check("退出社交记录也不主动追聊", followup_main.social_quiet("g1", "u2", db=path), True)
    check("跨群不串边界", followup_main.social_quiet("g2", "u1", db=path), False)
    check("缺库故障时放行", followup_main.social_quiet("g1", "u1", db=path + ".missing"), False)

if fails:
    raise SystemExit("FAILED %d: %s" % (len(fails), fails))
print("ALL PASS")
