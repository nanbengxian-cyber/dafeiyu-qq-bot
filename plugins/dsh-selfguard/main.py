# -*- coding: utf-8 -*-
"""dsh-selfguard -- 出口自制力。

两道出口闸门：一是阻止机器人紧邻复述自己，二是在群聊已经冲突时拦截继续挑衅。
重复回复不再替换成括号系统提示，而按同一用户短时间内的触发次数表现为递进式不耐烦。
"""

import os
import random
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_SELFGUARD")
GROUPS = _set("DSH_SELFGUARD_GROUPS", "100000001")
SHADOW = _flag("DSH_SELFGUARD_SHADOW", "0")
REPEAT_ON = _flag("DSH_SELFGUARD_REPEAT")
TAUNT_ON = _flag("DSH_SELFGUARD_TAUNT")
KEEP = max(1, int(os.environ.get("DSH_SELFGUARD_KEEP", "3")))
LCS_MIN = max(2, int(os.environ.get("DSH_SELFGUARD_LCS", "4")))
BG_MIN = max(1, int(os.environ.get("DSH_SELFGUARD_BIGRAM", "2")))
# 35% 可避开「…是吧」「…了是」等语气词尾造成的误拦。
BG_RATIO = min(1.0, max(0.05, float(os.environ.get("DSH_SELFGUARD_RATIO", "0.35"))))
CONFLICT_WINDOW = max(30.0, float(os.environ.get("DSH_SELFGUARD_WINDOW", "180")))
MIN_LEN = max(2, int(os.environ.get("DSH_SELFGUARD_MIN_LEN", "5")))
# [patch:short-echo] 极短句复读抑制。
#
# 2026-09-15 现场：群友「摸摸」→ 机器人「摸吧摸吧」；群友「抱抱」→ 机器人
# 「抱吧抱吧」；同一对句子当天各出现两次（12:23、17:19、18:30、18:31）。紧跟着
# 群友就说「你咋变这么乖了」—— 破绽不在"乖"，在于**输入什么形状、输出就什么
# 形状**，连着两次一模一样，一眼看出是程序。
#
# 现有 repeat_of() 抓不到它们：MIN_LEN=5 而「摸吧摸吧」只有 4 个字，第一条就被
# 挡在检测之外。把 MIN_LEN 调小会连带影响长句判据，所以这里单开一条只针对
# **极短句**的通道。
#
# 关键取舍：群主明确讨厌「这茬刚聊完／已经聊过／刚说过」这类不耐烦话术
# （2026-09-12 因此关掉了 DSH_SELFGUARD_REPEAT）。所以这条路**不拦、不换话术、
# 不说任何关于重复的话**，只是把已经说过的那句短句**换个同义说法**再发，
# 并且允许返回 None（同义已用尽）表示"那就别重复这条了，让模型换一句"。
SHORT_ECHO_ON = _flag("DSH_SELFGUARD_SHORT_ECHO", "1")
SHORT_ECHO_LEN = max(3, int(os.environ.get("DSH_SELFGUARD_SHORT_LEN", "6")))
# 同义变体表。键是「去掉语气尾巴的短句」，值是一组可直接替换的说法。
# 只在**同一个群里今天已经发过**时启用，不是每次都换 —— 偶尔重复才是真人。
_SHORT_VARIANTS = {
    "摸吧摸吧": ("来", "摸呗", "手给你", "摸一下得了"),
    "抱吧抱吧": ("抱一下", "来吧", "给你抱", "就一下"),
    "行吧行吧": ("行", "行吧", "成", "那就这样"),
    "好啊好啊": ("好", "行呀", "可以", "成啊"),
    "来吧来吧": ("来", "来呗", "走", "那就来"),
    "算了算了": ("算了", "那算", "行吧算了", "不说了"),
}
# 叠词回显的通用识别：`X吧X吧` / `XX吧` 这类由输入形状直接决定的输出。
_ECHO_TAIL_RE = re.compile(r"[～~！!。，,、\s]+$")
MEM_DB = os.environ.get("DSH_MEM_DB", "/AstrBot/data/dsh_memory.db")
OWNERS = _set("DSH_SELFGUARD_OWNER", "2774000001")

# 同一用户单独计数：甲反复追问不会导致机器人迁怒乙。4 分钟内升级，12 分钟后重置。
IMPATIENT_WINDOW = max(30.0, float(os.environ.get("DSH_SELFGUARD_IMPATIENT_WINDOW", "240")))
IMPATIENT_RESET = max(IMPATIENT_WINDOW, float(os.environ.get("DSH_SELFGUARD_IMPATIENT_RESET", "720")))
_IMPATIENT_REPLIES = {
    1: ("又来", "刚说过，别复读", "还问这个啊", "这茬刚聊完"),
    2: ("你怎么还在问", "都说过了，烦不烦", "还来？换个话题", "非得反复问是吧"),
    3: ("不聊这个了", "打住，换一个", "没完了是吧", "再问也不说了"),
}
_TAUNT_REPLIES = ("懒得跟你吵", "行了，别拱火", "不接你这茬")


@dataclass
class ImpatientState:
    count: int
    last_at: float
    last_reply: str = ""


def impatient_level(state: ImpatientState | None, now: float) -> int:
    """短时间连续触发就升级；隔久一点降一级，完全冷却后回到一级。"""
    if state is None or now - state.last_at > IMPATIENT_RESET:
        return 1
    if now - state.last_at <= IMPATIENT_WINDOW:
        return min(3, state.count + 1)
    return max(1, state.count - 1)


def choose_impatient_reply(level: int, last_reply: str = "", rng=None) -> str:
    rng = rng or random
    pool = _IMPATIENT_REPLIES[max(1, min(3, int(level)))]
    candidates = [reply for reply in pool if reply != last_reply] or list(pool)
    return rng.choice(candidates)


def choose_taunt_reply(last_reply: str = "", rng=None) -> str:
    rng = rng or random
    candidates = [reply for reply in _TAUNT_REPLIES if reply != last_reply] or list(_TAUNT_REPLIES)
    return rng.choice(candidates)


_PUNCT = re.compile(r"[\s，。！？、,.!?~…—\-：:；;\"'“”‘’（）()\[\]【】*]")


def core(s: str) -> str:
    return _PUNCT.sub("", s or "")


def lcs_len(a: str, b: str) -> int:
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def repeat_of(text: str, recent: list[str]) -> tuple[str, str] | None:
    cur = core(text)
    if len(cur) < MIN_LEN:
        return None
    ca = bigrams(cur)
    for old in recent:
        prev = core(old)
        if len(prev) < MIN_LEN:
            continue
        n = lcs_len(cur, prev)
        if n >= LCS_MIN:
            return old, "公共子串%d字" % n
        shared = ca & bigrams(prev)
        if len(shared) >= BG_MIN:
            ratio = len(shared) / max(1, min(len(ca), len(bigrams(prev))))
            if ratio >= BG_RATIO:
                return old, "共享%d组/%.0f%%（%s）" % (
                    len(shared), ratio * 100, "、".join(sorted(shared)[:3]))
    return None


def _norm_short(text: str) -> str:
    """极短句归一化：去空白与语气尾巴，用来比对"是不是同一句"。"""
    return _ECHO_TAIL_RE.sub("", core(text))


def short_echo_variant(text: str, recent: list[str]) -> tuple[str, str] | None:
    """极短句跟最近发过的重了，就给一个同义说法。

    返回 (新说法, 原因)；没有合适说法时返回 ("", 原因) 表示**这句别发了**；
    不属于"极短句复读"则返回 None（交给原来的逻辑）。

    与 repeat_of() 的区别：repeat_of 面向长句、命中就拦；这里面向 ≤SHORT_ECHO_LEN
    字的短句，命中只**换个说法**，绝不说任何关于"重复/聊过/刚说过"的话 ——
    群主明确不喜欢那种话术。
    """
    cur = _norm_short(text)
    if not cur or len(cur) > SHORT_ECHO_LEN:
        return None
    # 只用"整句完全相同"判定，不用子串/bigram：短句本来就短，模糊匹配极易误伤
    # （「行吧」和「来吧」共享一个「吧」，按 bigram 算会被当成重复）。
    for old in recent:
        if _norm_short(old) != cur:
            continue
        pool = _SHORT_VARIANTS.get(cur)
        if pool:
            # 变体里挑一个这次没用过的：先按字数差别大的挑，观感上更像换了句话
            for cand in pool:
                if _norm_short(cand) != cur:
                    return cand, "短句复读「%s」-> 换说法" % cur
        return "", "短句复读「%s」且没有现成变体，这句不重复" % cur
    return None


_TAUNT_RE = re.compile(
    r"这就(急|恼|破防|绷不住)"
    r"|(急|恼|破防)(了|什么|啥)[?？]"
    r"|气(抖冷|急)"
    r"|比你(强|好|行|厉害)|不如你|你也就(这样|这点)|就这(水平|点)|轮到你|你算(什么|啥)"
    r"|(你|他)(才|就)是?(傻|蠢|废物|垃圾|智障|沙雕)"
    r"|(傻|蠢|废物|垃圾|智障)\S{0,4}(也)?比你"
)
_CONFLICT_RE = re.compile(
    r"你妈|你马|尼玛|草你|艹你|你死|去死|该死|鞭尸"
    r"|傻[逼比批]|贱人|好贱|贱货|狗东西|智障|垃圾|废物"
    r"|滚(蛋|开|远|出去)|给我滚|快滚|^\s*滚\s*[!！。.]*$"
    r"|禁言我|举报|骂(你|我|他|她)|吵(什么|啥|架)|别活了"
)


def is_taunt(text: str) -> bool:
    return bool(_TAUNT_RE.search(text or ""))


def looks_conflict(texts: list[str]) -> str | None:
    for text in texts:
        if _CONFLICT_RE.search(text or ""):
            return text
    return None


_stat = {
    "seen": 0, "repeat": 0, "taunt": 0,
    "shadow_repeat": 0, "shadow_taunt": 0,
    "skip_group": 0, "not_model": 0,
    "short_echo": 0, "short_echo_drop": 0,
}
_last: list[str] = []


class Main(star.Star):
    def __init__(self, context: star.Context):
        super().__init__(context)
        self.context = context
        self._recent: dict[str, deque] = {}
        self._impatient: dict[tuple[str, str], ImpatientState] = {}
        self._last_taunt_reply = ""
        logger.info(
            "[selfguard] 已加载：%s 群=%s｜自我重复=%s(公共子串≥%d 或 共享≥%d组且≥%.0f%%，比最近%d条)｜"
            "拱火=%s(挑衅 且 %.0fs内群里在冲突)｜重复后不耐烦=%ss内升级/%ss重置｜"
            "短句复读抑制=%s(≤%d字整句相同就换说法)",
            "开" if ENABLED else "关", ",".join(sorted(GROUPS)) or "无",
            "开" if REPEAT_ON else "关", LCS_MIN, BG_MIN, BG_RATIO * 100, KEEP,
            "开" if TAUNT_ON else "关", CONFLICT_WINDOW, IMPATIENT_WINDOW, IMPATIENT_RESET,
            "开" if SHORT_ECHO_ON else "关", SHORT_ECHO_LEN,
        )

    @filter.after_message_sent()
    async def remember_sent(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or gid not in GROUPS:
                return
            result = event.get_result()
            if result is None:
                return
            text = (result.get_plain_text() or "").strip()
            if not text:
                return
            queue = self._recent.setdefault(gid, deque(maxlen=KEEP))
            queue.appendleft(text)
        except BaseException as exc:
            logger.warning("[selfguard] 记录自己发言失败，跳过: %r", exc)

    def _recent_human(self, gid: str) -> list[str]:
        since = time.time() - CONFLICT_WINDOW
        out: list[str] = []
        try:
            con = sqlite3.connect(f"file:{MEM_DB}?mode=ro", uri=True, timeout=1.5)
        except BaseException:
            return out
        try:
            for table in ("buffer", "archive"):
                try:
                    columns = {row[1] for row in con.execute("PRAGMA table_info(%s)" % table)}
                    if not columns:
                        continue
                    gid_col = "group_id" if "group_id" in columns else "gid"
                    text_col = "text" if "text" in columns else "content"
                    ts_col = "ts" if "ts" in columns else "time"
                    sql = (
                        "SELECT %s FROM %s WHERE %s=? AND %s>=? ORDER BY %s DESC LIMIT 30"
                        % (text_col, table, gid_col, ts_col, ts_col)
                    )
                    for (text,) in con.execute(sql, (gid, since)):
                        if text and text.strip():
                            out.append(text.strip())
                except sqlite3.Error:
                    continue
        finally:
            con.close()
        return out

    @filter.on_decorating_result()
    async def gate(self, event: AstrMessageEvent) -> None:
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
            try:
                if not result.is_model_result():
                    _stat["not_model"] += 1
                    return
            except BaseException:
                pass
            text = (result.get_plain_text() or "").strip()
            if not text:
                return
            _stat["seen"] += 1

            hit = why = None
            if REPEAT_ON:
                got = repeat_of(text, list(self._recent.get(gid, ())))
                if got:
                    hit = "自我重复"
                    why = "跟「%s」重了（%s）" % (got[0][:24], got[1])
            # [patch:short-echo] 短句复读**独立于 REPEAT_ON**：群主关掉的是"不耐烦
            # 话术"那条路，不是"允许自己一句话说两遍"。这里只换说法，不换话术。
            if hit is None and SHORT_ECHO_ON:
                echo = short_echo_variant(text, list(self._recent.get(gid, ())))
                if echo is not None:
                    new_text, reason = echo
                    if not new_text:
                        # 没有现成变体：把这条短句掐掉，让模型重说一句不同的。
                        try:
                            event.clear_result()
                        except BaseException:
                            pass
                        event.stop_event()
                        _stat["short_echo_drop"] = _stat.get("short_echo_drop", 0) + 1
                        logger.info("[selfguard] 短句复读且无变体，本条不重复：%s", reason)
                        return
                    # 逐段改 Plain，和 dsh-aiflavour 的做法一致：不自己构造 chain，
                    # 免得丢掉 @/图片等非文本段。
                    done = False
                    try:
                        for comp in result.chain:
                            if isinstance(comp, Plain):
                                comp.text = new_text
                                done = True
                    except BaseException:
                        done = False
                    if not done:
                        try:
                            event.set_result(new_text)
                        except BaseException:
                            pass
                    _stat["short_echo"] = _stat.get("short_echo", 0) + 1
                    logger.info("[selfguard] 短句换说法：%s -> %s", text[:20], new_text)
                    # 换过说法的这句也要记进去，否则下一轮又会被判定成"没说过"
                    self._recent.setdefault(gid, deque(maxlen=KEEP)).append(new_text)
                    return
            if hit is None and TAUNT_ON and is_taunt(text):
                spark = looks_conflict(self._recent_human(gid))
                if spark:
                    hit = "拱火"
                    why = "自己在挑衅，而群里正在吵（%s）" % spark[:24]
                else:
                    logger.info("[selfguard] 这句像挑衅但群里没在吵，放行：%s", text[:36])
            if hit is None:
                return

            key = "shadow_repeat" if (SHADOW and hit == "自我重复") else (
                "shadow_taunt" if SHADOW else ("repeat" if hit == "自我重复" else "taunt"))
            _stat[key] = _stat.get(key, 0) + 1
            brief = "%s｜%s｜原话：%s" % (hit, why, text[:40])
            _last.append(time.strftime("%H:%M:%S ") + brief)
            del _last[:-8]
            if SHADOW:
                logger.info("[selfguard] 影子模式：本来要拦（%s）", brief)
                return

            level = 0
            if hit == "自我重复":
                uid = str(event.get_sender_id() or "")
                now = time.time()
                state_key = (gid, uid)
                old_state = self._impatient.get(state_key)
                level = impatient_level(old_state, now)
                reply = choose_impatient_reply(level, old_state.last_reply if old_state else "")
                self._impatient[state_key] = ImpatientState(level, now, reply)
                if len(self._impatient) > 200:
                    cutoff = now - IMPATIENT_RESET
                    self._impatient = {
                        state_key: state for state_key, state in self._impatient.items()
                        if state.last_at >= cutoff
                    }
            else:
                reply = choose_taunt_reply(self._last_taunt_reply)
                self._last_taunt_reply = reply
            try:
                event.set_result(reply)
            except BaseException:
                event.clear_result()
                event.stop_event()
            logger.info(
                "[selfguard] 出口拦下一条（%s）-> %s%s",
                brief, reply, "（不耐烦%d级）" % level if level else "",
            )
        except BaseException as exc:
            logger.warning("[selfguard] 闸门异常，放行: %r", exc)

    @filter.command("自制力状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        stopped = _stat["repeat"] + _stat["taunt"]
        yield event.plain_result(
            "出口自制力：%s%s｜群：%s\n"
            "看过 %d 条自己的回复，拦下 %d 条（自我重复 %d｜拱火 %d）\n"
            "影子模式下本来会拦：重复 %d｜拱火 %d\n"
            "判据：公共子串≥%d字 或 共享≥%d组bigram且占≥%.0f%%（比最近%d条）；"
            "重复命中按同一用户在%.0fs内递进为不耐烦1～3级，%.0fs未触发后重置；"
            "拱火需「自己挑衅」+「%.0fs内群里在吵」同时成立\n"
            "短句复读抑制：%s（≤%d字整句相同就换说法，不说不耐烦的话）｜换说法 %d 次｜"
            "无变体不重复 %d 次\n最近：%s"
            % (
                "开" if ENABLED else "关", "（影子模式）" if SHADOW else "",
                "、".join(sorted(GROUPS)) or "无", _stat["seen"], stopped,
                _stat["repeat"], _stat["taunt"], _stat["shadow_repeat"], _stat["shadow_taunt"],
                LCS_MIN, BG_MIN, BG_RATIO * 100, KEEP, IMPATIENT_WINDOW, IMPATIENT_RESET,
                CONFLICT_WINDOW,
                "开" if SHORT_ECHO_ON else "关", SHORT_ECHO_LEN,
                _stat["short_echo"], _stat["short_echo_drop"],
                "｜".join(_last[-3:]) or "还没有",
            )
        )
