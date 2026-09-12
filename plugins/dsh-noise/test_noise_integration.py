# -*- coding: utf-8 -*-
"""dsh-noise 集成回归：命中噪点后必须把新链写回结果。"""

import asyncio
import importlib.util
import logging
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

for name in (
    "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.message_components",
    "astrbot.core",
):
    sys.modules[name] = types.ModuleType(name)


def deco(*args, **kwargs):
    return lambda func: func


class FakeStar:
    def __init__(self, context=None):
        self.context = context


class Plain:
    def __init__(self, text):
        self.text = text


sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=FakeStar, Context=object)
sys.modules["astrbot.api.event"].AstrMessageEvent = object
sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
    on_decorating_result=deco, command=deco,
)
sys.modules["astrbot.api.message_components"].Plain = Plain
sys.modules["astrbot.core"].logger = logging.getLogger("noise-test")

os.environ["DSH_NOISE"] = "1"
os.environ["DSH_NOISE_GROUPS"] = "group"

spec = importlib.util.spec_from_file_location("noise_integration", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Result:
    def __init__(self):
        self.chain = [Plain("你好呀")]

    def is_model_result(self):
        return True


class Event:
    is_at_or_wake_command = False

    def __init__(self):
        self.result = Result()

    def get_group_id(self):
        return "group"

    def get_result(self):
        return self.result


event = Event()
main = m.Main(None)
with patch.object(m, "maybe_swap", return_value="你号呀"), \
        patch.object(m, "random") as rnd:
    rnd.random.return_value = 0.0
    asyncio.run(main.add_noise(event))

assert event.result.chain[0].text == "你号呀", event.result.chain[0].text
assert m._stat["swap"] == 1, m._stat
print("NOISE_INTEGRATION_TEST_OK text=%s" % event.result.chain[0].text)
