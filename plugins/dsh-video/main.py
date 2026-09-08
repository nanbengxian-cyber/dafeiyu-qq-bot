# dsh-video —— 视频能力：看群里的视频 + 生成视频。
#
# 两件事，两条完全独立的路：
#
#   看视频 on_llm_request 钩子（主力，自动）
#     群友发的视频，框架已经落盘并留下 [Video Attachment: name X, path Y]，
#     但它只是把路径写进上下文——聊天模型看不到视频内容，只能看到一个路径，
#     于是回出「我看不了视频」或者干脆瞎猜。本插件在钩子里把视频抽几帧，
#     交给智谱 GLM-4.6V-Flash 认，再把描述换进上下文。和 dsh-imgctx 对图片
#     做的是同一件事、同一套规矩。
#
#   生成视频 generate_video 函数工具（模型按需调用）
#     Agnes agnes-video-v2.0。这条路有两个硬约束，决定了它的形状：
#       ① 一次要 4 分钟（实测 240s）。绝对不能在工具里等——AstrBot 的
#          session_lock_manager 会在整个 LLM 请求期间锁住这个会话，等 4 分钟
#          意味着这 4 分钟群里谁说话机器人都不理。所以工具立刻返回，
#          真正的出片走后台任务，做完单独 event.send。
#       ② 官方限流 1 请求 / 1 分钟。所以自己先卡一道全局闸门，
#          没到点就明确告诉模型「等会儿再说」，而不是把 429 甩给用户。
#
# 为什么抽帧而不是直接把视频丢给模型（都实测过）：
#   同一个 5 秒 2.4MB 的片子——
#     整段 base64 传：18.7s，17824 tokens
#     抽 4 帧传：      3.5s，  959 tokens，描述质量一样
#   群里视频动辄十几秒几 MB，抽帧是唯一现实的选择。代价是听不到声音、
#   看不出中间的快速动作，所以提示词里会明说「这是抽帧」，让模型别把
#   静态描述当成完整剧情来聊。

import asyncio
import os
import random
import re
import subprocess
import time
import uuid

import aiohttp

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Video
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import MessageChain

# ---------------------------------------------------------------- 通用泄漏清理
#
# 模型把函数调用当正文写出来时，无论是哪个工具的，都不能进群。
# 所以这里认全部六个工具名 —— 每个插件都清干净，谁先跑谁清完，幂等。
# 只有「抠参数拿来兜底」才是各插件自己的事。
_TOOL_NAMES = (
    "generate_image|send_voice|web_search|read_webpage|bilibili_video|generate_video"
)
# 反引号外壳。实测模型爱把伪调用写成 `generate_image(...)` 或
# ```send_voice(...)```，光清括号里的调用会留下光秃秃的 `` 进群
# （本轮冒烟测试五条里中了两条：「这就来\n``」「`` 晚安～」）。
# 右侧的 (?![A-Za-z]) 是防止吃掉群友真代码块的开栏 ```python。
_FENCE_L = r"(?:`{1,3}[ \t]*\n?)?"
_FENCE_R = r"(?:[ \t]*\n?`{1,3}(?![A-Za-z]))?"
# 形态 A：name(...) 括号闭合。非贪婪，允许一层嵌套括号。
_LEAK_A = re.compile(
    rf"{_FENCE_L}\b(?:{_TOOL_NAMES})\s*\([^()]*(?:\([^()]*\)[^()]*)*\){_FENCE_R}",
    re.S,
)
# 形态 B：name( 一直到全角右括号 —— 实测模型把 ) 打成了 ）
_LEAK_B = re.compile(
    rf"{_FENCE_L}\b(?:{_TOOL_NAMES})\s*\([^\n）]*）{_FENCE_R}", re.S
)
# 形态 C：name( 之后再没有右括号，一直到空行或文本结尾。
#         实测「generate_image(prompt=蓝白配色的小鲸鱼，…无复杂背景」就是这样。
#         限定「不跨空行」，避免把后面独立的段落也吃掉。
_LEAK_C = re.compile(
    rf"{_FENCE_L}\b(?:{_TOOL_NAMES})\s*\((?:[^\n]*)(?:\n(?!\s*\n)[^\n]*)*",
    re.S,
)
# 形态 D / E：XML 与 ```json 包裹
_LEAK_D = re.compile(
    r"<\s*(?:antml:)?function_calls\s*>.*?(?:</\s*(?:antml:)?function_calls\s*>|$)"
    rf"|<\s*(?:antml:)?invoke\b[^>]*(?:{_TOOL_NAMES}).*?"
    r"(?:</\s*(?:antml:)?invoke\s*>|$)",
    re.S | re.I,
)
_LEAK_E = re.compile(
    rf"```(?:json|tool_code)?\s*\{{[^`]*(?:{_TOOL_NAMES})[^`]*\}}\s*```",
    re.S | re.I,
)
# 形态 X：用工具名直接当标签名 —— `<send_voice>要念的话</send_voice>`。
# 实测「用语音念一段绕口令」整段进了群。闭合标签可缺（读到结尾）。
_LEAK_X = re.compile(
    rf"<\s*(?:{_TOOL_NAMES})\s*>.*?(?:</\s*(?:{_TOOL_NAMES})\s*>|$)",
    re.S | re.I,
)
# 形态 Y：`[工具名:参数]` —— 直接照抄人格里的 [贴纸:名称] 的形状。
# 实测「[send_voice:四是四，十是十…]」「[generate_image: 一只可爱的Q版小猫…]」。
# 判据只看方括号里的名字是不是工具名：[贴纸:x] 和 [At:123] 不能碰。
# 右括号可缺（模型有时忘了闭合），那就读到行尾。
_LEAK_Y = re.compile(
    rf"[\[【]\s*(?:{_TOOL_NAMES})\s*[:：][^\]】\n]*[\]】]?",
    re.I,
)
_LEAK_FRAG = re.compile(
    rf"</?\s*(?:antml:)?(?:function_calls|invoke|parameter|{_TOOL_NAMES})\b[^>]*>?",
    re.I,
)


# 恰好两个连续反引号 = 空的行内代码段，任何正常文本里都不该出现。
# 无条件清，顺带兜住「上一个插件清完、残渣传到下一个插件」的情形。
# 1 个（`pip install x`）和 3 个（```python）一律不碰。
_EMPTY_SPAN = re.compile(r"(?<!`)``(?!`)")
# 单独成行的反引号（跨行围栏被清空后的残渣）
_LONE_FENCE_LINE = re.compile(r"(?m)^[ \t]*`{1,3}[ \t]*$\n?")

# 模型自造的伪媒体标记。人格教了它 [贴纸:名称]，它就泛化出 [语音]/[图片]
# 当舞台提示写，实测两次都长在行首：「[语音] 行吧念给你听——…」。
# 这类标记不会变成任何真东西（真语音是插件发的），只会原样进群，
# 还会被 _clean_for_tts 当正文念出来。
# 三重收窄防误伤：① 只认行首；② 括号里只有那个词（「[语音条挺长]」不动）；
# ③ 带冒号的一律不碰（[贴纸:嘲笑] 有自己的链，[At:123] 是框架的）。
_MEDIA_TAG_RE = re.compile(
    r"(?m)^[ \t\u3000]*[\[【]\s*"
    r"(?:语音|語音|音频|音頻|voice|audio|图片|圖片|image|photo|pic"
    r"|视频|視頻|video|表情|动图|動圖|gif)"
    r"\s*[\]】][ \t\u3000]*"
)


# 形态 V：模型自己加的旁白 —— 「（图片自动发给你）」「（语音已发送）」。
# 不是伪调用，但同属「把机制说出来」这类：东西已经发了，这句纯属多余。
# 清括号比清标记危险得多（群友也会用括号讲话），所以三个条件同时满足才清：
# 内容 ≤14 字 + 含媒体名词 + 含发送动作词。
# 「（这图真好看）」缺动作词、「（我发链接给你）」缺媒体名词，都留着。
_STAGE_NOTE_RE = re.compile(
    r"[（(]"
    r"(?=[^）)\n]{0,14}[）)])"
    r"(?=[^）)\n]*(?:图片|图|语音|音频|视频|表情包|贴纸))"
    r"[^）)\n]*(?:自动发|已发|发给|发送|附上|见下|在下面|随后发|马上发)"
    r"[^）)\n]*[）)]"
)

# 形态 W：不带参数的裸标记 —— `[tool_call]`、`[工具调用]`、`[函数调用]`。
# 前面所有形态都要求有冒号或括号，这种光秃秃的全放过了。实测进过群。
_BARE_CALL_RE = re.compile(
    r"[\[【]\s*(?:tool_call|tool_calls|function_call|function_calls|"
    r"工具调用|函数调用|调用工具|工具call)\s*[\]】]",
    re.I,
)
# 清理完一个字都不剩时的回落短话 —— 群里只看到一个光秃秃的 @ 更像 bug。
_EMPTY_FALLBACKS = ("这就来", "等着", "来了", "行", "好嘞")

# 形态 Z：把工具名翻译成中文再套方括号 —— `[生成图片: 一只猫]`。
# 判据是**动词开头**而不是枚举译名：模型换个说法（制作视频／合成语音）照样命中。
# [贴纸:x]、[At:1]、[引用消息(张三: 在吗)]、[备注:x] 都不以这些动词开头，安全。
_CN_ACTS = "生成|发送|调用|播放|合成|搜索|联网|读取|查看|获取|制作|画"
_LEAK_Z = re.compile(
    rf"[\[【]\s*(?:{_CN_ACTS})[^\]】\n]{{0,10}}[:：][^\]】\n]*[\]】]?"
)
# 中文译名 -> 英文工具名，供 _extract_arg 抠参数用。
_CN_TOOL_ALIAS = {
    "generate_image": r"生成图片|生成图像|画图|生成一张图|作图|绘图|制作图片",
    "send_voice": r"发送语音|语音合成|合成语音|播放语音|发语音",
    "generate_video": r"生成视频|制作视频|生成动画|做视频",
    "web_search": r"联网搜索|网页搜索|搜索网页|搜索",
    "read_webpage": r"读取网页|查看网页|打开网页",
    "bilibili_video": r"查看视频信息|获取视频信息|查B站",
}

# markdown 图片：![alt](url)。QQ 不渲染 markdown，这东西一定原样进群，
# 而且实测那个地址是模型现编的（pollinations.ai，插件根本没用过）。
# 整段删：alt 文字对群友没有任何用处。
_MD_IMG_RE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
# markdown 链接：[文字](url) -> 只留文字。同样是「QQ 不渲染」的问题。
# 限定 http(s) 开头，避免误伤别的方括号写法；[贴纸:名称] 后面没有 (...)，
# 本来就不会命中。
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]*)\]\(\s*https?://[^)\n]*\)")


def _strip_all_leaks(text: str) -> str:
    """把任何工具的泄漏调用标记从正文里清掉。顺序要紧：先精确后兜底。"""
    out = text or ""
    before = out
    for pat in (_LEAK_D, _LEAK_E, _LEAK_X, _LEAK_Y, _LEAK_A, _LEAK_B, _LEAK_C):
        out = pat.sub("", out)
    out = _LEAK_FRAG.sub("", out)
    out = _EMPTY_SPAN.sub("", out)
    if out != before:
        # 真的清掉过泄漏，才去处理落单的反引号。
        # 没泄漏时一个字符都不动 —— 群友正常发的代码不能被弄坏。
        # 这个判据只看泄漏，不看下面的伪媒体标记：单纯清个 [语音]
        # 不该连带动人家的反引号。
        out = _LONE_FENCE_LINE.sub("", out)
        if out.count("`") % 2:
            out = out.replace("`", "")
    out = _MEDIA_TAG_RE.sub("", out)
    out = _LEAK_Z.sub("", out)
    out = _BARE_CALL_RE.sub("", out)
    out = _STAGE_NOTE_RE.sub("", out)
    out = _MD_IMG_RE.sub("", out)
    out = _MD_LINK_RE.sub(r"\1", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _extract_arg(text: str, tool: str, arg: str) -> str | None:
    """从泄漏的调用里抠出某个参数的值。带引号、不带引号都认。"""
    src = text or ""
    # 带引号：arg="值" / arg='值'
    m = re.search(
        rf"\b{tool}\s*\([^)]*?\b{arg}\s*=\s*(?P<q>[\"\'])(?P<v>.*?)(?<!\\)(?P=q)",
        src, re.S,
    )
    if m:
        v = (m.group("v") or "").strip()
        if v:
            return v
    # XML：<parameter name="arg">值</parameter>
    m = re.search(
        rf"<\s*(?:antml:)?parameter\s+name\s*=\s*[\"\']{arg}[\"\']\s*>(.*?)"
        r"(?:</\s*(?:antml:)?parameter\s*>|$)",
        src, re.S | re.I,
    )
    if m:
        v = m.group(1).strip()
        if v:
            return v
    # JSON："arg": "值"
    m = re.search(
        rf"[\"\']{arg}[\"\']\s*:\s*[\"\'](.+?)[\"\']", src, re.S
    )
    if m:
        v = m.group(1).strip()
        if v:
            return v
    # 裸值：arg=值 一直到右括号（半角/全角）或行尾/结尾。
    # 这是本轮新增的形态，实测最常见。
    m = re.search(
        rf"\b{tool}\s*\(\s*(?:\w+\s*=\s*[^,)]*,\s*)*?{arg}\s*=\s*"
        r"(?P<v>[^\n]*?)\s*(?:[\)）]|$)",
        src, re.S,
    )
    if m:
        v = (m.group("v") or "").strip().strip("\"'")
        if v:
            return v
    # 裸标签：<send_voice>要念的话</send_voice> —— 标签里包的就是参数值。
    # 放最后：前面几种形态更精确，能命中就不必走这里。
    m = re.search(
        rf"<\s*{tool}\s*>(.*?)(?:</\s*{tool}\s*>|$)", src, re.S | re.I
    )
    if m:
        v = (m.group(1) or "").strip().strip("\"'")
        if v:
            return v
    # 方括号形态：[send_voice:要念的话] —— 冒号后面就是参数值。
    m = re.search(
        rf"[\[【]\s*{tool}\s*[:：]([^\]】\n]*)[\]】]?", src, re.I
    )
    if m:
        v = (m.group(1) or "").strip().strip("\"'")
        if v:
            return v
    # 中文译名：[生成图片: 一只猫]。实测模型会把工具名翻成中文再套方括号。
    alias = _CN_TOOL_ALIAS.get(tool)
    if alias:
        m = re.search(
            rf"[\[【]\s*(?:{alias})\s*[:：]([^\]】\n]*)[\]】]?", src
        )
        if m:
            v = (m.group(1) or "").strip().strip("\"'")
            if v:
                return v
    return None



def _leak_relay(event, raw, tool, arg):
    """清理 + 抠参数，并在插件之间接力原文。

    坑：四个插件的 on_llm_response 钩子按**插件加载顺序**依次跑
    （call_event_hook 就是拿 registry 顺序 for 循环，没有优先级）。
    谁先跑谁就把泄漏标记清掉了，后面的插件再想从标记里抠参数就什么
    都拿不到 —— 实测 dsh-video 最先加载，于是 imagegen 和 voice 的
    「抠自泄漏调用」这条高保真路径直接失效（日志里能看到
     [video] 已清理… 之后 imagegen 只能自己重新推 prompt）。

    所以：第一个发现泄漏的插件把**原文**存进 event.extra，后面的都从
    这儿读。清理本身是幂等的，重复跑没有副作用。
    """
    raw = raw or ""
    cleaned = _strip_all_leaks(raw)
    if raw.strip() and not cleaned.strip():
        # 整条回复就是一个泄漏标记，清完什么都不剩。四个插件按加载顺序跑，
        # 第一个动手的换成短话，后面的看到 cleaned == raw 就不再动，不会叠加。
        cleaned = random.choice(_EMPTY_FALLBACKS)
    src = raw
    if cleaned != raw:
        # 我是第一个动手的，把原文留给后面的插件
        try:
            if not event.get_extra("dsh_leak_raw"):
                event.set_extra("dsh_leak_raw", raw)
        except Exception:  # noqa: BLE001
            pass
    else:
        # 已经被前面的插件清过了，去接力里取原文
        try:
            src = event.get_extra("dsh_leak_raw") or raw
        except Exception:  # noqa: BLE001
            src = raw
    leaked = _extract_arg(src, tool, arg) if tool else None
    return cleaned, leaked



# ---------------------------------------------------------------- 配置

# ---- 看视频 ----
UNDERSTAND = os.environ.get("DSH_VID_UNDERSTAND", "1") not in ("0", "false", "False")
VIS_BASE = os.environ.get(
    "DSH_VID_VIS_BASE", "https://open.bigmodel.cn/api/paas/v4"
).rstrip("/")
VIS_KEY = os.environ.get("DSH_VID_VIS_KEY", "")
VIS_MODEL = os.environ.get("DSH_VID_VIS_MODEL", "GLM-4.6V-Flash")
# 同一个站上的备用模型（逗号分隔），主用挂了按顺序试。
# 跟 dsh-voice / dsh-imagegen 的 fallback 列表是同一个形状。
VIS_MODEL_FALLBACKS = [
    s.strip() for s in os.environ.get("DSH_VID_VIS_MODEL_FALLBACKS", "").split(",") if s.strip()
]
# 换一个站的最后兜底（三个都填才生效）。
# 留着智谱当兜底的意义：主站在 Cloudflare 后面，偶发整片 403 时还有一条独立线路。
ALT_BASE = os.environ.get("DSH_VID_VIS_ALT_BASE", "").rstrip("/")
ALT_KEY = os.environ.get("DSH_VID_VIS_ALT_KEY", "")
ALT_MODEL = os.environ.get("DSH_VID_VIS_ALT_MODEL", "")
# 每档原地重试几次（不含首次）。403/429/5xx/超时才重试，
# 4xx 参数错重试也不会变好。
VIS_RETRY = int(os.environ.get("DSH_VID_VIS_RETRY", "2"))


def _vis_targets() -> list[tuple[str, str, str]]:
    """识别链：[(base, key, model), ...] 前面的优先。"""
    out: list[tuple[str, str, str]] = []
    if VIS_BASE and VIS_KEY and VIS_MODEL:
        out.append((VIS_BASE, VIS_KEY, VIS_MODEL))
    for m in VIS_MODEL_FALLBACKS:
        if VIS_BASE and VIS_KEY:
            out.append((VIS_BASE, VIS_KEY, m))
    if ALT_BASE and ALT_KEY and ALT_MODEL:
        out.append((ALT_BASE, ALT_KEY, ALT_MODEL))
    return out
# 抽几帧。4 帧够描述一个短片，再多就是浪费 token
FRAMES = int(os.environ.get("DSH_VID_FRAMES", "4"))
FRAME_W = int(os.environ.get("DSH_VID_FRAME_W", "448"))
# 超过这个长度只抽前面这段（群里偶尔有人发几分钟的录屏）
MAX_SECONDS = int(os.environ.get("DSH_VID_MAX_SECONDS", "60"))
VIS_TIMEOUT = int(os.environ.get("DSH_VID_VIS_TIMEOUT", "60"))
# 看视频的总预算：宁可这轮不看，也不能让群里等
UNDERSTAND_BUDGET = float(os.environ.get("DSH_VID_BUDGET", "40"))

# ---- 生成视频 ----
GEN = os.environ.get("DSH_VID_GEN", "1") not in ("0", "false", "False")
GEN_BASE = os.environ.get("DSH_VID_GEN_BASE", "https://apihub.agnes-ai.com/v1").rstrip("/")
GEN_KEY = os.environ.get("DSH_VID_GEN_KEY", "")
GEN_MODEL = os.environ.get("DSH_VID_GEN_MODEL", "agnes-video-v2.0")
GEN_SECONDS = os.environ.get("DSH_VID_GEN_SECONDS", "5")
GEN_SIZE = os.environ.get("DSH_VID_GEN_SIZE", "1280x720")
# 官方限流是 1 请求 / 1 分钟，自己留点余量
GEN_INTERVAL = float(os.environ.get("DSH_VID_GEN_INTERVAL", "75"))
# 同一会话多久才能再要一个（出片 4 分钟，别让人连点）
GEN_COOLDOWN = float(os.environ.get("DSH_VID_GEN_COOLDOWN", "300"))
GEN_POLL = float(os.environ.get("DSH_VID_GEN_POLL", "15"))
GEN_MAX_WAIT = float(os.environ.get("DSH_VID_GEN_MAX_WAIT", "600"))
# 画风后缀，和 dsh-imagegen 保持一致的审美
GEN_STYLE = os.environ.get(
    "DSH_VID_GEN_STYLE",
    "anime style, cute, clean line art, soft colors, simple background",
)

TMP_DIR = os.environ.get("DSH_VID_TMP", "/AstrBot/data/video")
# base64 之后的上限。5s 720p 约 1.8MB，留足余量；再大就是别的问题了
MAX_SEND_B64 = int(os.environ.get("DSH_VID_MAX_SEND", str(24 * 1024 * 1024)))

# 框架留下的视频附件标记
def _raw_has_video(event) -> bool:
    """这条消息的 OneBot 原始段里到底有没有视频。

    用来区分两种「没识别到视频」：
      · 群里本来就没视频 —— 正常，不用出声
      · 群里有视频但框架没转成 [Video Attachment] —— 功能坏了，必须出声
    实测后者是常态（适配器忽略 video 段），前者才是少数。
    """
    try:
        raw = getattr(event.message_obj, "raw_message", None)
        msg = raw.get("message") if isinstance(raw, dict) else getattr(raw, "message", None)
        if not isinstance(msg, list):
            return False
        return any(str((seg or {}).get("type", "")) == "video" for seg in msg)
    except BaseException:
        return False


ATTACH_RE = re.compile(
    r"\[Video Attachment(?: in quoted message)?:\s*name\s+([^,\]]+),\s*path\s+([^\]]+)\]"
)

# path -> 描述。同一个视频不重复识别（群友爱重复转发）
_cache: dict[str, str] = {}
# 生成侧的闸门
_gen_last_global = 0.0
_gen_last_session: dict[str, float] = {}
_gen_lock = asyncio.Lock()
# 正在跑的后台出片任务，防止插件重载时野任务乱飞
_gen_tasks: set[asyncio.Task] = set()

# ---------------------------------------------------------------- 意图与泄漏
#
# 端到端实测抓到的真实故障：用户说「给我做个视频，一条卡通鲸鱼在海里游」，
# 模型回的是
#     好嘞，这就给你整，等个几分钟嗷。\n\ngenerate_video(prompt="蓝白色卡通小鲸鱼…")
# tool_calls 空的，函数调用被当纯文本写了出来。后果有两个：那行 generate_video(...)
# 原样进群；视频压根没做。dsh-imagegen（XML 形态）和 dsh-voice（Python 调用形态）
# 都栽过同一个跟头，这里是第三次——便宜模型就是这样，插件必须自己兜住。

# 用户在要视频。视频词是硬要求：只有「做个」不算，得说「做个视频」。
VIDEO_INTENT_RE = re.compile(
    r"(视频|动画|短片|影片|片子|动图片段|小电影|录像)"
)
# 要「做」而不是「看」。「这视频啥意思」不该触发生成。
# 「给我/帮我/替我」是**受益者标记**，不是创建动词 —— 必须另有真动词。
# 原来它们跟「做/生成」并列，于是「给我…视频」不管中间是什么动词都命中：
# 实测「去给我找一个玉足视频」（找＝搜已有的）、「帮我查看这个视频」都误触发过。
VIDEO_MAKE_RE = re.compile(
    r"(?:给我|帮我|替我)?\s*(做|生成|整|搞|弄|来|画|拍|出|制作)\s*(?:一)?\s*"
    r"(?:个|段|条|部|支|下|点)?\s*"
    # 宾语长度不设上限，只要求不跨标点。掐字数会漏掉
    # 「做个小鲸鱼甩尾巴的动画」这种正常说法（实测宾语 7 字就漏）。
    r"(?:[^，,。.！!？?；;\n]{0,16}?)(视频|动画|短片|影片|片子|小电影)"
    r"|(视频|动画|短片)\s*(?:做|生成|整|搞|弄|来)"
)
# 用户在要求「理解一段已有的视频」，不是要做新的。
#
# 为什么必须单独一条而不是去精修 VIDEO_MAKE_RE：动词表里有「帮我/给我」，
# 加上中间的 .{0,6}? 通配，「帮我查看这个视频」会整串命中「做视频」。
# 实测 2026-09-03 09:13 真群就这么误触发过一次，白烧了一次出片额度，
# 而且模型没真看视频就凭链接瞎编了内容。
#
# 「做」和「看」是两个独立意图，各自单独识别 —— 指望一条正则同时管好
# 两件事，就会一直在动词表上打补丁（dsh-imagegen 已经踩过三次）。
#
# 命中后让路给：本插件的 attach_video 钩子（群里直接发的视频文件）、
# dsh-web 的 bilibili_video / read_webpage 工具（视频链接）。
VIDEO_WATCH_RE = re.compile(
    # 理解类动词 + 视频词。「看」单独成词太泛（「看我做的视频」是要做），
    # 所以只收明确表示「理解/转述」的说法。
    r"(查看|看看|看一下|看下|看看这|帮我看|给我看|瞧瞧|识别|辨认"
    r"|总结|概括|讲讲|说说|讲一下|说一下|介绍|解说|解读|分析|翻译)"
    r"[^\n]{0,8}?(视频|动画|短片|影片|片子|录像|b23|BV)"
    # 「这视频讲了啥」「视频里说了什么」—— 视频词在前，疑问在后
    r"|(视频|动画|短片|影片|片子|录像)\s*(里|中|内容)?\s*"
    r"(讲|说|是|演|放)[^\n]{0,6}?(什么|啥|内容|意思|谁)"
    r"|(视频|动画|短片|影片|片子)\s*(啥|什么)\s*(意思|内容)"
    # 视频平台链接：话里带这个就是让它去看，不可能是让它凭空做
    r"|b23\.tv|bilibili\.com|BV[0-9A-Za-z]{8,}|youtu\.be|youtube\.com/watch"
    r"|douyin\.com|v\.qq\.com|iqiyi\.com"
)
# 明确的图片名词。用户说「做个视频封面」「画个动画风格的壁纸」，要的是**图**，
# 视频/动画只是修饰语 —— 这时让路给 dsh-imagegen。
# 与 dsh-imagegen 里的 EXPLICIT_IMG_RE 是同一张表，两边镜像互斥：
#   imagegen 在「有视频/语音词且无图片名词」时让路；
#   本插件在「有图片名词」时让路。
# 刻意不收「画」和「画面」：「动画」带个画字，收了就把正常的做视频请求也否决了。
IMG_NOUN_RE = re.compile(
    r"(图|图片|照片|壁纸|头像|表情包|自拍|插画|海报|封面|立绘|简笔画)"
)
# 模型明确说做不了
GEN_REFUSE_RE = re.compile(
    r"(不能|不会|无法|没法|做不到|不支持|办不到|做不了)\s*(做|生成|出|视频|动画)"
    r"|我\s*(还)?\s*(不能|不会|没法|无法)\s*(做|生成)"
)
# 模型在反问要做什么
GEN_ASK_BACK_RE = re.compile(
    r"(做|生成|画|拍)\s*(什么|啥)\s*(视频|动画|样|内容)?"
    r"|想看\s*(什么|啥)|要\s*(什么|啥)\s*(样的)?\s*(视频|动画)"
)

# 泄漏形态一：generate_video(prompt="…")
LEAK_CALL_RE = re.compile(
    r"\bgenerate_video\s*\(\s*prompt\s*=\s*(?P<q>[\"\'])(?P<v>.*?)"
    # 收尾允许缺右引号/右括号：实测模型就漏过一个右引号（…画面简洁）
    r"(?:(?<!\\)(?P=q)\s*\)?|\s*[）)]|$)",
    re.S,
)
# 泄漏形态二：<invoke name="generate_video"><parameter name="prompt">…
LEAK_XML_RE = re.compile(
    r"<\s*(?:antml:)?function_calls\s*>.*?(?:</\s*(?:antml:)?function_calls\s*>|$)"
    r"|<\s*(?:antml:)?invoke\b[^>]*generate_video.*?(?:</\s*(?:antml:)?invoke\s*>|$)",
    re.S | re.I,
)
LEAK_XML_PROMPT_RE = re.compile(
    r"<\s*(?:antml:)?parameter\s+name\s*=\s*[\"\']prompt[\"\']\s*>(.*?)"
    r"(?:</\s*(?:antml:)?parameter\s*>|$)",
    re.S | re.I,
)
# 泄漏形态三：```json {"name":"generate_video","arguments":{"prompt":"…"}} ```
LEAK_JSON_RE = re.compile(
    r"```(?:json|tool_code)?\s*\{[^`]*generate_video[^`]*\}\s*```", re.S | re.I
)
LEAK_JSON_PROMPT_RE = re.compile(r"[\"\']prompt[\"\']\s*:\s*[\"\'](.+?)[\"\']", re.S)
LEAK_FRAG_RE = re.compile(
    r"</?\s*(?:antml:)?(?:function_calls|invoke|parameter)\b[^>]*>?", re.I
)

# 从用户原话里推画面描述（模型没留下 prompt 时用）。
#
# 逐词剥离是个陷阱：把「条」当量词无脑删掉，「穿条纹衬衫的女孩」会变成
# 「穿纹衬衫的女孩」。所以只整块剥「祈使短语」——动词+量词+视频名词连在
# 一起才算前缀，单独的量词一律不动。
CMD_PREFIX_RE = re.compile(
    r"^\s*(?:@\S+\s*)?"                                   # 可能的 @
    r"(?:大肥鱼|小鲸鱼|deepseek)?\s*[,，]?\s*"                # 可能的称呼
    r"(?:给我|帮我|替我|给|帮)?\s*"
    r"(?:来|做|生成|整|搞|弄|画|拍|出|制作)\s*"
    r"(?:一)?\s*(?:个|段|条|部|支|下|张)?\s*"
    r"(?:视频|动画|短片|影片|片子|小电影|录像)\s*",
    re.I,
)
# 视频词在句尾的说法：「做个猫娘跳舞的动画」——祈使词和视频词被内容隔开了，
# CMD_PREFIX_RE 抓不到。等 TAIL_RE 把句尾的「的动画」剥掉之后，再剥这个。
# 必须带量词才算前缀，否则「游」「拍手」这类正文动词会被误吃。
BARE_PREFIX_RE = re.compile(
    r"^\s*(?:@\S+\s*)?"
    r"(?:大肥鱼|小鲸鱼|deepseek)?\s*[,，]?\s*"
    r"(?:给我|帮我|替我|给|帮)?\s*"
    r"(?:来|做|生成|整|搞|弄|拍|制作)\s*(?:一)?\s*(?:个|段|条|部|支|下)\s*",
    re.I,
)
# 句尾的语气词和客套
TAIL_RE = re.compile(
    r"(?:的)?(?:视频|动画|短片|影片|片子)?\s*"
    r"(?:吧|呗|啊|呀|哦|嗷|喔|噢|好不好|好吗|可以吗|行吗|谢谢|谢啦|求了?|请)*\s*$"
)


def _clean_leaked_call(text: str, event=None) -> tuple[str, str | None]:
    """清掉泄漏的伪工具调用，并抠出它想画的内容。"""
    leaked = None
    m = LEAK_CALL_RE.search(text or "")
    if m:
        leaked = (m.group("v") or "").strip().strip("\"'）) ")
    if not leaked:
        block = LEAK_XML_RE.search(text or "")
        if block:
            m2 = LEAK_XML_PROMPT_RE.search(block.group(0))
            if m2:
                leaked = m2.group(1).strip()
    if not leaked:
        block = LEAK_JSON_RE.search(text or "")
        if block:
            m3 = LEAK_JSON_PROMPT_RE.search(block.group(0))
            if m3:
                leaked = m3.group(1).strip()

    if event is not None:
        cleaned, relay = _leak_relay(event, text or "", "generate_video", "prompt")
        return cleaned, (leaked or relay or None)
    cleaned = _strip_all_leaks(text or "")
    if not leaked:
        leaked = _extract_arg(text or "", "generate_video", "prompt")
    return cleaned, (leaked or None)


# 三层否决的总开关。关掉就退回旧行为（只看 VIDEO_MAKE_RE）。
STRICT_MAKE = os.environ.get("DSH_VID_STRICT_MAKE", "1") not in ("0", "false", "False")
# 推出来的画面描述最少几个字才认（中文两个字就能是一个画面）
MIN_PROMPT = int(os.environ.get("DSH_VID_MIN_PROMPT", "2"))

# 推测/议论开头的「画面描述」不是画面描述。
# 实测「生成视频好像要时间吧」推出来的 prompt 是「好像要时间」——
# 他在议论出片要多久，不是在点菜。
COMMENT_HEAD_RE = re.compile(
    r"^\s*(好像|应该|大概|可能|似乎|估计|貌似|说不定|恐怕|反正|其实|不过|但是"
    r"|感觉|觉得|听说|据说|是不是|要不要|能不能|可不可以)"
)


def _imperative_gain(user_text: str, prompt: str) -> int:
    """祈使前缀到底剥掉了几个字。

    这是兜底出片最硬的一道闸：`_derive_prompt` 的活儿就是把
    「给我做个视频」这种祈使前缀整块剥掉。**一个字都没剥掉**，
    说明这句话根本不是「动词+量词+视频」开头的祈使句，
    那它就不是在叫机器人做视频，而是在聊视频这件事。

    事故原文「唉，要是再来几次这种视频，那我的群也不用那么冷了」
    剥完还是它自己 —— 而且那一整句被当成画面描述送去出片了。
    """
    a = re.sub(r"\s+", "", user_text or "")
    b = re.sub(r"\s+", "", prompt or "")
    return len(a) - len(b)


def _derive_prompt(user_text: str) -> str:
    """用户原话剥掉祈使短语后就是画面描述。"""
    t = (user_text or "").strip()
    t = re.sub(r"^@\S+\s*", "", t)
    # 分隔符后面往往才是真正的内容：
    #   「给我做个视频，一条鲸鱼在海里游」-> 「一条鲸鱼在海里游」
    # 前半段必须像祈使句（含视频词）才切，否则
    #   「鲸鱼在海里游，做成视频」的前半段才是内容。
    for sep in ("，", ",", "：", ":"):
        if sep not in t:
            continue
        head, tail = t.split(sep, 1)
        head, tail = head.strip(), tail.strip()
        if VIDEO_INTENT_RE.search(head) and len(tail) >= 4:
            t = tail
            break
        if VIDEO_INTENT_RE.search(tail) and len(head) >= 4:
            t = head
            break
    t = CMD_PREFIX_RE.sub("", t)
    t = TAIL_RE.sub("", t)
    t = BARE_PREFIX_RE.sub("", t)
    t = t.strip(" ，,。.!！?？~、的")
    return t


CAPTION_PROMPT = (
    "这是同一个视频里按时间顺序抽出的 {n} 个画面。"
    "用一句中文说清这个视频在演什么，40 字以内，"
    "只描述看到的内容，不要评价、不要加前缀。"
)
CAPTION_PROMPT_1 = (
    "这是一个视频里的画面。用一句中文说清看到了什么，40 字以内，"
    "只描述内容，不要评价、不要加前缀。"
)


# ---------------------------------------------------------------- 抽帧


def _probe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", path,
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
        return float((out.stdout or "0").strip() or 0)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


def _extract_frames(path: str, n: int = FRAMES) -> list[str]:
    """抽 n 张 JPEG，返回文件路径列表。CPU 活儿，调用方要放线程池。

    fps 按时长算，让 n 帧尽量摊开覆盖整段；太短的片子就按 1fps 抽。
    """
    os.makedirs(TMP_DIR, exist_ok=True)
    dur = _probe_duration(path)
    span = min(dur, MAX_SECONDS) if dur > 0 else MAX_SECONDS
    fps = max(n / span, 0.2) if span > 0 else 1.0

    tag = uuid.uuid4().hex[:12]
    pattern = os.path.join(TMP_DIR, f"f_{tag}_%02d.jpg")
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-t", str(int(span) or 1), "-i", path,
        "-vf", f"fps={fps:.3f},scale={FRAME_W}:-2",
        "-frames:v", str(n), "-q:v", "3", pattern,
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=90, check=False)
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("[video] 抽帧失败：%s", e)
        return []
    out = sorted(
        os.path.join(TMP_DIR, f)
        for f in os.listdir(TMP_DIR)
        if f.startswith(f"f_{tag}_")
    )
    return out


# ---------------------------------------------------------------- 视觉识别


async def _caption(paths: list[str]) -> tuple[str, str]:
    """把这些帧交给视觉模型，返回 (描述, 错误)。"""
    if not _vis_targets():
        return "", "没配任何视觉模型"
    import base64

    content = []
    for p in paths:
        try:
            with open(p, "rb") as f:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": base64.b64encode(f.read()).decode()},
                    }
                )
        except OSError as e:
            logger.warning("[video] 读帧失败：%s", e)
    if not content:
        return "", "没有可用的帧"

    prompt = CAPTION_PROMPT.format(n=len(content)) if len(content) > 1 else CAPTION_PROMPT_1
    content.append({"type": "text", "text": prompt})

    targets = _vis_targets()
    if not targets:
        return "", "没配任何视觉模型"

    last_err = "没试过任何模型"
    for ti, (base, key, model) in enumerate(targets, 1):
        for attempt in range(VIS_RETRY + 1):
            txt, err, retryable = await _caption_once(base, key, model, content)
            if txt:
                if ti > 1 or attempt > 0:
                    logger.info(
                        "[video] 降级到第%d档 %s 才识别成功（第%d次）", ti, model, attempt + 1
                    )
                return txt, ""
            last_err = err
            logger.warning(
                "[video] 第%d档 %s 第%d次失败（%s）：%s",
                ti, model, attempt + 1,
                "可重试" if retryable else "不可重试，换下一档",
                err[:100],
            )
            if not retryable:
                break
    return "", last_err


# 这些才值得重试：403 是主站 Cloudflare 边缘偶发弹回（实测约 20%，且是
# 瞬时返回、几乎不花时间），429 是限流（智谱免费池 code 1305 很常见），
# 5xx 是上游抽风。4xx 里的参数/鉴权/余额错误重试一百次也是同样结果。
_VIS_RETRY_STATUS = (403, 408, 429, 500, 502, 503, 504, 522, 524)


async def _caption_once(
    base: str, key: str, model: str, content: list
) -> tuple[str, str, bool]:
    """单次请求。返回 (描述, 错误, 是否值得重试)。"""
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=VIS_TIMEOUT)
        ) as session:
            async with session.post(
                f"{base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": content}],
                    "max_tokens": 300,
                },
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    return (
                        "",
                        f"HTTP {resp.status} {body[:160]}",
                        resp.status in _VIS_RETRY_STATUS,
                    )
                import json as _json

                data = _json.loads(body)
    except asyncio.TimeoutError:
        return "", f"识别超时（>{VIS_TIMEOUT}s）", True
    except aiohttp.ClientError as e:
        return "", f"{type(e).__name__}: {e}", True
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}", False

    try:
        txt = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return "", "返回结构异常", False
    # 智谱有时在前面塞一个换行
    txt = txt.strip().strip("。").strip()
    return (txt, "", False) if txt else ("", "模型没给描述", True)


async def _describe_video(path: str) -> tuple[str, str]:
    """完整流程：抽帧 -> 识别 -> 清理临时帧。返回 (描述, 错误)。"""
    if path in _cache:
        return _cache[path], ""
    if not os.path.exists(path):
        return "", "视频文件不在了"

    frames = await asyncio.to_thread(_extract_frames, path)
    if not frames:
        return "", "抽不出画面（文件可能损坏或不是视频）"
    try:
        cap, err = await _caption(frames)
    finally:
        for f in frames:
            try:
                os.remove(f)
            except OSError:
                pass
    if cap:
        _cache[path] = cap
        if len(_cache) > 120:
            for k in list(_cache)[:60]:
                _cache.pop(k, None)
    return cap, err


# ---------------------------------------------------------------- 生成视频


# 上游偶发缺通道时的重试退避（秒）。实测同一提示词插件侧 503
# 「no available server」、手动直连立刻 200，属上游抖动而非参数问题。
# 出片本来要 3–5 分钟，多花二十几秒重试远好过让用户白等一句「接口出问题」。
# 退避拉开也顺带避开官方 1 请求/分钟的限流。
SUBMIT_RETRY_WAITS = (8.0, 20.0)
# 只有这些才重试；4xx（参数/余额/鉴权）重试也不会变好
_RETRYABLE_RE = re.compile(
    r"no available server|ServiceUnavailable|fail_to_fetch_task|Bad ?Gateway|timeout",
    re.I,
)


async def _submit(prompt: str) -> tuple[str, str]:
    """提交出片任务，返回 (task_id, 错误)。5xx/无可用通道会自动重试。"""
    last = ""
    for attempt in range(len(SUBMIT_RETRY_WAITS) + 1):
        tid, err = await _submit_once(prompt)
        if tid:
            if attempt:
                logger.info("[video] 提交第 %d 次才成功", attempt + 1)
            return tid, ""
        last = err
        retryable = _RETRYABLE_RE.search(err or "") or (
            err.startswith("HTTP 5") or err == "提交超时"
        )
        if not retryable or attempt >= len(SUBMIT_RETRY_WAITS):
            break
        wait = SUBMIT_RETRY_WAITS[attempt]
        logger.warning(
            "[video] 提交失败（%s），%.0fs 后重试", (err or "")[:120], wait
        )
        await asyncio.sleep(wait)
    return "", last


async def _submit_once(prompt: str) -> tuple[str, str]:
    """单次提交，不重试。"""
    body = {
        "model": GEN_MODEL,
        "prompt": f"{prompt}, {GEN_STYLE}",
        "seconds": GEN_SECONDS,
        "size": GEN_SIZE,
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=90)
        ) as session:
            async with session.post(
                f"{GEN_BASE}/videos",
                headers={
                    "Authorization": f"Bearer {GEN_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    return "", f"HTTP {resp.status} {text[:200]}"
                import json as _json

                tid = (_json.loads(text) or {}).get("id") or ""
                return (tid, "") if tid else ("", f"没拿到任务 id：{text[:160]}")
    except asyncio.TimeoutError:
        return "", "提交超时"
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"


async def _poll(task_id: str) -> tuple[str, str]:
    """轮询到出片，返回 (视频 url, 错误)。"""
    deadline = time.time() + GEN_MAX_WAIT
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            while time.time() < deadline:
                await asyncio.sleep(GEN_POLL)
                try:
                    async with session.get(
                        f"{GEN_BASE}/videos/{task_id}",
                        headers={"Authorization": f"Bearer {GEN_KEY}"},
                    ) as resp:
                        if resp.status != 200:
                            logger.warning("[video] 查询任务 HTTP %s", resp.status)
                            continue
                        import json as _json

                        d = _json.loads(await resp.text()) or {}
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning("[video] 查询任务失败：%s", e)
                    continue
                status = (d.get("status") or "").lower()
                if status in ("completed", "succeeded", "success"):
                    url = d.get("url") or ""
                    return (url, "") if url else ("", "完成了但没给地址")
                if status in ("failed", "error", "cancelled"):
                    return "", f"生成失败：{d.get('error') or status}"
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"
    return "", f"等太久了（>{GEN_MAX_WAIT:.0f}s）还没出来"


async def _download(url: str) -> tuple[str, str]:
    """下载成本地文件，返回 (路径, 错误)。"""
    os.makedirs(TMP_DIR, exist_ok=True)
    path = os.path.join(TMP_DIR, f"gen_{uuid.uuid4().hex[:12]}.mp4")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=180)
        ) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return "", f"下载 HTTP {resp.status}"
                with open(path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(262144):
                        f.write(chunk)
    except Exception as e:  # noqa: BLE001
        return "", f"下载失败 {type(e).__name__}: {e}"
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if size < 1024:
        return "", f"下载到的文件太小（{size}B）"
    return path, ""


async def _cleanup_old(ttl: int = 3600) -> None:
    try:
        now = time.time()
        for name in os.listdir(TMP_DIR):
            p = os.path.join(TMP_DIR, name)
            try:
                if now - os.path.getmtime(p) > ttl:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


# ---------------------------------------------------------------- 插件主体


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        os.makedirs(TMP_DIR, exist_ok=True)
        logger.info(
            "[video] 已加载：看视频=%s(%s 抽%d帧) 生成=%s(%s %ss %s) "
            "全局间隔%.0fs 会话冷却%.0fs",
            "开" if UNDERSTAND and _vis_targets() else "关",
            "→".join(t[2] for t in _vis_targets()) or "无", FRAMES,
            "开" if GEN and GEN_KEY else "关",
            GEN_MODEL, GEN_SECONDS, GEN_SIZE,
            GEN_INTERVAL, GEN_COOLDOWN,
        )

    # ------------------------------------------------ 看视频（自动）

    @filter.on_llm_request()
    async def attach_video(self, event: AstrMessageEvent, req) -> None:
        """把消息里视频的内容识别出来，替换掉框架那条只有路径的标记。"""
        if not (UNDERSTAND and _vis_targets()):
            return
        try:
            parts = getattr(req, "extra_user_content_parts", None) or []
            hits: list[tuple[int, str, str]] = []
            for i, part in enumerate(parts):
                text = getattr(part, "text", "") or ""
                for m in ATTACH_RE.finditer(text):
                    hits.append((i, m.group(1).strip(), m.group(2).strip()))
            if not hits:
                # ★ 这里必须出声：看视频功能实测从上线起一次都没成功过 ★
                # 2026-09-03 真群有人发视频，本插件全程零日志；24h 内
                # 「[video] 识别」出现 0 次。根因是框架适配器的洞：
                #   OneBot 段类型 "video" 不在 ComponentTypes 里
                #   → 适配器第 408 行直接 `continue` 忽略，不造 Video 组件
                #   → 框架的 _append_video_attachment 只在 isinstance(comp, Video)
                #     时才跑，于是永远不产生 [Video Attachment] 标记
                #   → 本插件的 ATTACH_RE 永远 0 命中
                # 插件层要修得从 raw_message 直接读 OneBot 段，那是下一步。
                # 现在至少让日志说清「群里明明有视频，但我拿不到它」——
                # 静默失败最糟的地方是没人知道它坏了。
                if _raw_has_video(event):
                    logger.warning(
                        "[video] 群里发了视频，但框架没给出 [Video Attachment] 标记，"
                        "这一轮看不了（已知：aiocqhttp 适配器忽略 video 段）"
                    )
                return
            await asyncio.wait_for(
                self._run_understand(req, hits), timeout=UNDERSTAND_BUDGET
            )
        except asyncio.TimeoutError:
            logger.warning("[video] 超过 %.0fs 预算，本轮放弃视频识别", UNDERSTAND_BUDGET)
        except BaseException as e:  # noqa: BLE001
            logger.error("[video] 看视频钩子异常：%s", e)

    async def _run_understand(self, req, hits) -> None:
        lines: list[str] = []
        drop: set[int] = set()
        for idx, name, path in hits:
            cap, err = await _describe_video(path)
            if cap:
                lines.append(f"- 有人发了个视频，内容是：{cap}")
                drop.add(idx)
                logger.info("[video] 识别成功 %s -> %s", os.path.basename(path), cap[:40])
            else:
                lines.append(
                    f"- 有人发了个视频（{name}），但你看不了它的内容（{err}）。"
                    "就说你看不了，别编里面有什么。"
                )
                drop.add(idx)
                logger.warning("[video] 识别失败 %s：%s", os.path.basename(path), err)

        if not lines:
            return
        # 把框架那条只有路径的标记删掉：留着的话模型会同时看到
        # 「path /AstrBot/data/temp/xxx.mp4」和我们的描述，
        # 十有八九会去聊那个路径，甚至说「我打不开这个文件」。
        # 这是 dsh-imgctx 擦 [Image Captioning Failed] 的同一个道理。
        parts = req.extra_user_content_parts
        for i in sorted(drop, reverse=True):
            if i < len(parts):
                text = getattr(parts[i], "text", "") or ""
                stripped = ATTACH_RE.sub("", text).strip()
                if stripped:
                    try:
                        parts[i].text = stripped
                    except Exception:  # noqa: BLE001
                        del parts[i]
                else:
                    del parts[i]

        req.extra_user_content_parts.append(
            TextPart(
                text="<video_context>\n"
                "下面是这条消息里视频的内容（视频本身你看不了，这是插件抽帧后的转述，"
                "没有声音、可能漏掉快动作）。当作你已经看过了，直接就内容回应。\n"
                + "\n".join(lines)
                + "\n</video_context>"
            )
        )

    # ------------------------------------------------ 生成视频（工具）

    # 注：generate_video 工具本身不加 WATCH 否决。
    # 模型主动发起这个调用时，判断责任在模型；插件在这里拦会把
    # 「看完这个视频，再做个类似的」这种合理请求也一起毁掉。
    # 兜底路径（on_llm_response）才是误判高发区，否决只加在那里。
    @filter.llm_tool(name="generate_video")
    async def generate_video(self, event: AstrMessageEvent, prompt: str):
        """生成一段视频（动画/短片）时调用本工具。注意很慢，要等好几分钟。

        Args:
            prompt(string): 视频内容的描述，写清画面主体、动作、场景
        """
        if not (GEN and GEN_KEY):
            return "视频生成没开，请告诉用户你现在做不了视频。"

        prompt = (prompt or "").strip()
        if len(prompt) < 2:
            return "没说清要什么视频，请反问用户想看什么内容。"

        sid = event.unified_msg_origin or "global"
        now = time.time()

        left = GEN_COOLDOWN - (now - _gen_last_session.get(sid, 0.0))
        if left > 0:
            return (
                f"这个群刚做过视频，还要等 {int(left)} 秒。"
                "请用你自己的语气告诉用户等一会儿，别重复调用工具。"
            )
        gleft = GEN_INTERVAL - (now - _gen_last_global)
        if gleft > 0:
            return (
                f"视频接口限流，还要等 {int(gleft)} 秒才能提交。"
                "请告诉用户稍等一下，别重复调用工具。"
            )

        # 立刻占位，防止同一轮里模型连着调两次，也让兜底钩子知道已经跑过了
        _gen_last_session[sid] = now
        event.set_extra("video_done", True)
        task = asyncio.create_task(self._gen_and_send(event, prompt))
        _gen_tasks.add(task)
        task.add_done_callback(_gen_tasks.discard)
        logger.info("[video] 已接单：%s", prompt[:60])
        return (
            "视频已经在做了，大概要三到五分钟，做完会自动发出来。"
            "请用你自己的语气说一句很短的话让用户等着（比如「做着呢」「等会儿」），"
            "不要说你做不了，也不要再调用工具。"
        )

    async def _gen_and_send(self, event: AstrMessageEvent, prompt: str) -> None:
        """后台出片。绝不能在工具里等——会话锁会把整个群卡住几分钟。"""
        global _gen_last_global
        sid = event.unified_msg_origin or "global"
        try:
            async with _gen_lock:
                gleft = GEN_INTERVAL - (time.time() - _gen_last_global)
                if gleft > 0:
                    await asyncio.sleep(gleft)
                _gen_last_global = time.time()
                tid, err = await _submit(prompt)
            if not tid:
                logger.error("[video] 提交失败：%s", err)
                _gen_last_session[sid] = 0.0
                await event.send(MessageChain(chain=[Plain("视频没做出来，接口那边出问题了")]))
                return

            logger.info("[video] 任务 %s 已提交，开始等", tid)
            t0 = time.time()
            url, err = await _poll(tid)
            if not url:
                logger.error("[video] 出片失败：%s", err)
                _gen_last_session[sid] = 0.0
                await event.send(MessageChain(chain=[Plain("视频翻车了，没做出来")]))
                return

            path, err = await _download(url)
            if not path:
                logger.error("[video] 下载失败：%s", err)
                await event.send(MessageChain(chain=[Plain("视频做好了但下不下来")]))
                return

            size = os.path.getsize(path)
            logger.info(
                "[video] 出片成功 %.0fs %dB -> %s", time.time() - t0, size, path
            )
            # 视频必须单独一条发。混进正常回复里会被
            # completion_text 的重新推导抹掉（get_plain_text 只留文字）。
            await self._send_video(event, path)
            await _cleanup_old()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001
            logger.error("[video] 后台出片异常：%s", e)
            _gen_last_session[sid] = 0.0

    # ------------------------------------------------ 真正把视频发出去

    async def _send_video(self, event: AstrMessageEvent, path: str) -> None:
        """把本地视频发给 QQ。

        为什么不能直接 Video.fromFileSystem：
          aiocqhttp 适配器对 Image/Record 会转 base64，但对 Video 只调
          comp.to_dict()，而 Video.to_dict() 在没配 callback_api_base 时
          原样把**容器内路径**塞进 OneBot 消息。napcat 是另一个容器、没有
          共享挂载，于是必然
              ENOENT: no such file or directory, open '/AstrBot/data/video/xxx.mp4'
          （实测就是这么失败的，astrbot 日志只留一句空的「后台出片异常」，
           因为 aiocqhttp 抛的 ActionFailed 的 str() 是空串）。
          框架自带的文件服务也走不通：/api/file/<token> 从 napcat 请求返回
          404 —— 那套 token 是给 WebUI 用的，不认这条路。

        所以自己转 base64://，和框架处理 Image/Record 的做法一致。
        napcat 实测能收（send_private_msg 返回 message_id），1.3MB 视频约
        1.8MB base64，5 秒片子这个量级完全可接受。
        """
        import base64

        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
        except OSError as e:
            logger.error("[video] 读视频失败：%s", e)
            return

        # 太大就不硬塞：OneBot 走 WS，几十 MB 的 base64 会把连接顶爆
        if len(b64) > MAX_SEND_B64:
            logger.error(
                "[video] 视频太大（base64 %dB > %dB），放弃发送", len(b64), MAX_SEND_B64
            )
            await event.send(MessageChain(chain=[Plain("视频做出来了但太大发不了")]))
            return

        try:
            await event.send(MessageChain(chain=[Video(file=f"base64://{b64}")]))
            logger.info("[video] 已发出（base64 %dB）", len(b64))
        except Exception as e:  # noqa: BLE001
            # ActionFailed 的 str() 常常是空的，把类型名也打出来
            logger.error("[video] 发送失败 %s: %s", type(e).__name__, e or "(无消息)")

    # ------------------------------------------------ 兜底：模型嘴上答应了却没调工具

    @filter.on_llm_response()
    async def auto_video(self, event: AstrMessageEvent, response) -> None:
        """用户要视频、模型只答应没调工具（或把调用吐成文本）时，插件自己做。"""
        try:
            raw = getattr(response, "completion_text", "") or ""
            cleaned, leaked = _clean_leaked_call(raw, event)

            # 无条件回写：泄漏的 generate_video(...) 绝不能进群。
            # 放在所有 return 之前——就算最后不做视频，文字也得是干净的。
            if cleaned != raw:
                try:
                    response.completion_text = cleaned
                except Exception:  # noqa: BLE001
                    response._completion_text = cleaned
                logger.warning("[video] 已清理模型泄漏的伪工具调用标记")

            if not (GEN and GEN_KEY):
                return
            if event.get_extra("video_done"):
                return
            if "generate_video" in (getattr(response, "tools_call_name", None) or []):
                return

            user_text = event.message_str or ""
            # 判据：泄漏的调用是铁证；否则要求用户话里既有「视频」也有「做」的意思。
            # 「这视频啥意思」只有视频词没有做的意思 —— 那是看视频，不是做视频。
            if not leaked:
                if not VIDEO_MAKE_RE.search(user_text):
                    return
                # 用户要的是「看懂一段已有的视频」，不是做新的。
                # leaked 不受这条否决：模型真的发起了 generate_video 调用，
                # 那是它自己要出片的铁证（「帮我看这视频，顺便做个类似的」）。
                if VIDEO_WATCH_RE.search(user_text):
                    logger.info(
                        "[video] 让路：用户要的是看视频而不是做视频 | 用户=%.60s",
                        user_text,
                    )
                    return
                if IMG_NOUN_RE.search(user_text):
                    # 「做个视频封面」这种，图才是他要的东西
                    logger.info(
                        "[video] 让路：用户要的是图而不是视频 | 用户=%.40s", user_text
                    )
                    return
            if GEN_REFUSE_RE.search(cleaned) or GEN_ASK_BACK_RE.search(cleaned):
                logger.info(
                    "[video] 未触发出片：拒绝=%s 反问=%s | 用户=%.40s",
                    bool(GEN_REFUSE_RE.search(cleaned)),
                    bool(GEN_ASK_BACK_RE.search(cleaned)),
                    user_text,
                )
                return

            prompt = leaked or _derive_prompt(user_text)
            # 2 字就够：中文画面词大量是两个字（下雨/日落/海浪/猫娘）。
            # 原来卡 3 字，「给我整段下雨的短片」剥出「下雨」会被误拦。
            # 真正把关的是下面两层（祈使前缀必须剥掉过、推出来不能是议论）。
            if len(prompt) < MIN_PROMPT:
                logger.info("[video] 未触发出片：推不出画面描述 | 用户=%.40s", user_text)
                return
            # leaked 不受下面两条约束：模型自己发起了 generate_video 调用，
            # 那是它要出片的铁证，prompt 也是它自己写的。
            if STRICT_MAKE and not leaked:
                gain = _imperative_gain(user_text, prompt)
                if gain < 2:
                    logger.info(
                        "[video] 未触发出片：这句话不是祈使句（祈使前缀一个字都没剥掉，"
                        "剥掉%d字）| 用户=%.60s", gain, user_text,
                    )
                    return
                if COMMENT_HEAD_RE.search(prompt):
                    logger.info(
                        "[video] 未触发出片：推出来的是议论不是画面（%.20s）| 用户=%.40s",
                        prompt, user_text,
                    )
                    return

            sid = event.unified_msg_origin or "global"
            left = GEN_COOLDOWN - (time.time() - _gen_last_session.get(sid, 0.0))
            if left > 0:
                logger.info("[video] 兜底触发但在冷却中（剩 %ds）", int(left))
                return
            _gen_last_session[sid] = time.time()
            event.set_extra("video_done", True)

            logger.info(
                "[video] 兜底出片（%s）：%s",
                "抠自泄漏调用" if leaked else "推自用户原话",
                prompt[:80],
            )
            task = asyncio.create_task(self._gen_and_send(event, prompt))
            _gen_tasks.add(task)
            task.add_done_callback(_gen_tasks.discard)
        except BaseException as e:  # noqa: BLE001
            logger.error("[video] 兜底钩子异常：%s", e)

    # ------------------------------------------------ 指令

    @filter.command("做视频")
    async def cmd_gen(self, event: AstrMessageEvent):
        """/做视频 <描述> —— 直接生成一段视频。"""
        arg = self._arg(event, "做视频")
        if not arg:
            yield event.plain_result("用法：/做视频 一条鲸鱼在海里翻身")
            return
        if not (GEN and GEN_KEY):
            yield event.plain_result("视频生成没开")
            return
        sid = event.unified_msg_origin or "global"
        left = GEN_COOLDOWN - (time.time() - _gen_last_session.get(sid, 0.0))
        if left > 0:
            yield event.plain_result(f"刚做过，等 {int(left)} 秒")
            return
        _gen_last_session[sid] = time.time()
        task = asyncio.create_task(self._gen_and_send(event, arg))
        _gen_tasks.add(task)
        task.add_done_callback(_gen_tasks.discard)
        yield event.plain_result("在做了，三到五分钟，做好自动发")

    @filter.command("视频状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """/视频状态 —— 查看视频能力配置。"""
        sid = event.unified_msg_origin or "global"
        left = max(0, GEN_COOLDOWN - (time.time() - _gen_last_session.get(sid, 0.0)))
        gleft = max(0, GEN_INTERVAL - (time.time() - _gen_last_global))
        yield event.plain_result(
            f"看视频：{'开' if UNDERSTAND and _vis_targets() else '关'}｜"
            f"{'→'.join(t[2] for t in _vis_targets()) or '无'}｜"
            f"抽 {FRAMES} 帧宽 {FRAME_W}｜最长看 {MAX_SECONDS}s｜预算 {UNDERSTAND_BUDGET:.0f}s\n"
            f"生成：{'开' if GEN and GEN_KEY else '关'}｜{GEN_MODEL}｜"
            f"{GEN_SECONDS}s {GEN_SIZE}｜等待上限 {GEN_MAX_WAIT:.0f}s\n"
            f"本群冷却剩 {int(left)}s｜全局限流剩 {int(gleft)}s｜"
            f"在跑 {len(_gen_tasks)} 个任务｜已缓存 {len(_cache)} 个视频描述"
        )

    @staticmethod
    def _arg(event: AstrMessageEvent, cmd: str) -> str:
        text = (event.message_str or "").strip()
        for p in (cmd, "/" + cmd):
            if text.startswith(p):
                return text[len(p) :].strip()
        return text
