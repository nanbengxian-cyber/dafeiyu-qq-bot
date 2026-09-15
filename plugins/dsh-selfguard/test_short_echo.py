"""短句复读抑制回归测试（dsh-selfguard）。

2026-09-15 现场（群 100000001，机器人 100000003）：

    18:30:32  时髦啊: [引用消息(大肥鱼: 那地方我不去了)] [At:100000003] 摸摸
    18:30:35  机器人: 摸吧摸吧
    18:30:54  霓虹殊华: [引用消息(大肥鱼: 摸吧摸吧)] [At:100000003] 抱抱
    18:31:01  机器人: 抱吧抱吧
    18:31:45  霓虹殊华: [At:100000003] 你咋变这么乖了

同一对「摸吧摸吧 / 抱吧抱吧」当天在 12:23、17:19、18:30、18:31 各出现一次 ——
**输入什么形状、输出就什么形状**，连着两次一字不差，一眼就是程序。原来的
repeat_of() 抓不到：MIN_LEN=5，而「摸吧摸吧」只有 4 个字，第一条就被挡掉。

这个测试锁四件事：
  1. 短句整句重复时必须给出**同义说法**，且说法必须与原文不同；
  2. **绝对不许**出现「这茬刚聊完／已经聊过／刚说过」这类不耐烦话术
     （群主 2026-09-12 明确讨厌，并因此关掉了 DSH_SELFGUARD_REPEAT）；
  3. 长句不受影响：>SHORT_ECHO_LEN 字的句子交回原来的逻辑（返回 None）；
  4. 短句**不同**时不许乱换（防误伤：短句共用「吧」「了」等字极易误判）。

跑法（容器内 py3.12）：
    docker exec astrbot python3 /tmp/sg/test_short_echo.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

path = Path(__file__).with_name("main.py")
src = path.read_text(encoding="utf-8")

# 这个插件的 import 里有 astrbot，本测试只测纯函数，用 stub 顶上。
for name in (
    "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.message_components",
    "astrbot.core",
):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
sys.modules["astrbot.api.event"].AstrMessageEvent = object
sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
    on_decorating_result=lambda *a, **k: (lambda f: f),
    command=lambda *a, **k: (lambda f: f),
    platform_adapter_type=lambda *a, **k: (lambda f: f),
    after_message_sent=lambda *a, **k: (lambda f: f),
    on_llm_request=lambda *a, **k: (lambda f: f),
    event_message_type=lambda *a, **k: (lambda f: f),
    on_astrbot_loaded=lambda *a, **k: (lambda f: f),
)
sys.modules["astrbot.api.message_components"].Plain = type(
    "Plain", (), {"__init__": lambda self, text="": setattr(self, "text", text)}
)
sys.modules["astrbot.core"].logger = types.SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None,
    error=lambda *a, **k: None, debug=lambda *a, **k: None,
)

spec = importlib.util.spec_from_file_location("selfguard", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# 开关默认必须是开：这个补丁存在的意义就是它默认生效。
# 不测这条的话，把 DSH_SELFGUARD_SHORT_ECHO 的默认值改成 "0" 也能让整个测试全绿
# —— 第一次变异测试就是这么漏过去的（变异①当时仍报 OK）。
assert m.SHORT_ECHO_ON is True, "短句复读抑制默认没开"

# 群主明确讨厌的不耐烦话术：一个都不许出现。
BANNED_PHRASES = ("这茬刚聊完", "已经聊过", "刚说过", "聊过了", "说过了", "重复了")

cases = [
    ("摸吧摸吧", "摸吧摸吧"),
    ("抱吧抱吧", "抱吧抱吧"),
    ("行吧行吧", "行吧行吧"),
    ("来吧来吧", "来吧来吧"),
    ("好啊好啊", "好啊好啊"),
    ("算了算了", "算了算了"),
]
for cur, old in cases:
    got = m.short_echo_variant(cur, [old])
    assert got is not None, "整句重复没被识别: %r" % cur
    new_text, reason = got
    assert new_text, "有变体表却没给出说法: %r -> %r" % (cur, got)
    assert new_text != cur, "换说法换成了原句: %r" % cur

# —— 硬约束：不许出现不耐烦话术 ——
for cur, old in cases:
    new_text, reason = m.short_echo_variant(cur, [old])
    blob = "%s %s" % (new_text, reason)
    for bad in BANNED_PHRASES:
        assert bad not in blob, "出现了群主讨厌的话术 %r：%r" % (bad, blob)

# —— 带语气尾巴、带空格的同一句，也算重复 ——
assert m.short_echo_variant("摸吧摸吧～", ["摸吧摸吧"]) is not None, "语气尾巴没归一化"
assert m.short_echo_variant(" 摸吧摸吧 ", ["摸吧摸吧"]) is not None, "空格没归一化"

# —— 没有现成变体的短句：返回空说法，表示"这条别重复" ——
got = m.short_echo_variant("好耶好耶", ["好耶好耶"])
assert got is not None and got[0] == "", "没有变体时应返回空说法，实际 %r" % (got,)
for bad in BANNED_PHRASES:
    assert bad not in got[1], "无变体路径也不能说不耐烦话术：%r" % got[1]

# —— 长句不管：交回原逻辑 ——
long_text = "我觉得这个U盘还是买闪迪的比较稳一点"
assert m.short_echo_variant(long_text, [long_text]) is None, "长句不该走短句通道"
assert m.SHORT_ECHO_LEN >= 4, "阈值太小会把正常短回话也换掉：%r" % m.SHORT_ECHO_LEN
assert len(m.core("摸吧摸吧")) <= m.SHORT_ECHO_LEN, "「摸吧摸吧」必须在短句通道内"

# —— 防误伤：短句**不同**时不许换 ——
negatives = [
    ("行吧", "来吧"),        # 共用「吧」
    ("来了", "好了"),        # 共用「了」
    ("摸吧摸吧", "抱吧抱吧"),  # 同形状不同字
    ("好的", "行吧"),
]
for new_said, old_said in negatives:
    got = m.short_echo_variant(new_said, [old_said])
    assert got is None, "不同短句被误判成重复: %r vs %r -> %r" % (
        new_said, old_said, got)

# —— 长短不一的循环：连说两遍同一句，第二次必换 ——
recent: list[str] = []
first = "摸吧摸吧"
assert m.short_echo_variant(first, recent) is None, "第一次不该触发"
recent.append(first)
second = m.short_echo_variant(first, recent)
assert second is not None and second[0] and second[0] != first, repr(second)

print("SHORT_ECHO_TEST_OK（换说法且不含任何不耐烦话术；长句与不同短句都不受影响）")
