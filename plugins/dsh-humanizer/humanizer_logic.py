# -*- coding: utf-8 -*-
"""dsh-humanizer 纯逻辑：AI 痕迹特征表与剥/影子检测。不 import astrbot，可单测。

特征来源：照抄 blader/humanizer 的 25 条“AI 痕迹检测”里跟我们 QQ 群语境、
跟人格【AI 味黑名单】对齐的部分，按群聊风险分两档：
  STRONG —— 真人绝不会在群里自然说出来的 AI 典型腔（命中可剥/拦）。
  WEAK   —— 跟日常口语撞车、真人偶尔也会说的软痕迹（只记日志，绝不剥）。
"""

import os
import re


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


def _norm(s: str) -> str:
    """归一化：去掉所有空白，用于短语比对。"""
    return re.sub(r"\s+", "", s or "")


# 强信号：真人绝不会在群里自然说出来的 AI 典型腔。
STRONG_BASE: set[str] = {
    "综上所述", "总而言之", "作为一个语言模型", "作为AI", "作为 AI",
    "希望这个回答对你有帮助", "有什么可以帮您", "随时找我",
    "这是一个好问题", "这是一个值得思考的问题", "我理解你的感受",
    "大家今天有什么安排吗", "你们觉得呢", "大家觉得呢",
    "值得注意的是", "值得一提",
}

# 影子层：跟日常口语撞车，只记日志、绝不剥（防误伤）。
WEAK_BASE: set[str] = {
    "首先", "其次", "最后", "让我们", "本质上", "说到底",
    "不仅仅是", "不是", "专家认为", "研究表明",
    "前所未有", "前景光明", "标志着",
}

# 句尾收束词（人格【别收尾】）：命中只记日志。
CLOSERS: set[str] = {"行了", "好了", "就这样", "该睡了"}

# 破折号连用（“——…—”两个及以上）。
_DASH_RE = re.compile(r"(?:——|-{2,}).{0,12}?(?:——|-{2,})")


def repeated_openings(text: str) -> bool:
    """一段内重复句首：按句/分句切分，取每句开头 2~6 字，同一开头 >=3 次即判 AI 味。"""
    if not text:
        return False
    clauses = re.split(r"[。！？；\n]+", text)
    seen: dict[str, int] = {}
    for c in clauses:
        c = c.strip(" ，,。")
        if len(c) < 4:
            continue
        head = c[: min(6, len(c))]
        if len(head) < 2:
            continue
        seen[head] = seen.get(head, 0) + 1
        if seen[head] >= 3:
            return True
    return False


def load_patterns() -> tuple[list[str], list[str]]:
    """用 env 组装 (剥用的强信号正则片段列表, 影子段正则片段列表)，均按长度降序。"""
    strong_raw = STRONG_BASE | {_norm(x) for x in _set("DSH_HUMANIZER_STRONG") if x.strip()}
    weak_raw = WEAK_BASE | {_norm(x) for x in _set("DSH_HUMANIZER_WEAK") if x.strip()}
    strip_pat = sorted((re.escape(p) for p in strong_raw), key=len, reverse=True)
    shadow_pat = sorted((re.escape(p) for p in weak_raw), key=len, reverse=True)
    return strip_pat, shadow_pat


def strip_text(text: str, strip_pat: list[str]) -> tuple[str, list[str]]:
    """剥掉正文里所有强信号短语。返回 (新文本, 命中的原短语列表)。"""
    hits: list[str] = []
    cur = text or ""
    for pat in strip_pat:
        new = re.sub(r"\s*" + pat + r"\s*[,，。；!！?？]?", "", cur)
        if new != cur:
            m = re.search(pat, cur)
            hits.append(m.group(0) if m else pat)
            cur = new
    return cur, hits


def shadow_hits(text: str, shadow_pat: list[str]) -> list[str]:
    """影子层命中：只记日志的软痕迹，返回标签列表。"""
    out: list[str] = []
    for p in shadow_pat:
        if _norm(text).find(_norm(p)) >= 0:
            out.append(p)
    for c in CLOSERS:
        if (text or "").rstrip().endswith(c):
            out.append("收尾词:" + c)
    if _DASH_RE.search(text or ""):
        out.append("破折号连用")
    if repeated_openings(text or ""):
        out.append("重复句首")
    return out
