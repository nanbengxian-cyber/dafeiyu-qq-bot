# -*- coding: utf-8 -*-
"""dsh-imgctx 阻塞策略回归：历史顺带图不阻塞本轮，当前图/引用图/在说图照等。

背景（生产实测，2026-09-13）：
    被@的**纯文字**回复，中间夹了识图的 中位 25.8s，没有识图的 中位 8.6s。
    也就是群里有人在打字间隙发张图，机器人回一句跟图无关的话要多等 17 秒。
所以把「这张图是不是本轮要回答的东西」当分界线。但不等会带来一个新风险：
发完图再问「这图是啥」，那张图算「历史图」，就会变成没看图直接答。因此有
两个例外照旧等 —— 判不准时一律退回旧行为，慢一点好过瞎答。

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
    def __init__(self, prompt=""):
        self.extra_user_content_parts = []
        self.prompt = prompt


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


def item(key, age=200, current=False, quoted=False):
    """age 默认 200s —— 大于 RECENT_IMAGE_S，才走「历史图不阻塞」那条路。"""
    return {
        "file": key, "url": "", "local": None, "who": "群友A",
        "age": age, "summary": "", "current": current, "quoted": quoted,
    }


def injected(req):
    return "".join(getattr(p, "text", "") or "" for p in req.extra_user_content_parts)


async def case_history_does_not_block():
    """历史图（不是刚发的、当轮没提图）：不等转述，本轮不注入，但后台起任务。"""
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    obj = make_plugin(caption_seconds=0.6)
    req = FakeReq(prompt="没激活的区块不是不计算吗")
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
    """历史图命中缓存：立刻注入，且只把没缓存的丢后台。"""
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    m._cache_put("k1", "缓存里的猫")
    obj = make_plugin(caption_seconds=0.6)
    req = FakeReq(prompt="刚那个谁说的")
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


async def case_asking_about_image_blocks():
    """例外一：这一轮就是在说图 —— 必须等，否则就是没看图直接答。"""
    for prompt in ("这图是啥", "发个截图看看", "这表情包哪来的", "照片里有谁"):
        m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
        obj = make_plugin(caption_seconds=0.4)
        req = FakeReq(prompt=prompt)
        await obj._emit(req, "pid", [item("ask", age=200)])
        assert "转述:ask" in injected(req), "「%s」这类问法必须等到转述：%r" % (
            prompt, injected(req))
        assert not m._inflight, "走阻塞路径时不该再起后台任务"
    return 4


async def case_recent_image_blocks():
    """例外二：图刚发出来（<= RECENT_IMAGE_S）—— 大概率就是眼下在聊的那张。"""
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    obj = make_plugin(caption_seconds=0.4)
    req = FakeReq(prompt="嗯")
    await obj._emit(req, "pid", [item("fresh", age=m.RECENT_IMAGE_S)])
    assert "转述:fresh" in injected(req), "刚发的图应等到转述"
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    obj = make_plugin(caption_seconds=0.4)
    req = FakeReq(prompt="嗯")
    await obj._emit(req, "pid", [item("stale", age=m.RECENT_IMAGE_S + 1)])
    assert injected(req) == "", "过了新鲜期的图不该再阻塞本轮"
    return m.RECENT_IMAGE_S


async def case_unknown_prompt_is_conservative():
    """拿不到当轮原文时保守处理：按「在说图」算，退回阻塞。"""
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    obj = make_plugin(caption_seconds=0.4)
    req = FakeReq()
    del req.prompt
    await obj._emit(req, "pid", [item("unk", age=200)])
    assert "转述:unk" in injected(req), "拿不到原文时应保守等图"
    return True


async def case_current_and_quoted_still_wait():
    """当前消息自带的图 / 用户精确引用的图：照旧等。"""
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    obj = make_plugin(caption_seconds=0.4)
    req = FakeReq(prompt="")
    t0 = time.time()
    await obj._emit(req, "pid", [item("cur", age=0, current=True)], rescue=True)
    cur = time.time() - t0
    assert cur >= 0.35, "当前消息的图必须阻塞等待，实测只花了 %.2fs" % cur
    assert "转述:cur" in injected(req)
    m._caption_cache.clear(); m._cache_order.clear(); m._inflight.clear()
    obj = make_plugin(caption_seconds=0.4)
    req = FakeReq(prompt="")
    t0 = time.time()
    await obj._emit(req, "pid", [item("q", age=0, quoted=True)], quote_rescue=True)
    q = time.time() - t0
    assert q >= 0.35, "引用图必须阻塞等待，实测只花了 %.2fs" % q
    assert "转述:q" in injected(req)
    return cur, q


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
    n_ask = await case_asking_about_image_blocks()
    fresh = await case_recent_image_blocks()
    await case_unknown_prompt_is_conservative()
    cur, q = await case_current_and_quoted_still_wait()
    cap = await case_warm_dedup_and_cap()
    print("IMGCTX_BLOCKING_TEST_OK")
    print("  历史图（远图·无缓存）耗时 %.2fs（不阻塞）" % h)
    print("  历史图（命中缓存）  耗时 %.2fs（不阻塞）" % c)
    print("  当前消息的图        %.2fs（阻塞）｜精确引用的图 %.2fs（阻塞）" % (cur, q))
    print("  例外：在说图的 %d 种问法 / 图在 %ds 内 / 拿不到原文 → 全部阻塞"
          % (n_ask, fresh))
    print("  后台预转述并发上限 = %d" % cap)


if __name__ == "__main__":
    asyncio.run(main())
