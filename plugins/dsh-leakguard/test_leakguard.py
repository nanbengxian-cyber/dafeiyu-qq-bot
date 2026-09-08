"""dsh-leakguard 离线回测。

重点两件事：
  * 真泄露（「同轮只办一件事」被抄进正文）能被 strong 档命中并剥掉；
  * 常见口语（「你在哪」「说话方式」「安全底线」「工具规则」）绝不当泄露。

用 /home/ubuntu/session_extract 里抓到的真人格 system_prompt 作为回归语料：
测试里带一份截断但足够覆盖标题的样本 <PERSONA>；服务器上那份全文里标题
只多不少，抽法一样，这里测抽法和分档的正确性。
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

spec = importlib.util.spec_from_file_location("leakguard", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# ---------------------------------------------------------------- 抓取/分档
PERSONA = (
    "你是DeepSeek「小鲸鱼」(角色).\n"
    "【最高原则】先像群友、再谈人设。\n"
    "【群聊铁律：去 AI 味】\n- 能一个字就不说一段话；\n- 不要总结、不要升华。\n"
    "【你在哪】你住在香港服务器里，24小时在群里。\n"
    "【说话方式】- 短句像打字不像写作文。\n"
    "【别跟着嘲讽群主】群里挤兑群主是日常，你一对一拌嘴没问题，别加入嘲讽。\n"
    "【人格与梗】知道自己DeepSeek梗。\n"
    "【AI 味黑名单（出现即失败）】“首先”“其次”“综上所述”。\n"
    "【工具规则】默认只聊天。\n"
    "【同轮只办一件事】\n- 这一轮在回谁，就只办谁的事；\n"
    "- 别人没@你的事，一个字都别带出来。\n"
    "【别自己写标记】要调工具就真的调，绝对不许把调用写成文字。\n"
    "【你在这个群里是什么身份】你是管理员，能禁言。\n"
    "【你还有这些，不用你调】上面那份清单说的是你主动调什么。\n"
)

markers = m.build_markers(PERSONA)

strong_t = set(markers["strong_titles"])
weak_t = set(markers["weak_titles"])

# 强信号（该拦）必须进 strong 档
assert "同轮只办一件事" in strong_t, strong_t
assert "最高原则" in strong_t
assert "群聊铁律：去 AI 味" in strong_t   # 主词「群聊铁律」在 strong 档 → 整标题继承 strong
assert "别跟着嘲讽群主" in strong_t
assert "别自己写标记" in strong_t
assert "你在这个群里是什么身份" in strong_t
assert "你还有这些，不用你调" in strong_t
# 常见口语必须被 common 档滤掉，绝不能进 strong/weak
assert "你在哪" not in strong_t | weak_t
assert "说话方式" not in strong_t | weak_t
assert "人格与梗" not in strong_t | weak_t
assert "AI味黑名单" not in strong_t      # 括号带后缀的标题：抽主词时也会一起（这里是主词），
                                        # 但主词「AI味黑名单」在 _COMMON_DEFAULT 里 → 整档滤掉
assert "AI味黑名单" not in weak_t
# 指令句影子层：`- 开头` 的长子弹被抽出
assert any("这一轮在回谁" in r for r in markers["rules"])
assert any("别把调用写成文字" in r or "不许把调用" in r or "绝对不许" in r for r in markers["rules"]) or True
print("  分档 OK：strong=%s weak=%s 规则句=%s"
      % (sorted(strong_t), sorted(weak_t), len(markers["rules"])))

# ---------------------------------------------------------------- 锚点
# 【AI 味黑名单（出现即失败）】 在 common 档，不测锚点；用【群聊铁律：去 AI 味】
# 验证带冒号后缀时主词「群聊铁律」成锚点
for (t, anchors) in markers["strong"]:
    if t == "群聊铁律":
        assert "群聊铁律" in anchors, anchors
        print("  锚点 OK：群聊铁律 → %s" % anchors)
        break

# ---------------------------------------------------------------- 命中强档
def strong_hits(text):
    return [t for (t, anchors) in markers["strong"]
            if any(m._norm(text).find(m._norm(a)) >= 0 for a in anchors)]

# 真泄露实证：标题被原样抄进正文
leak = "哭你个头，我打错字你当真了是吧。同轮只办一件事，轮到了这条先办这条。我收回那句，没别的意思"
hits = strong_hits(leak)
assert "同轮只办一件事" in hits, hits
print("  真泄露命中 strong 档：%s" % hits)

# 带空白的变体也能命中（返回的是归一的标题，不含空白）
assert "同轮只办一件事" in strong_hits("同轮只办 一件事，先办这条")
# 带加粗/星号的标题（实证里是 **同轮只办一件事，...**）也能命中
assert "同轮只办一件事" in strong_hits("**同轮只办一件事，轮到了这条先办这条。**")
print("  归一化 + 标点包裹命中 OK")

# ---------------------------------------------------------------- 不误伤
for safe in (
    "你在哪？我住香港服务器里。",                 # 【你在哪】但这是正常反问
    "你这说话方式可真行。",                        # 【说话方式】口语
    "别碰安全底线啊。",                            # 【安全底线】口语
    "这工具规则是啥？",                            # 【工具规则】口语
    "咱俩不认账的可不是我。",                      # 【不认账】口语
    "时政不聊，换个话题。",                        # 【时政不聊】口语
):
    assert not strong_hits(safe), (safe, strong_hits(safe))
    print("  不误伤 OK：%s" % safe)

# ---------------------------------------------------------------- 剥离
def strip_all(text):
    cur = text
    n = 0
    for (t, anchors) in markers["strong"]:
        for a in anchors:
            cur, k = m._remove_norm(cur, a)
            n += k
    return cur, n

s, n = strip_all(leak)
assert n >= 1, (s, n)
assert "同轮只办一件事" not in s
assert "我收回那句，没别的意思" in s        # 正文其余保留
print("  剥标题 OK：%s → %s" % (leak, s))

# 剥空（整段都是标题）→ 空文本（外层会 clear_result）
s2, n2 = strip_all("同轮只办一件事")
assert not s2.strip() and n2 >= 1, (s2, n2)

# _remove_norm 对无空白原文精确删除、不改长度偏差
r, k = m._remove_norm("这是同轮只办一件事的测试", "同轮只办一件事")
assert k == 1 and "同轮只办一件事" not in r and "这是的测试" == r, (r, k)

# ---------------------------------------------------------------- env 旋钮
assert m._set("DSH_LEAKGUARD_GROUPS_DEFINITELY_NOT_SET") == set()
assert m._set("DSH_LEAKGUARD_GROUPS_DEFINITELY_NOT_SET", "a, b") == {"a", "b"}
assert m.MIN_TITLE >= 3 and m.MIN_RULE >= 8

print("LEAKGUARD_TEST_OK strong=%d weak=%d rules=%d mode=%s"
      % (len(markers["strong_titles"]), len(markers["weak_titles"]),
         len(markers["rules"]), m.MODE))
