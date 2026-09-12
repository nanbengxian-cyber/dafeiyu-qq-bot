# -*- coding: utf-8 -*-
"""dsh-voice 审核与机械提示的离线回归。

2026-09-12 群动态记分卡抓到两类真实故障，这个文件把它们钉住：

  P6  8h 内 9 次语音被挡，其中
        - 「先V我50解锁转账功能」被语义审核判「否」（玩梗被当诈骗）
        - 2 次「审核通道超时」（10s 太短，fail-closed 白挡无害内容）
        - 模型输出「可。」这类带标点的结论时，raw == "可" 的精确比较直接判否
  P7  16:46~16:50 群友轮流敲 /说话，机器人往群里丢了 12 条「慢点，还有 N 秒冷却」

跑法（宿主机 py3.8 跑不了 main.py 的类型注解，必须在容器里跑）：
    sudo docker cp plugins/dsh-voice astrbot:/tmp/dsh-voice
    sudo docker exec astrbot python3 /tmp/dsh-voice/test_censor.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

if "astrbot" not in sys.modules:
    try:
        import astrbot  # noqa: F401
    except ImportError:
        for name in ("astrbot", "astrbot.api", "astrbot.api.event",
                     "astrbot.api.message_components", "astrbot.core",
                     "astrbot.api.provider",
                     "astrbot.core.message.message_event_result",
                     "astrbot.core.platform.message_type"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
        sys.modules["astrbot.api.event"].AstrMessageEvent = object
        sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
            on_llm_response=lambda *a, **k: (lambda f: f),
            command=lambda *a, **k: (lambda f: f),
            event_message_type=lambda *a, **k: (lambda f: f))
        sys.modules["astrbot.api.message_components"].Record = object
        sys.modules["astrbot.api.provider"].LLMResponse = object
        sys.modules["astrbot.core"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            error=lambda *a, **k: None, debug=lambda *a, **k: None)
        sys.modules["astrbot.core.message.message_event_result"].MessageChain = object
        sys.modules["astrbot.core.platform.message_type"].MessageType = types.SimpleNamespace(
            GROUP_MESSAGE="g")

spec = importlib.util.spec_from_file_location("dsh_voice", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append("%s: got %r want %r" % (name, got, want))


# ------------------------------------------------ 1. 审核结论容错解析
# 思考模型爱加标点/引号，也爱把「不可以」写成三个字。这里逐个钉死。
CASES = [
    ("可", True),
    ("可。", True),
    ("可\n", True),
    ('"可"', True),
    ("「可」", True),
    ("可以", True),
    ("适合", True),
    ("没问题", True),
    ("否", False),
    ("否。", False),
    ("不可以", False),          # 含「可」但是否定，必须先判否
    ("不适合念出来", False),
    ("不宜", False),
    ("", None),                 # 空的当没结论，走重试，不当拒绝
    ("嗯……让我想想", None),      # 答非所问也是没结论
]
for raw, want in CASES:
    check("verdict(%r)" % raw, m._parse_censor_verdict(raw), want)

# ------------------------------------------------ 2. 机械提示按群去重
sid = "group:476573490"
other = "group:1067341190"
m.NOTICE_GAP = 90
m._notice_last.clear()
check("首次提示放行", m._notice_allowed(sid, 1000.0), True)
check("窗口内第二次静默", m._notice_allowed(sid, 1010.0), False)
check("窗口内第三次静默", m._notice_allowed(sid, 1050.0), False)
check("另一个群不受影响", m._notice_allowed(other, 1050.0), True)
check("超过间隔恢复", m._notice_allowed(sid, 1091.0), True)

# ------------------------------------------------ 3. 冷却倒计时确实只发一条
class _M:
    def __init__(self, text):
        self.text = text


class _Event:
    unified_msg_origin = sid
    message_str = "/说话 测试一下"

    def __init__(self):
        self.sent = []

    def plain_result(self, text):
        return _M(text)

    def set_extra(self, *a):
        pass


class _VoiceProbe:
    """复用真实 cmd_speak，但把 _send_voice 换成不联网的桩。"""

    def __init__(self):
        self.calls = 0

    async def _send_voice(self, event, text):
        self.calls += 1
        return True, ""


async def _run_cmd():
    m._notice_last.clear()
    m._last_call.clear()
    m.NOTICE_GAP = 90
    probe = _VoiceProbe()
    body = m.Main.cmd_speak.__get__(probe, _VoiceProbe)
    # 冷却生效：先假装刚发过一条语音
    m._last_call[sid] = 1000.0
    texts = []
    import time as _t
    real = _t.time
    _t.time = lambda: 1000.0            # 冻结时间，确保一定在冷却里
    try:
        for _ in range(5):
            ev = _Event()
            async for seg in body(ev):
                texts.append(seg.text)
    finally:
        _t.time = real
    return texts, probe.calls


texts, calls = asyncio.get_event_loop().run_until_complete(_run_cmd())
check("冷却期连敲 5 次只提示 1 条", len(texts), 1)
check("提示文案", texts[0] if texts else None, "慢点，还有 20 秒冷却")
check("冷却期一次都没真的去合成", calls, 0)

# ------------------------------------------------ 4. 审核两次都超时 -> 拒发，但原因给人看
class _Ctx:
    def __init__(self, mode):
        self.mode = mode
        self.calls = 0

    async def get_current_chat_provider_id(self, umo):
        return "p1"

    async def llm_generate(self, **kw):
        self.calls += 1
        if self.mode == "ok":
            return types.SimpleNamespace(completion_text="可。")
        if self.mode == "retry":
            # 第 1 次答非所问，第 2 次给结论
            return types.SimpleNamespace(
                completion_text="嗯……" if self.calls == 1 else "可")
        return types.SimpleNamespace(completion_text="否")


async def _run_censor(mode, text="先V我50解锁转账功能"):
    ctx = _Ctx(mode)
    return await m._censor_text(ctx, _Event(), text), ctx.calls


res, n = asyncio.get_event_loop().run_until_complete(_run_censor("ok"))
check("带句号的「可。」放行", res, (True, ""))
check("放行只问一次", n, 1)

res, n = asyncio.get_event_loop().run_until_complete(_run_censor("retry"))
check("结论无法解析时重试一次并放行", res, (True, ""))
check("重试确实问了两次", n, 2)

res, n = asyncio.get_event_loop().run_until_complete(
    _run_censor("no", "我就像无能的丈夫,只能眼睁睁看着大肥鱼被用了"))
check("真有害内容仍拦", res[0], False)
check("真有害内容不重试", n, 1)

# 本地快拦优先级最高，不花一次 LLM 调用
res, n = asyncio.get_event_loop().run_until_complete(_run_censor("ok", "啊啊啊嗯嗯嗯"))
check("拟声由本地快拦兜住", res[0], False)
check("本地拦掉就不问模型", n, 0)

res, n = asyncio.get_event_loop().run_until_complete(_run_censor("ok", "操你妈"))
check("词表命中同样本地拦", res[0], False)
check("词表命中也不问模型", n, 0)

# 设施类原因不能当口播素材
check("设施拒绝映射成自然话",
      "审核通道超时" in m._INFRA_REASONS and "审核通道异常" in m._INFRA_REASONS, True)

if FAILS:
    print("FAIL x%d" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("test_censor.py: 全部通过（%d 项断言）" % (
    len(CASES) + 12))
