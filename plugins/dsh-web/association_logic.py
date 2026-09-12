# -*- coding: utf-8 -*-
"""dsh-web 的联想搜索判据（纯函数，便于离线回测）。"""

from __future__ import annotations

import re


# 明显依赖新鲜信息：这些场景不该只靠模型训练时记忆。
_FRESH_RE = re.compile(
    r"(?:最近|刚刚|这两天|这周|本周|现在|目前|今年|最新版|新版本|新模型)"
    r".{0,16}(?:有什么|有啥|出了什么|发生什么|更新了什么|消息|新闻|价格|多少钱|"
    r"活动|变化|进展|怎么样|如何|能不能|还能|可不可以|是否|是不是|还.{0,4}值得|推荐)"
    r"|(?:更新|发布|上线|发售|开服|停服|涨价|降价|倒闭|跑路|官宣|宣布)"
    r".{0,8}(?:了吗|了没|了|没有|吗|啥|什么)"
    r"|(?:还|现在).{0,8}(?:能用|可用|活着|更新|维护|值得|多少钱)"
)

# 传闻、陌生事物、出处、横向选择：适合顺手查一眼，再自然补一条关联。
# 不单收“好像/似乎/真的假的”——群里大量是“好像也是”这种无对象接话。
_DISCOVERY_RE = re.compile(
    r"(?:听说|据说|网传|什么来头|哪来的|出处|起源|怎么火的|为什么火|啥情况|咋回事)"
    r"|(?:推荐|哪个好|选哪个|有什么区别|有啥区别|有什么差别|有啥差别|对比|横评|值不值得|值得买吗|值得玩吗)"
    r"|(?:是什么|什么意思|什么梗|是谁|干嘛的|做什么的)\s*[？?。！!]*$"
)

# 至少要有一点可供搜索的主题形状，避免“这是真的吗”这种无对象句子乱搜。
_TOPIC_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9_.+-]{2,}"
    r"|[\u4e00-\u9fff]{2,}(?:游戏|软件|模型|公司|平台|网站|插件|版本|电影|动画|漫画|梗|事件|功能|手机|显卡|服务器)"
)

# 本质是让机器人办事，不是了解外部信息。除非同时命中新鲜信息，否则不抢着搜。
_TASK_RE = re.compile(
    r"(?:帮我|给我|替我|麻烦).{0,8}(?:写|改|修|生成|画|部署|配置|安装|删除|总结|翻译)"
    r"|(?:这段|这个).{0,6}(?:代码|配置|文件).{0,8}(?:怎么改|报错|修一下)"
)

_PREFIX_RE = re.compile(
    r"^\s*(?:话说|对了|顺便|我问下|问一下|想问下|听说|据说|网传)\s*[，,:：]?\s*"
)
_MENTION_RE = re.compile(r"@[^\s(（]{1,24}(?:\([^)]*\)|（[^）]*）)?\s*")


def build_association_query(text: str, max_chars: int = 80) -> str:
    """清掉聊天填充词和 QQ 展示型 @，保留能直接交给搜索引擎的自然问句。"""
    query = _MENTION_RE.sub("", str(text or ""))
    query = _PREFIX_RE.sub("", query, count=1)
    query = re.sub(r"\s+", " ", query).strip(" 　,，。!！?？:：、")
    return query[:max_chars].rstrip()


def decide_associative_search(
    text: str,
    *,
    explicit: bool = False,
    has_link: bool = False,
    roll: float = 0.0,
    rate: float = 0.5,
    min_len: int = 4,
) -> tuple[bool, str, str, str]:
    """返回 (触发, 查询, 类型, 原因)。

    fresh（强时效）不抽样；discovery（探索联想）按 rate 抽样，避免每轮都搜形成
    新的机器规律。这里仅决定是否预搜，搜索结果仍须经过 dsh-web 原有出口审核。
    """
    raw = str(text or "").strip()
    if explicit:
        return False, "", "", "已有显式搜索主路径"
    if has_link:
        return False, "", "", "已有链接主路径"
    if not raw or raw.startswith("/"):
        return False, "", "", "空消息或命令"
    if len(raw) < min_len:
        return False, "", "", "消息太短"

    query = build_association_query(raw)
    if len(query) < min_len:
        return False, "", "", "没有可用查询"

    if _FRESH_RE.search(raw):
        return True, query, "fresh", "时效信息"
    if _TASK_RE.search(raw):
        return False, "", "", "办事请求"
    if not _DISCOVERY_RE.search(raw):
        return False, "", "", "没有联想搜索信号"
    # 明确的“什么梗/哪来的/推荐比较”本身已带对象；纯代词问句必须另有主题形状。
    vague = re.fullmatch(r"(?:这|这个|那|那个|它|他|她).{0,5}(?:是什么|是谁|啥情况|咋回事|真的假的)", query)
    if vague and not _TOPIC_RE.search(query):
        return False, "", "", "缺少搜索对象"
    if roll >= max(0.0, min(1.0, rate)):
        return False, "", "", "探索抽样未中"
    return True, query, "discovery", "探索联想"
