# -*- coding: utf-8 -*-
"""Pure scoring and decision helpers for dsh-followup."""

from __future__ import annotations

import re

_ACK_RE = re.compile(
    r"^(?:嗯+|哦+|噢+|好+|行+|可以|知道了|收到|哈哈+|呵呵+|嘿嘿+|6+|草+|艹+|谢了|谢谢|ok|OK|好的)[！!。.]?$"
)
_CUE_RE = re.compile(
    r"^(?:那|然后|所以|但是|不过|而且|还有|确实|也是|对了|刚才|你说|这个|那个|它|他|她|继续)"
    r"|(?:呢|然后呢|怎么办|咋办)[？?]?$"
)
_QUESTION_RE = re.compile(r"[？?]|(?:吗|呢)\s*$|怎么|咋|为什么|为啥|哪个|哪里|是不是|能不能")
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]+|\d+(?:\.\d+)?")
# 这些二字组合自身没有话题辨识度，留下会让两句无关的话虚高。
_STOP_BIGRAMS = {
    "这个", "那个", "然后", "就是", "还是", "不是", "可以", "一个", "什么", "怎么",
    "感觉", "觉得", "知道", "真的", "现在", "刚才", "一下", "已经", "还有", "但是",
    "所以", "因为", "如果", "的话", "你说", "我说", "他们", "我们", "你们", "自己",
}


def is_short_ack(text: str) -> bool:
    """Return True for a turn-ending acknowledgement that needs no forced reply."""
    value = re.sub(r"\s+", "", text or "")
    return not value or bool(_ACK_RE.fullmatch(value))


def keyword_tokens(text: str) -> set[str]:
    """Extract dependency-free topic tokens suitable for short Chinese group messages.

    Chinese uses character bigrams so names and topic words such as “显卡/电脑/白米” survive
    without a tokenizer. Latin words and numbers stay whole. Generic bigrams are removed.
    """
    value = (text or "").lower()
    out = {m.group(0) for m in _WORD_RE.finditer(value)}
    for run in _CJK_RE.findall(value):
        if len(run) == 1:
            continue
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return {token for token in out if token not in _STOP_BIGRAMS}


def topical_score(current: str, context: str) -> tuple[int, list[str]]:
    """Return 0..100 topical continuity score and matching tokens.

    Coverage gets most weight: a short follow-up only needs its own keywords to be explained by
    the previous exchange; it need not repeat every word in the much longer context. Jaccard keeps
    one accidental common token from scoring too highly.
    """
    cur = keyword_tokens(current)
    old = keyword_tokens(context)
    if not cur or not old:
        base = 0.0
        hits: list[str] = []
    else:
        common = cur & old
        hits = sorted(common, key=lambda x: (-len(x), x))
        if not common:
            base = 0.0
        else:
            coverage = len(common) / len(cur)
            jaccard = len(common) / len(cur | old)
            ratio_score = 100.0 * (0.78 * coverage + 0.22 * jaccard)
            # 中文短追问会产生不少边界二元词；若只按集合占比，“显卡/散热”这种
            # 明确话题锚点反而会被分母稀释。一个经过停用词过滤的共同关键词给 45
            # 分，此后每多一个再加 12 分；比例分更高时仍取比例分。
            anchor_score = min(88.0, 45.0 + 12.0 * (len(common) - 1))
            base = max(ratio_score, anchor_score)

    compact = re.sub(r"\s+", "", current or "")
    # 续接词只能小幅加分，不能凭一句“然后呢”伪装成高度同话题。
    cue_bonus = 8 if _CUE_RE.search(compact) else 0
    question_bonus = 4 if _QUESTION_RE.search(compact) else 0
    return min(100, int(round(base + cue_bonus + question_bonus))), hits[:8]


def reply_probability(score: int) -> float:
    """Map topical continuity to a smooth 5%..95% follow-up wake probability."""
    bounded = max(0, min(100, int(score)))
    return round(0.05 + 0.90 * bounded / 100.0, 4)


def should_wake_followup(
    *,
    sender_id: str,
    target_id: str,
    text: str,
    context_text: str,
    now: float,
    expires_at: float,
    quotes_self: bool = False,
    at_other: bool = False,
    roll: float = 1.0,
    min_score: int = 20,
    quiet: bool = False,
) -> tuple[bool, str, int, float, list[str]]:
    """Score and sample whether an unmentioned message continues the bot conversation.

    ``min_score`` is a semantic floor, not merely a probability tuning knob.  Without it,
    completely unrelated messages still have the five-percent floor from ``reply_probability``
    and occasionally make the bot barge into a new topic.  ``quiet`` is supplied by the social
    boundary layer and suppresses only optional unmentioned follow-ups; an explicit quote still
    counts as a direct address and must be answered.
    """
    if quotes_self:
        return True, "引用了机器人", 100, 1.0, []
    if quiet:
        return False, "对方要求少打扰", 0, 0.0, []
    if not target_id or sender_id != target_id:
        return False, "不是上一轮对话对象", 0, 0.0, []
    if now > expires_at:
        return False, "续聊窗口已过", 0, 0.0, []
    if at_other:
        return False, "明确@了别人", 0, 0.0, []
    if (text or "").lstrip().startswith("/"):
        return False, "是指令", 0, 0.0, []
    if is_short_ack(text):
        return False, "只是收尾应答", 0, 0.0, []

    score, hits = topical_score(text, context_text)
    probability = reply_probability(score)
    floor = max(0, min(100, int(min_score)))
    if score < floor:
        return False, "上下文关联不足", score, probability, hits
    if roll < probability:
        return True, "上下文相似度抽中", score, probability, hits
    return False, "上下文相似度未抽中", score, probability, hits
