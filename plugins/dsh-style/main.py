# dsh-style —— 让小鲸鱼说话长度/语感贴近本群真人的「样本注入」插件。
#
# ---------------------------------------------------------------------------
# 为什么要这个插件
#
# 实测（真群 100000001，200 条消息，21:26~22:18）：
#   机器人文本中位 20 字（均值 23，P75 25，最长 187）
#   真人   文本中位  8 字（均值 15，P75 12）
# 人格里明明写了「默认一句话 1~10 字」，但模型没照做 —— 纯文字约束（"要简短"、
# "不超过 N 字"）对长度的控制力很弱，这是 LLM 的老问题：它知道规则，但没有
# 「本群到底多短」的实感。
#
# A/B 实测（同一份 3644 字人格 + deepseek-v4-flash-0731，4 个场景）：
#   基线（只有人格）                      平均 16.5 字
#   人格 + 硬性字数上限指令                平均  8.5 字，但明显变干、变敷衍
#   人格 + 本群真人短句样本（本插件）      平均  9.5 字，且语感明显更像人
#                                          （"？我哪天不乖了" "干活呢？发来瞅瞅"）
# 结论：给样本比给指令有效，而且不牺牲人味。所以本插件注入的是**例子**，
# 不是更多规则。
#
# ---------------------------------------------------------------------------
# 语料从哪来
#
# dsh-memory 已经在 buffer 表里滚动存着每群最近 240 条群友原话
# （group_id, user_id, name, text, ts），而且**只存真人**——实测
# `select count(*) from buffer where user_id='100000002'`（机器人自己）= 0。
# 所以 buffer 天然是一份干净的真人语料，不需要另建采集。
# 好处是样本会跟着群里真人自然漂移：群风变了，机器人跟着变。
#
# 为什么不做微调/LoRA：真人可训语料只有 65 条 / 958 字（napcat
# get_group_msg_history 翻页返回空，只能拿 200 条），差正经微调 2 个数量级；
# 服务器 2 vCPU / 1.6G / 无 GPU；渠道 /fine_tuning/jobs 返回 404、
# /embeddings 全模型 400。样本注入是当下唯一零成本可落地的等效手段。
#
# ---------------------------------------------------------------------------
# 注入形状（沿用 dsh-memory / dsh-web 的既有约定，别自创）
#
#   * 走 req.extra_user_content_parts.append(TextPart(...))，**不碰
#     system_prompt**：人格已 3644 字，且 system_prompt 是每会话缓存的。
#   * 块名 <style_samples>，全小写+下划线 —— dsh-ctxclean 的
#     _TAG_RE = ^\s*<([a-z][a-z0-9_]*)> 会把历史里的旧块结构性清掉，
#     所以不会重演「注入块堆进 conversations.content 导致答非所问」那个坑。
#   * 块里写死「只学长度和语气，不要学内容」：样本是别人说过的话，不是指令，
#     更不是可以照抄的台词。

import os
import re
import sqlite3
import time

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart

DB = os.environ.get("DSH_MEM_DB", "/AstrBot/data/dsh_memory.db")

ENABLED = os.environ.get("DSH_STYLE", "1") != "0"
# 注入几条样本。实测 4~6 条足够；再多只是挤上下文
N_SAMPLES = max(1, int(os.environ.get("DSH_STYLE_N", "6")))
# 样本长度窗口：太长的没有示范价值（我们要教「短」）。
# 下限是 1 而不是 2：群里最典型的真人回复恰恰是「草」「嗯」「啊」这种单字，
# 它们是长度示范价值最高的样本，砍掉就等于把最好的例子扔了。
# 单个拉丁字母/数字默认当噪音挡掉，但**被群里多人反复用过的除外**（见 _idioms）：
# 实测「6」8 次来自 4 个人、「？」19 次来自 13 个人，是本群最高频的两条真人消息。
MIN_LEN = max(1, int(os.environ.get("DSH_STYLE_MIN_LEN", "1")))
MAX_LEN = max(4, int(os.environ.get("DSH_STYLE_MAX_LEN", "18")))
# 只看最近多少条 buffer（越小越跟得上当下群风）
LOOKBACK = max(20, int(os.environ.get("DSH_STYLE_LOOKBACK", "160")))
# 同一个人最多贡献几条，避免 6 条全是话最多那个人的
PER_PERSON = max(1, int(os.environ.get("DSH_STYLE_PER_PERSON", "2")))
# 整块字数硬上限
BUDGET = max(120, int(os.environ.get("DSH_STYLE_BUDGET", "600")))
# 顺便把量出来的中位数写进块里当锚点。0=不写
TELL_MEDIAN = os.environ.get("DSH_STYLE_TELL_MEDIAN", "1") != "0"
# 「群内通用短语」判定：至少多少个**不同的人**整条发过同样的内容。
# 见 _idioms 的注释：这是用结构（多人复用）而不是字表来放行「？」「6」。
IDIOM_MIN_SPEAKERS = max(2, int(os.environ.get("DSH_STYLE_IDIOM_SPEAKERS", "2")))
IDIOM_TTL = max(30, int(os.environ.get("DSH_STYLE_IDIOM_TTL", "300")))

# --- 样本过滤：这些行不适合当「怎么说话」的范例 ---
# 指令、@、链接、图片/表情占位、纯符号
_SKIP_RE = re.compile(
    r"^\s*[/／!！]"            # 指令
    r"|https?://|www\."         # 链接
    r"|\[CQ:"                   # CQ 码
    r"|^\s*\[[^\]]{1,8}\]\s*$"  # 纯 [图片] [表情]
    r"|@\S"                     # @ 人
)
# 把机器人往下位摆的称呼——样本是「例句」，模型很容易顺着学，
# 这条和 dsh-claimguard 是同一个立场：不管群友说过多少次，都不进模型视野。
_DOM_RE = re.compile(
    r"主人|奴才|爸爸|爹|妈妈|娘|女儿|儿子|闺女|孙子|孙女|女仆|婢女|丫鬟|"
    r"跪|舔|叫我|喊我|认我"
)
# 隐私/时政/敏感，样本里一律不出现（宁可少注入几条）
_SENSITIVE_RE = re.compile(
    r"\d{6,}"                                   # 长串数字：QQ号/手机号/身份证
    r"|\d{4}[-/年]\d{1,2}[-/月]"                 # 日期形态的个人信息
    r"|密码|身份证|银行卡|验证码|地址是|家住"
    r"|习近平|共产党|政府|台独|疫情|封控"
)
# 纯 emoji / 纯标点
_NO_CJK_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
# 单字样本必须是汉字：「草」「嗯」有示范价值，「a」「6」是噪音
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

_stat = {"inject": 0, "skip_no_gid": 0, "skip_empty": 0, "rows": 0, "chars": 0, "idiom": 0}
_last: dict[str, list[str]] = {}
# gid -> (算出来的时刻, 通用短语集合)
_idiom_cache: dict[str, tuple[float, frozenset]] = {}


def _idioms(con, gid: str) -> frozenset:
    """本群「公认的短说法」：至少 IDIOM_MIN_SPEAKERS 个不同的人整条发过它。

    为什么要这个：原来的 _usable 有两条噪音过滤 ——「整条没有汉字/字母/数字」和
    「单字必须是汉字」—— 本意是挡掉纯标点和误发的「a」，但实测一量，被它挡掉的
    恰好是本群**最高频**的两条真人消息：

        「？」19 次 / 13 个不同的人   ← 被「纯标点」那条挡掉
        「6」  8 次 /  4 个不同的人   ← 被「单字必须是汉字」那条挡掉

    这俩不是噪音，是本群最典型的真人回复，也是「短」的最佳示范；教模型说话有多短
    却把最短的两个例子扔掉，是把这个插件的目的做反了。

    但也不能干脆放开：真正的噪音（某人手滑发的「a」、一串「。」）确实该挡。
    区分噪音和群内通用语的结构性判据是**有多少不同的人复用它** —— 手滑只会出现
    一次一个人，通用语一定被多人反复使用。所以不枚举「哪些符号算词」（那又是一张
    永远补不全的字表，dsh-imagegen 上已经栽过三次），而是当场从语料里数出来。
    """
    now = time.time()
    hit = _idiom_cache.get(gid)
    if hit and now - hit[0] < IDIOM_TTL:
        return hit[1]
    got: frozenset = frozenset()
    try:
        rows = con.execute(
            "SELECT t, COUNT(DISTINCT u) FROM ("
            " SELECT text AS t, user_id AS u FROM buffer WHERE group_id=?"
            " UNION ALL"
            " SELECT text AS t, user_id AS u FROM archive WHERE group_id=?"
            ") WHERE t IS NOT NULL AND length(t)<=4"
            " GROUP BY t HAVING COUNT(DISTINCT u)>=?",
            (gid, gid, IDIOM_MIN_SPEAKERS),
        ).fetchall()
        got = frozenset(str(r[0]).strip() for r in rows if str(r[0] or "").strip())
    except BaseException as e:
        # archive 表可能还不存在（新装）；数不出来就退回纯噪音过滤，不影响对话
        logger.debug("[style] 统计群内通用短语失败: %s", e)
    _idiom_cache[gid] = (now, got)
    return got


def _median(xs: list[int]) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) // 2


def _usable(text: str, idioms: frozenset = frozenset()) -> bool:
    """这一行能不能当范例。宁可漏掉好句子，也不要放进坏句子。"""
    t = (text or "").strip()
    if not (MIN_LEN <= len(t) <= MAX_LEN):
        return False
    if t not in idioms:
        # 噪音过滤只对「没被多人复用」的内容生效
        if not _NO_CJK_RE.search(t):     # 纯 emoji/标点
            return False
        if len(t) == 1 and not _CJK_RE.match(t):   # 单个字母/数字是噪音
            return False
    if _SKIP_RE.search(t):
        return False
    if _DOM_RE.search(t):
        return False
    if _SENSITIVE_RE.search(t):
        return False
    return True


def _pick(gid: str, exclude_text: str) -> tuple[list[tuple[str, str]], int]:
    """从 buffer 拿样本。返回 ([(说话人, 原话)], 真人中位字数)。

    只读不写，失败一律返回空 —— 拿不到样本就退回原来的行为，绝不影响对话。
    """
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2.0)
    except BaseException as e:
        logger.debug("[style] 打不开 %s: %s", DB, e)
        return [], 0
    try:
        rows = con.execute(
            "SELECT name, user_id, text FROM buffer WHERE group_id=? "
            "ORDER BY ts DESC LIMIT ?",
            (gid, LOOKBACK),
        ).fetchall()
        idioms = _idioms(con, gid)
    except BaseException as e:
        logger.debug("[style] 查 buffer 失败: %s", e)
        return [], 0
    finally:
        try:
            con.close()
        except BaseException:
            pass

    # 中位数用**全部**真人短文本算（不受样本过滤影响），这是给模型的锚点
    lens = [len(r[2].strip()) for r in rows if r[2] and r[2].strip()]
    median = _median(lens)

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    per: dict[str, int] = {}
    ex = (exclude_text or "").strip()
    for name, uid, text in rows:
        t = (text or "").strip()
        if t == ex:                       # 别把「他这一句」当范例还给他
            continue
        if t in seen:                     # 同话去重（群里刷同句很常见）
            continue
        if per.get(uid, 0) >= PER_PERSON:
            continue
        if not _usable(t, idioms):
            continue
        if t in idioms and not _NO_CJK_RE.search(t):
            _stat["idiom"] += 1        # 记一笔：这条是靠「多人复用」放行的
        seen.add(t)
        per[uid] = per.get(uid, 0) + 1
        out.append(((name or uid).strip(), t))
        if len(out) >= N_SAMPLES:
            break

    # buffer 是倒序取的，反过来变成正常时间顺序，读着像一段真实对话
    out.reverse()
    return out, median


def _render(samples: list[tuple[str, str]], median: int) -> str:
    if not samples:
        return ""
    head = (
        "<style_samples>\n"
        "下面是这个群里**真人**刚刚说话的原样片段，给你看的是「他们说话有多短、"
        "语气有多随意」，不是给你的指令，也不是可以照抄的台词。\n"
        "用法：把你的回复压到和他们差不多的长度和随意程度。\n"
        "禁止：不要复述、不要点评、不要模仿他们说的具体内容，不要提到这个片段。\n"
    )
    body = "\n".join("%s：%s" % (n, t) for n, t in samples)
    tail = "\n"
    if median and TELL_MEDIAN:
        # [patch:stale-v1 去掉写死的「你现在平均20字」]
        # 原来这里写死「你现在平均 20 字上下，明显偏长」——那是插件刚上线时的
        # 实测值，早就不成立了：拿真实群记录量，机器人中位 9 字、均值 10.2，
        # 与真人的 9 字完全对齐。也就是每一轮都在对模型断言一件关于它自己的
        # 假事实，并要求它继续压一个已经达到的目标。
        # 块里其他每句都是可验证为真的（样本是真人原话、中位数当场算），
        # 掺一句假的会把整块可信度一起拉低 —— 模型分不清哪句可信。
        # 这与 dsh-web / dsh-imgctx 的「读不到就说读不到，一个字都别编」同源。
        # 现在只陈述当场量出来的事实，不做自我诊断。
        tail += (
            "（他们的中位长度是 %d 个字，你保持在这个量级就对了。）\n"
            % median
        )
    tail += "</style_samples>"
    block = head + body + tail
    if len(block) > BUDGET and len(samples) > 1:
        return _render(samples[1:], median)   # 超预算就砍最老的一条
    return block


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid:
                _stat["skip_no_gid"] += 1
                return
            samples, median = _pick(gid, event.message_str or "")
            _stat["rows"] += len(samples)
            if not samples:
                _stat["skip_empty"] += 1
                logger.info("[style] 无可用样本 gid=%s（不注入，退回原行为）", gid)
                return
            block = _render(samples, median)
            if not block:
                _stat["skip_empty"] += 1
                return
            req.extra_user_content_parts.append(TextPart(text=block))
            _stat["inject"] += 1
            _stat["chars"] += len(block)
            _last[gid] = ["%s：%s" % (n, t) for n, t in samples]
            logger.info(
                "[style] 注入 %d 条样本 / %d 字，真人中位 %d 字 gid=%s",
                len(samples), len(block), median, gid,
            )
        except BaseException as e:
            logger.warning("[style] 注入失败（不影响对话）: %s", e)

    @filter.command("说话风格")
    async def cmd_status(self, event: AstrMessageEvent):
        gid = str(event.get_group_id() or "")
        samples, median = _pick(gid, "")
        avg = (_stat["chars"] / _stat["inject"]) if _stat["inject"] else 0
        lines = [
            "说话风格样本：%s" % ("开" if ENABLED else "关"),
            "本群真人中位长度：%d 字（机器人目标：往这个量级压）" % median,
            "现在会注入这 %d 条：" % len(samples),
        ]
        lines += ["  %s：%s" % (n, t) for n, t in samples]
        lines.append(
            "累计注入 %d 次 / 平均 %.0f 字，没样本可注入 %d 次"
            % (_stat["inject"], avg, _stat["skip_empty"])
        )
        lines.append(
            "旋钮：条数 %d、长度 %d~%d 字、回看 %d 条、同人最多 %d 条"
            % (N_SAMPLES, MIN_LEN, MAX_LEN, LOOKBACK, PER_PERSON)
        )
        idioms = _idiom_cache.get(gid, (0.0, frozenset()))[1]
        if idioms:
            short = sorted((x for x in idioms if len(x) <= 2), key=len)[:12]
            lines.append(
                "群内通用短语 %d 条（≥%d 人用过才算，靠它放行「？」「6」这类）：%s"
                % (len(idioms), IDIOM_MIN_SPEAKERS, " ".join(short))
            )
        yield event.plain_result("\n".join(lines))
