# dsh-vischain 慢档降权（SLOW_TTL）单测 —— 容器内 py3.12 运行
#
# 背景：2026-09-12 回查日志发现平均每张图白花 16.4 秒在「超时→429→才轮到
# 真正能用的档」这条固定链条上。本测试盯住四件事：
#   ① 白花过时间的档，下一轮排到最后（但仍在候选里，fail-open）；
#   ② 秒失败的档（配置错）不受这条规则影响；
#   ③ 成功一次就撤销降权；
#   ④ 所有档都被降权时，顺序可以变但一个都不能丢。
#
# 运行：sudo docker cp plugins/dsh-vischain astrbot:/tmp/vs && \
#       sudo docker exec astrbot python3 /tmp/vs/test_slow_demote.py
import asyncio
import os
import sys

os.environ["DSH_VIS_CONSEC_BAN"] = "3"
os.environ["DSH_VIS_RETRY"] = "0"
os.environ["DSH_VIS_DEAD_TTL"] = "0"      # 本测试只验 SLOW_TTL，隔离 DEAD_TTL
os.environ["DSH_VIS_SLOW_TTL"] = "180"
os.environ["DSH_VIS_CHAIN"] = "vision-scnet,zhipu-vision"
os.environ["DSH_VIS_MODEL_FALLBACKS"] = ""
os.environ["DSH_VIS_TIMEOUT"] = "5"

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import main as vis  # noqa: E402

# 这个测试是在 astrbot 容器里跑的，而 vis.logger 是 astrbot 的 loguru ——
# 它挂着一个**生产日志文件**的 sink。不掐掉的话，测试每跑一次都会往
# /AstrBot/data/logs/astrbot.log 里灌一批假日志，把群动态记分卡和
# vischain_waste.py 的统计口径污染掉（2026-09-12 第一版就踩了这个坑）。
vis.logger = type("_Quiet", (), {
    "info": staticmethod(lambda *a, **k: None),
    "warning": staticmethod(lambda *a, **k: None),
    "error": staticmethod(lambda *a, **k: None),
    "debug": staticmethod(lambda *a, **k: None),
})()

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print("%-46s %s %s" % (name, "PASS" if cond else "FAIL", detail if not cond else ""))


class MockProv:
    """fail 为 None=成功；否则是抛出的异常（或异常字符串）。"""

    def __init__(self, pid, fail=None):
        self.provider_config = {"id": pid, "model": "m", "modalities": ["text", "image"]}
        self._fail = fail
        self.calls = 0

    def get_model(self):
        return "m"

    async def text_chat(self, *args, **kwargs):
        self.calls += 1
        if self._fail is None:
            return "ok"
        if isinstance(self._fail, BaseException):
            raise self._fail
        raise RuntimeError(self._fail)


def order_ids(chain):
    return [lk.provider_config["id"] for lk in chain._order()]


async def main():
    check("SLOW_TTL 默认 180s", vis.SLOW_TTL == 180.0, repr(vis.SLOW_TTL))

    # ---------- ① 超时被降权：下一轮排到最后，但仍会被走到 ----------
    vis._slow.clear(), vis._dead.clear(), vis._banned.clear(), vis._consec.clear()
    slow = MockProv("vision-scnet", fail=asyncio.TimeoutError())
    good = MockProv("zhipu-vision")
    chain = vis.ChainProvider([slow, good], {})
    check("首轮顺序=声明顺序", order_ids(chain) == ["vision-scnet", "zhipu-vision"])

    await chain.text_chat("x", image_urls=["/tmp/vsit.png"])
    check("超时档被记进 _slow", "vision-scnet" in vis._slow)
    check("降权后它排到最后", order_ids(chain) == ["zhipu-vision", "vision-scnet"])

    calls_before = slow.calls
    await chain.text_chat("x", image_urls=["/tmp/vsit.png"])
    check("第二轮不再先撞它（省掉那 10s）", slow.calls == calls_before,
          "slow.calls=%d" % slow.calls)
    check("健康档顶上来", good.calls == 2, "good.calls=%d" % good.calls)

    # ---------- ② 429（限流）同样算白花 ----------
    vis._slow.clear(), vis._banned.clear(), vis._consec.clear()
    limited = MockProv("vision-scnet", fail="Error code: 429 - rate limit")
    chain2 = vis.ChainProvider([limited, MockProv("zhipu-vision")], {})
    await chain2.text_chat("x", image_urls=["/tmp/vsit.png"])
    check("429 也降权", "vision-scnet" in vis._slow)

    # ---------- ③ 秒失败（非白花）不降权 ----------
    vis._slow.clear(), vis._banned.clear(), vis._consec.clear()
    fast = MockProv("vision-scnet", fail="model_not_found: no available channel")
    chain3 = vis.ChainProvider([fast, MockProv("zhipu-vision")], {})
    await chain3.text_chat("x", image_urls=["/tmp/vsit.png"])
    check("秒失败不记 _slow", "vision-scnet" not in vis._slow)

    # ---------- ④ 降权会过期；被降权期间不撞它，所以它不会自己撤销 ----------
    # 这一条是**有意**的行为：降权的意思就是「这段时间别碰它」。它只在
    # ①SLOW_TTL 到期 或 ②前面的档也挂了、不得不走到它 这两种情况下才会被
    # 重新调用，成功一次才撤销。否则「降权」就等于「每轮还是先撞一次」。
    vis._slow.clear(), vis._banned.clear(), vis._consec.clear()
    import time as _t
    flaky = MockProv("vision-scnet", fail=asyncio.TimeoutError())
    good2 = MockProv("zhipu-vision")
    chain4 = vis.ChainProvider([flaky, good2], {})
    await chain4.text_chat("x", image_urls=["/tmp/vsit.png"])
    check("先降权", "vision-scnet" in vis._slow)

    flaky._fail = None                       # 它恢复了，但还在降权期内
    flaky_calls = flaky.calls
    await chain4.text_chat("x", image_urls=["/tmp/vsit.png"])
    check("降权期内仍然不去撞它", flaky.calls == flaky_calls)
    check("降权期内 _slow 保持（等窗口过）", "vision-scnet" in vis._slow)

    vis._slow["vision-scnet"] = _t.time() - vis.SLOW_TTL - 1   # 窗口过期
    check("窗口过期后回到声明顺序",
          order_ids(chain4) == ["vision-scnet", "zhipu-vision"])
    check("窗口过期后 _slow 记录被清掉", "vision-scnet" not in vis._slow)

    # 前面的档也挂了 → 不得不走到被降权的档 → 它成功了就撤销
    vis._slow.clear(), vis._banned.clear(), vis._consec.clear()
    ok_again = MockProv("vision-scnet")
    vis._slow["vision-scnet"] = _t.time()          # 先手动打上降权
    dying = MockProv("zhipu-vision", fail="model_not_found")
    chain4b = vis.ChainProvider([ok_again, dying], {})
    r = await chain4b.text_chat("x", image_urls=["/tmp/vsit.png"])
    check("兜底走到它时能救场", r == "ok")
    check("救场成功后撤销降权", "vision-scnet" not in vis._slow)

    # ---------- ⑤ fail-open：全被降权时顺序可换、档不能丢 ----------
    vis._slow.clear(), vis._banned.clear(), vis._consec.clear()
    a = MockProv("vision-scnet", fail=asyncio.TimeoutError())
    b = MockProv("zhipu-vision", fail=asyncio.TimeoutError())
    chain5 = vis.ChainProvider([a, b], {})
    try:
        await chain5.text_chat("x", image_urls=["/tmp/vsit.png"])
    except Exception:                        # 全挂本来就该往外抛
        pass
    ids = order_ids(chain5)
    check("两档都在候选里（没被丢掉）", sorted(ids) == ["vision-scnet", "zhipu-vision"], str(ids))

    # ---------- ⑥ 超时线撞满但报的是别的错，也算白花 ----------
    vis._slow.clear(), vis._banned.clear(), vis._consec.clear()
    check("_expensive 认异常类型（空消息的 TimeoutError）",
          vis._expensive(asyncio.TimeoutError(), 0.0) is True)
    check("_expensive 认 429 文本",
          vis._expensive(RuntimeError("HTTP 429 too many requests"), 0.1) is True)
    check("_expensive 认撞满超时线",
          vis._expensive(RuntimeError("some weird error"), vis.ATTEMPT_TIMEOUT) is True)
    check("_expensive 不冤枉秒失败",
          vis._expensive(RuntimeError("model_not_found"), 0.05) is False)

    # ---------- ⑦ 开关：SLOW_TTL=0 退回老行为 ----------
    vis._slow.clear(), vis._banned.clear(), vis._consec.clear()
    old_ttl = vis.SLOW_TTL
    vis.SLOW_TTL = 0.0
    c = MockProv("vision-scnet", fail=asyncio.TimeoutError())
    chain6 = vis.ChainProvider([c, MockProv("zhipu-vision")], {})
    await chain6.text_chat("x", image_urls=["/tmp/vsit.png"])
    check("SLOW_TTL=0 时完全不记", "vision-scnet" not in vis._slow)
    check("SLOW_TTL=0 时顺序不变",
          order_ids(chain6) == ["vision-scnet", "zhipu-vision"])
    vis.SLOW_TTL = old_ttl

    bad = [n for n, ok, _ in CHECKS if not ok]
    print("\n%d/%d 通过" % (len(CHECKS) - len(bad), len(CHECKS)))
    if bad:
        print("失败：" + "、".join(bad))
    return 1 if bad else 0


raise SystemExit(asyncio.run(main()))
