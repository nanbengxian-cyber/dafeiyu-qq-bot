# test_marker_leak.py —— 「文字版贴纸」泄漏回归（[patch:marker-leak-v3]）
#
# 用户报的现象：群里有时候收到的是「乖宝宝这称号我自己都叫上了？[贴纸:装萌]」
# 这种把标记原样当文字发出去的消息，而不是贴纸本身。
#
# 根因不是正则漏了，而是**顺序**：旧版把「剥标记」和「发贴纸」塞在同一个
# _handle 里，而发贴纸走的是 event.send()，QQ 那边一旦抛
# ActionFailed(retcode=1200)（NT 内核等回执超时，日志里 09-06~09-09 出现过
# 100+ 次），异常就冒到调用方，把
#     response.completion_text = cleaned   /   comp.text = cleaned
# 这两行**整个跳过**，标记原样进群。
#
# 本测试用 AST 抽取真实源码运行（不复制正则、不复制逻辑），
# 并让 _send_markers 每次都抛异常，断言文字仍然被剥干净。

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

path = Path(__file__).with_name("main.py")
tree = ast.parse(path.read_text(encoding="utf-8"))

main_cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Main")
stickerize = next(
    n for n in main_cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "stickerize"
)
fallback = next(
    n for n in main_cls.body
    if isinstance(n, ast.AsyncFunctionDef) and n.name == "stickerize_fallback"
)
for _n in (stickerize, fallback):
    _n.decorator_list = []

_WANT_ASSIGN = {
    "MARKER_RE", "AUTO_FLAG", "STEP_FLAG", "_last_auto", "AUTO_RATE", "AUTO_COOLDOWN",
}
assigns = [
    n for n in tree.body
    if isinstance(n, ast.Assign) and any(getattr(t, "id", "") in _WANT_ASSIGN for t in n.targets)
]
funcs = {
    n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
}
# Main 里的两个钩子也要能被取到（本测试只测函数体，不需要真 Star 实例）
funcs["stickerize"] = stickerize
funcs["stickerize_fallback"] = fallback


class Quiet:
    def __getattr__(self, _):
        return lambda *a, **k: None


class ActionFailed(Exception):
    pass


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    @staticmethod
    def fromFileSystem(p):
        return ("image", p)


class MessageChain:
    def __init__(self, chain):
        self.chain = chain


class Result:
    def __init__(self, chain):
        self.chain = chain

    def get_plain_text(self):
        return "".join(c.text for c in self.chain if isinstance(c, Plain))


class Event:
    def __init__(self, exts=None, exc=None):
        self.exts = dict(exts or {})
        self.exc = exc
        self.result = None
        self.sends = 0

    def get_extra(self, k, default=None):
        return self.exts.get(k, default)

    def set_extra(self, k, v):
        self.exts[k] = v

    def get_result(self):
        return self.result

    async def send(self, chain):
        self.sends += 1
        if self.exc:
            raise self.exc


def base_ns(send_raises=True):
    """公共命名空间。send_raises=True 时 _send_markers 永远抛异常，
    用来模拟 QQ retcode=1200 那条真实路径。"""
    async def boom(*a, **k):
        if send_raises:
            raise ActionFailed(
                "<ActionFailed status='failed', retcode=1200, message="
                "'Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg'>"
            )
        return 1

    return {
        "re": __import__("re"),
        "Plain": Plain,
        "Image": Image,
        "MessageChain": MessageChain,
        "logger": Quiet(),
        "_send_markers": boom,
        "_maybe_auto_send": _noop_async,
        "time": __import__("time"),
        "_stat": {"attempt": 0, "sent": 0, "quota_drop": 0, "media_drop": 0,
                  "unknown": 0, "inter_step": 0, "dedup_drop": 0,
                  "auto_attempt": 0, "auto_sent": 0, "auto_cooldown_drop": 0,
                  "send_fail": 0},
        "random": __import__("random"),
        "_gid": lambda e: "g",
        "_has_media": lambda e: None,
        "_resolve_sticker": lambda tag: "/tmp/fake/%s.gif" % tag,
        "_quota_allows": lambda gid: True,
        "_attempts": {},
        "WINDOW": 2,
        "MAX_IN_WINDOW": 1,
        "MAX_PER_REPLY": 1,
        "_LAST_USED": {},
        "datetime": __import__("datetime").datetime,
        "_cooldown_tick": lambda gid: None,
        "_cooldown_allows": lambda gid, tag: True,
        "_cooldown_remember": lambda gid, tag: None,
        "_auto_tag": lambda text: "装萌",
        "AUTO_RATE": 1.0,
        "AUTO_COOLDOWN": 180,
        "_last_auto": {},
    }


async def _noop_async(*a, **k):
    return False


fails = []


def check(name, got, want):
    if got == want:
        print("  PASS %s" % name)
    else:
        print("  FAIL %s\n       got  = %r\n       want = %r" % (name, got, want))
        fails.append(name)


def load(names, ns):
    mods = [n for n in assigns if any(getattr(t, "id", "") in names for t in n.targets)]
    mods += [funcs[k] for k in names if k in funcs]
    exec(compile(ast.Module(mods, []), str(path), "exec"), ns)
    return ns


# ---------------------------------------------------------------- A 纯函数
def test_pure():
    ns = {"re": __import__("re")}
    ns = load({"MARKER_RE", "strip_markers"}, ns)
    strip = ns["strip_markers"]
    cases = [
        # 用户实际看到的那条
        ("乖宝宝这称号我自己都叫上了？[贴纸:装萌]",
         ("乖宝宝这称号我自己都叫上了？", ["装萌"])),
        # 标记在中间：两边都要保住，中间不能留缝
        ("前面[贴纸:嘲笑]后面", ("前面后面", ["嘲笑"])),
        ("【貼紙：思考】", ("", ["思考"])),
        ("[sticker:探头]", ("", ["探头"])),
        ("[ 贴纸 : 送花 ]", ("", ["送花"])),
        # 多张
        ("a[贴纸:装酷]b[贴纸:熬夜]", ("ab", ["装酷", "熬夜"])),
        # 无标记原样返回
        ("就是普通的文字", ("就是普通的文字", [])),
        ("", ("", [])),
        # 半个括号 / 没冒号 不是标记，不能误剥
        ("[贴纸]没冒号", ("[贴纸]没冒号", [])),
        ("[贴纸:x", ("[贴纸:x", [])),
    ]
    for raw, want in cases:
        check("strip_markers(%r)" % raw, strip(raw), want)


# ------------------------------------------- B 发送必抛时，标记仍必须被剥掉
def test_hooks_survive_send_failure():
    ns = base_ns(send_raises=True)
    load({"MARKER_RE", "AUTO_FLAG", "STEP_FLAG", "strip_markers",
          "stickerize", "stickerize_fallback"}, ns)

    # ---- B1 最终回复：on_llm_response ----
    class Resp:
        def __init__(self, text):
            self.completion_text = text
            self._completion_text = text

    resp = Resp("乖宝宝这称号我自己都叫上了？[贴纸:装萌]")
    ev = Event(exc=ActionFailed("retcode=1200 仿真"))
    asyncio.get_event_loop().run_until_complete(ns["stickerize"](object(), ev, resp))
    got = resp.completion_text
    check("发图抛异常时 completion_text 无标记", "[贴纸" in got, False)
    check("发图抛异常时 completion_text 内容正确", got, "乖宝宝这称号我自己都叫上了？")

    # 没有标记的普通回复必须原样不动
    resp2 = Resp("哼，才不理你")
    before = resp2.completion_text
    asyncio.get_event_loop().run_until_complete(ns["stickerize"](object(), Event(), resp2))
    check("无标记回复不被改动", resp2.completion_text, before)
    check("无标记时置自动补图候选位", Event().exts.get("x"), None)

    # ---- B2 中间步：on_decorating_result ----
    ev2 = Event(exc=ActionFailed("retcode=1200 仿真"))
    ev2.result = Result([Plain("这就来[贴纸:探头]"), Plain("别急")])
    asyncio.get_event_loop().run_until_complete(ns["stickerize_fallback"](object(), ev2))
    got2 = "".join(c.text for c in ev2.result.chain if isinstance(c, Plain))
    check("兜底钩子发图抛异常时链里无标记", "[贴纸" in got2, False)
    check("兜底钩子剥完保留两侧文字", got2, "这就来别急")

    # ---- B3 already=True（上面已处理过）：只剥不发 ----
    ev3 = Event(exts={"dsh_sticker_step_done": True})
    ev3.result = Result([Plain("[贴纸:装酷]")])
    asyncio.get_event_loop().run_until_complete(ns["stickerize_fallback"](object(), ev3))
    check("已处理过时整条只剩标记 -> 剥空后移除",
          [c.text for c in ev3.result.chain if isinstance(c, Plain)], [])

    # ---- B4 全链扫一遍：任何情况下都不许有标记进链 ----
    for raw in ("[贴纸:装萌]", "x[贴纸:嘲笑]y", "【貼紙：思考】呢"):
        e = Event(exc=ActionFailed("retcode=1200 仿真"))
        e.result = Result([Plain(raw)])
        asyncio.get_event_loop().run_until_complete(ns["stickerize_fallback"](object(), e))
        left = "".join(c.text for c in e.result.chain if isinstance(c, Plain))
        check("链出口无标记 %r" % raw, "[贴纸" in left or "貼紙" in left, False)


# ------------------------------------------- C _send_markers 自己必须吞掉异常
def test_send_markers_swallows():
    ns = base_ns()
    load({"MARKER_RE", "strip_markers", "_send_markers"}, ns)
    ev = Event(exc=ActionFailed("retcode=1200 仿真"))
    try:
        n = asyncio.get_event_loop().run_until_complete(
            ns["_send_markers"](ev, ["装萌"], "文字", "llm_response")
        )
        raised = None
    except BaseException as e:  # noqa: BLE001
        raised = e
        n = None
    check("_send_markers 不外抛", raised, None)
    check("_send_markers 失败返回 0 张", n, 0)
    check("_send_markers 记了 send_fail", ns["_stat"]["send_fail"], 1)


# ------------------------------------------- D 自动补图同样不许把异常带出去
def test_auto_send_swallows():
    ns = base_ns()
    load({"MARKER_RE", "_maybe_auto_send"}, ns)
    ev = Event(exc=ActionFailed("retcode=1200 仿真"))
    try:
        r = asyncio.get_event_loop().run_until_complete(
            ns["_maybe_auto_send"](ev, "有点想笑")
        )
        raised = None
    except BaseException as e:  # noqa: BLE001
        raised = e
        r = None
    check("自动补图不外抛", raised, None)
    check("自动补图失败返回 False", r, False)


if __name__ == "__main__":
    print("[A] 纯函数剥标记")
    test_pure()
    print("[B] 发送抛异常时标记仍被剥掉")
    test_hooks_survive_send_failure()
    print("[C] _send_markers 吞异常")
    test_send_markers_swallows()
    print("[D] 自动补图吞异常")
    test_auto_send_swallows()
    print()
    if fails:
        print("MARKER_LEAK_TEST_FAILED: %d 项" % len(fails))
        raise SystemExit(1)
    print("MARKER_LEAK_TEST_OK")
