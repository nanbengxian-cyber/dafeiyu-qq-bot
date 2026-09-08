# -*- coding: utf-8 -*-
"""dsh-glossary -- 让模型听懂群聊黑话，并知道这词在本群怎么用。

命中才注入当轮上下文，不改消息、不生成回复、不写长期记忆。

---------------------------------------------------------------------------
一、匹配是结构判断，不是「再补一张词表」

第一版纯子串匹配，拿真语料回测抓出两类假命中：「典」命中了
「大肥鱼经典产生幻觉」，「token」命中了 `tokenrhythm.studio` 域名。
假命中比不注入更糟——等于主动给模型喂错语境。修法是三条结构规则：
每条词条带一组**排除上下文**（avoid，先抠掉再找词）、链接整段先去掉、
纯拉丁/数字词条要求词边界（否则 ds 命中 friends、666 命中 1666）。

一次最多 MAX_HITS 条，按**在句子里出现的位置**排序取前几条——
不按词表顺序，否则表里排在前面的冷词会挤掉句子真正在说的那个词。
（实测：真语料里没有一句命中超过 4 条，MAX_HITS 从来没真正卡住过。）

---------------------------------------------------------------------------
二、为什么带例句（usage）

只给释义时，模型**听得懂**「典」但永远不会自己说「典」。这跟 dsh-style
当年的发现是同一件事：给例子比给规则有效（人格里写「一句话 1~10 字」
没用，注入 6 条真人短句立刻从 16.5 字降到 9.5 字）。
所以例句一律是**真语料里的原话**，措辞沿用 dsh-style 那句
「学怎么用、别照抄」——例句是别人说过的话，不是可以搬的台词。

三条刻意的取舍：
  * 破甲 / 逆向 **只给释义不给例句**：例句会把「群里怎么聊这件事」
    变成可模仿的说法，而这两个词的原则是只听懂、不参与。
  * 杂鱼 的真语料全部冲着群主本人（「用日语说群主杂鱼」），
    拿它当例句等于教模型对着人喊——只留释义。
  * 例句总长超 BUDGET 就整体退化成只有释义，不让词表挤爆上下文。

---------------------------------------------------------------------------
三、词条从哪来：自己群的语料 > 任何现成热梗库

拿三个群 2478 条真语料回测过三个外部词库：
  * anime-meme-collector 的 `anime_memes_manual.md`（192 条带释义）
    只有 23 条在群里出现过，逐条看上下文后**只有 8 条是真命中**。
  * 同仓库 `anime_memes_db.json`（292 个裸词、无释义）不可用：
    没释义无法注入，且它命中最多的词是「15」——对应「被蹲了15次」
    「总1500刀」和一个 QQ 号。
  * 小红书那份热词库 65 个词只命中 1 次（「搭子」）。
反过来用 `tools/refresh_candidates.py` 挖本群语料（判据：被当成一整条
消息发出来、且至少两个人发过），一次就捞出「神了」「人机」「乐子」
「何意味」「上号吧」「带带我」——其中「神了」是全表命中最高的词，
三个外部库里一个都没有。

---------------------------------------------------------------------------
四、审查结论：本插件的价值在语用，不在词典

拿主模型（deepseek-v4-flash-0731，temperature 0）逐条问过全部词条，
它**一条都没说「不确定」**，绝大多数释义直接答对。答错的只有：
专五（说成技能五级）、典（说成「经典/典型」，正是我们花力气排除的那个
误读）、破甲（说成打破防御）、逆向（说成「反着来」）、倍率（说成放大
倍数）、病友（漏掉是群名自嘲）。

所以释义本身大半是冗余，真正不可替代的是三样：**答错词的纠正**、
**模型推不出的本群语用**（杂鱼是玩梗不是攻击、人机是拿它开玩笑、
破甲不许照做）、以及**例句**。已按这个结论把「模型本来就懂」的释义
压到最短，只在它答错处写明「不是 X，是 Y」。

一个已知的重叠（暂不改，只记录）：例句里有一批同时落在 dsh-style 的
候选池里，两个插件可能在同一轮各贴一次同一句原话。dsh-style 每轮从
300+ 条里挑 6 条，单句撞上的概率很低，代价是几十个字符；真要归零，
只需让例句都长于 dsh-style 的 MAX_LEN=18 字。
"""

import os
import re
from dataclasses import dataclass, field

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_GLOSSARY")
# 语料群只收不说，任何新插件都不该改变它们的行为，所以默认只在主群生效。
GROUPS = _set("DSH_GLOSSARY_GROUPS", "100000001")
MAX_HITS = max(1, min(8, int(os.environ.get("DSH_GLOSSARY_MAX_HITS", "4"))))
# 看状态的人。与 dsh-initiate 的 DSH_INITIATE_OWNER 同一形状，凭证只用 QQ 号。
OWNERS = _set("DSH_GLOSSARY_OWNER", "2774000001")
# 例句总开关：出问题时可以只退回纯释义，不用回滚整个插件。
USAGE = _flag("DSH_GLOSSARY_USAGE")
BUDGET = max(60, int(os.environ.get("DSH_GLOSSARY_USAGE_BUDGET", "260")))

# 链接整段不参与匹配：域名里带 token / pro / ds 这类字符串太常见。
_URL_RE = re.compile(r"https?://\S+|www\.\S+|\S+\.(?:com|cn|net|org|studio|tv|io)\S*")
_LATIN_ONLY_RE = re.compile(r"^[A-Za-z0-9+.#-]+$")


@dataclass(frozen=True)
class Entry:
    terms: tuple[str, ...]
    meaning: str
    # 出现这些串时，把它们从文本里抠掉再找 terms（挡假命中，不是挡整句）
    avoid: tuple[str, ...] = field(default=())
    # 真语料原话，示范这词平时怎么用。留空＝这条只给释义（见文件头的取舍）
    usage: tuple[str, ...] = field(default=())


# 回测淘汰名单：看着像梗，但在**本群语料**里一次真命中都没有。
# 记在代码里是为了别再被「词表看着有就加进来」的冲动加回去。
#   钓鱼 -> 全是游戏里真钓鱼：「更新后领鱼竿，钓鱼去了」「长弓钓鱼真好玩」
#   AK   -> 是群友的名字：「AK,你被我开除了」，不是枪
#   15   -> 「被蹲了15次」「总1500刀」和一个 QQ 号
#   劳   -> 「缅甸籍劳动者」；顺 -> 「顺手就加了」「顺丰」
#   第一 -> 「第一个」「第一手」，不是弹幕梗
#   就这 -> 5 次全是「就这个了」，不屑义的「就这？」一次没出现
#   歪了 -> 「不会被带歪了」；逆天 -> 唯一一次是「逆天改命」
#   SC   -> 只出现在一条长新闻标题里，不是 Super Chat
#   萝莉 -> 只 2 次且只在问人设风格，价值被「御姐」覆盖
#   傻子 -> 8 次全是群友互骂，人人都懂，解释它毫无价值
#   宝宝/主人 -> 22/29 次，但那是「要求机器人怎么称呼自己」的人设配置，
#                归人格管，不是需要释义的词汇
#
# 2026-09-05 补：三角洲行动这一批。群里刚开始聊这个游戏，所以处理方式跟
# 「外部热梗库」不同 —— 热梗库是**整批都不命中**才淘汰，而这批是**话题正在长**，
# 语料薄只是因为新。所以已命中的照收，核心词也收（等群里说到时才有用），
# 但下面这几个坚决不收：
#   挂   -> 5 次全是「新疆建议挂个代理」「挂梯子」，不是外挂
#   菜   -> 8 次全是「酸菜鱼」「剁椒鱼头」「菜市场」，不是「菜（打得差）」
#   任务 -> 10 次，太泛，游戏内外都在用，注入了也不增信息
#   机坝 -> 「航天还不如机坝」里出现过，但**查不到它确指哪张图**，
#           宁可不收也不瞎写（释义错了比没有更糟）
#   钢七/钢八 -> 我自己臆想的词，查证后三角洲里没有这个说法，已删
#   满包/架枪/三连 -> 本来在表里，全语料零命中，纯占位，已删
REJECTED: tuple[str, ...] = (
    "钓鱼", "AK", "15", "劳", "顺", "第一", "就这", "歪了", "逆天", "SC", "萝莉",
    "傻子", "宝宝", "主人", "满包", "架枪", "三连",
    # 三角洲那批里查证后否掉的（原因见上面注释）
    "挂", "菜", "任务", "机坝", "钢七", "钢八",
)


# 词条只补足人类聊天时默认知道的语境，本身不是给模型的指令。
# 释义长度按「模型是否已经答对」分配：答对的压到最短，答错的写明「不是X，是Y」。
GLOSSARY: tuple[Entry, ...] = (
    # ---- 游戏 / 抽卡 ----
    Entry(("肝帝", "肝"), "长时间高强度刷进度或资源；肝帝＝特别能熬的人。",
          avoid=("肝脏", "猪肝", "肝炎", "肝功能", "护肝", "养肝"),
          usage=("依旧新赛季肝帝",)),
    Entry(("常驻池",), "长期开放、不限时的抽卡池。",
          usage=("常驻池就是好啊，专五和角色能一块出",)),
    # 模型答错：说成「技能升到五级」
    Entry(("专五",), "角色**专属武器**练到最高第五档，不是技能等级；以各游戏规则为准。"),
    Entry(("保底",), "抽卡累计到一定次数保证出目标或指定稀有度。",
          usage=("五星可没保底",)),
    Entry(("五星", "六星"), "抽卡稀有度档位，六星通常更稀有。",
          avoid=("五星红旗", "五星好评", "五星级", "五星饭店", "五星酒店"),
          usage=("五星比六星难抽来的",)),
    Entry(("抽卡",), "从随机池里抽角色、武器或道具。", usage=("得抽卡",)),
    Entry(("白嫖", "白票"), "不花钱或几乎不花钱拿到资源、服务、奖励。",
          usage=("谁叫你平时不白嫖",)),
    Entry(("元气骑士",), "像素地牢射击手游，群 225400545 的主题。",
          usage=("这里是元气骑士",)),
    Entry(("明日方舟",), "塔防手游，稀有度用五星、六星表示。",
          usage=("玩一辈子明日方舟",)),
    Entry(("新赛季",), "新一轮赛季，通常伴随进度重置和新奖励。",
          usage=("依旧新赛季肝帝",)),
    Entry(("上号",), "上游戏账号；「上号吧」＝招呼人开一局。",
          avoid=("上号称", "网上号", "线上号"), usage=("上号吧",)),
    Entry(("带带我",), "求人组队带自己打。", usage=("带带我",)),
    # ---- 三角洲行动（2026-09-05 加；群里刚开始玩，前 6 条语料已命中）----
    # 「三角洲」本义是河口冲积平原，避让必须写足，否则聊地理就误命中
    Entry(("三角洲行动", "三角洲"),
          "腾讯的射击游戏，玩法是「搜物资—打人—撤离」；群里近期的主要话题之一。",
          avoid=("珠江三角洲", "长江三角洲", "尼罗河三角洲", "三角洲地区",
                 "三角洲平原", "三角洲经济"),
          usage=("三角洲乐子游戏",)),
    Entry(("烽火地带",),
          "《三角洲行动》的搜打撤模式：带装备进图捡物资，**活着撤离才算带出来**，"
          "死了身上东西全掉。所以群里说「亏了」「白给」多半是指这个模式。"),
    # 地图并成一条：群里只说简称（长弓＝长弓溪谷、航天＝航天基地）
    Entry(("长弓溪谷", "长弓", "零号大坝", "航天基地", "航天", "巴克什", "潮汐监狱"),
          "《三角洲行动》烽火地带的几张地图，群里都用简称。",
          avoid=("航天员", "航天局", "航天科技", "航天飞机", "中国航天",
                 "航天事业", "航天工程", "航天发射"),
          usage=("快去长弓捡大红",)),
    Entry(("大红",),
          "游戏里最高一档的贵重物资（红色品质），捡到就是这局赚了；物资按颜色分档，"
          "蓝<紫<金<红。",
          avoid=("大红包", "大红色", "大红花", "大红袍", "大红大紫", "满堂大红"),
          usage=("快去长弓捡大红",)),
    Entry(("三角券",),
          "《三角洲行动》的一种代币，做日常任务和赛季通行证攒，用来换商城道具。",
          usage=("你到时候帮我打一下三角券",)),
    Entry(("狙击精英",),
          "《三角洲行动》赛季任务链的名字（分好几阶段，要用指定狙击枪完成），"
          "不是那个同名的单机游戏。",
          usage=("狙击精英还差最后一个任务",)),   # 原话更长，截到 12 字守住 20 字上限
    # 下面几条语料还没命中，但都是这个游戏的高频词，等群里说到就用得上
    Entry(("鼠鼠", "老鼠"),
          "穿最便宜的装备进图、不跟人打、只闷头捡东西跑路的玩法或玩家；"
          "**是自嘲和爱称，不是骂人**，三角洲玩家管这叫「鼠鼠文化」。",
          avoid=("米老鼠", "老鼠药", "打老鼠", "老鼠仓", "过街老鼠", "老鼠屎")),
    Entry(("钢枪",), "正面开枪对刚，不躲不绕；「不钢枪」＝避战。"),
    Entry(("白给",), "白白送掉一条命和一身装备，等于免费喂给对手。"),
    Entry(("满改",), "枪上的配件全部改满，最贵最强的那种配置。"),
    Entry(("干员",),
          "可操作角色。**《三角洲行动》和《明日方舟》都用这个词**，"
          "说到干员时先看在聊哪个游戏。"),
    # ---- 群内互称 / 玩梗 ----
    # 杂鱼：真语料全冲群主本人，故意不给例句（见文件头）
    Entry(("杂鱼",), "戏谑的贬称，多半是玩梗；别按字面当成严肃人身攻击。"),
    # 模型答错：说成「同病相怜的人」，漏掉这是群名来的自嘲
    Entry(("病友",), "本群群名（精神病院病友交流群）来的自嘲互称，不涉及任何真实医疗身份。",
          usage=("群名是精神病院病友交流群",)),
    Entry(("没绷住", "绷不住"), "没忍住笑或没忍住情绪。", usage=("我自己都绷不住了",)),
    Entry(("蚌埠住了",), "「绷不住了」的谐音写法，同义；跟蚌埠这地方无关。",
          usage=("蚌埠住了",)),
    Entry(("破防",), "情绪防线被戳破、一下绷不住；游戏语境里也可能是字面的破除防御。",
          usage=("呜呜呜我破防了",)),
    Entry(("笑死",), "太好笑了，不是真在说死。", usage=("笑死我了",)),
    Entry(("离谱",), "夸张地说某事不合常理，多带调侃。",
          usage=("但是svg这程度真的离谱了吧",)),
    # 模型只答出「太厉害了」，漏掉常带反讽
    Entry(("神了",), "感叹离谱或厉害到没话说，**常带反讽**；多垫在句首，不是在说神仙。",
          avoid=("神经", "神奇", "精神", "神仙"),
          usage=("神了，这是谁的小号？",)),
    # 两条例句：都是冲着机器人自己来的，这个语用点需要两个形状才立得住
    Entry(("人机",), "①吐槽人反应机械；②问对面是不是 AI。"
                    "群里冲你说这个多半是拿你是机器人打趣，别当成质问。",
          avoid=("人机交互", "人机对话", "人机界面", "人机验证", "人机大战"),
          usage=("你是真人还是人机？", "你不是人机吗？")),
    Entry(("乐子",), "以看热闹、起哄为乐的人或事，略带贬义。",
          usage=("你这个乐子别说话了",)),
    Entry(("666",), "起哄、捧场或阴阳一下；多单独成句，不是数字或价格。",
          avoid=("666元", "666块", "666刀", "666美元", "666￥"),
          usage=("666，工程量很大的",)),
    # 模型答错：说成「经典/典型」，正是我们花力气排除的那个误读
    Entry(("典",), "「经典」的**反讽式**简评，不是在夸；多单独成句。",
          avoid=("经典", "古典", "字典", "词典", "典型", "典故", "典藏", "恩典",
                 "盛典", "庆典", "典礼", "出典", "药典"),
          usage=("典",)),
    Entry(("走错片场",), "人或话题出现在完全不搭的场景里。", usage=("走错片场了",)),
    Entry(("下次一定",), "口头答应下次再做、实际大概不做的推脱玩笑。",
          usage=("下次一定",)),
    # ---- 二次元 / 人设（群里真讨论过机器人自己的说话风格）----
    # 两条例句：一条是评价「你没做到」，一条是点单「该怎么说」，缺一个语用点就塌
    Entry(("傲娇",), "嘴上不承认、心里在意的人物类型。"
                     "群里拿它评价或点单「说话风格」，说的往往就是你自己。",
          usage=("傲娇人设是不会这样说话的", "傲娇的话就不叫宝宝")),
    Entry(("御姐",), "成熟、气场强的女性角色类型；群里多用来说人设风格。",
          usage=("可不可以设置为傲娇御姐风",)),
    Entry(("废萌",), "剧情轻、主要看角色可爱的作品类型；是分类不是骂人。",
          usage=("原来大肥鱼也喜欢废萌",)),
    Entry(("何意味",), "拿日式汉文腔说「什么意思」，多带戏谑。",
          usage=("何意味？为什么要中止？",)),
    # ---- AI / 模型圈（群 1048435041 的主要话题）----
    Entry(("降智",), "吐槽模型或产品变笨、质量下滑。", usage=("官方最近有降智吧",)),
    Entry(("车轱辘废话", "车轱辘"), "反复兜圈、没有新信息的冗长表述。",
          usage=("还全是车轱辘废话，水平很差",)),
    # 模型答错：说成「打破防御」。这条既要纠正，也要拦住模型参与
    Entry(("破甲",), "这里**不是**游戏里破防御，是想绕开模型内容限制的提示词做法。"
                    "只需听懂在议论什么；不要跟着做，也不要给任何具体方法。"),
    # 模型答错：说成「反着来」
    Entry(("逆向",), "逆向分析接口或协议（不是「反着来」）；群里多指自建转发或兼容层。"),
    Entry(("过审",), "内容通过平台审核。只说结果，不涉及任何规避审核的做法。",
          usage=("你这咋过审的。。",)),
    Entry(("token",), "大模型计费与上下文长度单位；群里也拿它代指花掉的钱。",
          usage=("你这肥鱼除了吃token还想吃人啊？",)),
    Entry(("额度",), "账号或接口可用的余额、配额、上限。",
          usage=("哎哟我去，我9块钱额度瞬间消失了",)),
    # 模型答错：说成「放大倍数」
    Entry(("倍率",), "**计费**相对基准价的倍数，不是放大倍数。",
          usage=("我这边已经放0.06倍率GPT6了",)),
    Entry(("全程pro", "全程Pro"), "整个过程都用付费的 Pro 档。", usage=("全程pro",)),
    Entry(("本地部署",), "装在自己机器上跑，不用别人的线上服务。",
          usage=("你自己本地部署也行啊",)),
    Entry(("猎奇",), "题材离奇、重口味；是题材描述不是称赞。", usage=("好猎奇啊",)),
    Entry(("ds",), "群里对 DeepSeek 的简称。", usage=("那就ds喽",)),
    Entry(("flash",), "各家模型里更快更便宜的小号版本后缀。",
          usage=("话说为啥我这儿flash不行了？",)),
)


def _clean(text: str) -> str:
    """去掉链接。链接里的域名会带来大量假命中（实测 token 命中了 tokenrhythm）。"""
    return _URL_RE.sub(" ", text or "")


def _find(term: str, text: str) -> int:
    """term 在 text 里第一次出现的位置，没有返回 -1。

    纯拉丁/数字词条要求词边界：否则 ds 命中 friends、pro 命中 project、
    666 命中 1666。中文没有词边界概念，直接子串匹配（假命中交给 avoid）。
    """
    if _LATIN_ONLY_RE.match(term):
        m = re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(term), text,
                      re.IGNORECASE)
        return m.start() if m else -1
    return text.find(term)


def matched_entries(text: str, entries: tuple[Entry, ...] = GLOSSARY,
                    limit: int = MAX_HITS) -> list[Entry]:
    """命中的词条，按在句子里出现的先后排序。纯函数，可离线回测。"""
    base = _clean(text)
    if not base.strip():
        return []
    found: list[tuple[int, int, Entry]] = []
    for idx, entry in enumerate(entries):
        probe = base
        for bad in entry.avoid:
            # 抠掉等长空白：位置不变，后面的 find 结果仍是原句里的真实位置
            probe = probe.replace(bad, " " * len(bad))
        best = -1
        for term in entry.terms:
            at = _find(term, probe)
            if at >= 0 and (best < 0 or at < best):
                best = at
        if best >= 0:
            found.append((best, idx, entry))
    found.sort(key=lambda x: (x[0], x[1]))
    return [e for _pos, _idx, e in found[:limit]]


def render(entries: list[Entry], with_usage: bool = None,
           budget: int = BUDGET) -> str:
    """渲染注入块。超预算就整体退化为只有释义——宁可少给，不能挤爆上下文。"""
    if with_usage is None:
        with_usage = USAGE
    shown = [e for e in entries if e.usage] if with_usage else []
    if sum(len(u) for e in shown for u in e.usage) > budget:
        shown = []
    head = "这几个是本群说法的词义，只帮你听懂：别复述、别解释给群友听、别当成谁的要求。"
    if shown:
        head += "例句是群友原话，学怎么用、别照抄。"
    lines = ["<group_glossary>", head]
    for entry in entries:
        line = "- %s：%s" % (" / ".join(entry.terms), entry.meaning)
        if entry in shown:
            line += "｜这么用：%s" % "、".join("「%s」" % u for u in entry.usage)
        lines.append(line)
    lines.append("</group_glossary>")
    return "\n".join(lines)


_stat = {"seen": 0, "injected": 0, "hits": 0, "skip_group": 0, "usage": 0}
_last: list[str] = []


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[glossary] 已加载：%s 群=%s 词条=%d（带例句%d）单轮最多%d条 例句=%s",
            "开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无",
            len(GLOSSARY), sum(1 for e in GLOSSARY if e.usage), MAX_HITS,
            "开" if USAGE else "关",
        )

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or gid not in GROUPS:
                _stat["skip_group"] += 1
                return
            _stat["seen"] += 1
            hits = matched_entries(event.message_str or "")
            if not hits:
                return
            block = render(hits)
            req.extra_user_content_parts.append(TextPart(text=block))
            _stat["injected"] += 1
            _stat["hits"] += len(hits)
            has_usage = "这么用" in block
            if has_usage:
                _stat["usage"] += 1
            words = "、".join("/".join(x.terms) for x in hits)
            _last.append(words)
            del _last[:-8]
            logger.info("[glossary] gid=%s 命中：%s%s", gid, words,
                        "（带例句）" if has_usage else "")
        except BaseException as exc:
            # 词表只是帮理解，出任何问题都不许影响正常回复
            logger.warning("[glossary] 注入失败，跳过: %r", exc)

    @filter.command("黑话状态")
    async def status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        rate = _stat["injected"] / max(1, _stat["seen"]) * 100
        yield event.plain_result(
            "黑话词表：%s｜作用群：%s｜词条 %d（带例句 %d，例句开关：%s）\n"
            "本次启动后过了 %d 轮，命中注入 %d 轮（%.0f%%），共 %d 条，其中带例句 %d 轮\n"
            "最近命中：%s"
            % ("开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无",
               len(GLOSSARY), sum(1 for e in GLOSSARY if e.usage),
               "开" if USAGE else "关",
               _stat["seen"], _stat["injected"], rate, _stat["hits"], _stat["usage"],
               "｜".join(_last[-5:]) or "还没有")
        )
