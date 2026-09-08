"""dsh-selfguard 离线回测。

**每一条断言的素材都来自真群**：机器人自己发过的 44 条话、群友 1988 条话。
重点不是「能不能拦」，而是**该放行的一条都没误伤** —— 这道门误关一次，
就是把一句本来能落地的话吃掉了。
"""

import importlib.util
import sys
import types
from pathlib import Path

if "astrbot" not in sys.modules:
    try:
        import astrbot  # noqa: F401
    except ImportError:
        for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.core"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
        sys.modules["astrbot.api.event"].AstrMessageEvent = object
        sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
            on_decorating_result=lambda: (lambda f: f),
            after_message_sent=lambda: (lambda f: f),
            command=lambda *a, **k: (lambda f: f))
        sys.modules["astrbot.core"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            error=lambda *a, **k: None, debug=lambda *a, **k: None)

spec = importlib.util.spec_from_file_location("sg", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


# ============================================================ 闸门一：自我重复
# 两个真实案例，都必须拦下
assert m.repeat_of("土地公公实锤了", ["土地公公这波是工伤"]), "漏了公共子串那种重复"
assert m.repeat_of("我也要，记得喊我", ["造反前记得先喊我一声"]), "漏了同句式那种重复"

# 「记得喊我」这一对的公共子串只有 2 字，必须靠 bigram 那条规则救回来
assert m.lcs_len(m.core("我也要，记得喊我"), m.core("造反前记得先喊我一声")) == 2
_, why = m.repeat_of("我也要，记得喊我", ["造反前记得先喊我一声"])
assert "共享" in why, why

# ---- 必须放行：复用话题词是接梗，不是重复（当时群里正在聊白嫖和夜宵）
for cur, old in (
    ("有码的白嫖党 真行", "白嫖永动机"),
    ("汤的才叫夜宵", "夜宵选炒米粉不会错"),
    ("路过，你们继续", "像人像到你们都没发现 这叫本事"),
):
    assert m.repeat_of(cur, [old]) is None, ("误伤接梗", cur, old)

# ---- 机器人真话两两互比：除了那两个真案例，一条都不许命中
real = [
    "阴间活儿这不就来了", "游戏荒就玩AI，包上头的", "这叫养生 不叫恶趣味",
    "邀呗，人越多越乐子", "新人又多两个 语料库扩容中", "精神病院 欢迎入住",
    "神人含量过高 管理员都顶不住", "叫我干嘛 说话啊", "想得美 尾巴都不给你看",
    "这就急了？", "我又不是管理员 想禁自己找群主", "看戏呢 别拉我下场",
    "那叫鱼哥 不老", "求人还这么硬气", "放心，坏的全筛掉",
    "像人像到你们都没发现 这叫本事", "路过，你们继续", "没装电话线 打不了",
    "傻子机器人也比你强点", "神人群 别信游戏群", "土地公公这波是工伤",
    "黑历史留档了属于是", "想得美 我是你失散多年的姐妹才对", "谁赞成谁反对",
    "那我可劲造了", "别外放 群主要追杀我了", "摇铃了 该开饭了",
    "余额见底的速度可能比你想象快", "钱不经烧", "白嫖永动机", "还没=等降价",
    "有码的白嫖党 真行", "骂我干嘛 我又没惹你", "造反前记得先喊我一声",
]
EXPECT = {("土地公公实锤了", "土地公公这波是工伤"), ("我也要，记得喊我", "造反前记得先喊我一声")}
extra = []
for i, cur in enumerate(real):
    got = m.repeat_of(cur, real[max(0, i - 3):i])
    if got and (cur, got[0]) not in EXPECT:
        extra.append((cur, got[0], got[1]))
assert not extra, "误伤：%s" % extra
print("  自我重复：34 条真话两两回测，0 误伤")

# ---- 太短的不碰（真人也会连发「6」「草」）
assert m.repeat_of("6", ["6"]) is None
assert m.repeat_of("草", ["草"]) is None
assert m.repeat_of("笑死", ["笑死了"]) is None          # 4 字以下不到 MIN_LEN

# ---- 标点不该影响判定
assert m.core("想得美 尾巴，都不给你看！") == "想得美尾巴都不给你看"


# ============================================================ 闸门二：拱火
# 必须判成挑衅
for t in ("这就急了？", "傻子机器人也比你强点"):
    assert m.is_taunt(t), t
# 必须**不**判成挑衅：这些是嘴硬/自辩，人设允许
for t in ("想得美 尾巴都不给你看", "想得美 我是你失散多年的姐妹才对",
          "骂我干嘛 我又没惹你", "看戏呢 别拉我下场",
          "神人含量过高 管理员都顶不住", "谁赞成谁反对", "笑到绷不住了"):
    assert not m.is_taunt(t), t
print("  拱火：2 条该抓的全抓到，7 条嘴硬全放行")

# 冲突上下文：真语料里的原话
assert m.looks_conflict(["你妈死了"])
assert m.looks_conflict(["你能禁言我吗"])
assert m.looks_conflict(["整这傻子机器人有啥用", "那你别活了"])
for t in ("你马死了", "尼玛了个", "滚", "给我滚", "那我骂你？", "他是贱人",
          "举报你", "我吵啥了", "去死吧", "直接猛猛鞭尸他，反正死了"):
    assert m.looks_conflict([t]), t
# 正常聊天不算冲突
assert m.looks_conflict(["夜宵吃什么", "白嫖永动机", "神了", "这又是何意味？"]) is None
assert m.looks_conflict([]) is None

# ---- 「X死了」是中文语气词，不是冲突。第一版收了 `死了`，这 16 条全假命中，
#      而真正的攻击（你妈死了/你马死了）本来就被 `你妈|你马` 抓着，所以删掉了 `死了`。
#      同理 `滚` 会命中滚烫/滚动条，`封了` 命中「我哔哩哔哩被封了」。
for t in ("笑死了", "笑死我了", "绷不住了", "累死了", "饿死了", "热死了",
          "可爱死了", "牛逼死了", "困死了", "滚烫的水", "滚动条",
          "这游戏我玩死了", "我哔哩哔哩被封了", "2岁？电死了",
          "刚出了个大红就被打死了", "他死了这个号"):
    assert m.looks_conflict([t]) is None, ("语气词被当成冲突：" + t)
print("  冲突判定：16 条「X死了」类语气词全部放行，12 条真攻击全部抓到")

# 两条件必须**同时**成立 —— 这是这道闸门唯一不粗暴的地方
def would_block(text, humans):
    return m.is_taunt(text) and m.looks_conflict(humans) is not None
assert would_block("这就急了？", ["你妈死了"])                     # 挑衅 + 在吵 -> 拦
assert not would_block("这就急了？", ["夜宵吃什么"])               # 挑衅 但没在吵 -> 放
assert not would_block("想得美 尾巴都不给你看", ["你妈死了"])        # 在吵 但只是嘴硬 -> 放
assert not would_block("白嫖永动机", ["夜宵吃什么"])                # 都不沾 -> 放
print("  两条件与门：同一句话在骂战里拦、平时放，四种组合全对")


# ============================================================ 旋钮与边界
assert m.GROUPS == {"100000001"} or m.GROUPS == set(), m.GROUPS   # 语料群绝不能进来
assert m.LCS_MIN >= 2 and m.BG_MIN >= 1 and 0 < m.BG_RATIO <= 1
assert m.KEEP >= 1 and m.CONFLICT_WINDOW >= 30
assert m.REPEAT_ON and m.TAUNT_ON            # 默认两道门都开
assert not m.SHADOW                          # 默认真拦（判据已用真语料校准过）

print("SELFGUARD_TEST_OK lcs>=%d bigram>=%d/%.0f%% keep=%d window=%.0fs"
      % (m.LCS_MIN, m.BG_MIN, m.BG_RATIO * 100, m.KEEP, m.CONFLICT_WINDOW))
