# -*- coding: utf-8 -*-
"""dsh-imgctx 阻塞策略回归：历史顺带图不阻塞本轮，当前图/引用图照等。

背景（生产实测，2026-09-13）：
    被@的**纯文字**回复，中间夹了识图的 中位 25.8s，没有识图的 中位 8.6s。
    也就是群里有人在打字间隙发张图，机器人回一句跟图无关的话要多等 17 秒。
所以把「这张图是不是本轮要回答的东西」当分界线。

跑法（容器内 py3.12）：
    docker exec astrbot python3 /tmp/ic/test_blocking.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DSH_IMGCTX", "1")

import main as m  # noqa: E402

# 静音：dsh-imgctx 会 `from astrbot.core import logger`，而 import astrbot.core
# 会顺带初始化 AstrBot 的日志配置，把文件 sink 挂到**生产日志**
# /AstrBot/data/logs/astrbot.log 上。不清掉的话，跑一次测试就往生产日志里
# 掺几行（实测 2026-09-13 01:09 混进 2 行 [ic.main:666]）。
# 这是独立进程，remove() 只影响自己，动不到正在跑的 AstrBot。
try:
    from loguru import logger as _loguru_logger

    _loguru_logger.remove()
except Exception:
    pass


class FakeReq:
    def __init__(self):
        self.extra_user_content_parts = []


def make_plugin(caption_seconds=0.0, record=None):
    """绕开 __init__ 造实例：只需要被副作用替换掉 _caption 的几个方法。"""
    obj = object.__new__(m.Main)

    async def fake_caption(provider_id, items):
        if record is not None:
            record.append([it["file"] for it in items])
        if caption_seconds:
            await asyncio.sleep(caption_seconds)
        for it in items:
            it["caption"] = "转述:" + it["file"]
            m._cache_put(it["file"], it["caption"])
        return items

    obj._caption = fake_caption
    return obj


def item(key, current=False, quoted=False):
    return {
        "file": key, "url": "", "local": None, "who": "群友A",
        "age": 30, "summary": "", "current": current, "quoted": quoted,
    }


def injected(req):
    return "".join(getattr(p, "text", "") or "" for p in req.extra_user_content_parts)


async def case_history_does_not_block():
    """历史图：不等转述，本轮不注入，但后台起任务；转述完进缓存。"""
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    seen = []
    obj = make_plugin(caption_seconds=0.6, record=seen)
    req = FakeReq()
    items = [item("k1"), item("k2")]
    t0 = time.time()
    await obj._emit(req, "pid", items)
    cost = time.time() - t0
    assert cost < 0.25, "历史图不该等转述，实测等了 %.2fs" % cost
    assert injected(req) == "", "本轮没有可用转述，不该注入任何东西"
    assert len(m._inflight) == 2, "两张图都应转入后台，实际 %d" % len(m._inflight)
    await asyncio.sleep(1.0)
    assert "k1" in m._caption_cache and "k2" in m._caption_cache, "后台转述没写进缓存"
    assert not m._inflight, "后台任务结束后 _inflight 应清空，实际 %r" % list(m._inflight)
    return cost


async def case_history_uses_cache():
    """历史图命中缓存：立刻注入，且不起后台任务。"""
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    m._cache_put("k1", "缓存里的猫")
    obj = make_plugin(caption_seconds=0.6)
    req = FakeReq()
    t0 = time.time()
    await obj._emit(req, "pid", [item("k1"), item("k2")])
    cost = time.time() - t0
    assert cost < 0.25, "命中缓存应即时，实测 %.2fs" % cost
    text = injected(req)
    assert "缓存里的猫" in text, "命中缓存的转述必须注入"
    assert "k2" not in text, "没缓存的图不该出现在本轮注入里"
    assert len(m._inflight) == 1, "只有没缓存的那张该进后台"
    await asyncio.sleep(1.0)
    return cost


async def case_current_image_still_waits():
    """当前消息自带的图：必须等到转述完 —— 回答就靠它。"""
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    obj = make_plugin(caption_seconds=0.4)
    req = FakeReq()
    t0 = time.time()
    await obj._emit(req, "pid", [item("cur", current=True)], rescue=True)
    cost = time.time() - t0
    assert cost >= 0.35, "当前消息的图必须阻塞等待，实测只花了 %.2fs" % cost
    assert "转述:cur" in injected(req), "当前消息的图必须注入"
    assert not m._inflight, "当前图是同步转述的，不该再有后台任务"
    return cost


async def case_quoted_image_still_waits():
    """用户精确引用的图：同样必须等。"""
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    obj = make_plugin(caption_seconds=0.4)
    req = FakeReq()
    t0 = time.time()
    await obj._emit(req, "pid", [item("q", quoted=True)], quote_rescue=True)
    cost = time.time() - t0
    assert cost >= 0.35, "引用图必须阻塞等待，实测只花了 %.2fs" % cost
    assert "转述:q" in injected(req)
    return cost


async def case_warm_dedup_and_cap():
    """同一张图不并行转述两次；并发不超过 WARM_MAX。"""
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    obj = make_plugin(caption_seconds=0.5)
    n = obj._warm("pid", [item("d1"), item("d1"), item("d2"), item("d3")])
    assert n == 3, "重复的 d1 只该起一个任务，实际起了 %d 个" % n
    again = obj._warm("pid", [item("d1"), item("d2")])
    assert again == 0, "已在跑的图不该重复起任务，实际又起了 %d 个" % again
    await asyncio.sleep(1.0)
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    obj2 = make_plugin(caption_seconds=0.5)
    many = obj2._warm("pid", [item("m%d" % i) for i in range(10)])
    assert many == m.WARM_MAX, "并发上限应为 %d，实际起了 %d" % (m.WARM_MAX, many)
    await asyncio.sleep(1.0)
    return many


async def main():
    h = await case_history_does_not_block()
    c = await case_history_uses_cache()
    cur = await case_current_image_still_waits()
    q = await case_quoted_image_still_waits()
    cap = await case_warm_dedup_and_cap()
    print("IMGCTX_BLOCKING_TEST_OK")
    print("  历史图（无缓存）耗时 %.2fs（不阻塞）" % h)
    print("  历史图（命中缓存）耗时 %.2fs（不阻塞）" % c)
    print("  当前消息的图 耗时 %.2fs（阻塞，回答靠它）" % cur)
    print("  精确引用的图 耗时 %.2fs（阻塞，回答靠它）" % q)
    print("  后台预转述并发上限 = %d" % cap)


if __name__ == "__main__":
    asyncio.run(main())
