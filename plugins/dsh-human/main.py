# -*- coding: utf-8 -*-
"""dsh-human：把回复的**形状**改成真人打字的形状。

## 为什么需要这个插件

人格提示词里已经写了「一句话 1~10 字」「少用句号」「想补一句让它成为下一条」，
但拿真群语料一量，模型根本没做到，而且偏差是**系统性**的（不是偶发）：

    指标              真人(n=1691)   机器人(n=31)
    中位长度              8 字          10 字
    ≤5 字占比           34.8%          6.5%
    1-2 字占比          10.6%          0.0%
    带逗号占比          16.4%         67.7%
    「短句，短句」句式   —            54.8%
    句末句号             0.5%          0.0%

也就是说：**它最像 AI 的地方不是用词，是句子形状**。真人一次说 2 条以上占
39%（消息量占 63%），而模型习惯用一个逗号把两句话粘成一条发出去。
「忙着修我，哪有空打」这种写法，真人会打成两条：「忙着修我」「哪有空打」。

提示词治不了这个 —— 已经写在人格里两周了，实测 54.8% 照旧。这是**结构**问题，
用结构手段解决：在发送前把逗号粘起来的两句拆成两条消息，交给框架的分段回复
按 1.2~2.8 秒间隔发出去，自然就有了真人的连打节奏。

（这条「结构判断优于提示词劝说」的教训在 dsh-imagegen / dsh-mention /
dsh-ctxclean 上都验证过。）

## 做法

挂 `on_decorating_result`（已核对源码顺序：这个钩子在
`result_decorate/stage.py:160` 触发，框架的分段回复在同文件 :210 才执行，
所以我们拆出来的多个 Plain 会被分段逻辑正常地逐条发出）。

拆分是**保守**的，拿不准就不拆：
  · 只动模型产生的纯文本，指令回显、图片、语音一律不碰；
  · 整条太长（>MAX_LEN）不拆 —— 长文本拆成两条长消息并不更像人；
  · 逗号两侧都得像能独立成句（长度够、左边不以连接词/助词结尾）；
  · 一条最多拆一次（真人连发 2 条占 26.6%，3 条只有 5%）；
  · 数字千分位、URL、贴纸标记、CQ 码、换行一概跳过；
  · 按 SPLIT_RATE 概率决定拆不拆，避免 100% 拆反而成了新的机器规律。

另外去掉句末的「。」：真人只有 0.5% 用句末句号，模型的书面句号是明显的 AI 味。

## 钩子顺序：必须做到与顺序无关

`on_decorating_result` 这一档现在有三个插件：dsh-mention（插 @）、dsh-sticker
（兜底剥贴纸标记）、本插件。框架 `star_handler.py:150 get_handlers_by_event_type`
按 `self._handlers` 的**注册顺序**返回，而注册顺序跟着插件加载顺序，加载顺序又是
`star_manager.py:292 os.listdir()` 的目录顺序 —— 也就是**文件系统顺序，不是字母序**
（实测加载序：mention, guard, acl, ctxclean, memory, poke, welcome, emotion, …）。
新建一个插件目录会落在哪一位无法预测，所以本插件不能假设自己先跑还是后跑：

  · dsh-sticker 在前：标记已被剥掉，我们看到的是干净文本，正常拆；
    本插件在前：文本里还有 `[贴纸:x]`，被 _SKIP_RE 挡掉、整条不动，随后由
    sticker 剥掉标记。两种顺序都不会把标记拆断。
  · dsh-mention 在前：chain 变成 [At, Plain(" 正文")]，它在正文前补了一个空格。
    本插件保留这个前导空白（只补回第一条），但**它其实不是必需的**：核对源码
    发现 aiocqhttp 适配器在每个 At 后面自己会插一段 {"type":"text","text":" "}
    （`aiocqhttp_message_event.py:74-78`），而框架分段时对每段都做了 `seg.strip()`
    （`result_decorate/stage.py:259`），所以 mention 那个手工空格在 LLM 回复里
    本来就会被 strip 掉。保留它只是为了「没走分段的路径」也不改变现状。
    本插件在前：chain 是 [Plain(左), Plain(右)]，mention 随后插 At 到 0 位、
    给 chain[1]（也就是左半）补空格，同样正确。
  框架 `respond/stage.py:257 _extract_comp` 把 At/Reply 抽出来只跟**第一条**走，
  所以拆成两条后 @ 自然只出现在第一条，正是真人的样子。

## 和框架分段回复的分工

框架自己会在 `。？！~…` 处分段（regex 实测值 `[^\n。？！~…]+[。？！~…]*|[^\n]+`），
所以句中带这些标点的消息**本来就会**被拆成 ≥2 条。本插件只管逗号，并且句中
已经有框架分割点时直接不动手（`_FW_SPLIT_RE`）—— 否则「昵称是别人改的，不算数！
我本名小鲸鱼」会变成 3 条，而真人一次连发 3 条只占 5%。
"""

import os
import random
import re

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger

ENABLED = os.environ.get("DSH_HUMAN", "1").lower() not in {"0", "false", "off"}
# 概率拆分。拿真人分布当靶（平均连发 1.60 条 / 带逗号 16.2% / ≤5 字 34.2%），
# 把 31 条真实回复各跑 400 次随机扫一遍 rate（sweep_human.py）：
#
#     rate   平均连发   带逗号   ≤5字    综合偏差
#     0.0     1.19     70.3%   21.6%   126.7%   ← 现状，最像 AI
#     0.5     1.56     31.3%   43.1%    38.5%
#     0.7     1.71     20.0%   49.3%    23.6%   ← 取这个
#     0.8     1.78     15.4%   52.0%    24.2%
#     0.9     1.86     10.7%   54.6%    37.7%
#     1.0     1.94      6.7%   56.7%    49.2%
#
# 取偏差最小的 0.7：逗号从 70% 压到 20%（真人 16.8%），连发 1.71 条贴着真人的 1.60。
# 两条轴天生矛盾——拆一条 10 字逗号句必然得到两条 5 字句，所以 ≤5 字压不回 34%，
# 这是模型输出长度本身缺变化，不是拆错。
# 靶值取自**本群**语料：早先写死的 16.2%/34.2% 是拿 buffer+archive 全表算的，
# 里面混了另一个群（元气骑士交流群）约 9% 的消息；按本群重算是 16.8%/34.1%，
# 结论没变，但基线得是干净的。
SPLIT_RATE = min(1.0, max(0.0, float(os.environ.get("DSH_HUMAN_SPLIT_RATE", "0.7"))))
# 超过这个长度不拆：长文本拆两半还是两条长消息，不像人。真人 p90=16 字。
MAX_LEN = max(8, int(os.environ.get("DSH_HUMAN_MAX_LEN", "26")))
# 拆出来的**左**半至少多少字：它要能独当一条、自己开个头，太碎的（「对，」）很怪
MIN_PART = max(2, int(os.environ.get("DSH_HUMAN_MIN_PART", "3")))
# 拆出来的**右**半至少多少字。比左半宽一档，理由是这两半的角色不对称：
# 右半是**接着上一条说**的，读者已经有上文，两个字完全立得住 ——
# 真群实测「饱了」「别装」「不看」这种 1~2 字消息占真人 11.1%、机器人 0.0%，
# 是差距最大的一档，而右半下限卡 3 恰好把它整类挡在门外
# （线上实例：「刚啃完token，饱了」原本因为「饱了」只有 2 字而整条不拆）。
# 1 字仍然不许（「我不去了，行」拆出个「行」太碎），依赖性词头另有 _DEPENDENT_HEAD_RE 挡。
MIN_RIGHT = max(1, int(os.environ.get("DSH_HUMAN_MIN_RIGHT", "2")))
# 例外：左半本身已经是一句完整的话时，允许短到 2 字。
# 依据是真群实测——1~2 字的消息占真人 11.0%，而机器人 0.0%；真人最常单发的
# 就是「对」「行」「不」「啥」「6」这类。「嗯嗯，这个确实」在真人手里就是
# 「嗯嗯」+「这个确实」两条，卡在 MIN_PART=3 上恰好把最像人的那一刀挡掉了。
# 三种「完整」判据，都是结构性的，不是枚举句子：
#   ① 整条就是应答词/语气词（下面这张白名单，^…$ 全匹配）；
#   ② 以句末语气词结尾 —— 汉语的「啊/呢/吧」只出现在句末，出现即句子已收尾；
#   ③ 句中有疑问词 —— 「乐啥」「谁画的」本身就是完整问句。
_INTERJ_RE = re.compile(
    r"^(?:嗯+|哦+|噢+|啊+|欸+|诶+|哎+|唉+|呃+|额+|对+|是+|行+|好+|草+|靠+|"
    r"懂了|明白|收到|好吧|行吧|得了|算了|不是|没有|没事|真的|确实)$"
)
# 句末语气词：出现在末尾说明这半句已经说完了
_SENT_END_RE = re.compile(r"(?:啊|呀|吧|呢|嘛|哦|喔|噢|啦|咯|嘞|哈|哟|唷|欸)$")
# 去掉句末句号
DROP_PERIOD = os.environ.get("DSH_HUMAN_DROP_PERIOD", "1") != "0"

# 出现这些就整条不拆：拆了会破坏内容本身
_SKIP_RE = re.compile(
    r"https?://|www\."          # 链接
    r"|\[贴纸[:：]|\[CQ:"        # 贴纸标记 / CQ 码
    r"|```|\n"                  # 代码块 / 已经多行
    r"|\d[,，]\d"               # 千分位数字
)
# 左半以这些结尾说明句子真的没说完（连接词、介词、助动词），拆开就断句。
_HARD_TAIL_RE = re.compile(
    r"(?:和|跟|与|或|及|把|被|让|给|对|向|从|在|是|有|想|要|会|能|就|还|也|但|而|"
    r"因为|所以|如果|虽然|不但|一边|又|为了|的话)$"
)
# 「的/地/得」结尾是**歧义**的，不能一刀切：
#   ·「这图谁画的」是完整疑问句，拆开完全自然（真人就这么打）
#   ·「我说的」只是个名词化片段，单独成条很怪
# 用结构区分：左半带疑问词就是完整问句，允许拆；否则当没说完。
# 这比枚举句子可靠，也是 dsh-imagegen 那条「结构判断优于词表」的同一手法。
_SOFT_TAIL_RE = re.compile(r"(?:的|地|得)$")
_QUESTION_WORD_RE = re.compile(r"谁|什么|啥|哪|怎|多少|几|吗|呢")
# 「是…的」是判断句的固定结构，整句已完整（「昵称是别人改的」），
# 属于 _SOFT_TAIL_RE 的合法例外。
_SHI_DE_RE = re.compile(r"是[^，,]*的$")
# 右半以这些开头说明它是上半句的补语，不该独立成条
_DEPENDENT_HEAD_RE = re.compile(r"^(?:的|地|得|了|着|吗|呢|吧|啊|呀|嘛)")
# 框架自己会在 。？！~… 处分段（platform_settings.segmented_reply.regex，
# 实测值 `[^\n。？！~…]+[。？！~…]*|[^\n]+`，在 result_decorate/stage.py:210
# 也就是本钩子之后执行）。所以句中已经有这些标点的，框架本来就会拆成 ≥2 条，
# 我们再补一刀会变成 3 条以上 —— 真人一次连发 3 条只占 5%，不划算。
# 判据是「标点后面还有内容」，句末的问号不算（那只会分出 1 段）。
_FW_SPLIT_RE = re.compile(r"[。？！~…][^。？！~…]*\S")

_stat = {"seen": 0, "split": 0, "skip_long": 0, "skip_shape": 0, "skip_roll": 0, "period": 0}


def drop_trailing_period(text: str) -> str:
    """去掉句末句号。真人 0.5% 才用，模型的书面句号是明显 AI 味。

    只去「。」：问号、感叹号、省略号都是有语气的，真人会用，不能动。
    """
    return re.sub(r"。+$", "", text) if DROP_PERIOD else text


def left_can_stand(left: str) -> bool:
    """左半能不能单独成条。纯函数，便于断言。"""
    if not left:
        return False
    if _INTERJ_RE.match(left):
        # 整条就是应答词。注意「对」「是」「行」同时也在 _HARD_TAIL_RE 里
        # （它们做连接成分时确实没说完），但整体匹配白名单说明这里是独立应答，
        # 所以要绕过尾字检查，只保留 2 字下限。
        return len(left) >= 2
    complete = bool(_SENT_END_RE.search(left) or _QUESTION_WORD_RE.search(left))
    if len(left) < (2 if complete else MIN_PART):
        return False
    if _HARD_TAIL_RE.search(left):
        # 硬连接成分结尾就是没说完，有疑问词也不能豁免
        return False
    if _SOFT_TAIL_RE.search(left) and not (
        _QUESTION_WORD_RE.search(left) or _SHI_DE_RE.search(left)
    ):
        return False
    return True


def split_at_comma(text: str) -> tuple[str, str] | None:
    """能拆就返回 (左, 右)，拿不准返回 None。纯函数，便于断言。

    只在**第一个**合格的逗号处拆一次。
    """
    t = (text or "").strip()
    if not t or len(t) > MAX_LEN:
        return None
    if _SKIP_RE.search(t):
        return None
    if _FW_SPLIT_RE.search(t):
        return None                     # 框架已经会拆，别叠成 3 条
    for m in re.finditer(r"[，,]", t):
        left = t[:m.start()].strip()
        right = t[m.end():].strip()
        if not left_can_stand(left) or len(right) < MIN_RIGHT:
            continue
        if _DEPENDENT_HEAD_RE.match(right):
            continue
        # 右半自己还带逗号时也允许（它会作为一整条发出去），
        # 但左半不许再含逗号，保证一条只拆一次。
        if "，" in left or "," in left:
            continue
        return left, right
    return None


def humanize(text: str, roll: float) -> list[str]:
    """把一条回复变成 1~2 条真人形状的消息。roll 由调用方给，便于测试。"""
    t = (text or "").strip()
    if not t:
        return []
    pair = split_at_comma(t)
    if pair is None or roll >= SPLIT_RATE:
        return [drop_trailing_period(t)]
    left, right = pair
    return [drop_trailing_period(left), drop_trailing_period(right)]


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._seg_warned = False
        logger.info(
            "[human] 已加载：%s 拆句概率%.0f%% 上限%d字 左半≥%d字/右半≥%d字 去句末句号=%s",
            "开" if ENABLED else "关", SPLIT_RATE * 100, MAX_LEN, MIN_PART, MIN_RIGHT,
            "开" if DROP_PERIOD else "关",
        )

    def _seg_reply_on(self, event: AstrMessageEvent) -> bool:
        """框架的分段回复是否开着 —— 这是本插件能工作的前提。

        本插件把一条回复拆成两个 Plain，靠框架把它们**分别**发出去
        （`respond/stage.py:130 is_seg_reply_required` → :270 逐段 send + sleep）。
        一旦 `platform_settings.segmented_reply.enable` 被关掉，同一个 chain 里的
        两个 Plain 会被拼进**同一条**消息，「偷完了，今天的量到账」就变成
        「偷完了今天的量到账」—— 逗号没了，比不改还差。所以每次当场读配置
        （带 umo，会话级覆盖也算），关了就不拆。
        读不到就按「关」算（fail closed）：宁可退回原样，不能发出丢标点的句子。
        """
        try:
            cfg = self.context.get_config(event.unified_msg_origin)
            return bool(cfg["platform_settings"]["segmented_reply"]["enable"])
        except BaseException as exc:
            if not self._seg_warned:
                self._seg_warned = True
                logger.warning("[human] 读不到分段回复配置，本插件按关处理: %s", exc)
            return False

    @filter.on_decorating_result()
    async def reshape(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            result = event.get_result()
            if result is None or not result.chain:
                return
            # 只改模型生成的回复。指令回显（/贴纸状态 之类）是给人看的清单，
            # 拆开会变成一堆碎片。
            try:
                if not result.is_model_result():
                    return
            except BaseException:
                pass
            allow_split = self._seg_reply_on(event)

            new_chain = []
            changed = False
            for comp in result.chain:
                if not isinstance(comp, Plain):
                    new_chain.append(comp)
                    continue
                text = comp.text or ""
                if not text.strip():
                    new_chain.append(comp)
                    continue
                # dsh-mention 可能已经跑过并在正文前补了一个空格（避免
                # 「@昵称正文」粘住）。它和本插件都挂 on_decorating_result，
                # 而框架按目录顺序决定谁先跑，谁在前都可能 —— 所以这里必须
                # 原样保留前导空白，否则等于把它的修复又破坏回去。
                lead = text[: len(text) - len(text.lstrip())]
                _stat["seen"] += 1
                # 分段回复关着时用 roll=1.0，等于「只去句号、绝不拆」
                pieces = humanize(text, random.random() if allow_split else 1.0)
                if not pieces:
                    new_chain.append(comp)
                    continue
                if len(pieces) > 1:
                    _stat["split"] += 1
                    logger.info("[human] 拆成 %d 条：%s", len(pieces), " ｜ ".join(pieces))
                elif pieces[0] != text.strip():
                    _stat["period"] += 1
                if pieces == [text.strip()]:
                    new_chain.append(comp)      # 一个字都没改，原对象带回去
                    continue
                changed = True
                # 前导空白只补回第一条：@ 只跟着第一条走
                # （respond/stage.py:257 _extract_comp 把 At 抽出来只发一次）
                for i, piece in enumerate(pieces):
                    new_chain.append(Plain((lead + piece) if i == 0 else piece))
            if changed:
                result.chain[:] = new_chain
        except BaseException as exc:  # 形状问题绝不能挡住消息
            logger.error("[human] 改写失败，保持原样: %s", exc)

    @filter.command("拟人状态")
    async def cmd_status(self, event: AstrMessageEvent):
        rate = (_stat["split"] / _stat["seen"] * 100) if _stat["seen"] else 0.0
        yield event.plain_result(
            "拟人改写：%s（框架分段回复：%s）\n"
            "看过 %d 条，拆成两条 %d 次（%.0f%%），只去句号 %d 次\n"
            "旋钮：拆句概率 %.0f%%、上限 %d 字、左半≥%d 字、右半≥%d 字\n"
            "（本群真人实测：中位 8 字、≤5字占 34.1%%、带逗号 16.8%%、1~2字占 11.1%%、平均连发 1.6 条）"
            % ("开" if ENABLED else "关",
               "开" if self._seg_reply_on(event) else "关（此时只去句号不拆）",
               _stat["seen"], _stat["split"], rate,
               _stat["period"], SPLIT_RATE * 100, MAX_LEN, MIN_PART, MIN_RIGHT)
        )
