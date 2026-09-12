"""发送阶段异常回归：NapCat retcode=1200 不得冒泡或自动重发。"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

path = Path(__file__).with_name("main.py")
tree = ast.parse(path.read_text(encoding="utf-8"))
main_cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Main")
send_fn = next(n for n in main_cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_voice")


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def info(self, *args):
        pass

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

    def __init__(self, exc=None):
        self.exc = exc
        self.sends = 0

    async def send(self, chain):
        self.sends += 1
        if self.exc:
            raise self.exc


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
        "Record": Record,
        "MessageChain": MessageChain,
        "logger": FakeLogger(),
        "asyncio": asyncio,
        "_cleanup_old": lambda: asyncio.sleep(0),
    }
    exec(compile(ast.Module([send_fn], []), str(path), "exec"), ns)
    owner = type("Owner", (), {"context": object(), "_tasks": Tasks()})()

    timeout = ActionFailed(
        "<ActionFailed status='failed', retcode=1200, "
        "message='Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg'>"
    )
    event = Event(timeout)
    ok, err = await ns["_send_voice"](owner, event, "测试语音")
    assert not ok and "可能已经送达" in err
    assert event.sends == 1, "不确定送达时不能自动重发"
    assert ns["logger"].warnings

    event2 = Event(ActionFailed("network closed"))
    ok, err = await ns["_send_voice"](owner, event2, "测试语音")
    assert not ok and err == "QQ 语音通道暂时发送失败"
    assert event2.sends == 1

    event3 = Event()
    ok, err = await ns["_send_voice"](owner, event3, "测试语音")
    assert ok and not err and event3.sends == 1
    if owner._tasks:
        await asyncio.gather(*owner._tasks)

    print("VOICE_SEND_FAILURE_TEST_OK")


asyncio.run(run())
