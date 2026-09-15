"""dsh-selfaware v1.1 集成测试：注入当前自我、长期块、聊天成功遥测与权限命令。"""

import asyncio
import importlib.util
import logging
import os
import sys
import tempfile
import types
from pathlib import Path

for name in (
    "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.core",
    "astrbot.core.agent", "astrbot.core.agent.message",
):
    sys.modules[name] = types.ModuleType(name)

class FakeStar:
    pass

class FakeTextPart:
    def __init__(self, text):
        self.text = text

def deco(*args, **kwargs):
    return lambda func: func

fake_logger = logging.getLogger("selfaware-integration-test")
fake_logger.handlers.clear()
fake_logger.propagate = False
fake_logger.setLevel(logging.DEBUG)
sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=FakeStar, Context=object)
sys.modules["astrbot.api.event"].AstrMessageEvent = object
sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
    on_llm_request=deco, on_llm_response=deco, command=deco,
)
sys.modules["astrbot.core"].logger = fake_logger
sys.modules["astrbot.core.agent.message"].TextPart = FakeTextPart

tmp = tempfile.mkdtemp()
os.environ["DSH_SELFAWARE_DB"] = os.path.join(tmp, "selfaware.db")
os.environ["DSH_SELFAWARE_GROUPS"] = "group"
os.environ["DSH_SELFAWARE_OWNER"] = "owner"
os.environ["DSH_SELFAWARE_JOIN_FILE"] = os.path.join(tmp, "join.json")
os.environ["DSH_SELFAWARE_BUDGET"] = "3000"

spec = importlib.util.spec_from_file_location("selfaware_integration", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class Context:
    async def get_current_chat_provider_id(self, umo):
        return "chat-main"
    def get_config(self):
        return {"provider_settings": {"default_image_caption_provider_id": "vision-main"}}

class MessageObj:
    group_id = "group"

class Event:
    message_obj = MessageObj()
    unified_msg_origin = "umo"
    message_str = "看看这个"
    def get_sender_id(self):
        return "owner"
    def plain_result(self, text):
        return text

class Req:
    image_urls = []
    extra_user_content_parts = [FakeTextPart("<image_caption>一只猫</image_caption>")]

main = m.Main(Context())
req = Req()
asyncio.run(main.inject(Event(), req))
text = "\n".join(getattr(x, "text", str(x)) for x in req.extra_user_content_parts)
assert "<current_machine_self>" in text, text
assert "图的内容转述过来了" in text, text
assert "chat-main" in text and "vision-main" in text, text
# [no-mech-talk] 机器自述块（<current_machine_self>…</current_machine_self>）
# 里不许出现技术词：模型会把它们照搬进群（2026-09-15「真看不见 图没递到我这边」）。
_machine_block = text.split("<current_machine_self>")[1].split("</current_machine_self>")[0]
for _mech in ("像素", "遥测", "容器", "进程", "接口"):
    assert _mech not in _machine_block, \
        "机器自述块里出现了会被照搬的技术词 %r：%r" % (_mech, _machine_block)
# 措辞规则必须在，且要点名这些词才禁得掉。
assert "说法要求" in text, "缺少「别用技术词」的措辞规则"
assert "模型没递到我这边" in text, "措辞规则没点名那句实际问题台词"
assert sum(len(getattr(x, "text", str(x))) for x in req.extra_user_content_parts[1:]) <= m.BUDGET

asyncio.run(main.observe_chat_response(Event(), object()))
states = {x["capability"]: x for x in main.sense.latest_states()}
assert states["chat"]["status"] == m.STATUS_AVAILABLE, states
assert states["vision"]["status"] == m.STATUS_AVAILABLE, states
assert states["qq"]["status"] == m.STATUS_AVAILABLE, states

async def collect(agen):
    return [x async for x in agen]

status = asyncio.run(collect(main.sense_status(Event())))
assert status and "图片理解" in status[0] and "文字思考与回复" in status[0], status

asyncio.run(main.terminate())
print("SELFAWARE_INTEGRATION_TEST_OK blocks=%d states=%d" % (len(req.extra_user_content_parts) - 1, len(states)))
