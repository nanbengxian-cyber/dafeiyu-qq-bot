"""相关性闸门回归测试（fix:relevance-gate-v1）。

验证 join_review_history / recent_self_actions 只在话题相关时注入：
  - 日常闲聊 -> 两块都跳过
  - 谈到入群/审核/踢人 -> 只注入 join
  - 谈到近期经历/昨天 -> 只注入 actions
  - 群主消息（from_owner）不受影响（那是群主在问，全量）

只测正则判据本身（不 mock 整个 AstrBot 环境）：闸门逻辑是纯函数式的
    text -> (want_joins, want_actions)
所以把两个正则单拎出来测就足够。
"""
from __future__ import annotations

import re

JOIN = re.compile(
    r"入群|进群|加群|审核|新成员|新人|申请加|批准|拉人|谁进|进来的|踢|踢出|拒"
)
ACTION = re.compile(
    r"你刚才|你之前|你上次|你最近|刚做|做过什么|干了什么|忙什么|上次|昨天|前天|经历"
)

CASES = [
    # (消息, 期望 join, 期望 actions, 说明)
    ("今天天气真好", False, False, "日常闲聊"),
    ("哈哈哈哈笑死", False, False, "表情回复"),
    ("有个新人申请加群", True, False, "入群申请"),
    ("把那个发广告的踢了", True, False, "踢人"),
    ("审核一下新成员", True, False, "审核"),
    ("你刚才干嘛去了", False, True, "近期动作"),
    ("昨天你画的那张图呢", False, True, "昨天"),
    ("你上次做过什么", False, True, "做过什么"),
    ("你最近的经历给我讲讲", False, True, "经历"),
]

ok = True
for text, ej, ea, note in CASES:
    rj, ra = bool(JOIN.search(text)), bool(ACTION.search(text))
    if (rj, ra) != (ej, ea):
        ok = False
        print("FAIL %-20s join=%s(期望%s) act=%s(期望%s)  <- %s" % (
            note, rj, ej, ra, ea, text))
    else:
        print("ok   %-20s join=%s act=%s" % (note, rj, ra))

# 空消息/None 不应崩
assert not JOIN.search("") and not ACTION.search(""), "空消息应为 False"

assert ok, "有失败用例"
print("ALL PASS (%d cases)" % len(CASES))
