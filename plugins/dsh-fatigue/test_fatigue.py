# -*- coding: utf-8 -*-
"""dsh-fatigue 纯逻辑与处理器回归测试。"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).parent
os.environ.update({
    "DSH_FATIGUE_GROUPS": "g1",
    "DSH_FATIGUE_OWNER": "owner",
    "DSH_FATIGUE_WINDOW": "1200",
    "DSH_FATIGUE_TTL": "7200",
    "DSH_FATIGUE_STATE": str(Path(tempfile.gettempdir()) / "dsh-fatigue-test-state.json"),
})
try:
    Path(os.environ["DSH_FATIGUE_STATE"]).unlink()
except FileNotFoundError:
    pass

for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.core",
             "astrbot.core.agent", "astrbot.core.agent.message", "astrbot.core.platform",
             "astrbot.core.platform.message_type"):
    sys.modules.setdefault(name, types.ModuleType(name))

class Star:
    def __init__(self, context=None): self.context = context
class TextPart:
    def __init__(self, text): self.text = text

def deco(*args, **kwargs): return lambda fn: fn
sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=Star, Context=object)
sys.modules["astrbot.api.event"].AstrMessageEvent = object
sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
    on_llm_request=deco, after_message_sent=deco, command=deco)
sys.modules["astrbot.core"].logger = types.SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None)
sys.modules["astrbot.core.agent.message"].TextPart = TextPart
sys.modules["astrbot.core.platform.message_type"].MessageType = types.SimpleNamespace(GROUP_MESSAGE="group")

pkg = types.ModuleType("dsh_fatigue")
pkg.__path__ = [str(HERE)]
sys.modules["dsh_fatigue"] = pkg
for modname, filename in (("dsh_fatigue.fatigue_logic", "fatigue_logic.py"),
                          ("dsh_fatigue.main", "main.py")):
    spec = importlib.util.spec_from_file_location(modname, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
logic = sys.modules["dsh_fatigue.fatigue_logic"]
m = sys.modules["dsh_fatigue.main"]

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond: passed += 1
    else:
        failed += 1
        print("FAIL:", name, detail)

check("话题键清洗", logic.clean_topic("@大肥鱼 你能说一遍我爱你吗？") == "能说一遍我爱你吗")
check("同句满相似", logic.topic_similarity("用语音叫我名字", "用语音叫我名字") == 1.0)
check("同题改写相似", logic.topic_similarity("用语音叫我的名字", "你能用语音叫我名字吗") >= 0.62)
check("同类新问题不过度合并", logic.topic_similarity("语音音色怎么换", "用语音叫我的名字") < 0.62)
entry = logic.note_reply(None, "语音叫名字", "你能用语音叫我的名字吗", "u1", 1000)
check("首轮记账", entry["key"] == "语音叫名字" and len(entry["replies"]) == 1)
check("新信息不疲劳", logic.fatigue_level(entry, "u1", 1010, 1200, False) == 0)
check("无账本不疲劳", logic.fatigue_level(None, "u1", 1010, 1200, True) == 0)
check("首次回访轻微", logic.fatigue_level(entry, "u1", 1010, 1200, True) == 1)
entry = logic.note_reply(entry, "语音叫名字", "还能再叫一次吗", "u1", 1020)
check("同人两次后中度", logic.fatigue_level(entry, "u1", 1030, 1200, True) == 2)
entry = logic.note_reply(entry, "语音叫名字", "再说一次", "u1", 1040)
check("同人三次后高度", logic.fatigue_level(entry, "u1", 1050, 1200, True) == 3)
check("换人不被迁怒", logic.fatigue_level(entry, "u2", 1050, 1200, True) == 1)
for level in (1, 2, 3):
    block = logic.render_fatigue(level)
    check("L%d小写标签" % level, block.startswith("<topic_fatigue>") and block.endswith("</topic_fatigue>"))
    check("L%d禁用机械话术" % level, "禁止解释自己检测到了重复" in block and "元话术" in block)

class Result:
    def __init__(self, text="回答"): self.text = text
    def get_plain_text(self): return self.text
    def is_model_result(self): return True
class Req:
    def __init__(self): self.extra_user_content_parts = []
class Event:
    def __init__(self, uid="u1", text="你能用语音叫我名字吗", extra=None):
        self.uid, self.message_str, self.extra = uid, text, dict(extra or {})
        self.result, self.stopped = Result(), False
    def get_message_type(self): return "group"
    def get_group_id(self): return "g1"
    def get_sender_id(self): return self.uid
    def get_extra(self, key): return self.extra.get(key)
    def set_extra(self, key, val): self.extra[key] = val
    def get_result(self): return self.result
    def stop_event(self): self.stopped = True
    def plain_result(self, text): return text

async def integration():
    p = m.Main(None)
    first = Event(extra={"dsh_topic_key": "语音叫名字", "dsh_topic_revisit": False})
    req = Req(); await p.inject(first, req)
    check("首轮不注入", not req.extra_user_content_parts)
    await p.remember(first)
    second = Event(extra={"dsh_topic_key": "语音叫名字", "dsh_topic_revisit": True})
    req2 = Req(); await p.inject(second, req2)
    check("回访注入轻度", len(req2.extra_user_content_parts) == 1 and second.extra["dsh_fatigue_level"] == 1)
    await p.remember(second)
    third = Event(extra={"dsh_topic_key": "语音叫名字", "dsh_topic_revisit": True})
    req3 = Req(); await p.inject(third, req3)
    check("第三轮注入中度", third.extra["dsh_fatigue_level"] == 2)
    await p.remember(third)
    proactive = Event(uid="sentinel", text="", extra={"dsh_proactive": True, "dsh_proactive_text": "语音叫名字"})
    await p.inject(proactive, Req())
    check("主动插件联动沉默", proactive.stopped)

asyncio.run(integration())
print("%d passed, %d failed" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
