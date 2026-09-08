# -*- coding: utf-8 -*-
"""dsh-typo -- 偶尔打个同音错别字，并且有时候会自己纠正。

---------------------------------------------------------------------------
为什么

一个号连着发几千条，一个错别字都没有，这本身就是 AI 标记。真人用拼音输入法
打字会选错同音字（「很好」打成「狠好」、「饿死了」打成「饿似了」），
而且发现之后常常补一条「*很好」。

参考 MaiBot（github.com/Mai-with-u/MaiBot）的两处：
`src/chat/utils/typo_generator.py`（拼音+字频造错别字）和 changelog 里的
「错别字纠正消息引用，可在回复后处理中配置开关和引用概率」。

---------------------------------------------------------------------------
为什么不抄它的实现：用固定表而不是 pypinyin 现算

MaiBot 用 pypinyin + jieba 字频动态生成，`error_rate=0.3`（每字 30%）。
两条都没照抄：

  1. **容器里没有 pypinyin**（只有 jieba），而这个功能不值得为它加依赖 ——
     加了还会在镜像重建时丢。
  2. 更重要的是**可控性**：动态生成意味着输出集合是开放的，可能造出生僻字、
     或者恰好拼成另一个有明确含义（甚至难听）的词。这个号在一个 94 人的真群里
     说话，我宁可要一张**每一对都人工看过**的表。

所以这里是一张手写的同音混淆表，覆盖真语料里出现最频繁的那些字
（是407 的378 有224 没187 就159 要144 好139 他124 …），
每一对都是拼音输入法真会犯的错，错字一侧全部人工过目过。

---------------------------------------------------------------------------
概率怎么定

MaiBot 的 30%/字 是它自己的口味，放到这里等于满屏错字。
拿主群 1967 条真语料找过人类的同音错别字，能确认的极少
（「作者要饿似了」＝饿死了，是唯一一条明确的；「难胃炎」是故意谐音玩梗
不算打错）。所以真人的错字率其实很低。

定成 **每条消息 6% 概率、且一条最多错一个字**。按机器人的发言量大约
十几条里出现一次 —— 看得见，但不会像坏了。嫌多就调 DSH_TYPO_RATE。

---------------------------------------------------------------------------
几条硬规则

  * 只改模型生成的回复。指令回显（/黑话状态 之类）是给人看的清单，
    错一个字就变成误导。
  * 一条最多错一个字。真人不会在一句短话里错三个字。
  * 保护名单：黑话词条、机器人和群主的名字、贴纸标记、CQ 码、链接、@、
    数字和拉丁串一律不碰。把「新赛季」写成「心赛季」不是拟人，是制造混乱。
  * 纠正消息只在**框架分段回复开着**时才发。关着的时候同一个 chain 里的两个
    Plain 会被拼进同一条消息，变成「狠好*很好」—— 比不改还差。
    这条坑是 dsh-human 踩过并写下来的（见它的 _seg_reply_on）。
  * 前导空白原样保留：dsh-mention 可能已经在正文前补了空格，
    两个插件都挂 on_decorating_result，谁先跑都可能。
"""

import os
import random
import re

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_TYPO")
GROUPS = _set("DSH_TYPO_GROUPS", "100000001")
# 每条消息打错一个字的概率。真人错字率很低，别调高。
RATE = min(1.0, max(0.0, float(os.environ.get("DSH_TYPO_RATE", "0.06"))))
# 打错之后补一条「*很好」的概率（在已经打错的前提下）
FIX_RATE = min(1.0, max(0.0, float(os.environ.get("DSH_TYPO_FIX_RATE", "0.35"))))
# 太短的消息错一个字就面目全非（「好」→「号」），不碰
MIN_LEN = max(2, int(os.environ.get("DSH_TYPO_MIN_LEN", "5")))
OWNERS = _set("DSH_TYPO_OWNER", "2774000001")


# ---------------------------------------------------------------------------
# 同音混淆表：正确字 -> 可能被打成的错字
#
# 收录标准三条，缺一不可：
#   ① 真是同音或只差声调（拼音输入法会把它排在候选里）；
#   ② 正确字在本群真语料里出现过（不然这条永远用不上）；
#   ③ 错字一侧人工看过，不会拼成难听或有歧义的词。
# 按真语料词频从高到低排，高频字自然更容易被抽到。
HOMOPHONES: dict[str, tuple[str, ...]] = {
    "是": ("事", "时", "试"),
    "的": ("得", "地"),
    "有": ("又", "友"),
    "没": ("每",),
    "就": ("旧",),
    "要": ("药",),
    "好": ("号",),
    "他": ("她", "它"),          # 最像真人的一对
    "以": ("已",),
    "还": ("孩",),
    "那": ("哪",),
    "在": ("再",),
    "吗": ("嘛",),
    "吧": ("把",),
    "看": ("砍",),
    "会": ("汇",),
    "得": ("的",),
    "行": ("型", "形"),
    "为": ("位", "未"),
    "真": ("针",),
    "玩": ("完",),
    "想": ("响",),
    "做": ("作", "坐"),
    "时": ("是", "十", "实"),
    "到": ("道",),
    "后": ("厚",),
    "道": ("到",),
    "快": ("块",),
    "对": ("队",),
    "已": ("以",),
    "事": ("是",),
    "感": ("敢",),
    "知": ("织",),
    "很": ("狠",),               # 「狠好」是最常见的真人错字之一
    "成": ("承",),
    "像": ("想", "象"),
    "新": ("心", "信"),
    "出": ("初",),
    "直": ("值",),
    "完": ("玩",),
    "把": ("吧",),
    "正": ("整",),
    "又": ("有",),
    "只": ("织", "支"),
    "跟": ("根",),
    "态": ("太",),
    "实": ("是", "时"),
    "应": ("因",),               # 「应该」→「因该」
    "该": ("改",),
    "找": ("照",),
    "心": ("新", "信"),
    "生": ("声", "升"),
    "之": ("知", "只"),
    "号": ("好",),
    "太": ("态",),
    "家": ("加",),
    "再": ("在",),
    "哪": ("那",),
    "前": ("钱",),
    "死": ("似",),               # 语料实证：「作者要饿似了」
}

# 保护名单：这些串里的字一个都不许动。
# 黑话词条被改一个字就从「帮模型听懂」变成「制造混乱」（「新赛季」→「心赛季」）；
# 人名和群名改错了更糟。
_PROTECT: tuple[str, ...] = (
    # 人 / 群
    "大肥鱼", "群主", "肥鱼", "群主", "病友",
    # 含可替换字的黑话词条（与 dsh-glossary 对齐，那边加词时这里要同步）
    "新赛季", "本地部署", "走错片场", "下次一定", "明日方舟", "元气骑士",
    "车轱辘废话", "蚌埠住了", "何意味", "带带我", "全程pro", "全程Pro",
    "常驻池", "五星", "六星", "专五", "保底", "抽卡", "白嫖", "白票",
    "肝帝", "杂鱼", "没绷住", "绷不住", "破防", "笑死", "离谱", "神了",
    "人机", "乐子", "傲娇", "御姐", "废萌", "降智", "破甲", "逆向",
    "过审", "额度", "倍率", "猎奇", "上号", "三连", "典",
    # 三角洲那批（2026-09-05）。整批 11 条词条里只有这两条含可替换字：
    # 「三角洲行动」的行、「零号大坝」的号。其余（长弓/航天/大红/三角券/
    # 狙击精英/鼠鼠/钢枪/白给/满改/干员/烽火地带）一个可替换字都不含，
    # 是脚本比对同音表算出来的，不是眼看的。
    "三角洲行动", "零号大坝",
)

# 这些片段整段跳过：贴纸标记、CQ 码、链接、@、数字/拉丁串
_SKIP_RE = re.compile(
    r"[\[【]\s*(?:贴纸|貼紙|sticker)\s*[:：][^\]】]*[\]】]"
    r"|\[CQ:[^\]]*\]"
    r"|https?://\S+|www\.\S+|\S+\.(?:com|cn|net|org|studio|tv|io)\S*"
    r"|[@＠]\S+"
    r"|[A-Za-z0-9][A-Za-z0-9.\-_+#]*"
)


def _blocked(text: str) -> list[bool]:
    """标出每个下标是否受保护（保护名单 + 跳过片段）。"""
    mask = [False] * len(text)
    for pat in _PROTECT:
        start = 0
        while True:
            at = text.find(pat, start)
            if at < 0:
                break
            for i in range(at, at + len(pat)):
                mask[i] = True
            start = at + 1
    for m in _SKIP_RE.finditer(text):
        for i in range(m.start(), m.end()):
            mask[i] = True
    return mask


def candidates(text: str) -> list[int]:
    """可以打错的下标。纯函数，可离线回测。"""
    mask = _blocked(text)
    return [i for i, ch in enumerate(text)
            if ch in HOMOPHONES and not mask[i]]


def apply_typo(text: str, pick: float = 0.0, alt: float = 0.0,
               min_len: int = None) -> tuple[str, int, str, str] | None:
    """打一个同音错别字。

    pick / alt 是 0~1 的随机数（外部传进来，方便测试复现）：
    pick 决定错哪个位置，alt 决定选哪个错字。
    返回 (新文本, 下标, 正确字, 错字)；不改就返回 None。
    """
    min_len = MIN_LEN if min_len is None else min_len
    body = (text or "").strip()
    if len(body) < min_len:
        return None
    spots = candidates(text)
    if not spots:
        return None
    i = spots[min(len(spots) - 1, int(pick * len(spots)))]
    right = text[i]
    opts = HOMOPHONES[right]
    wrong = opts[min(len(opts) - 1, int(alt * len(opts)))]
    return text[:i] + wrong + text[i + 1:], i, right, wrong


def correction_for(text: str, i: int, form: float = 0.0) -> str:
    """补发的纠正消息。

    只放**那一个正确字**，不带上下文。第一版顺手把前一个字也带上（想凑成
    「*很好」这种更自然的形状），结果「这个东西真的很好用」错在「真」上时
    补出来的是「*西真」—— 前一个字来自「东西」，跨了词边界，纯属噪音。
    不引 jieba 只为这点门面（一个 6% 概率的功能不值得加依赖），
    而真人本来就常常只补一个字：「*的」「*很」。
    """
    right = text[i]
    forms = ("*%s" % right, "%s，打错了" % right, "打错，%s" % right)
    return forms[min(len(forms) - 1, int(form * len(forms)))]


_stat = {"seen": 0, "typo": 0, "fixed": 0, "no_spot": 0, "too_short": 0,
         "unlucky": 0, "skip_group": 0, "not_model": 0}
_last: list[str] = []


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._seg_warned = False
        logger.info(
            "[typo] 已加载：%s 群=%s 错字率=%.0f%%（一条最多错1个字，>=%d字才碰）"
            " 补纠正=%.0f%% 同音对=%d 保护名单=%d",
            "开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无",
            RATE * 100, MIN_LEN, FIX_RATE * 100, len(HOMOPHONES), len(_PROTECT),
        )

    def _seg_reply_on(self, event: AstrMessageEvent) -> bool:
        """框架分段回复是否开着 —— 关着就不能补纠正消息（会拼进同一条）。"""
        try:
            cfg = self.context.get_config(event.unified_msg_origin)
            return bool(cfg["platform_settings"]["segmented_reply"]["enable"])
        except BaseException as exc:
            if not self._seg_warned:
                self._seg_warned = True
                logger.warning("[typo] 读不到分段回复配置，不补纠正: %s", exc)
            return False

    # priority=200：统一去AI味管线第三步 —— 打同音错别字。
    # 固定排在 humanizer(300) 之后、noise(100) 之前（错字在拆句/剥词之后、噪点之前）。
    @filter.on_decorating_result(priority=200)
    async def maybe_typo(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or gid not in GROUPS:
                _stat["skip_group"] += 1
                return
            result = event.get_result()
            if result is None or not result.chain:
                return
            # 只改模型生成的回复：指令回显错一个字就变成误导
            try:
                if not result.is_model_result():
                    _stat["not_model"] += 1
                    return
            except BaseException:
                pass

            # 找第一个够长、有可错位置的 Plain。一条消息最多错一个字，
            # 所以命中一个就收工。
            for idx, comp in enumerate(result.chain):
                if not isinstance(comp, Plain) or not (comp.text or "").strip():
                    continue
                text = comp.text
                _stat["seen"] += 1
                if len(text.strip()) < MIN_LEN:
                    _stat["too_short"] += 1
                    continue
                if not candidates(text):
                    _stat["no_spot"] += 1
                    continue
                if random.random() >= RATE:
                    _stat["unlucky"] += 1
                    continue
                got = apply_typo(text, random.random(), random.random())
                if not got:
                    _stat["no_spot"] += 1
                    continue
                bad, at, right, wrong = got
                # 前导空白原样保留（dsh-mention 可能已经补过空格）
                result.chain[idx] = Plain(bad)
                _stat["typo"] += 1
                note = "%s→%s" % (right, wrong)
                if random.random() < FIX_RATE and self._seg_reply_on(event):
                    fix = correction_for(text, at, random.random())
                    result.chain.append(Plain(fix))
                    _stat["fixed"] += 1
                    note += "，补「%s」" % fix
                _last.append(note)
                del _last[:-8]
                logger.info("[typo] gid=%s 打错一个字：%s｜%s", gid, note, bad[:40])
                return
            # 一个字都没改也要能看见，否则排查只能靠猜
            logger.debug("[typo] 这轮没打错（gid=%s）", gid)
        except BaseException as exc:
            # 错别字纯属锦上添花，出任何问题都不许挡住消息
            logger.error("[typo] 改写失败，保持原样: %r", exc)

    @filter.command("错字状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        s = _stat
        rate = s["typo"] / max(1, s["seen"]) * 100
        yield event.plain_result(
            "同音错别字：%s｜群：%s｜设定 %.0f%%｜补纠正 %.0f%%\n"
            "看过 %d 段正文，打错 %d 次（实际 %.1f%%），其中补了纠正 %d 次\n"
            "没打错的原因：没摇中 %d｜没有可错的字 %d｜太短 %d\n"
            "最近：%s"
            % ("开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无",
               RATE * 100, FIX_RATE * 100, s["seen"], s["typo"], rate, s["fixed"],
               s["unlucky"], s["no_spot"], s["too_short"],
               "｜".join(_last[-5:]) or "还没有")
        )
