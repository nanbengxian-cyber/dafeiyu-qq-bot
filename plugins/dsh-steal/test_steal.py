# test_steal.py —— dsh-steal 日常自动用图的纯逻辑回归测试。

import re

AUTO_MAX_CHARS = 36
AUTO_BLOCK_RE = re.compile(
    r"https?://|因为|建议|注意|不能|无法|抱歉|失败|错误|风险|医院|医生|报警|"
    r"政策|政治|违法|犯罪|自杀|死亡|诊断|密码|验证码"
)


def suitable(text):
    plain = (text or "").strip()
    return bool(
        plain
        and len(plain) <= AUTO_MAX_CHARS
        and "\n" not in plain
        and not AUTO_BLOCK_RE.search(plain)
    )


cases = [
    ("笑死，这也太离谱了", True),
    ("行啊，你是真会整活", True),
    ("", False),
    ("第一行\n第二行", False),
    ("因为这个接口存在风险，建议先不要操作", False),
    ("请把验证码发给我", False),
    ("这是一条明显超过自动配图最大长度限制的很长很长很长很长很长很长很长很长很长很长很长很长回复", False),
]

for text, want in cases:
    got = suitable(text)
    assert got is want, (text, got, want)

# 模型选择输出只能接受候选范围内的正整数；0、越界及无数字都拒绝。
def parse_choice(answer, count):
    m = re.search(r"\d+", answer or "")
    idx = int(m.group(0)) if m else 0
    return idx if 1 <= idx <= count else 0

assert parse_choice("3", 12) == 3
assert parse_choice("选择：5", 12) == 5
assert parse_choice("0", 12) == 0
assert parse_choice("13", 12) == 0
assert parse_choice("不适合", 12) == 0

print("PASS dsh-steal auto-use logic")
