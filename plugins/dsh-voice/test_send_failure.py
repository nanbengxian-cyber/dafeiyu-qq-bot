"""发送阶段异常回归：NapCat retcode=1200 不得冒泡、不得自动重发、不得播报。

[patch:receipt-silent 2026-09-13] 第二条断言是本轮新增的：
retcode=1200 只是 QQ NT 内核没等到发送回执，语音**多半已经送达**。
旧版把它当成失败，往群里发「这次语音没确认发出去：QQ 发送回执超时，可能已经
送达；为避免重复语音未自动重发」—— 群友刚听到语音，紧接着看到机器人在念自己
的故障码，而且这句话大概率是假的。现在按已送达收口，一个字都不说。
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

path = Path(__file__).with_name("main.py")
tree = ast.parse(path.read_text(encoding="utf-8"))
main_cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Main")
send_fn = next(n for n in main_cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_voice")
cmd_fn = next(n for n in main_cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "cmd_speak")
# 常量从源码里取，测试不会和实现走散
_WANT = {"RECEIPT_TIMEOUT", "GENERIC_FAIL", "BANNED_HINT", "_HUMAN_REASONS"}
consts = [
    n for n in tree.body
    if isinstance(n, ast.Assign)
    and any(getattr(t, "id", "") in _WANT for t in n.targets)
]
human_fn = next(
    n for n in tree.body
    if isinstance(n, ast.FunctionDef) and n.name == "_human_err"
)
# 摘掉 @filter.command(...) 之类的装饰器：这里只测函数体，不引框架。
for _node in (send_fn, cmd_fn):
    _node.decorator_list = []


class FakeLogger:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def info(self, *args):
        self.infos.append(args)

    def warning(self, *args):
        self.warnings.append(args)


class Record:
    @staticmethod
    def fromFileSystem(path):
        return ("record", path)


class MessageChain:
    def __init__(self, chain):
        self.chain = chain


class ActionFailed(Exception):
    pass


class Event:
    unified_msg_origin = "group:1"
    message_str = "/说话 测试语音"

    def __init__(self, exc=None):
        self.exc = exc
        self.sends = 0
        self.sent_texts = []

    async def send(self, chain):
        self.sends += 1
        if self.exc:
            raise self.exc

    def set_extra(self, *a, **k):
        pass

    def plain_result(self, text):
        self.sent_texts.append(text)
        return ("plain", text)


class Tasks(set):
    pass


async def run():
    ns = {
        "AstrMessageEvent": object,
        "_truncate": lambda x: x,
        "_clean_for_tts": lambda x: x,
        "_censor_text": lambda *a: asyncio.sleep(0, result=(True, "")),
        "_synthesize": lambda *a: asyncio.sleep(0, result=("/tmp/a.mp3", "")),
        "_ref_for": lambda x: "ref",
        "_notice_allowed": lambda *a, **k: True,
        "NOTICE_GAP": 90,
        "_cooldown_left": lambda sid: 0,
        "_last_call": {},
        "Record": Record,
        "MessageChain": MessageChain,
        "logger": FakeLogger(),
        "asyncio": asyncio,
        "time": __import__("time"),
        "_cleanup_old": lambda: asyncio.sleep(0),
    }
    exec(compile(ast.Module([*consts, human_fn, send_fn, cmd_fn], []), str(path), "exec"), ns)
    RECEIPT_TIMEOUT = ns["RECEIPT_TIMEOUT"]
    owner = type("Owner", (), {"context": object(), "_tasks": Tasks()})()
    owner._send_voice = ns["_send_voice"].__get__(owner, type(owner))

    timeout = ActionFailed(
        "<ActionFailed status='failed', retcode=1200, "
        "message='Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg'>"
    )
    event = Event(timeout)
    ok, err = await ns["_send_voice"](owner, event, "测试语音")
    assert not ok and err == RECEIPT_TIMEOUT, repr(err)
    assert event.sends == 1, "不确定送达时不能自动重发"
    assert ns["logger"].warnings, "至少要留下一条 warning 日志"

    # —— 关键回归：命令路径在回执超时时**一个字都不许发** ——
    ev_cmd = Event(timeout)
    out = [x async for x in ns["cmd_speak"](owner, ev_cmd)]
    assert out == [], "回执超时不得往群里发任何提示，实际发了：%r" % out

    event2 = Event(ActionFailed("network closed"))
    ok, err = await ns["_send_voice"](owner, event2, "测试语音")
    assert not ok and err == "这次声音没送出去，等一下再试试", repr(err)
    assert event2.sends == 1

    # —— 内部词不许外发 ——
    ns["_synthesize"] = lambda *a: asyncio.sleep(0, result=(None, "未配置 DSH_VOICE_API_KEY"))
    ev_cmd2 = Event()
    out2 = [x async for x in ns["cmd_speak"](owner, ev_cmd2)]
    texts2 = [x[1] for x in out2]
    assert texts2 == ["这次声音没送出去，等一下再试试"], repr(texts2)
    assert all("API_KEY" not in str(x) for x in out2), "内部配置名不许进群"

    event3 = Event()
    ns["_synthesize"] = lambda *a: asyncio.sleep(0, result=("/tmp/a.mp3", ""))
    ok, err = await ns["_send_voice"](owner, event3, "测试语音")
    assert ok and not err and event3.sends == 1
    if owner._tasks:
        await asyncio.gather(*owner._tasks)

    print("VOICE_SEND_FAILURE_TEST_OK")


asyncio.run(run())
