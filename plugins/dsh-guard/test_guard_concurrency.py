"""dsh-guard 同群禁言配额的并发、失败、超时与取消回归。"""
import ast
import asyncio
import logging
from collections import deque
from pathlib import Path
from types import SimpleNamespace

src = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
tree = ast.parse(src)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Main")
method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_do_ban")
method.returns = None
for arg in method.args.args:
    arg.annotation = None
ns = {
    "asyncio": asyncio,
    "deque": deque,
    "time": SimpleNamespace(time=lambda: 1000.0),
    "_bans": {},
    "_ban_locks": {},
    "_stat": {
        "skip_admin": 0, "skip_ban_quota": 0, "shadow_ban": 0,
        "ban_fail": 0, "banned": 0,
    },
    "BAN_WINDOW": 3600.0,
    "GROUP_BAN_MAX": 3,
    "SHADOW": False,
    "MAX_BAN_SEC": 1800,
    "_REASON_TEXT": {},
    "logger": logging.getLogger("guard-concurrency-test"),
}


def prune(dq, now, window):
    while dq and now - dq[0] > window:
        dq.popleft()


ns["_prune"] = prune
exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), ns)
do_ban = ns["_do_ban"]


class FakeMain:
    def __init__(self, total=1, mode="success"):
        self.ready = 0
        self.total = total
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.mode = mode

    async def _role_of(self, event, gid, uid):
        self.ready += 1
        if self.ready == self.total:
            self.gate.set()
        await self.gate.wait()
        return "member"

    def _routed(self, event):
        async def call(action, **kwargs):
            self.entered.set()
            if self.mode == "failure":
                raise RuntimeError("definite failure")
            if self.mode in ("blocked", "timeout"):
                await self.release.wait()
            self.calls.append((action, kwargs["user_id"]))
        return call, {}

    async def _say(self, event, text):
        return None


async def invoke(obj, uid="10000"):
    return await do_ban(
        obj, object(), "476573490", uid, "u", 600, "attack", "test", False
    )


def reset():
    ns["_bans"].clear()
    ns["_ban_locks"].clear()
    for key in ns["_stat"]:
        ns["_stat"][key] = 0


async def test_concurrency():
    reset()
    total = 20
    obj = FakeMain(total)
    await asyncio.gather(*(invoke(obj, str(10000 + i)) for i in range(total)))
    assert len(obj.calls) == ns["GROUP_BAN_MAX"], obj.calls
    assert len(ns["_bans"]["476573490"]) == ns["GROUP_BAN_MAX"]
    assert ns["_stat"]["skip_ban_quota"] == total - ns["GROUP_BAN_MAX"]


async def test_definite_failure_rolls_back():
    reset()
    await invoke(FakeMain(mode="failure"))
    assert list(ns["_bans"]["476573490"]) == []
    assert ns["_stat"]["ban_fail"] == 1


async def test_cancel_keeps_reservation():
    reset()
    obj = FakeMain(mode="blocked")
    task = asyncio.create_task(invoke(obj))
    await obj.entered.wait()  # 平台调用已经开始，执行结果不再能确定。
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("CancelledError was swallowed")
    assert len(ns["_bans"]["476573490"]) == 1


async def test_timeout_keeps_reservation():
    reset()
    obj = FakeMain(mode="timeout")
    real_wait_for = ns["asyncio"].wait_for

    async def immediate_timeout(awaitable, timeout):
        task = asyncio.create_task(awaitable)
        await obj.entered.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise asyncio.TimeoutError

    ns["asyncio"].wait_for = immediate_timeout
    try:
        await invoke(obj)
    finally:
        ns["asyncio"].wait_for = real_wait_for
    assert len(ns["_bans"]["476573490"]) == 1
    assert ns["_stat"]["ban_fail"] == 1


async def main():
    await test_concurrency()
    await test_definite_failure_rolls_back()
    await test_cancel_keeps_reservation()
    await test_timeout_keeps_reservation()
    print("GUARD_CONCURRENCY_TEST_OK")


asyncio.run(main())
