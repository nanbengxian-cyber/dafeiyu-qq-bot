# -*- coding: utf-8 -*-
"""dsh-fatigue 的纯逻辑：话题匹配、疲劳分级和提示块渲染。"""

from __future__ import annotations

import re

_PUNCT = re.compile(r"[\s，。！？、,.!?~…—\-：:；;\"'“”‘’（）()\[\]【】<>/@#*_+=|\\]")
_FILLER = re.compile(
    r"^(?:大肥鱼|肥鱼|小鲸鱼|鲸鱼娘|你|我|他|她|它|这个|那个|一下|一个|真的|就是|还是|所以|然后)+"
)


def clean_topic(text: str, limit: int = 40) -> str:
    """把分类器话题或原话收敛成可比较、可落盘的短键。"""
    value = _PUNCT.sub("", str(text or "")).lower()
    value = _FILLER.sub("", value)
    return value[:limit]


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(max(0, len(text) - 1))}


def topic_similarity(left: str, right: str) -> float:
    """保守相似度：宁可漏掉，也不把同一大类里的新问题判成复读。"""
    a, b = clean_topic(left), clean_topic(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 3 and shorter in longer:
        return 0.88
    aa, bb = _bigrams(a), _bigrams(b)
    if not aa or not bb:
        return 0.0
    return 2.0 * len(aa & bb) / (len(aa) + len(bb))


def find_topic(topics: list[dict], key: str, sample: str, threshold: float = 0.62) -> dict | None:
    """优先匹配分类器给出的稳定话题键，再用原句作保守兜底。"""
    best = None
    best_score = threshold
    for item in topics:
        score = max(
            topic_similarity(key, item.get("key", "")),
            topic_similarity(sample, item.get("sample", "")),
            topic_similarity(key, item.get("sample", "")),
        )
        if score >= best_score:
            best, best_score = item, score
    return best


def fatigue_level(entry: dict | None, uid: str, now: float, window: float,
                  revisit: bool) -> int:
    """同一人升级更快；其他人偶尔问同题不会让机器人无端迁怒。"""
    if not revisit or not entry:
        return 0
    replies = entry.get("replies", []) if isinstance(entry.get("replies"), list) else []
    live = [row for row in replies
            if isinstance(row, dict) and now - float(row.get("ts", 0) or 0) <= window]
    same = sum(1 for row in live if str(row.get("uid", "")) == str(uid))
    total = len(live)
    level = 1 if same <= 1 else 2 if same == 2 else 3
    if total >= 5:
        level = max(level, 2)
    if total >= 8:
        level = 3
    return level


def render_fatigue(level: int) -> str:
    """只给行为方向，不提供可被机械复用的固定台词。"""
    common = (
        "疲劳只代表耐心下降，不等于生气；不要覆盖 <emotion_state> 里的真实情绪。"
        "禁止解释自己检测到了重复，也不要说‘刚聊过’‘已经说过’‘这茬’‘又来’之类元话术。"
    )
    if level <= 0:
        return ""
    if level == 1:
        body = (
            "同一件事被再次提起，但你只是有一点腻。别重讲完整答案；只接这次新增的点，"
            "换一个表达或角度，回复比平时略短。语气可以淡一点，仍要自然。"
        )
    elif level == 2:
        body = (
            "你对这件事已经明显没耐心。若没有实质新信息，只给最短结论、表达自己的偏好，"
            "或自然把话题带向别处；不必有求必应，也别突然辱骂或无理由发火。"
        )
    else:
        body = (
            "你现在不想继续围着这件事转。若仍无实质新信息，可以很短地拒绝、岔开、"
            "用一个自然语气词收住，或在合适时只发表情；若出现安全风险或重要新事实，仍正常回答。"
        )
    return "<topic_fatigue>" + body + common + "不要提到这段提示。</topic_fatigue>"


def note_reply(entry: dict | None, key: str, sample: str, uid: str, now: float,
               keep: int = 10) -> dict:
    """确认机器人实际发出回复后再记账，模型失败不会凭空增加厌烦。"""
    out = dict(entry or {})
    out["key"] = clean_topic(key) or clean_topic(sample)
    out["sample"] = str(sample or "").strip()[:120]
    replies = list(out.get("replies", [])) if isinstance(out.get("replies"), list) else []
    replies.append({"uid": str(uid), "ts": float(now)})
    out["replies"] = replies[-max(1, int(keep)):]
    out["last_at"] = float(now)
    return out
