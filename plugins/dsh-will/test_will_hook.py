# -*- coding: utf-8 -*-
"""dsh-will 钩子级集成测试：跑**真实的 act 出口处理器**，不碰真群、不发消息。

必须在 astrbot 容器里跑：

    sudo docker cp dsh-will astrbot:/tmp/dsh-will-check
    sudo docker exec astrbot sh -c 'cd /tmp/dsh-will-check && touch __init__.py && python3 test_will_hook.py'

框架不在时打印 SKIP 并以 0 退出，不假装通过。

★ 这个测试存在的理由（纯函数测试抓不到的东西）★

  这个插件的**全部价值**就是「真的做」而不是「光说说」。所以测试必须证明：

   1. `silence` 档真的调到了 `clear_result()` + `stop_event()` ——
      少一个，消息照发，功能就退化成「嘴上说我不回」。
   2. `snub`/`curt` 档真的 `set_result(台词)` —— 而不是把台词写进日志就算完。
   3. **正经任务一个字节都不许动** —— 这是硬不变量，误伤比不反抗严重得多。
   4. 影子模式下**绝不改结果** —— 影子就该是影子。
   5. 群主永不被沉默（含额度充足时）。
   6. 没被 @ 时什么都不做（否则会变成随机失踪）。
   7. 账本/事件表里**没有原文**（只有命中的那个词）。
   8. 一切异常都放行，`act` 不抛。
"""

import asyncio
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# ★ 镜像里的群号/群主是脱敏占位；测试自己给值，不靠生产 env
os.environ["DSH_WILL"] = "1"
os.environ["DSH_WILL_GROUPS"] = "100000001"
os.environ["DSH_WILL_OWNER"] = "2774000001"
os.environ["DSH_WILL_ADMINS"] = "2774000002"
os.environ["DSH_WILL_SILENCE_PER_DAY"] = "2"
os.environ["DSH_WILL_QUIET_MIN"] = "0"
_TMP = tempfile.mkdtemp(prefix="dsh-will-hook-")
os.environ["DSH_WILL_DB"] = os.path.join(_TMP, "dsh_will.db")
os.environ["DSH_WILL_EFFECT_DB"] = os.path.join(_TMP, "no_such_effect.db")

GID = "100000001"
UID = "1234567890"
BOT = "3752949000"

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

ROOT = str(Path(__file__).resolve().parent)
spec = importlib.util.spec_from_file_location(
    "dsh_will_pkg", os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT])
pkg = importlib.util.module_from_spec(spec)
sys.modules["dsh_will_pkg"] = pkg
spec.loader.exec_module(pkg)
main = importlib.import_module("dsh_will_pkg.main")
logic = importlib.import_module("dsh_will_pkg.will_logic")


# ---------------------------------------------------------------- 假事件
class Chain(object):
    def __init__(self, text="这是模型生成的一句正常回复，字数够长以显得像真的"):
        self._text = text


class Result(object):
    def __init__(self, text, model=True):
        self.chain = [Chain(text)] if text is not None else []
        self._text = text
        self._model = model

    def is_model_result(self):
        return self._model

    def get_plain_text(self):
        return self._text


class Sender(object):
    def __init__(self, role):
        self.role = role


class Raw(object):
    def __init__(self, role, text):
        self.sender = Sender(role)
        self.raw_message = text


class MsgObj(object):
    def __init__(self, uid, role, raw_text, mid):
        self.self_id = BOT
        self.message_id = mid
        self.raw_message = {"sender": {"role": role}, "raw_message": raw_text}


class Ev(object):
    def __init__(self, uid=UID, role="member", message="", result=None, mid="1",
                 gid=GID, directed=True):
        text = message
        if directed and ("[CQ:at,qq=%s]" % BOT) not in text:
            text = "[CQ:at,qq=%s] %s" % (BOT, text)
        self.message_obj = MsgObj(uid, role, text, mid)
        self.message_str = message
        self._uid = uid
        self._result = result if result is not None else Result(None)
        self._gid = gid
        self.role = role if role == "admin" else ""
        self.is_at_or_wake_command = directed
        self.cleared = 0
        self.stopped = 0

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._uid

    def get_result(self):
        return self._result

    def set_result(self, text):
        self._result = Result(text)

    def clear_result(self):
        self.cleared += 1
        self._result = Result(None)

    def stop_event(self):
        self.stopped += 1

    def plain_result(self, text):
        return type("R", (), {"text": text})()


def text_of(ev):
    r = ev._result
    return r.get_plain_text()


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


inst = main.Main.__new__(main.Main)
inst.context = None
inst.store = main.WillStore(Path(os.environ["DSH_WILL_DB"]))
inst._lines = {}
inst._last_any = {}
inst._seen_mid = set()


BASE = "模型认真回答了这个问题的完整内容"


def fresh():
    """全新账本 + 清冷却。每节独立，免得上一节攒的火污染下一节的判定
    （第一版就是被这个坑掉的：影子那节量到「火98」，其实是前面几节攒的）。"""
    path = os.path.join(_TMP, "w%d.db" % (time.time_ns() % 10 ** 9))
    inst.store = main.WillStore(Path(path))
    inst._lines = {}
    inst._last_any = {}
    inst._seen_mid = set()
    return path


def feed(message, uid=UID, role="member", result_text=BASE, mid=None, directed=True,
         shadow=None, gid=GID, cool=True):
    """喂一条消息进真实处理器。cool=False 用来模拟「过了一段时间」以越过冷却。"""
    if shadow is not None:
        main.SHADOW = shadow
    if not cool:
        inst._last_any = {}
    ev = Ev(uid=uid, role=role, message=message,
            result=Result(result_text) if result_text else Result(None),
            mid=mid or ("m%d" % (time.time_ns() % 10 ** 9)), directed=directed, gid=gid)
    run(inst.act(ev))
    if shadow is not None:
        main.SHADOW = False
    return ev


main.SHADOW = False      # 默认按「真做」测；影子单独测
fresh()

# ---------------------------------------------------------------- 1. 真的执行
# 一句直接贬损就该有反应，而且反应必须是**真的改了结果**。
ev = feed("傻")
check("贬损：结果被改写（不是光记录）", text_of(ev) in logic.CURT_LINES, text_of(ev))
check("贬损：真的 set_result（不是注释放行）",
      text_of(ev) != "模型本来写的正常回复，够长", text_of(ev))
check("贬损：不是沉默", ev.cleared == 0)

# 继续加压 → 顶回去
ev2 = feed("内裤送我", cool=False)   # 模拟过了群级冷却
check("加压：升级成顶回去", text_of(ev2) in logic.SNUB_LINES, text_of(ev2))
ev3 = feed("妈妈", cool=False)
check("继续加压：到沉默档", ev3.cleared >= 1 and ev3.stopped >= 1,
      (ev3.cleared, ev3.stopped, text_of(ev3)))
check("沉默：结果被清掉（真的不发）", text_of(ev3) is None)

# ---------------------------------------------------------------- 2. 硬不变量：任务永远照办
main.SHADOW = False
fresh()
for msg in ("/说话 叮咚鸡", "什么是np猜想", "帮我算一下", "证明雅可比猜想",
            "封了他", "重启一下服务"):
    ev_t = feed(msg, cool=False)
    check("任务照办（未被改写）：%s" % msg, text_of(ev_t) == BASE, text_of(ev_t))
# 骂着人问正经问题：任务照办，但账照记、且绝不动手
ev_t2 = feed("傻逼，帮我算一下这个题", cool=False)
check("带骂的任务：照样办（一字不改）", text_of(ev_t2) == BASE, text_of(ev_t2))
con_t = sqlite3.connect(inst.store.path)
n_struck = con_t.execute("SELECT count(*) FROM event WHERE kind LIKE 'insult%'").fetchone()[0]
con_t.close()
check("带骂的任务：账照样记（只是不动手）", n_struck >= 1, n_struck)

# ---------------------------------------------------------------- 3. 影子模式
fresh()
shadow_ev = feed("傻", shadow=True)
check("影子：结果一个字节都没改", text_of(shadow_ev) == BASE, text_of(shadow_ev))
check("影子：没有清结果", shadow_ev.cleared == 0)
main.SHADOW = False

# ---------------------------------------------------------------- 4. 群主/管理员
fresh()
owner_many = [feed("傻", uid="2774000001", role="owner", cool=False) for _ in range(6)]
check("群主：永不被沉默", all(e.cleared == 0 for e in owner_many),
      [e.cleared for e in owner_many])
fresh()
admin_ev = [feed("傻", uid="2774000002", role="admin", cool=False) for _ in range(6)]
check("管理员：永不被沉默", all(e.cleared == 0 for e in admin_ev))

# ---------------------------------------------------------------- 5. 没被点名 / 非生效群
fresh()
quiet = feed("傻", directed=False)
check("没被 @：什么都不做", text_of(quiet) == BASE, text_of(quiet))
other = feed("傻", gid="999999999")
check("非生效群：什么都不做", text_of(other) == BASE, text_of(other))

# ---------------------------------------------------------------- 6. 冷却
fresh()
c1 = feed("内裤送我")
c2 = feed("内裤送我")                      # 60 秒内第二条：不该再动手
check("群级冷却：连发时不逐条动手", text_of(c2) == BASE, (text_of(c1), text_of(c2)))
c3 = feed("内裤送我", cool=False)          # 过了冷却：账还在攒，所以档位会升级
check("冷却过后还能再动（可能已升级到沉默）",
      text_of(c3) is None or text_of(c3) in logic.CURT_LINES + logic.SNUB_LINES, text_of(c3))

# ---------------------------------------------------------------- 7. 库里不留原文
main.SHADOW = False
fresh()
feed("内裤送我")
con = sqlite3.connect(inst.store.path)
rows = con.execute("SELECT kind,evidence,reason FROM event").fetchall()
kinds = {r[0] for r in rows}
check("账本：记下了类别", ("insult" in kinds) or ("harass" in kinds), kinds)
blob = " ".join(str(r[1]) + str(r[2]) for r in rows)
check("账本：不含整句原文", "内裤送我" not in blob and BASE not in blob, blob[:80])
check("账本：证据只有命中的词", all(len(str(r[1])) <= 20 for r in rows))
led = con.execute("SELECT count(*) FROM ledger").fetchone()[0]
check("账本：每人一行", led >= 1, led)
con.close()

# ---------------------------------------------------------------- 8. 失败必须放行
class Boom(object):
    def get_group_id(self):
        return GID

    def get_sender_id(self):
        raise RuntimeError("boom")

    def get_result(self):
        raise RuntimeError("boom")

    @property
    def message_obj(self):
        raise RuntimeError("boom")


main.SHADOW = False
try:
    run(inst.act(Boom()))
    check("异常不抛（fail-open）", True)
except BaseException as exc:
    check("异常不抛（fail-open）", False, exc)

# 事件表被写坏时也不该影响回复
fresh()
missing = Ev(uid=UID, message="傻", result=Result("原文"), mid="zzz")
inst.store.path = os.path.join(_TMP, "nodir", "x.db")     # 目录不存在 → 全链路报错
try:
    run(inst.act(missing))
    check("账本写不进去也放行", text_of(missing) == "原文", text_of(missing))
except BaseException as exc:
    check("账本写不进去也放行", False, exc)
fresh()

# ---------------------------------------------------------------- 9. 优先级
try:
    from astrbot.core.star.star_handler import star_handlers_registry
    found = [h for h in star_handlers_registry
             if "dsh_will_pkg.main" in str(getattr(h, "handler_module_path", ""))
             and getattr(h, "handler_name", "") == "act"]
    check("注册表里找得到 act", bool(found), len(found))
    check("act 的优先级是 -200（排在所有加工之后，反抗是最终决定）",
          bool(found) and int(found[0].extras_configs.get("priority", 0)) == -200,
          [getattr(h, "extras_configs", None) for h in found])
except Exception as exc:                                  # pragma: no cover
    check("优先级检查可执行", False, exc)

print("WILL_HOOK_TEST_OK passed=%d failed=%d" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
