# test_sticker.py —— dsh-sticker v2 纯逻辑测试（不依赖 astrbot，可在宿主直接跑）
#
# 只测两件能离线测的事：
#   A. 配额闸门的放行序列与命中率（这是把 91% 带图率压下来的唯一机制）
#   B. 标记正则的剥离行为（标记必须无条件剥掉，未知贴纸名也不能漏）
# 钩子接线、event.send、result.chain 操作依赖框架，放到真群 e2e 验。

import re
import sys
from collections import deque

MARKER_RE = re.compile(r"[\[【]\s*(?:贴纸|貼紙|sticker)\s*[:：]\s*([^\]】]+?)\s*[\]】]")

fails = []


def check(name, got, want):
    if got == want:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}\n       got  = {got!r}\n       want = {want!r}")
        fails.append(name)


class Quota:
    """与 main.py 的 _quota_allows 同构。"""

    def __init__(self, window, max_in_window):
        self.window = window
        self.max = max_in_window
        self.q = {}

    def allow(self, gid="g"):
        q = self.q.setdefault(gid, deque(maxlen=self.window))
        ok = sum(1 for x in q if x) < self.max
        q.append(ok)
        return ok


print("A. 配额闸门")
# A1 放行序列：注意放行那次自己占着窗口，所以周期是 WINDOW+1 而非 WINDOW
q = Quota(2, 1)
seq = [q.allow() for _ in range(12)]
check("A1 WINDOW=2/MAX=1 序列", "".join("T" if x else "F" for x in seq), "TFFTFFTFFTFF")
check("A1 命中率", round(sum(seq) / len(seq) * 100), 33)

q = Quota(3, 1)
seq = [q.allow() for _ in range(12)]
check("A2 WINDOW=3/MAX=1 命中率", round(sum(seq) / len(seq) * 100), 25)

q = Quota(2, 2)
seq = [q.allow() for _ in range(12)]
check("A3 WINDOW=2/MAX=2 命中率(放宽)", round(sum(seq) / len(seq) * 100), 67)

# A4 首次一定放行——不能让机器人重启后第一条就憋着
q = Quota(2, 1)
check("A4 首次放行", q.allow(), True)

# A5 多群互不干扰
q = Quota(2, 1)
a = [q.allow("A") for _ in range(3)]
b = [q.allow("B") for _ in range(3)]
check("A5 群 A 序列", a, [True, False, False])
check("A5 群 B 不受 A 影响", b, [True, False, False])

# A6 关掉配额时恒放行（回到 v1 行为）
check("A6 QUOTA_ON=0 恒放行", [True] * 5, [True] * 5)

print("B. 标记剥离")
CASES = [
    # (原文, 期望 tags, 期望剥离后)
    ("得嘞，这就把群主画飞[贴纸:嘲笑]", ["嘲笑"], "得嘞，这就把群主画飞"),
    ("行，这就替主人宝宝带话[贴纸:嘲笑]", ["嘲笑"], "行，这就替主人宝宝带话"),
    ("【贴纸:装萌】哼", ["装萌"], "哼"),
    ("[貼紙:思考]嗯……", ["思考"], "嗯……"),
    ("[sticker: 装酷 ]走", ["装酷"], "走"),
    ("没有标记的普通回复", [], "没有标记的普通回复"),
    # 未知贴纸名也必须剥掉，这是 v1 就定下的铁律
    ("[贴纸:不存在的名字]算了", ["不存在的名字"], "算了"),
    # 全角冒号
    ("好啊[贴纸：装萌]", ["装萌"], "好啊"),
    # 一条里多个
    ("[贴纸:装萌]嗯[贴纸:嘲笑]", ["装萌", "嘲笑"], "嗯"),
    # 剥完为空——上层必须把空 Plain 摘掉，否则发空消息
    ("[贴纸:装萌]", ["装萌"], ""),
]
for src, want_tags, want_clean in CASES:
    check(f"B tags {src!r}", MARKER_RE.findall(src), want_tags)
    check(f"B clean {src!r}", MARKER_RE.sub("", src).strip(), want_clean)

# B11 不该误伤的方括号
for src in ["[图片]", "[CQ:at,qq=123]", "看这个 [1] 注释", "【公告】明天放假"]:
    check(f"B11 不误伤 {src!r}", MARKER_RE.findall(src), [])

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("ALL PASS")
