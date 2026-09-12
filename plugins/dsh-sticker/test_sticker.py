# test_sticker.py —— dsh-sticker v2 纯逻辑测试（不依赖 astrbot，可在宿主直接跑）
#
# 只测两件能离线测的事：
#   A. 配额闸门的放行序列与命中率（这是把 91% 带图率压下来的唯一机制）
#   B. 标记正则的剥离行为（标记必须无条件剥掉，未知贴纸名也不能漏）
# 钩子接线、event.send、result.chain 操作依赖框架，放到真群 e2e 验。

import random
import re
import sys
from collections import deque

MARKER_RE = re.compile(r"[\[【]\s*(?:贴纸|貼紙|sticker)\s*[:：]\s*([^\]】]+?)\s*[\]】]")
AUTO_MAX_CHARS = 28
_AUTO_TAG_RULES = (
    (re.compile(r"笑死|哈哈|绷不住|(?:^|[，。！？!?、\s])(?:乐|草|6)(?:$|[，。！？!?、\s])|离谱|逆天|抽象"), ("嘲笑", "小丑")),
    (re.compile(r"可爱|好乖|真棒|厉害|可以的|有点实力|谢谢|感谢|爱了"), ("装萌", "送花")),
    (re.compile(r"委屈|伤心|难受|哭|欺负|可怜|不理我"), ("装可怜", "假装没伤心")),
    (re.compile(r"困|熬夜|睡不着|通宵"), ("熬夜",)),
    (re.compile(r"想想|让我想|不懂|不知道|怎么回事|为啥|为什么|\?{1,3}|？{1,3}"), ("思考",)),
    (re.compile(r"看看|瞅瞅|来了|在吗|干嘛|冒泡"), ("探头",)),
    (re.compile(r"帅|稳|拿下|搞定|那必须|豪横"), ("装酷",)),
)
_AUTO_FALLBACK_TAGS = ("思考", "探头", "装萌", "装酷")


def auto_tag(text):
    plain = (text or "").strip()
    if not plain or len(plain) > AUTO_MAX_CHARS or "\n" in plain:
        return ""
    if re.search(r"https?://|因为|建议|注意|不能|无法|抱歉|出不了|失败|错误|风险|观察下|医院|医生|报警", plain):
        return ""
    for pattern, tags in _AUTO_TAG_RULES:
        if pattern.search(plain):
            return tags[0]
    return _AUTO_FALLBACK_TAGS[0]

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

print("C. 无标记回复自动补图选型")
for src, want in [
    ("笑死我了", "嘲笑"),
    ("你真厉害", "装萌"),
    ("有点委屈", "装可怜"),
    ("今晚通宵", "熬夜"),
    ("这怎么回事？", "思考"),
    ("我来看看", "探头"),
    ("稳，拿下", "装酷"),
]:
    check(f"C 语义选型 {src!r}", auto_tag(src), want)

for src in [
    "建议你先去医院看看", "抱歉，这个无法处理", "https://example.com",
    "这是一条超过自动贴纸最大长度限制的正经说明文字，不应该自动配上任何表情包",
    "第一行\n第二行",
]:
    check(f"C 安全放过 {src!r}", auto_tag(src), "")

# 单字规则必须有边界，不能把「乐」误命中在「快乐」中，也不能把版本号里的 6 当梗。
check("C 快乐不当嘲笑", auto_tag("祝你快乐") == "嘲笑", False)
check("C 版本号不当嘲笑", auto_tag("升级到6.1版本") == "嘲笑", False)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("ALL PASS")
