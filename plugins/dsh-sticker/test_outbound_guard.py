# -*- coding: utf-8 -*-
"""dsh-sticker 出口兜底闸测试（v4）。

起因（2026-09-13）：群友 00:53 引用回来的机器人原文是

    零基础上太空？先学会在群里别被禁言吧[贴纸:装酷]

而它在全量日志的 `respond.stage:206 Prepare to send` 里**一次都没出现过** ——
说明这条没走 respond.stage，是 `event.send()` 平台直发的，
所以 on_llm_response / on_decorating_result 两道钩子都没碰到它。

跑法：sudo docker cp 本目录 astrbot:/tmp/st/ && sudo docker exec astrbot python3 /tmp/st/test_outbound_guard.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from loguru import logger as _lg          # noqa: E402

_LGR = type(_lg)
_ADD = _LGR.add
_LGR.add = lambda self, *a, **k: 0
try:
    import main as S
finally:
    _LGR.add = _ADD
    _lg.remove()

from astrbot.core.message.message_event_result import MessageChain   # noqa: E402
from astrbot.core.message.components import Image, Plain            # noqa: E402
from astrbot.core.platform.astr_message_event import AstrMessageEvent  # noqa: E402

LEAK = "零基础上太空？先学会在群里别被禁言吧[贴纸:装酷]"
CLEAN = "零基础上太空？先学会在群里别被禁言吧"

fail = []


def ck(c, m):
    if not c:
        fail.append(m)


print("=== 1. 生产里真漏出来的那条 ===")
chain = MessageChain(chain=[Plain(LEAK)])
n = S.guard_outgoing(chain)
print("   剥掉 %d 个标记 → %r" % (n, chain.chain[0].text))
ck(n == 1, "没剥到：n=%r" % n)
ck(chain.chain[0].text == CLEAN, "剥完文字不对：%r" % chain.chain[0].text)

print("\n=== 2. 干净文字一个字符都不动 ===")
for t in ["早上好", "", "这个 [不是标记] 是方括号", "1+1=2", "[At:3752949717] 在吗"]:
    c = MessageChain(chain=[Plain(t)])
    before = c.chain[0].text
    ck(S.guard_outgoing(c) == 0, "干净文字被剥了：%r" % t)
    ck(c.chain[0].text == before, "干净文字被改了：%r → %r" % (before, c.chain[0].text))
print("   5 条干净文本零改动 ✓")

print("\n=== 3. 只碰 Plain，图片原样 ===")
c = MessageChain(chain=[Image.fromFileSystem("/tmp/nope.gif"), Plain("看这个[贴纸:嘲笑]")])
before_img = c.chain[0]
S.guard_outgoing(c)
ck(c.chain[0] is before_img, "图片组件被换了")
ck(c.chain[1].text == "看这个", "文字没剥干净：%r" % c.chain[1].text)
print("   图片仍在，文字剥成 %r ✓" % c.chain[1].text)

print("\n=== 4. 非链对象不炸（fail-open）===")
for bad in [None, "字符串", 42, object(), MessageChain(chain=None)]:
    try:
        r = S.guard_outgoing(bad)
        ck(r == 0, "非链对象返回了 %r" % r)
    except BaseException as e:
        fail.append("非链对象抛了异常：%r" % e)
print("   None / str / int / object / 空链 都不抛 ✓")

print("\n=== 5. 闸真的装上了 ===")
ck(getattr(AstrMessageEvent.send, "_dsh_sticker_guard", False), "send 没被包")
ck(
    getattr(AstrMessageEvent.send_streaming, "_dsh_sticker_guard", False),
    "send_streaming 没被包",
)
print("   AstrMessageEvent.send / send_streaming 都带 _dsh_sticker_guard 标记 ✓")

print("\n=== 6. 端到端：走过被包的 send，原函数收到的是剥干净的链 ===")
sent = []


async def _recorder(self, message, *a, **k):
    sent.append(message)
    return "sent-ok"


_real = S._ORIG_SEND
S._ORIG_SEND = _recorder
try:
    c = MessageChain(chain=[Plain(LEAK), Plain("再来一张[贴纸:装萌]")])
    r = asyncio.get_event_loop().run_until_complete(
        AstrMessageEvent.send(object.__new__(AstrMessageEvent), c)
    )
    ck(r == "sent-ok", "返回值没透传：%r" % r)
    ck(len(sent) == 1, "原 send 没被调到")
    got = [x.text for x in sent[0].chain if isinstance(x, Plain)]
    print("   平台真正收到的文字：%r" % got)
    ck(got == [CLEAN, "再来一张"], "平台收到的还是带标记的：%r" % got)
finally:
    S._ORIG_SEND = _real

print("\n=== 7. 兜底闸自己坏了也不能吞消息 ===")


def _boom(message):
    raise RuntimeError("兜底闸故意炸")


_real_guard = S.guard_outgoing
S.guard_outgoing = _boom
S._ORIG_SEND = _recorder
try:
    sent.clear()
    c = MessageChain(chain=[Plain(LEAK)])
    r = asyncio.get_event_loop().run_until_complete(
        AstrMessageEvent.send(object.__new__(AstrMessageEvent), c)
    )
    ck(r == "sent-ok", "闸炸了之后消息被吞了：%r" % r)
    ck(len(sent) == 1, "闸炸了之后原 send 没被调到")
    print("   闸抛异常 → 消息照发（只是这次没剥）✓")
finally:
    S.guard_outgoing = _real_guard
    S._ORIG_SEND = _real

print("\n=== 7b. 流式那条路（收的是生成器不是链）===")


async def _src():
    yield MessageChain(chain=[Plain(LEAK)])
    yield MessageChain(chain=[Plain("干净的")])


async def _rec_streaming(self, generator, *a, **k):
    out = []
    async for c in generator:
        out.append([x.text for x in c.chain if isinstance(x, Plain)])
    return out


_real_st = S._ORIG_SEND_STREAMING
S._ORIG_SEND_STREAMING = _rec_streaming
try:
    got = asyncio.new_event_loop().run_until_complete(
        AstrMessageEvent.send_streaming(object.__new__(AstrMessageEvent), _src())
    )
    print("   流式收到的文字：%r" % got)
    ck(got == [[CLEAN], ["干净的"]], "流式那条没剥干净：%r" % got)
finally:
    S._ORIG_SEND_STREAMING = _real_st

print("\n=== 9. 第三条出口：Context.send_message（dsh-merge 走的）===")

MERGE_LEAK = "我没发涩图啊，头像那个别赖我[贴纸:装可怜]"
MERGE_CLEAN = "我没发涩图啊，头像那个别赖我"

from astrbot.core.star import Context                            # noqa: E402

ck(
    getattr(Context.send_message, "_dsh_sticker_guard", False),
    "Context.send_message 没被包",
)
print("   Context.send_message 带 _dsh_sticker_guard 标记 ✓")

ctx_sent = []


async def _rec_ctx(self, session, chain, *a, **k):
    ctx_sent.append((session, chain))
    return True


_real_ctx = S._ORIG_CTX_SEND
S._ORIG_CTX_SEND = _rec_ctx
try:
    c = MessageChain(chain=[Plain(MERGE_LEAK)])
    r = asyncio.new_event_loop().run_until_complete(
        Context.send_message(object.__new__(Context), "aiocqhttp:GroupMessage:476573490", c)
    )
    ck(r is True, "返回值没透传：%r" % r)
    ck(len(ctx_sent) == 1, "原 Context.send_message 没被调到")
    got = [x.text for x in ctx_sent[0][1].chain if isinstance(x, Plain)]
    print("   平台真正收到的文字：%r" % got)
    ck(got == [MERGE_CLEAN], "dsh-merge 这条路还是带标记的：%r" % got)
    ck(ctx_sent[0][0].endswith("476573490"), "session 被改动了")
finally:
    S._ORIG_CTX_SEND = _real_ctx

print("\n=== 9b. Context 闸坏了也不能吞消息 ===")
S.guard_outgoing = _boom
S._ORIG_CTX_SEND = _rec_ctx
try:
    ctx_sent.clear()
    c = MessageChain(chain=[Plain(MERGE_LEAK)])
    r = asyncio.new_event_loop().run_until_complete(
        Context.send_message(object.__new__(Context), "aiocqhttp:GroupMessage:476573490", c)
    )
    ck(r is True, "闸炸了之后 merge 的消息被吞了：%r" % r)
    ck(len(ctx_sent) == 1, "闸炸了之后原函数没被调到")
    print("   闸抛异常 → 消息照发（只是这次没剥）✓")
finally:
    S.guard_outgoing = _real_guard
    S._ORIG_CTX_SEND = _real_ctx

print("\n=== 9c. 干净文本 / 非链对象不误伤 ===")
c = MessageChain(chain=[Plain("今天群里挺热闹")])
ck(S.guard_outgoing(c) == 0, "干净文本被剥了")
ck(c.chain[0].text == "今天群里挺热闹", "干净文本被改了")
for bad in [None, "字符串", 42, MessageChain(chain=None)]:
    try:
        ck(S.guard_outgoing(bad) == 0, "非链对象返回了非 0")
    except BaseException as e:
        fail.append("非链对象抛了异常：%r" % e)
print("   干净文本零改动 / None·str·int·空链 都不抛 ✓")

print("\n=== 8. 重复装载不会套娃 ===")
cur = AstrMessageEvent.send
mod = sys.modules["main"]
import importlib                                            # noqa: E402

importlib.reload(mod)
ck(AstrMessageEvent.send is cur, "reload 后又被包了一层（会出多层日志）")
print("   reload 之后仍是同一个包装 ✓")

print("\n" + "=" * 60)
if fail:
    print("GUARD_TEST_FAILED（%d 项）" % len(fail))
    for f in fail:
        print("  ✗ " + f)
    sys.exit(1)
print("GUARD_TEST_OK")
