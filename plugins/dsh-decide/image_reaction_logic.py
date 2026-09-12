"""dsh-decide 的纯图片自然反应判据（无 AstrBot 依赖，便于单测）。"""

import re


# OneBot/AstrBot 对纯图片、动图表情的常见文本占位。只剥占位，不吞掉用户真文字。
_MEDIA_PLACEHOLDER_RE = re.compile(
    r"\[(?:图片|动画表情|表情|Image|image|/[^\]\r\n]{1,24})\]"
    r"|\[CQ:image,[^\]]*\]",
    re.IGNORECASE,
)
_IMAGE_CAPTION_RE = re.compile(
    r"<image_caption>\s*(.*?)\s*</image_caption>",
    re.IGNORECASE | re.DOTALL,
)


def meaningful_image_text(message: str) -> str:
    """去掉媒体占位后剩下的用户真文字。"""
    text = _MEDIA_PLACEHOLDER_RE.sub(" ", message or "")
    return " ".join(text.split())


def is_image_only(has_image: bool, message: str) -> bool:
    """消息确实含图，且除图片占位外没有文字。"""
    return bool(has_image) and not meaningful_image_text(message)


def current_image_caption(parts, limit: int = 240) -> str:
    """只取框架为当前消息生成的 <image_caption>，不误拿历史图片上下文。"""
    found = []
    for part in parts or ():
        text = str(getattr(part, "text", part) or "")
        for match in _IMAGE_CAPTION_RE.finditer(text):
            caption = " ".join(match.group(1).split())
            if caption and caption not in found:
                found.append(caption)
    return "；".join(found)[: max(0, int(limit))]


def image_dive_rate(flags: dict, rates: dict) -> float:
    """纯图片候选的潜水率；越像在直接互动，越愿意回应。"""
    if flags.get("replying_to_bot") and not flags.get("ack"):
        key = "reply"
    elif flags.get("about_bot"):
        key = "about"
    elif flags.get("open"):
        key = "open"
    elif flags.get("banter"):
        key = "banter"
    else:
        key = "none"
    try:
        value = float(rates[key])
    except (KeyError, TypeError, ValueError):
        value = 1.0
    return min(1.0, max(0.0, value))
