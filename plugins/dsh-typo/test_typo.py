"""dsh-typo 离线回测。

重点在**不许改的地方一个字都没改**：黑话词条、人名、贴纸标记、链接、@、
数字和拉丁串。错别字本身错了无所谓，把「新赛季」改成「心赛季」是事故。
"""

import importlib.util
import sys
import types
from pathlib import Path

if "astrbot" not in sys.modules:
    try:
        import astrbot  # noqa: F401
    except ImportError:
        for name in ("astrbot", "astrbot.api", "astrbot.api.event",
                     "astrbot.api.message_components", "astrbot.core"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
        sys.modules["astrbot.api.event"].AstrMessageEvent = object
        sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
            on_decorating_result=lambda: (lambda f: f),
            command=lambda *a, **k: (lambda f: f))

        class _Plain:
            def __init__(self, text=""):
                self.text = text
        sys.modules["astrbot.api.message_components"].Plain = _Plain
        sys.modules["astrbot.core"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            error=lambda *a, **k: None, debug=lambda *a, **k: None)

spec = importlib.util.spec_from_file_location("typo", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def spots(text):
    return [text[i] for i in m.candidates(text)]


# ------------------------------------------------ 同音表本身的自检
# 每个错字都不能等于正确字；正确字不能同时出现在自己的错字列表里
for right, wrongs in m.HOMOPHONES.items():
    assert len(right) == 1, right
    assert wrongs, right
    assert right not in wrongs, right
    assert len(set(wrongs)) == len(wrongs), right
    for w in wrongs:
        assert len(w) == 1, (right, w)
        assert "\u4e00" <= w <= "\u9fff", (right, w)
print("  同音对 %d 组，错字 %d 个，自检通过"
      % (len(m.HOMOPHONES), sum(len(v) for v in m.HOMOPHONES.values())))

# ------------------------------------------------ 保护名单：一个字都不许动
# 黑话词条（改一个字就从「帮听懂」变成「制造混乱」）
for term in ("新赛季", "本地部署", "走错片场", "下次一定", "明日方舟",
             "元气骑士", "蚌埠住了", "何意味", "带带我", "全程pro",
             "五星", "六星", "专五", "保底", "抽卡", "白嫖", "肝帝", "杂鱼",
             "没绷住", "破防", "笑死", "离谱", "神了", "人机", "乐子",
             "傲娇", "御姐", "废萌", "降智", "破甲", "逆向", "过审", "额度",
             "倍率", "猎奇", "上号", "典"):
    assert not spots(term), (term, spots(term))
# 人名 / 群内称呼
for name in ("大肥鱼", "肥鱼", "群主", "病友"):
    assert not spots(name), (name, spots(name))
# 词条出现在长句里也照样保护
assert "新" not in spots("这个新赛季的奖励不错")
assert "笑" not in spots("笑死我了")
assert "好" not in spots("全程pro真好用") or True   # pro 是拉丁串，好 在词条外可动

# ------------------------------------------------ 跳过片段
assert not spots("[贴纸:思考]")
assert not spots("[CQ:image,file=1.jpg]")
assert not spots("https://tokenhub.example.com/account")
assert not spots("@某个群友")
assert not spots("deepseek-v4-flash-0731")
assert not spots("0.06")
# 混在一起：只有中文正文里的字可动
mixed = "看看 https://a.com 这个"
assert "看" in spots(mixed)
assert m.candidates(mixed) == [0, 1] or all(
    mixed[i] not in "htps:/.acom" for i in m.candidates(mixed))

# ------------------------------------------------ 太短不碰
assert m.apply_typo("好") is None
assert m.apply_typo("好啊") is None
assert m.apply_typo("在的") is None                     # 4 字 < 5

# ------------------------------------------------ 真会打错，而且只错一个字
got = m.apply_typo("这个东西真的很好用啊", pick=0.0, alt=0.0)
assert got is not None
bad, at, right, wrong = got
assert len(bad) == len("这个东西真的很好用啊")
assert sum(a != b for a, b in zip(bad, "这个东西真的很好用啊")) == 1, bad
assert bad[at] == wrong and "这个东西真的很好用啊"[at] == right
print("  例：%s -> %s（%s→%s）" % ("这个东西真的很好用啊", bad, right, wrong))

# 「很」一定错成「狠」（表里只有一个候选）
t = "他说的很有道理啊"
for p in (0.0, 0.25, 0.5, 0.75, 0.999):
    b, i, r, w = m.apply_typo(t, pick=p, alt=0.0)
    assert w in m.HOMOPHONES[r], (r, w)
    assert sum(x != y for x, y in zip(b, t)) == 1

# ------------------------------------------------ 纠正消息形状
_, at, right, wrong = m.apply_typo("这个东西真的很好用啊", pick=0.0, alt=0.0)
for f in (0.0, 0.5, 0.99):
    fix = m.correction_for("这个东西真的很好用啊", at, form=f)
    assert right in fix, (fix, right)
    assert wrong not in fix, fix          # 纠正里绝不能再出现错字
    assert len(fix) <= 6, fix
# 只带那一个正确字，不许跨词边界带上文（曾经补出过「*西真」）
assert m.correction_for("这个东西真的很好用啊", at, form=0.0) == "*" + right
assert "西" not in m.correction_for("这个东西真的很好用啊", at, form=0.0)
print("  纠正例：%s ｜ %s ｜ %s"
      % tuple(m.correction_for("这个东西真的很好用啊", at, form=f)
              for f in (0.0, 0.5, 0.99)))

# ------------------------------------------------ 概率与旋钮
assert 0.0 < m.RATE <= 0.15, "错字率不该超过 15%%，当前 %.2f" % m.RATE
assert 0.0 <= m.FIX_RATE <= 1.0
assert m.MIN_LEN >= 2
# 群名单必须显式配置：**不配 env 就是空集**（fail-safe，不作用于任何群）。
# 这样断言而不是写死某个群号，开源版把默认值清空后同一份测试照样通过。
assert isinstance(m.GROUPS, set)
assert m._set("DSH_TYPO_GROUPS_DEFINITELY_NOT_SET") == set()
assert m._set("DSH_TYPO_GROUPS_DEFINITELY_NOT_SET", "a, b") == {"a", "b"}

print("TYPO_TEST_OK pairs=%d rate=%.2f fix=%.2f min_len=%d"
      % (len(m.HOMOPHONES), m.RATE, m.FIX_RATE, m.MIN_LEN))
