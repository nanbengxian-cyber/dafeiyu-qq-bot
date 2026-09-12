# -*- coding: utf-8 -*-
"""dsh-repeat 纯逻辑与处理器测试。"""
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.update({
    "DSH_REPEAT_GROUPS": "g1",
    "DSH_REPEAT_TRIGGER": "3",
    "DSH_REPEAT_WINDOW": "90",
    "DSH_REPEAT_COOLDOWN": "30",
})

for name in (
    "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.message_components",
    "astrbot.core", "astrbot.core.platform", "astrbot.core.platform.message_type",
):
    sys.modules.setdefault(name, types.ModuleType(name))

class Star:
    def __init__(self, context=None): self.context = context
class Plain:
    def __init__(self, text): self.text = text
class MessageChain:
    def __init__(self, chain): self.chain = chain

def deco(*args, **kwargs):
    return lambda fn: fn

sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=Star, Context=object)
sys.modules["astrbot.api.event"].AstrMessageEvent = object
sys.modules["astrbot.api.event"].MessageChain = MessageChain
sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
    platform_adapter_type=deco, command=deco,
    PlatformAdapterType=types.SimpleNamespace(ALL="all"),
)
sys.modules["astrbot.api.message_components"].Plain = Plain
sys.modules["astrbot.core"].logger = types.SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None)
sys.modules["astrbot.core.platform.message_type"].MessageType = types.SimpleNamespace(GROUP_MESSAGE="group")

spec = importlib.util.spec_from_file_location("repeat", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

passed = failed = 0
def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name, detail)

check("空白规范化", m.normalize_text("  你好\n 世界  ") == "你好 世界")
s = None
s, fire = m.advance(s, "哈", 0)
check("第一条不触发", s[1] == 1 and not fire)
s, fire = m.advance(s, "哈", 10)
check("第二条不触发", s[1] == 2 and not fire)
s, fire = m.advance(s, "哈", 20)
check("第三条触发", s[1] == 3 and fire)
s, fire = m.advance(s, "哈", 21)
check("第四条不重复触发", s[1] == 4 and not fire)
s, fire = m.advance(s, "嘿", 22)
check("不同文本重置", s[0] == "嘿" and s[1] == 1 and not fire)
s, fire = m.advance(("哈", 2, 0, False), "哈", 91)
check("超时重置", s[1] == 1 and not fire)

class Obj:
    def __init__(self, gid="g1", uid="u1", mid="1", text="哈", comps=None):
        self.gid, self.uid, self.text = gid, uid, text
        self.message_obj = types.SimpleNamespace(
            message_id=mid, message=comps if comps is not None else [Plain(text)], raw_message={"message_id": mid})
        self.sent = []
    def get_message_type(self): return "group"
    def get_platform_name(self): return "aiocqhttp"
    def get_group_id(self): return self.gid
    def get_sender_id(self): return self.uid
    def get_self_id(self): return "bot"
    def get_message_str(self): return self.text
    async def send(self, chain): self.sent.append(chain)
    def plain_result(self, text): return text

async def integration():
    m._state.clear(); m._recent_ids.clear(); m._last_sent.clear()
    plugin = m.Main(None)
    events = [Obj(mid=str(i), uid="u" + str(i)) for i in range(1, 4)]
    for event in events:
        await plugin.collect(event)
    check("三名群友三连后发送", len(events[-1].sent) == 1 and events[-1].sent[0].chain[0].text == "哈")
    fourth = Obj(mid="4", uid="u4")
    await plugin.collect(fourth)
    check("第四条不再发送", not fourth.sent)

    m._state.clear(); m._recent_ids.clear(); m._last_sent.clear()
    duplicate = Obj(mid="same")
    await plugin.collect(duplicate); await plugin.collect(duplicate); await plugin.collect(duplicate)
    check("同一事件不会虚构三连", not duplicate.sent and m._state["g1"][1] == 1)

    m._state.clear(); m._recent_ids.clear(); m._last_sent.clear()
    rich = Obj(mid="r", comps=[Plain("哈"), object()])
    await plugin.collect(rich)
    check("富消息不参与", "g1" not in m._state and not rich.sent)

    other = Obj(gid="g2", mid="x")
    await plugin.collect(other)
    check("群白名单生效", "g2" not in m._state)

asyncio.run(integration())
print("%d passed, %d failed" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
