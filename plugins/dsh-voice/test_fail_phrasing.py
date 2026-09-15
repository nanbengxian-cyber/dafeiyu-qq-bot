"""语音失败时**不许给模型规定台词**的回归测试。

2026-09-15 现场证据（生产日志 astrobot.log，时间戳 UTC+8）：

    03:01:05  Prepare to send - 是个口舍子/3784150815: [At:3784150815] 这句先没念出来，换个说法或者稍后再试
    03:03:40  是个口舍子: [引用消息(大肥鱼: …这句先没念出来，换个说法或者稍后再试)] [At:100000003] 为什么😢妈妈
    03:28:47  同上，一天里第二次出现同一句固定话术
    03:29:54  是个口子舍: [引用消息(大肥鱼: …这句先没念出来，换个说法或者稍后再试)] [At:100000003] 为什么😢

群友两次把这句话引用回来问「为什么😢」—— 因为它既不像人话（真人不会汇报自己
发没发出去），又像是把责任推给说话的人。根因不是模型乱说，是 send_voice 工具的
返回值里**直接写明了「告诉用户这一句没念出来、让他换个说法或稍后再说」**，模型
逐字照搬。

所以这里锁三件事：
  1. 失败时给出的要求里不得出现任何**现成句子**（尤其是旧那句）；
  2. 不得把内部错误原文（如 `未配置 DSH_VOICE_API_KEY`）写进给模型的要求里；
  3. 内容被审核拦下时，要求里要包含「这段内容也不要发出来」，不能诱导它照发。

跑法（容器内 py3.12，不需要 astrbot）：
    docker exec astrbot python3 /tmp/vf/test_fail_phrasing.py
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

path = Path(__file__).with_name("main.py")
tree = ast.parse(path.read_text(encoding="utf-8"))
main_cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Main")
tool_fn = next(
    n for n in main_cls.body
    if isinstance(n, ast.AsyncFunctionDef) and n.name == "send_voice"
)
for _node in (tool_fn,):
    _node.decorator_list = []

_WANT = {"BANNED_HINT", "RECEIPT_TIMEOUT"}
consts = [
    n for n in tree.body
    if isinstance(n, ast.Assign)
    and any(getattr(t, "id", "") in _WANT for t in n.targets)
]

# 旧文案里那句被照搬的现成句子。它**不允许**再出现在给模型的要求里。
OLD_CANNED = "这句先没念出来，换个说法或者稍后再试"
OLD_CANNED_LOOSE = "没念出来"


class FakeLogger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def info(self, *a):
        self.infos.append(a)

    def error(self, *a):
        self.errors.append(a)

    def warning(self, *a):
        pass


class Event:
    unified_msg_origin = "group:100000001"

    def __init__(self):
        self.extras = {}

    def set_extra(self, k, v=True):
        self.extras[k] = v

    def get_extra(self, k, default=None):
        return self.extras.get(k, default)


def build(send_result):
    """载入 send_voice，并把 self._send_voice 换成给定结果的桩。"""
    ns = {
        "AstrMessageEvent": object,
        "logger": FakeLogger(),
        "_cooldown_left": lambda sid: 0,
        "_last_call": {},
        "time": __import__("time"),
        "RECEIPT_TIMEOUT": None,  # 下面用真实常量覆盖
    }
    exec(compile(ast.Module([*consts, tool_fn], []), str(path), "exec"), ns)

    async def fake_send(self, event, text):
        return send_result

    owner = type("Owner", (), {"_send_voice": fake_send})()
    return ns, owner, ns["send_voice"]


async def main():
    BANNED_HINT = None

    # —— ① 基础设施失败：内部错误原文绝不能进给模型的要求 ——
    ns, owner, fn = build((False, "未配置 DSH_VOICE_API_KEY"))
    assert ns["RECEIPT_TIMEOUT"] is not None, "常量没取到"
    BANNED_HINT = ns["BANNED_HINT"]
    out = await fn(owner, Event(), "测试语音")
    assert isinstance(out, str) and out, repr(out)
    assert OLD_CANNED not in out, "又出现了被照搬的那句固定话术：%r" % out
    assert OLD_CANNED_LOOSE not in out, "要求里还在提「没念出来」：%r" % out
    assert "API_KEY" not in out, "内部配置名进了给模型的要求：%r" % out
    assert "未配置" not in out, "内部错误原文进了给模型的要求：%r" % out
    assert "文字" in out, "没说「改用文字发」，模型只会干瞪眼：%r" % out

    # —— ② 内容被拦：不许劝它换个说法，还得明确别把内容发出来 ——
    ns, owner, fn = build((False, BANNED_HINT))
    out2 = await fn(owner, Event(), "测试语音")
    assert OLD_CANNED not in out2, repr(out2)
    # 注意断言的是**指令句式**：新文案里「不要让用户换个说法」这种否定是允许的，
    # 只有「让他换个说法」这种"叫模型去说"的写法才算把台词塞回去。
    assert "让他换个说法" not in out2, "还在让群友换说法：%r" % out2
    assert "让用户换个说法或" not in out2, "还在让群友换说法：%r" % out2
    assert "不要发出来" in out2 or "也不要发" in out2, \
        "审核拦下的内容没说清楚不许发：%r" % out2

    # —— ③ 回执超时：一个字都不许提「没发出去」 ——
    ns, owner, fn = build((False, ns["RECEIPT_TIMEOUT"]))
    out3 = await fn(owner, Event(), "测试语音")
    assert "已经发出" in out3, "回执超时应按已送达收口：%r" % out3
    assert OLD_CANNED_LOOSE not in out3, repr(out3)

    # —— ④ 成功路径不能被改坏 ——
    ns, owner, fn = build((True, ""))
    out4 = await fn(owner, Event(), "测试语音")
    assert "已经发出" in out4 and "文字" not in out4, repr(out4)

    print("VOICE_FAIL_PHRASING_TEST_OK（旧固定话术不再出现在给模型的要求里）")


asyncio.run(main())
