"""dsh-glossary 离线回测。断言全部来自真实语料里出现过的句子。

分三块：真命中、假命中防回归、结构规则。
新增词条（v2）必须两样都有：一条真语料真命中 + 至少一条挡假命中的断言。
"""

import importlib.util
import sys
import types
from pathlib import Path

# 容器外跑时 astrbot 不在 sys.path，打桩让 main.py 能 import（容器内不会走到）
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
            info=lambda *a, **k: None, warning=lambda *a, **k: None)
        sys.modules["astrbot.core.agent.message"].TextPart = object

spec = importlib.util.spec_from_file_location("glossary", Path(__file__).with_name("main.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def terms(text):
    return [entry.terms[0] for entry in module.matched_entries(text)]


# ---------------------------------------------------------------- 真命中（v1）
assert terms("常驻池就是好啊，专五和角色能一块出") == ["常驻池", "专五"]
assert terms("依旧新赛季肝帝") == ["新赛季", "肝帝"]
assert terms("五星可没保底") == ["五星", "保底"]
assert terms("还全是车轱辘废话，水平很差") == ["车轱辘废话"]
assert terms("典") == ["典"]
assert terms("走错片场了") == ["走错片场"]
assert terms("我这边已经放0.06倍率GPT6了") == ["倍率"]

# ---------------------------------------------------------------- 真命中（v2 新词条）
# 每条都是 anime_memes_manual.md 的候选词里，在三群真语料中确认过的原话
assert terms("666，工程量很大的") == ["666"]
assert terms("666变脸这么快") == ["666"]
assert terms("傲娇人设是不会这样说话的") == ["傲娇"]
assert terms("可不可以设置为傲娇御姐风") == ["傲娇", "御姐"]
assert terms("笑死我了") == ["笑死"]
assert terms("蚌埠住了") == ["蚌埠住了"]
assert terms("呜呜呜我破防了") == ["破防"]
assert terms("🤔原来大肥鱼也喜欢废萌") == ["废萌"]
assert terms("但是svg这程度真的离谱了吧") == ["离谱"]

# ------------------------- 真命中（v2 语料侧挖出来的本群口头禅，外部词库都没有）
assert terms("神了，这是谁的小号？") == ["神了"]
assert terms("你们也是神了") == ["神了"]
assert terms("你是真人还是人机？") == ["人机"]
assert terms("你这个乐子别说话了") == ["乐子"]
assert terms("何意味？为什么要中止？") == ["何意味"]
assert terms("上号吧") == ["上号"]
assert terms("带带我") == ["带带我"]
# 「神经，一言不合就乐子」里的「神经」不能被「神了」蹭到
assert terms("神经，一言不合就乐子") == ["乐子"], terms("神经，一言不合就乐子")

# ------------------------------------------------- 假命中防回归（v1 回测抓到的）
assert terms("大肥鱼经典产生幻觉") == [], terms("大肥鱼经典产生幻觉")
assert terms("糖醋鲤鱼好吃是对的😋经典鲁菜") == []
assert terms("https://tokenrhythm.studio/account/account") == []
assert terms("肝脏不好少喝酒") == []
assert terms("猪肝面好吃") == []
assert terms("这套连招是三连击") == []
assert terms("五星红旗迎风飘扬") == []
assert terms("我给了五星好评") == []

# --------------------------------- 假命中防回归（v2 回测淘汰掉的候选词，原句）
# 这些句子是候选词表在真语料里的**全部**命中处，逐条看都不是梗义，故不入表。
assert terms("更新后领鱼竿，钓鱼去了") == []          # 钓鱼：游戏里真钓鱼
# 「长弓钓鱼真好玩」以前整句零命中，现在 2026-09-05 加了三角洲地图，
# 「长弓」＝长弓溪谷是**真命中**，不是回归。这条断言改成守住它原本的目的：
# 「钓鱼」不许进表。
assert "钓鱼" not in {t for e in module.GLOSSARY for t in e.terms}
assert terms("长弓钓鱼真好玩") == ["长弓溪谷"], terms("长弓钓鱼真好玩")
assert terms("AK,你被我开除了") == []                  # AK：群友的名字
assert terms("昨天到现在被蹲了15次") == []             # 15：次数
assert terms("总1500刀") == []
assert terms("多模态顺手就加了") == []                 # 顺：顺手
assert terms("是顺丰收发货的那种站") == []
assert terms("看来豆包才是宣传的第一大手") == []       # 第一：不是弹幕梗
assert terms("那不废话吗？官方肯定是第一手啊") == []
assert terms("就这个了") == []                         # 就这：不是「就这？」
assert terms("不是，就这个小东西他妈要钱？") == []
assert terms("这下应该不会被带歪了") == []             # 歪了：被带歪
assert terms("所以你得逆天改命") == []                 # 逆天：逆天改命
# v2 新词条自己的假命中防线
assert terms("这是精神损失费") == []                   # 神了：精神
assert terms("神奇的操作") == []                       # 神了：神奇
assert terms("人机交互设计") == []                     # 人机：人机交互
assert terms("过个人机验证") == []                     # 人机：人机验证
assert terms("网上号称第一") == []                     # 上号：网上号称
# 淘汰名单不许再出现在任何词条里
_all_terms = {t for e in module.GLOSSARY for t in e.terms}
for bad in module.REJECTED:
    assert bad not in _all_terms, "淘汰词又进表了：%s" % bad

# --------------------------------------------------- 结构规则
# 纯拉丁词条要词边界
assert terms("friends and words") == []
assert terms("ds 后训练后甲上来了") == ["ds"]
# 666 走同一条边界规则：夹在别的数字里不算
assert terms("1666") == []
assert terms("66678") == []
assert terms("QQ 2774066612") == []
# 666 的金额用法由 avoid 挡
assert terms("这套要666元") == []
assert terms("充了666块") == []
# 蚌埠只在完整短语里才算，地名不算
assert terms("我在蚌埠") == []
# 命中顺序按句子里出现的位置，不按词表顺序
assert terms("降智了，抽卡也没保底")[0] == "降智"
# 单轮上限
assert len(module.matched_entries("肝帝 常驻池 专五 保底 抽卡 降智")) == module.MAX_HITS

# --------------------------------------------------- 注入块形状
block = module.render(module.matched_entries("白嫖token额度"))
assert block.startswith("<group_glossary>") and block.endswith("</group_glossary>")
assert "别复述" in block
# 例句：开了就要出现，并且必须带「别照抄」的措辞
assert "这么用" in block, block
assert "别照抄" in block
assert "「谁叫你平时不白嫖」" in block, block
# 关掉例句时退回纯释义形状，释义仍在
plain = module.render(module.matched_entries("白嫖token额度"), with_usage=False)
assert "这么用" not in plain and "别照抄" not in plain
assert "白嫖" in plain and "额度" in plain
# 风险词条只给释义，任何时候都不带例句
risky = module.render(module.matched_entries("我稍微加一点点破甲词"))
assert "破甲" in risky and "这么用" not in risky, risky
assert module.render(module.matched_entries("用日语发语音说杂鱼")).count("这么用") == 0
assert module.render(module.matched_entries("逆向?")).count("这么用") == 0
# 例句超预算就整体退化成只有释义
tight = module.render(module.matched_entries("白嫖token额度"), budget=1)
assert "这么用" not in tight and "白嫖" in tight
# 固定说明文字别再膨胀：一次注入里它已经占掉一半，超过 70 字就要重新压
_head = block.split("\n")[1]
assert len(_head) <= 70, "注入块说明文字 %d 字，太长了" % len(_head)

# --------------------------------------------------- 审查过的不变量
# 每条词条都必须在真语料里命中过（零命中的死词已删，别再加回来）
assert all(e.terms for e in module.GLOSSARY)
# 只有傲娇一条配两句例句，多了就是在跟 dsh-style 抢活
# （人机原来也是两句，9-09 换成第三人称原话后只剩一句，见下面的自指降级断言）
_two = [e.terms[0] for e in module.GLOSSARY if len(e.usage) > 1]
assert _two == ["傲娇"], _two
# 例句一律不超过 dsh-style 的样本上限 18 字 + 一点余量，太长就不像口语了
assert max(len(u) for e in module.GLOSSARY for u in e.usage) <= 20

# --------------------------------------------------- 自指类词条：第三人称降级（9-09）
class _FakeEvent:
    def __init__(self, text, at=False):
        self.message_str = text
        self.is_at_or_wake_command = at


def rendered(text, at=False):
    event = _FakeEvent(text, at)
    return module.render(module.matched_entries(text), addressed=module._addressed(event))


# 群主原话：在说别的 AI 账号，必须出现「是在说别人」的硬提示
assert "是在说别人" in rendered("主要是那些都是人机啊")
# 第二人称 = 冲机器人来的，不降级（这是旧词条要教的语用，不能一起丢掉）
assert "是在说别人" not in rendered("你是不是人机？")
# 被 @/唤醒同样不降级
assert "是在说别人" not in rendered("人机咋了", at=True)
# 释义本身也必须中性：不许再出现「冲你说这个」这种第二人称引导
_hit = [e for e in module.GLOSSARY if e.terms == ("人机",)][0]
assert _hit.selfref and "冲你说" not in _hit.meaning
assert "那些都是人机" in _hit.meaning
# 别的词条没标 selfref，不受影响
assert "是在说别人" not in rendered("还全是车轱辘废话，水平很差")

print("GLOSSARY_TEST_OK terms=%d usage=%d meaning_chars=%d head=%d"
      % (len(module.GLOSSARY), sum(len(e.usage) for e in module.GLOSSARY),
         sum(len(e.meaning) for e in module.GLOSSARY), len(_head)))
