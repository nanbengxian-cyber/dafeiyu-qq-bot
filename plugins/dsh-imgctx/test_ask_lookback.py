"""dsh-imgctx「说图时放宽回看条数」回归测试。

2026-09-15 生产翻车：群友 11:57:32 发了图，12:00:24 才 @ 它问「图片里面的内容是
什么意思」，中间隔了 6 条。LOOKBACK=4 够不着那张图，于是它没图可看，还被
clarify 判成「图片内容未知」，最后反问「图片里是什么意思？」并回「真看不见
图没递到我这边」。群友当场不满： 「为什么不吐槽一下图片里的内容」「你看不见吗」。

这个测试锁住两件事：
  1. 默认 LOOKBACK 找 4 条以外的图，本来就应该找不到（保留旧行为）；
  2. 传了加宽的 lookback 之后，同样一段历史必须能找到那张图。
"""

import importlib.util
import sys
import time
import types
from pathlib import Path

if "astrbot" not in sys.modules:
    try:
        import astrbot  # noqa: F401
    except ImportError:
        for name in (
            "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.core",
            "astrbot.core.agent", "astrbot.core.agent.message",
            "astrbot.core.platform", "astrbot.core.platform.message_type",
        ):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
        sys.modules["astrbot.api.event"].AstrMessageEvent = object
        sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
            on_llm_request=lambda: (lambda f: f),
            command=lambda *a, **k: (lambda f: f),
        )
        sys.modules["astrbot.core"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            error=lambda *a, **k: None, debug=lambda *a, **k: None,
        )
        sys.modules["astrbot.core.agent.message"].TextPart = type(
            "TextPart", (), {"__init__": lambda self, text="": setattr(self, "text", text)}
        )
        sys.modules["astrbot.core.platform.message_type"].MessageType = types.SimpleNamespace(
            GROUP_MESSAGE="group"
        )

# dsh-imgctx 会 `from astrbot.core import logger`，import astrbot.core 会把
# loguru 的文件 sink 挂到**生产日志** /AstrBot/data/logs/astrbot.log 上。
# 不清掉的话跑一次测试就往生产日志掺几行。独立进程，remove() 动不到在跑的 AstrBot。
try:
    from loguru import logger as _loguru_logger

    _loguru_logger.remove()
except Exception:
    pass

path = sys.argv[1] if len(sys.argv) > 1 else "main.py"
spec = importlib.util.spec_from_file_location("imgctx_ask", Path(path))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# 载入模块本身会把 sink 装回去，所以要在载入之后再清一次。
try:
    from loguru import logger as _loguru_logger

    _loguru_logger.remove()
except Exception:
    pass

now = time.time()


def _text(mid, uid, body, age_s):
    return {
        "message_id": mid,
        "user_id": uid,
        "time": now - age_s,
        "sender": {"card": "群友%s" % uid, "nickname": "群友%s" % uid},
        "message": [{"type": "text", "data": {"text": body}}],
    }


def _image(mid, uid, age_s):
    return {
        "message_id": mid,
        "user_id": uid,
        "time": now - age_s,
        "sender": {"card": "发图的人", "nickname": "发图的人"},
        "message": [
            {
                "type": "image",
                "data": {"file": "f-%s.jpg" % mid, "url": "https://example.invalid/%s" % mid},
            }
        ],
    }


# 历史：一张图（172 秒前，还在 MAX_AGE 内），后面跟 6 条纯文字，最后是当前这条。
history = [_image("img1", "111", 172)]
history += [_text("t%d" % i, "222", "闲聊 %d" % i, 160 - i * 20) for i in range(6)]
history.append(_text("cur", "333", "图片里面的内容是什么意思", 1))

assert m.LOOKBACK == 4, "默认回看条数变了：%r" % m.LOOKBACK
assert m.ASK_LOOKBACK >= 8, "说图时的加宽太小：%r" % m.ASK_LOOKBACK
assert m.ASK_LOOKBACK > m.LOOKBACK, "加宽必须大于默认值"

old = m._pick_images(history, self_id="999", cur_msg_id="cur")
assert old == [], "默认 %d 条不该够到 8 条以外的图，却挑出了 %r" % (m.LOOKBACK, old)

new = m._pick_images(history, self_id="999", cur_msg_id="cur", lookback=m.ASK_LOOKBACK)
assert len(new) == 1, "加宽后应挑到那张图，实际 %r" % (new,)
assert new[0]["file"] == "f-img1.jpg", new
assert new[0]["who"] == "发图的人", new

# 显式传 lookback 不能破坏默认行为：不传时仍然按 LOOKBACK 走。
assert m._pick_images(history, self_id="999", cur_msg_id="cur", lookback=None) == []

# 自己发的图仍然跳过。
self_history = [dict(_image("img2", "999", 30))]
assert m._pick_images(self_history, self_id="999", cur_msg_id="cur", lookback=12) == []
assert m._pick_images(self_history, self_id="999", cur_msg_id="cur", lookback=12,
                      include_current=True) == []


# 在说图、但回看到底也没捞到图（图太老/链接过期/被撤）：
# 必须注入「别否认看图能力、别解释机制」的兜底提示，否则模型会说出
# 「真看不见 图没递到我这边」这种话（2026-09-15 现场原话）。
import asyncio  # noqa: E402

Main = m.Main.__new__(m.Main)  # 只测 _run，不走框架初始化


class _Part:
    def __init__(self, text):
        self.text = text


class _Req:
    def __init__(self, prompt, parts=()):
        self.prompt = prompt
        self.extra_user_content_parts = [_Part(x) for x in parts]


class _Obj:
    self_id = "999"
    message_id = "cur"


class _Ev:
    message_obj = _Obj()


class _Bot:
    def __init__(self, messages):
        self._messages = messages
        self.count = None

    async def get_group_msg_history(self, **kw):
        self.count = kw.get("count")
        return {"messages": self._messages}


plain = [_text("t%d" % i, "222", "闲聊 %d" % i, 160 - i * 20) for i in range(6)]
plain.append(_text("cur", "333", "图片里面的内容是什么意思", 1))

req_ask = _Req("图片里面的内容是什么意思")
bot_ask = _Bot(plain)
asyncio.run(
    Main._run(_Ev(), req_ask, bot_ask, "100000001", "vision-scnet")
)
assert bot_ask.count >= m.ASK_LOOKBACK, \
    "在说图时没放宽取历史条数：count=%r" % bot_ask.count
assert len(req_ask.extra_user_content_parts) == 1, "在说图却取不到图时没有提示"
hint = req_ask.extra_user_content_parts[0].text
for need in ("不要说自己看不见图", "不要", "解释"):
    assert need in hint, "兜底提示内容不对: %r" % hint

# 不是在说图（纯闲聊、也没图）→ 不要多嘴注入。
req_chat = _Req("今天吃啥")
bot_chat = _Bot(plain)
asyncio.run(
    Main._run(_Ev(), req_chat, bot_chat, "100000001", "vision-scnet")
)
assert req_chat.extra_user_content_parts == [], "不在说图却注入了兜底提示"
assert bot_chat.count == m.LOOKBACK + 2, \
    "不在说图时不该放宽取历史条数：count=%r" % bot_chat.count

print("ALL PASS: LOOKBACK=%d ASK_LOOKBACK=%d，加宽后能找到 8 条以外的图；"
      "取不到图时会提示别否认看图能力" % (m.LOOKBACK, m.ASK_LOOKBACK))
