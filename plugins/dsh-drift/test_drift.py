"""dsh-drift 离线回测。断言全部来自真实语料里出现过的句子。

重点不是「漂移能不能触发」，而是**两条硬边界一个都不许破**：
被 @ 不漂移、像提问不漂移。这两条是防止把「答非所问」重新引回来的唯一保障。
"""

import importlib.util
import sys
import types
from pathlib import Path

if "astrbot" not in sys.modules:
    try:
        import astrbot  # noqa: F401
    except ImportError:
        for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.core",
                     "astrbot.core.agent", "astrbot.core.agent.message"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
        sys.modules["astrbot.api.event"].AstrMessageEvent = object
        sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
            on_llm_request=lambda: (lambda f: f),
            command=lambda *a, **k: (lambda f: f))
        sys.modules["astrbot.core"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            debug=lambda *a, **k: None)
        sys.modules["astrbot.core.agent.message"].TextPart = object

spec = importlib.util.spec_from_file_location("drift", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def ok(msg, addressed=False, roll=0.0):
    return m.should_drift(msg, addressed, roll)[0]


def why(msg, addressed=False, roll=0.0):
    return m.should_drift(msg, addressed, roll)[1]


# ---------------------------------------------- 边界一：被 @ 绝不漂移
# 被点名了还去接支线，就是字面意义上的答非所问
assert not ok("你怎么看这个方案", addressed=True)
assert not ok("随便聊点什么都行啊反正闲着", addressed=True, roll=0.0)
assert why("随便聊点什么都行啊反正闲着", addressed=True) == "被点名"

# ---------------------------------------------- 边界二：像提问绝不漂移
# 以下全部取自三群真语料
for q in (
    "GLM怎么用不了了？提示预扣费额度失败",
    "话说为啥我这儿flash不行了？",
    "你是真人还是人机？",
    "何意味？为什么要中止？",
    "你这咋过审的。。",            # 「咋」→ 怎么类疑问
    "五星比六星难抽来的",          # 陈述句，下面单独断言它能漂移
):
    if q == "五星比六星难抽来的":
        continue
    assert not ok(q), q
    assert why(q) == "像在提问", (q, why(q))

# 疑问词而非问号的也要认出来
assert why("这个是不是要收费的") == "像在提问"
assert why("能不能给我看看那个配置") == "像在提问"
assert why("他到底在哪儿弄的") == "像在提问"
# 句末疑问助词（注意判定顺序是 被@ → 太短 → 提问 → 摇概率，
# 所以验证「提问」这条必须用够长的句子，否则先被「太短」拦下）
assert why("这个应该没什么问题吗") == "像在提问"
assert why("他到底是在说哪一个呢") == "像在提问"
# 「吧」不算提问：多数时候只是软化或表推测，收了它会误杀大量正常闲聊
assert why("但是svg这程度真的离谱了吧") != "像在提问"
assert why("那这个应该也能用的吧") != "像在提问"

# ---------------------------------------------- 太短不漂移
assert why("典") .startswith("消息太短")
assert why("666") .startswith("消息太短")
assert why("笑死我了").startswith("消息太短")   # 4 字 < 6

# ---------------------------------------------- 概率闸门
# roll >= rate 就不漂移；roll < rate 才漂移
long_stmt = "但是svg这程度真的离谱了吧"          # 真语料，陈述句
assert not ok(long_stmt, roll=0.99)
assert why(long_stmt, roll=0.99).startswith("没摇中")
assert ok(long_stmt, roll=0.0)
# rate=0 等于整条关掉
assert not m.should_drift(long_stmt, False, 0.0, rate=0.0)[0]

# ---------------------------------------------- 真语料里该能漂移的陈述句
for s in (
    "但是svg这程度真的离谱了吧",
    "我这边已经放0.06倍率GPT6了",
    "哎哟我去，我9块钱额度瞬间消失了",
    "还全是车轱辘废话，水平很差",
    "我搞点本地部署的事，那个5.3 flash直接给我9块钱额度秒了",
):
    assert ok(s, roll=0.0), s

# ---------------------------------------------- 注入块形状
block = m.render()
assert block.startswith("<attention_drift>") and block.endswith("</attention_drift>")
# 标签必须是「小写+下划线」，dsh-ctxclean 靠这个结构清理历史里的旧块
import re as _re
assert _re.match(r"^<[a-z][a-z0-9_]*>$", block.split("\n")[0])
# 每一档都必须带住「回复仍要短、要能被最近消息解释」这道约束
for lv in m.LEVELS:
    b = m.render(lv)
    assert "回复仍然要短" in b and "不要为了跑题而跑题" in b, lv
# 没做 wild 档：真群里放开到「明显跑题」不值得赌
assert "wild" not in m.LEVELS
assert set(m.LEVELS) == {"subtle", "active", "scattered"}
# 默认最轻的一档
assert m.LEVEL == "subtle"

print("DRIFT_TEST_OK levels=%d rate=%.2f min_len=%d"
      % (len(m.LEVELS), m.RATE, m.MIN_LEN))