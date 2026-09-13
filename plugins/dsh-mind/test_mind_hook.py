# -*- coding: utf-8 -*-
"""dsh-mind 钩子级集成测试：跑**真实的 observe 处理器**，不碰真群、不发消息。

必须在 astrbot 容器里跑（要 import astrbot；宿主机只有 py3.8 且没有框架）：

    sudo docker cp dsh-mind astrbot:/tmp/dsh-mind-check
    sudo docker exec astrbot sh -c 'cd /tmp/dsh-mind-check && touch __init__.py && python3 test_mind_hook.py'

框架不在时打印 SKIP 并以 0 退出，不假装通过。

★ 为什么这个测试必须存在（纯函数测试抓不到的东西）★
  · 观测器绝不能改 `req` —— 一旦它塞了东西，P0 的「零行为变更」就是假的。
  · `priority=-1` 是**观测正确性的前提**：排在注入器之前看到的是半成品，
    量出来的块数和字数全是错的，而且不会报错、只会静默偏小。
  · 非生效群 / 合成事件不入库，否则统计口径被稀释。
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# ★ 镜像里的群号/群主是脱敏占位符，测试必须自己给真值 ——
#   生产靠 imagegen.env 提供（cp 到生产后忘了配 env 会导致「插件装上了但一行都不记」）。
os.environ["DSH_MIND_GROUPS"] = "100000001"
os.environ["DSH_MIND_OWNER"] = "2774000001"
os.environ["DSH_MIND_MODE"] = "observe"
_TMP = tempfile.mkdtemp(prefix="dsh-mind-hook-")
os.environ["DSH_MIND_DB"] = os.path.join(_TMP, "dsh_mind.db")

ROOT = str(Path(__file__).resolve().parent)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name, detail)


try:
    import astrbot  # noqa: F401
except ImportError:
    print("SKIP: 没有 astrbot（本测试要在 astrbot 容器里跑）")
    sys.exit(0)

spec = importlib.util.spec_from_file_location(
    "dsh_mind_pkg", os.path.join(ROOT, "__init__.py"),
    submodule_search_locations=[ROOT])
pkg = importlib.util.module_from_spec(spec)
sys.modules["dsh_mind_pkg"] = pkg
spec.loader.exec_module(pkg)
main = importlib.import_module("dsh_mind_pkg.main")


class Part:
    def __init__(self, text):
        self.text = text


class Req:
    def __init__(self):
        self.extra_user_content_parts = [
            Part("<emotion_state>%s</emotion_state>" % ("e" * 140)),
            Part("<scene>%s</scene>" % ("s" * 1200)),
            Part("<agency_state>%s</agency_state>" % ("a" * 600)),
            Part("<spine>%s</spine>" % ("p" * 300)),
        ]
        self.contexts = [{"role": "user", "content": "x" * 5000},
                         {"role": "assistant", "content": "y" * 2000}]


class Ev:
    """最小的假事件。字段与 AstrBot 的 AstrMessageEvent 对齐（只取用到的）。"""

    def __init__(self, gid, uid, text, at=False, ex=None):
        self._gid, self._uid, self._text = gid, uid, text
        self.is_at_or_wake_command = at
        self._extra = ex or {}
        self.message_obj = None

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._uid

    def get_message_str(self):
        return self._text

    def get_extra(self, key):
        return self._extra.get(key)

    def plain_result(self, text):
        return type("R", (), {"text": text})()


class BoomEv(Ev):
    """适配器的 get_extra 抛异常 —— 观测层必须退回中性并**继续观测**。"""

    def get_extra(self, key):
        raise RuntimeError("adapter broken")


class CrashEv(Ev):
    """连 gid 都拿不到 —— 观测层必须吞掉，不许把这一轮回复带崩。"""

    def get_group_id(self):
        raise RuntimeError("no group id")


async def run() -> None:
    inst = main.Main.__new__(main.Main)          # 不跑 __init__，自己装 store
    inst.context = None
    inst.reader = main.MindReader()
    inst.store = main.MindStore(Path(os.environ["DSH_MIND_DB"]))

    # ---- 1. 正常轮：必须一动不动地还回同一个 req
    req = Req()
    before = list(req.extra_user_content_parts)
    await inst.observe(Ev("100000001", "2774000001", "@大肥鱼 在吗", at=True), req)
    check("不注入：extra_user_content_parts 原样", req.extra_user_content_parts == before)
    check("不注入：块数没变", len(req.extra_user_content_parts) == 4)
    check("不注入：没给 req 加字段", set(vars(req)) == {"extra_user_content_parts", "contexts"}, vars(req))

    # ---- 2. 疲劳走 event extra（精确档），而不是粗代理
    exf = {"dsh_fatigue_level": 3, "dsh_fatigue_topic": "玩梗复读"}
    await inst.observe(Ev("100000001", "1115276783", "又是这个", ex=exf), Req())

    # ---- 3. 非生效群：一行日志都不该记，也不该入库
    rows_before = len(inst.store.recent(time.time() - 3600))
    await inst.observe(Ev("999999", "1115276783", "别的群"), Req())
    check("非生效群：不入库",
          len(inst.store.recent(time.time() - 3600)) == rows_before)

    # ---- 4. 合成事件（主动开口/探头）：单独计数，不进正常口径
    await inst.observe(Ev("100000001", "1", "合成", ex={"dsh_initiate": True}), Req())
    check("合成事件：不入库",
          len(inst.store.recent(time.time() - 3600)) == rows_before)

    # ---- 5. 适配器的 get_extra 炸了：退回中性但**继续观测**（fail-open）
    await inst.observe(BoomEv("100000001", "1115276783", "坏适配器"), Req())

    # ---- 6. 连 gid 都拿不到：吞掉异常、不入库、计数到 fail，不许把回复带崩
    fail_before = main._stat["fail"]
    try:
        await inst.observe(CrashEv("100000001", "1115276783", "彻底坏"), Req())
    except BaseException as exc:
        check("坏事件：不许把异常抛出去", False, exc)
    check("坏事件：记进 fail 计数", main._stat["fail"] == fail_before + 1, main._stat)

    rows = inst.store.recent(time.time() - 3600)
    check("入库：存 3 条正常/半正常轮（坏到没 gid 的那条不入）", len(rows) == 3, len(rows))
    check("入库：全是生效群", all(r["gid"] == "100000001" for r in rows))

    row = [r for r in rows if r["n_conflicts"]][0]
    # 4 块的字节数：<emotion_state>171 + <scene>1215 + <agency_state>629 + <spine>315
    check("入库：块数/字数与 req 一致",
          row["n_blocks"] == 4 and row["inject_chars"] == 2330,
          (row["n_blocks"], row["inject_chars"]))
    check("入库：历史字数算到", row["ctx_chars"] == 7000, row["ctx_chars"])
    check("入库：记到疲劳 3 档带来的冲突", row["n_conflicts"] >= 1, row["conflicts"])
    # 镜像用的脱敏群号在生产状态库里没有群内记录，所以只有**全局**状态
    # （今日馋、能力事件）会亮 —— 这不是 bug，是「读不到就中性」的正常表现。
    check("入库：岛数如实（脱敏群号只有全局状态）", row["n_islands"] >= 1, row["n_islands"])
    check("入库：草稿短于现有注入",
          row["draft_len"] < row["inject_chars"], (row["draft_len"], row["inject_chars"]))

    stats = dict((tag, cnt) for tag, cnt, _total, _avg in inst.store.block_stats(time.time() - 3600))
    check("块统计：四种标签都记到",
          stats.get("scene") == 3 and stats.get("agency_state") == 3, stats)

    # ---- 6. 不存群聊原文：库里只有标签和字数
    raw = Path(os.environ["DSH_MIND_DB"]).read_bytes()
    check("隐私：库文件里没有注入块原文", b"ssssssssss" not in raw and b"eeeeeeeeee" not in raw)

    # ---- 7. /心智状态 对非管理员闭嘴
    out = []
    async for result in inst.cmd_status(Ev("100000001", "999999", "/心智状态")):
        out.append(result)
    text = "".join(getattr(r, "text", "") or "" for r in out)
    check("权限：非管理员看不到口径", "岛数" not in text and "注入" not in text, text)


asyncio.run(run())

# ---- 8. 排序不变量：priority 必须是 -1（排在所有注入器之后）
# 这条不是洁癖：排在注入器之前看到的是**半成品**，量出来的块数和字数会静默偏小，
# 而且不会报任何错。P0 的全部结论都建立在这个顺序上。
try:
    from astrbot.core.star.star_handler import star_handlers_registry
    prios = {}
    for handler in star_handlers_registry:
        # 注意 handler_module_path 是**字符串**（'dsh_mind_pkg.main'），不是模块对象
        module = str(getattr(handler, "handler_module_path", "") or "")
        if "dsh_mind" in module:
            prios[str(getattr(handler, "handler_name", ""))] = \
                getattr(handler, "extras_configs", {}).get("priority")
    check("排序：observe 的 priority 是 -1（最后跑）", prios.get("observe") == -1, prios)
    check("排序：注册了两个处理器（observe + 心智状态）", len(prios) == 2, prios)
except ImportError:
    print("WARN: 取不到 star_handlers_registry，跳过排序检查")

print("MIND_HOOK_TEST_OK passed=%d failed=%d" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
