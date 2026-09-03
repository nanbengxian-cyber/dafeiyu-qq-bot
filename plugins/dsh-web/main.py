# dsh-web —— QQ 群聊「小鲸鱼」联网插件：看网页、搜东西、查 B 站视频。
#
# 三种能力、两种触发形状：
#
#   能力 A 看网页 read_webpage —— 抓正文并转成纯文本。
#   能力 B 搜索   web_search   —— 智谱 web_search（主）/ DuckDuckGo（备）。
#   能力 C 查 B 站 bilibili_video —— 标题/UP/时长/播放/点赞/简介。
#
#   触发 1：on_llm_request 钩子（**主力**）。群消息里出现链接就自动抓好、
#           把正文塞进这次请求的上下文。不依赖模型调工具。
#   触发 2：LLM 函数工具（模型主动搜/查时用）。
#   触发 3：显式指令 /看网页 <url>、/搜 <词>、/b站 <链接或BV号>。
#
# 为什么钩子是主力而不是只给工具：
#   dsh-imagegen 已经踩透了这个坑——deepseek-v4-flash-0731 这类便宜模型在
#   「完整人格提示词 + 几百条群聊历史」的上下文里经常不调工具，只嘴上答应。
#   而「群友发了个链接问这是什么」是最高频场景，不能指望模型自觉。链接就在
#   消息里，插件自己抓完注入，命中率 100%。
#
# 网络现实（都是在这台香港机器上实测出来的，别想当然）：
#   - 香港出口访问 api.bilibili.com/x/web-interface/view 一律 -412 request
#     was banned，加 UA/Referer 也没用；但 **wbi/view** 端点正常返回 code 0。
#     popular / ranking / search / archive/desc 也正常。所以只用 wbi 系端点。
#   - r.jina.ai 对这台机器整体 403，不能当兜底。
#   - 微信公众号、掘金、36氪、百家号、微博、知乎、维基百科在香港直连都拿不到
#     正文（验证码 / 403 / 前端渲染）。抓不到就**老实说抓不到**，绝不让模型
#     拿着空白去编——这是 imgctx 里「图看不了就说看不了」的同一条规矩。
#   - 智谱 web_search 可用且每条结果自带约 680 字摘要，比自己抓正文稳得多，
#     所以「抓不到正文」时用搜索结果兜底（拿 URL 里的标题词去搜）。
#
# 安全：SSRF 是这类插件的头号风险（群里随便发个 http://172.18.0.2:6185 就能
#   让机器人去读内网面板）。所以强制：只允许 http/https、解析后的 IP 必须是
#   公网地址（私网/环回/链路本地/保留段全拒）、跟随重定向时每一跳都重新校验、
#   响应体截断、Content-Type 白名单。

import asyncio
import ipaddress
import os
import random
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import aiohttp

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.platform.message_type import MessageType
from astrbot.core.agent.message import TextPart

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

ENABLED = os.environ.get("DSH_WEB_ENABLE", "1") not in ("0", "false", "False")
UA = os.environ.get(
    "DSH_WEB_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
TIMEOUT = int(os.environ.get("DSH_WEB_TIMEOUT", "20"))
# 单页最多注入多少字（模型上下文是钱，也别把群聊挤掉）
MAX_CHARS = int(os.environ.get("DSH_WEB_MAX_CHARS", "1500"))
# 下载上限，防止有人发个 500MB 的文件把 1.6G 内存打满
MAX_BYTES = int(os.environ.get("DSH_WEB_MAX_BYTES", str(3 * 1024 * 1024)))
# 一条消息里最多抓几个链接
MAX_URLS = int(os.environ.get("DSH_WEB_MAX_URLS", "2"))
# 钩子总预算：宁可这轮不看网页，也不能让回复卡住
BUDGET = float(os.environ.get("DSH_WEB_BUDGET", "25"))
# 同一 URL 的抓取结果缓存多久
CACHE_TTL = int(os.environ.get("DSH_WEB_CACHE_TTL", "1800"))
MAX_REDIRECTS = 5
# 正文低于这个字数就当没抓到——宁可说读不了，也别让模型拿着半句话去编
MIN_TEXT = int(os.environ.get("DSH_WEB_MIN_TEXT", "120"))

ZHIPU_KEY = os.environ.get("DSH_WEB_SEARCH_KEY", "")
ZHIPU_BASE = os.environ.get(
    "DSH_WEB_SEARCH_BASE", "https://open.bigmodel.cn/api/paas/v4"
).rstrip("/")
SEARCH_ENGINE = os.environ.get("DSH_WEB_SEARCH_ENGINE", "search_std")
SEARCH_COUNT = int(os.environ.get("DSH_WEB_SEARCH_COUNT", "5"))
# 每条搜索结果注入多少字
SEARCH_SNIPPET = int(os.environ.get("DSH_WEB_SEARCH_SNIPPET", "260"))

# url -> (时间戳, 标题, 正文)
_cache: dict[str, tuple[float, str, str]] = {}

URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff，。！？；、）】》\"'<>]+", re.I)
# 群里也常见不带协议头的裸链接
BARE_RE = re.compile(
    r"(?<![\w.@/-])((?:www\.|b23\.tv/|bilibili\.com/)[^\s\u4e00-\u9fff，。！？；、）】》\"'<>]+)",
    re.I,
)
BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
AV_RE = re.compile(r"\bav(\d{1,12})\b", re.I)

_BILI_HOSTS = ("bilibili.com", "b23.tv", "acg.tv", "bilibili.tv")

# ---------------------------------------------------------------- SSRF 防护


def _is_public_ip(host: str) -> tuple[bool, str]:
    """解析主机名，要求所有解析结果都是公网 IP。返回 (放行, 拒绝原因)。

    DNS 失败和「解析到内网」要分开报：前者是域名不存在/打不通，后者是安全拦截。
    混成一句话会让排查时把两件事搞混。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False, "域名解析不了（可能不存在或网络不通）"
    if not infos:
        return False, "域名解析不到地址"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            return False, "解析出的地址不合法"
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False, "指向内网地址，已拒绝"
    return True, ""


async def _check_url(url: str) -> tuple[bool, str]:
    """协议 + 主机 + IP 三层校验。返回 (放行, 拒绝原因)。"""
    try:
        p = urlparse(url)
    except ValueError:
        return False, "URL 格式不对"
    if p.scheme not in ("http", "https"):
        return False, f"只支持 http/https（收到 {p.scheme or '空'}）"
    if not p.hostname:
        return False, "没有主机名"
    # DNS 解析放线程池：解析可能阻塞几秒，别卡住事件循环
    ok, why = await asyncio.to_thread(_is_public_ip, p.hostname)
    if not ok:
        return False, why
    return True, ""


# ---------------------------------------------------------------- 正文提取

_CONTENT_SELECTORS = [
    "article",
    "main",
    "[role=main]",
    "#mw-content-text",
    "#js_content",
    ".rich_media_content",
    ".article-content",
    ".post-content",
    ".markdown-body",
    "#readme",
    ".repository-content",
    ".topic_content",
    ".article",
    "#content",
    ".content",
]
_DROP_TAGS = [
    "script", "style", "nav", "footer", "header", "noscript",
    "aside", "form", "iframe", "svg", "button", "select",
]


async def _read_capped(resp) -> bytes:
    """分块读到上限。踩过的坑：StreamReader.read(n) 只返回当前缓冲区里的数据，
    直接 read(MAX_BYTES) 在 gzip 大页面上只能拿到第一块（26KB），HTML 被截断，
    <article> 之类的正文容器还没出现，于是「抓到了但没正文」。必须循环。"""
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        buf.extend(chunk)
        if len(buf) >= MAX_BYTES:
            break
    return bytes(buf)


# 拦截页/错误页的特征。这些页面 HTTP 都是 200，正文也有几十个字，
# 光看长度分辨不出来，只能认特征词。
# 正文里出现就判定被拦（这些说法只会出现在拦截页，正常文章不会这么开头）
_BLOCK_IN_TEXT = [
    ("参数错误", "链接参数不对或已失效"),
    ("完成验证后即可继续访问", "被要求验证"),
    ("请输入验证码", "被要求验证码"),
    ("Enable JavaScript and cookies to continue", "要求 JS/Cookie"),
    ("请开启 JavaScript", "要求 JS"),
    ("访问页面不存在", "页面不存在"),
    ("页面找不到了", "页面不存在"),
    ("页面不存在或已删除", "页面不存在"),
]
# 只在标题里出现才判定被拦。放标题是为了避免误杀——一篇正经讲「验证码」
# 的文章正文里当然会出现「验证码」，但它的标题不会是「百度安全验证」。
_BLOCK_IN_TITLE = [
    ("安全验证", "被安全验证拦下"),
    ("环境异常", "被要求验证（环境异常）"),
    ("验证码", "被要求验证码"),
    ("Just a moment", "被 Cloudflare 拦下"),
    ("Attention Required", "被 Cloudflare 拦下"),
    ("Sina Visitor System", "微博要求登录"),
    ("Too Many Req", "被限流"),
    ("Wikimedia Error", "被限流"),
    ("404", "页面不存在"),
    ("Not Found", "页面不存在"),
    ("Access Denied", "被拒绝访问"),
    ("Forbidden", "被拒绝访问"),
]


def _looks_blocked(title: str, text: str) -> str:
    """像拦截页/错误页就返回原因，否则返回空串。"""
    head = text[:400]
    for needle, why in _BLOCK_IN_TEXT:
        if needle in head:
            return why
    for needle, why in _BLOCK_IN_TITLE:
        if needle.lower() in title.lower():
            return why
    return ""


def _extract(html: str) -> tuple[str, str]:
    """从 HTML 里抽出 (标题, 正文纯文本)。抽不到正文就返回 meta 描述。"""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return "", re.sub(r"<[^>]+>", " ", html)[:MAX_CHARS]

    soup = BeautifulSoup(html, "lxml")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        node = soup.select_one('meta[property="og:title"]')
        if node and node.get("content"):
            title = node["content"].strip()

    meta_desc = ""
    for sel in ('meta[property="og:description"]', 'meta[name="description"]'):
        node = soup.select_one(sel)
        if node and node.get("content"):
            meta_desc = node["content"].strip()
            break

    for tag in soup(_DROP_TAGS):
        tag.decompose()

    best = None
    for sel in _CONTENT_SELECTORS:
        node = soup.select_one(sel)
        if node and len(node.get_text(" ", strip=True)) > 200:
            best = node
            break
    body = best or soup.body or soup
    text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))

    # 正文太短说明是前端渲染 / 验证码页，退回 meta 描述（至少有点信息）
    if len(text) < 200 and meta_desc:
        text = meta_desc
    return title, text


# ---------------------------------------------------------------- 抓取


async def _fetch(url: str) -> tuple[str, str, str]:
    """抓一个 URL，返回 (标题, 正文, 错误)。手动跟随重定向并逐跳校验。"""
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1], hit[2], ""

    current = url
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        ) as session:
            for _ in range(MAX_REDIRECTS):
                ok, why = await _check_url(current)
                if not ok:
                    return "", "", why
                async with session.get(
                    current, headers=headers, allow_redirects=False, ssl=False
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if not loc:
                            return "", "", f"HTTP {resp.status} 但没给跳转地址"
                        current = urljoin(str(resp.url), loc)
                        continue
                    if resp.status >= 400:
                        return "", "", f"HTTP {resp.status}"
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if not any(
                        k in ctype for k in ("html", "text/plain", "json", "xml")
                    ):
                        return "", "", f"不是网页（{ctype.split(';')[0] or '未知类型'}）"
                    raw = await _read_capped(resp)
                    charset = resp.charset or "utf-8"
                    try:
                        html = raw.decode(charset, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        html = raw.decode("utf-8", errors="replace")
                    break
            else:
                return "", "", "重定向太多次"
    except asyncio.TimeoutError:
        return "", "", f"超时（>{TIMEOUT}s）"
    except aiohttp.ClientError as e:
        return "", "", f"连不上：{type(e).__name__}"
    except Exception as e:  # noqa: BLE001
        return "", "", f"{type(e).__name__}: {e}"

    title, text = _extract(html)
    blocked = _looks_blocked(title, text)
    if blocked:
        return title, "", blocked
    if len(text) < MIN_TEXT:
        return title, "", "抓到页面但取不到正文（大概是前端渲染或要验证码）"
    text = text[:MAX_CHARS]
    _cache[url] = (time.time(), title, text)
    if len(_cache) > 200:
        for k in sorted(_cache, key=lambda x: _cache[x][0])[:100]:
            _cache.pop(k, None)
    return title, text, ""


# ---------------------------------------------------------------- 搜索


async def _search_zhipu(query: str) -> tuple[list[dict], str]:
    if not ZHIPU_KEY:
        return [], "未配置搜索 key"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT + 10)
        ) as session:
            async with session.post(
                f"{ZHIPU_BASE}/web_search",
                headers={
                    "Authorization": f"Bearer {ZHIPU_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "search_engine": SEARCH_ENGINE,
                    "search_query": query,
                    "count": SEARCH_COUNT,
                },
            ) as resp:
                if resp.status != 200:
                    return [], f"HTTP {resp.status} {(await resp.text())[:160]}"
                data = await resp.json()
    except asyncio.TimeoutError:
        return [], "搜索超时"
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"

    out = []
    for it in (data.get("search_result") or [])[:SEARCH_COUNT]:
        out.append(
            {
                "title": (it.get("title") or "").strip(),
                "link": (it.get("link") or "").strip(),
                "content": re.sub(r"\s+", " ", it.get("content") or "").strip(),
                "date": (it.get("publish_date") or "").strip(),
            }
        )
    return out, "" if out else "没有结果"


async def _search_ddg(query: str) -> tuple[list[dict], str]:
    """备用搜索：DuckDuckGo lite（实测这台机器可达）。"""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return [], "缺少 bs4"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        ) as session:
            async with session.post(
                "https://lite.duckduckgo.com/lite/",
                data={"q": query},
                headers={"User-Agent": UA},
            ) as resp:
                if resp.status != 200:
                    return [], f"HTTP {resp.status}"
                html = await resp.text()
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"

    soup = BeautifulSoup(html, "lxml")
    links = soup.select("a.result-link")
    snips = soup.select("td.result-snippet")
    out = []
    for a, sn in list(zip(links, snips))[:SEARCH_COUNT]:
        out.append(
            {
                "title": a.get_text(" ", strip=True),
                "link": a.get("href", ""),
                "content": sn.get_text(" ", strip=True),
                "date": "",
            }
        )
    return out, "" if out else "没有结果"


async def _search(query: str) -> tuple[list[dict], str]:
    res, err = await _search_zhipu(query)
    if res:
        return res, ""
    logger.warning("[web] 主搜索失败(%s)，降级 DuckDuckGo", err)
    res2, err2 = await _search_ddg(query)
    return res2, "" if res2 else f"主：{err}；备：{err2}"


def _fmt_search(query: str, res: list[dict]) -> str:
    lines = [f"「{query}」的搜索结果："]
    for i, r in enumerate(res, 1):
        head = r["title"][:70]
        if r["date"]:
            head += f"（{r['date']}）"
        lines.append(f"{i}. {head}")
        if r["content"]:
            lines.append(f"   {r['content'][:SEARCH_SNIPPET]}")
        if r["link"]:
            lines.append(f"   {r['link'][:110]}")
    return "\n".join(lines)


# ---------------------------------------------------------------- B 站


def _bili_id(text: str) -> tuple[str, str]:
    """从文本里找 B 站视频 id，返回 (类型, 值)：('bvid', 'BV..') 或 ('aid','123')。"""
    m = BV_RE.search(text)
    if m:
        return "bvid", m.group(0)
    m = AV_RE.search(text)
    if m:
        return "aid", m.group(1)
    return "", ""


async def _resolve_b23(url: str) -> str:
    """b23.tv 短链换成真实地址。"""
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        ) as session:
            async with session.get(
                url, headers={"User-Agent": UA}, allow_redirects=True, ssl=False
            ) as resp:
                return str(resp.url)
    except Exception:  # noqa: BLE001
        return url


def _fmt_dur(sec: int) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}小时{m}分" if h else (f"{m}分{s}秒" if m else f"{s}秒")


def _fmt_cnt(n: int) -> str:
    n = int(n or 0)
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


async def _bili_info(text: str) -> tuple[str, str]:
    """查 B 站视频信息，返回 (可读文本, 错误)。"""
    src = text
    if "b23.tv" in src:
        m = re.search(r"https?://b23\.tv/\S+", src) or re.search(r"b23\.tv/\S+", src)
        if m:
            u = m.group(0)
            if not u.startswith("http"):
                u = "https://" + u
            src = await _resolve_b23(u)

    kind, val = _bili_id(src)
    if not kind:
        return "", "没认出 BV 号或 av 号"

    params = {kind: val} if kind == "bvid" else {"aid": val}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        ) as session:
            # 只用 wbi/view：香港出口访问普通 /view 一律 -412
            async with session.get(
                "https://api.bilibili.com/x/web-interface/wbi/view",
                params=params,
                headers={
                    "User-Agent": UA,
                    "Referer": "https://www.bilibili.com/",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    return "", f"HTTP {resp.status}"
                data = await resp.json(content_type=None)
    except asyncio.TimeoutError:
        return "", "超时"
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"

    if data.get("code") != 0:
        return "", f"B 站返回 {data.get('code')}：{data.get('message')}"
    d = data.get("data") or {}
    stat = d.get("stat") or {}
    owner = d.get("owner") or {}
    pub = ""
    if d.get("pubdate"):
        pub = time.strftime("%Y-%m-%d", time.localtime(int(d["pubdate"])))
    lines = [
        f"B 站视频《{d.get('title', '?')}》",
        f"UP 主：{owner.get('name', '?')}｜时长 {_fmt_dur(d.get('duration', 0))}｜发布 {pub}",
        f"播放 {_fmt_cnt(stat.get('view'))}｜点赞 {_fmt_cnt(stat.get('like'))}｜"
        f"弹幕 {_fmt_cnt(stat.get('danmaku'))}｜评论 {_fmt_cnt(stat.get('reply'))}｜"
        f"收藏 {_fmt_cnt(stat.get('favorite'))}｜投币 {_fmt_cnt(stat.get('coin'))}",
    ]
    desc = re.sub(r"\s+", " ", (d.get("desc") or "").strip())
    if desc:
        lines.append(f"简介：{desc[:300]}")
    pages = d.get("pages") or []
    if len(pages) > 1:
        lines.append(f"共 {len(pages)} 个分P：" + "、".join(
            (p.get("part") or "")[:20] for p in pages[:5]
        ))
    return "\n".join(lines), ""


# 时政内容识别。命中的搜索结果/网页正文不注入给模型 ——
# 上游渠道会对这类内容直接 content_filter 拒绝**整个** completion，
# 结果群里收到一行「LLM 响应错误: …内容安全过滤被拒绝」，比不回答更难看。
# 实测触发场景：「搜一下今天有什么新闻」搜到领导人出访 + 政治局会议。
# 只收几乎必然踩线的词；「经济」「疫情」这类正常词不收，避免大面积误杀。
SENSITIVE_RE = re.compile(
    r"习近平|李强总理|政治局|中共中央|总书记|国家主席|人大常委|全国政协"
    r"|台独|港独|疆独|藏独|法轮|六四|达赖|维吾尔|新疆再教育"
    r"|颜色革命|政变|军事演习|统一台湾|武统"
)


def _drop_sensitive(res: list[dict]) -> tuple[list[dict], int]:
    """摘掉时政条目，返回 (保留的, 摘掉的条数)。"""
    keep = []
    for r in res:
        blob = "%s %s" % (r.get("title") or "", r.get("content") or "")
        if SENSITIVE_RE.search(blob):
            continue
        keep.append(r)
    return keep, len(res) - len(keep)


# ---------------------------------------------------------------- 自动搜索

# 用户明确要求联网搜索。刻意只收显式动词，不收「X是什么」这类泛问句：
# 泛问句模型自己答得挺好（「哈基米是什么梗」实测答对），每句都去搜纯浪费。
SEARCH_CMD_RE = re.compile(
    # 负向后视排掉「检查/调查/审查/排查…」这些复合动词 ——
    # 「检查一下代码」含「查一下」，纯前缀匹配会误判成要联网搜。
    r"(?<![检调审侦排稽普抽复核盘])"
    r"(搜一?下|搜搜|搜索|搜一?搜|帮我搜|去搜|查一?下|查查|帮我查|去查|查一?查)"
    r"|(百度|谷歌|google|bing)\s*一?下"
    r"|(联网|上网)\s*(搜|查)"
)
# 把命令词从查询里剥掉，剩下的才是真正要搜的东西。
# 整段前缀/后缀一起剥（imagegen 那次逐词剥把「穿条纹衬衫」剥成「穿纹衬衫」，
# 同一个坑不踩第二次）。
SEARCH_STRIP_PREFIX = re.compile(
    r"^\s*(帮我|给我|你|去|快|再)?\s*"
    r"(搜一?下|搜搜|搜索|搜一?搜|查一?下|查查|查一?查|百度一?下|谷歌一?下|google|bing|联网搜|上网搜|联网查)"
    r"\s*(一?下|看看|吧|呗|啊|嘛)?\s*[，,：:]?\s*"
)
SEARCH_STRIP_TAIL = re.compile(
    r"\s*(吧|呗|啊|嘛|谢谢|thx|好不好|行不行|可以吗|好吗)?\s*[？?。.！!]*\s*$"
)


def _derive_query(text: str) -> str:
    """从「搜一下今天有什么新闻」里得到「今天有什么新闻」。"""
    q = SEARCH_STRIP_PREFIX.sub("", text or "", count=1)
    q = SEARCH_STRIP_TAIL.sub("", q, count=1)
    q = q.strip(" 　,，.。!！?？:：、")
    # 剥完什么都不剩（用户只说了「搜一下」），退回原句让搜索引擎自己判断
    return q or (text or "").strip()


# ---------------------------------------------------------------- 链接收集


def _collect_urls(text: str) -> list[str]:
    urls = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?)]}»")
        if u not in urls:
            urls.append(u)
    for m in BARE_RE.finditer(text or ""):
        u = "https://" + m.group(1).rstrip(".,;:!?)]}»")
        if u not in urls:
            urls.append(u)
    return urls[:MAX_URLS]


def _is_bili(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _BILI_HOSTS)


# ---------------------------------------------------------------- 插件主体


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[web] 已加载：开关=%s 搜索=%s(%s) 单页%d字 最多%d链接 预算%.0fs",
            ENABLED,
            "已配置" if ZHIPU_KEY else "未配置",
            SEARCH_ENGINE,
            MAX_CHARS,
            MAX_URLS,
            BUDGET,
        )

    # ------------------------------------------------ 触发 1：自动抓链接

    @filter.on_llm_request()
    async def attach_links(self, event: AstrMessageEvent, req) -> None:
        """群消息里有链接就先抓好，把正文附到这次 LLM 请求上。"""
        if not ENABLED:
            return
        try:
            text = event.message_str or ""
            urls = _collect_urls(text)
            bare_bv = "" if urls else (_bili_id(text)[1] or "")
            if not urls and not bare_bv:
                return
            await asyncio.wait_for(
                self._attach(req, urls, bare_bv), timeout=BUDGET
            )
        except asyncio.TimeoutError:
            logger.warning("[web] 超过 %.0fs 预算，本轮放弃网页上下文", BUDGET)
        except BaseException as e:  # noqa: BLE001
            logger.error("[web] 钩子异常：%s", e)

    async def _attach(self, req, urls: list[str], bare_bv: str) -> None:
        blocks: list[str] = []

        if bare_bv:
            info, err = await _bili_info(bare_bv)
            blocks.append(info if info else f"（{bare_bv} 查不到：{err}）")

        for url in urls:
            if _is_bili(url):
                info, err = await _bili_info(url)
                if info:
                    blocks.append(info)
                    continue
                logger.info("[web] B 站查询失败(%s)，退回抓网页", err)
            title, body, err = await _fetch(url)
            if body and SENSITIVE_RE.search("%s %s" % (title or "", body)):
                # 同理：时政正文注进去会让整条 completion 被拒
                blocks.append(
                    f"链接 {url[:90]} 是时政内容，这类你不聊。"
                    "用自己的语气说一句「这个我不聊」，别复述里面的内容。"
                )
                logger.info("[web] 链接 %s 命中时政，已拦下", url[:60])
                continue
            if body:
                head = f"链接 {url[:90]}"
                if title:
                    head += f"，标题《{title[:60]}》"
                blocks.append(f"{head}\n正文摘录：{body}")
            else:
                blocks.append(
                    f"链接 {url[:90]} 打不开或读不到内容（{err}）。"
                    "不要编造这个链接的内容，就说你打不开。"
                )

        if not blocks:
            return
        req.extra_user_content_parts.append(
            TextPart(
                text="<webpage_context>\n"
                "下面是这条消息里链接的内容（你自己看不了网页，这是插件抓来的）。"
                "凭它回答，别编。\n" + "\n\n".join(blocks) + "\n</webpage_context>"
            )
        )
        logger.info("[web] 已附加 %d 个链接的内容", len(blocks))

    @filter.on_llm_request()
    async def attach_search(self, event: AstrMessageEvent, req) -> None:
        """用户明确要搜时，先搜好再交给模型。

        放在 attach_links 之后：如果消息里已经有链接，那才是他真正想问的
        东西，别再多搜一遍。
        """
        if not ENABLED:
            return
        try:
            text = event.message_str or ""
            if not SEARCH_CMD_RE.search(text):
                return
            # 消息里带链接 —— 链接钩子已经处理，不重复
            if _collect_urls(text) or _bili_id(text)[1]:
                return
            query = _derive_query(text)
            if len(query) < 2:
                return
            res, err = await asyncio.wait_for(_search(query), timeout=BUDGET)
            if res:
                res, dropped = _drop_sensitive(res)
                if dropped:
                    logger.info(
                        "[web] 自动搜索「%s」摘掉 %d 条时政结果",
                        query[:40], dropped,
                    )
                if not res:
                    # 全是时政。给模型一句说明，让它用自己的语气打哈哈，
                    # 而不是把时政正文塞进去换来一行 content_filter 报错。
                    req.extra_user_content_parts.append(
                        TextPart(
                            text="<search_context>\n"
                            f"搜「{query}」搜出来的全是时政新闻，这类你不聊。"
                            "用你自己的语气说一句「这个我不聊」糊过去，"
                            "别复述任何内容、别列标题。\n</search_context>"
                        )
                    )
                    logger.info("[web] 自动搜索「%s」全为时政，已拦下", query[:40])
                    return
            if not res:
                req.extra_user_content_parts.append(
                    TextPart(
                        text="<search_context>\n"
                        f"你试着搜了「{query}」但没搜到（{err}）。"
                        "如实告诉用户没查到，别编。\n</search_context>"
                    )
                )
                logger.info("[web] 自动搜索「%s」失败：%s", query[:40], err)
                return
            req.extra_user_content_parts.append(
                TextPart(
                    text="<search_context>\n"
                    "用户要你联网搜，插件已经搜好了，下面是结果。"
                    "凭它回答，别编没出现的内容；说话还是你平时的语气，"
                    "别念标题列表、别贴网址。\n"
                    + _fmt_search(query, res)
                    + "\n</search_context>"
                )
            )
            logger.info("[web] 自动搜索「%s」→ %d 条", query[:40], len(res))
        except asyncio.TimeoutError:
            logger.warning("[web] 自动搜索超过 %.0fs 预算，放弃", BUDGET)
        except BaseException as e:  # noqa: BLE001
            logger.error("[web] 自动搜索异常 %s：%s", type(e).__name__, e or "(无消息)")

    # ------------------------------------------------ 触发 2：LLM 函数工具

    @filter.llm_tool(name="web_search")
    async def web_search(self, event: AstrMessageEvent, query: str):
        """需要查网上的信息、时事新闻、不确定的事实、某个东西是什么时调用本工具联网搜索。

        Args:
            query(string): 搜索关键词，写成一句简短的查询语句
        """
        res, err = await _search(query)
        if not res:
            return f"搜索失败：{err}。请告诉用户你没查到，不要编造。"
        res, dropped = _drop_sensitive(res)
        if dropped:
            logger.info("[web] 工具搜索摘掉 %d 条时政结果", dropped)
        if not res:
            return (
                "搜出来的全是时政新闻，这类不聊。"
                "用你自己的语气说一句「这个我不聊」，别复述内容。"
            )
        logger.info("[web] 工具搜索「%s」→ %d 条", query[:40], len(res))
        return _fmt_search(query, res) + "\n（据此回答用户，别编造没出现的内容）"

    @filter.llm_tool(name="read_webpage")
    async def read_webpage(self, event: AstrMessageEvent, url: str):
        """需要读取某个网页/文章/链接的具体内容时调用本工具。

        Args:
            url(string): 完整网址，必须以 http:// 或 https:// 开头
        """
        if _is_bili(url):
            info, err = await _bili_info(url)
            if info:
                return info
        title, body, err = await _fetch(url)
        if not body:
            return f"打不开这个网页：{err}。请如实告诉用户你读不了，不要编造内容。"
        logger.info("[web] 工具读页 %s → %d 字", url[:60], len(body))
        return f"《{title}》\n{body}"

    @filter.llm_tool(name="bilibili_video")
    async def bilibili_video(self, event: AstrMessageEvent, video: str):
        """需要查 B 站（哔哩哔哩）视频的标题、UP 主、播放量等信息时调用本工具。

        Args:
            video(string): B 站视频链接、BV 号（如 BV1xx411c7mD）或 av 号
        """
        info, err = await _bili_info(video)
        if not info:
            return f"查不到这个视频：{err}。请如实告诉用户，不要编造。"
        return info

    # ------------------------------------------------ 只做清理，不兜底
    #
    # 实测「搜一下 deepseek 最新消息」时模型回了
    #     行，我看看最近有啥动静。\n\nweb_search(query="DeepSeek 最新消息 2026年9月")
    # 搜索压根没执行，那行调用却原样进了群。
    #
    # 这里刻意**不做兜底搜索**：搜索结果要进模型的上下文才有意义，
    # 而 on_llm_response 已经在模型说完之后，补搜出来的东西只能干巴巴地
    # 贴在后面，反而更怪。主路径是 on_llm_request 自动注入（链接场景）
    # 和工具（模型愿意调的时候）。这个钩子只负责把脏东西擦掉。

    @filter.on_llm_response()
    async def strip_leaks(self, event: AstrMessageEvent, response) -> None:
        try:
            raw = getattr(response, "completion_text", "") or ""
            # 参与接力：万一 web 先跑，也要把原文留给后面的插件
            cleaned, _ = _leak_relay(event, raw, None, None)
            if cleaned != raw:
                try:
                    response.completion_text = cleaned
                except Exception:  # noqa: BLE001
                    response._completion_text = cleaned
                logger.warning("[web] 已清理模型泄漏的伪工具调用标记")
        except BaseException as e:  # noqa: BLE001
            logger.error("[web] 清理钩子异常：%s", e)

    # ------------------------------------------------ 触发 3：显式指令

    @filter.command("看网页")
    async def cmd_read(self, event: AstrMessageEvent):
        """/看网页 <url> —— 直接抓网页正文。"""
        arg = self._arg(event, "看网页")
        if not arg:
            yield event.plain_result("用法：/看网页 https://example.com")
            return
        urls = _collect_urls(arg)
        if not urls:
            yield event.plain_result("没认出网址")
            return
        if _is_bili(urls[0]):
            info, err = await _bili_info(urls[0])
            yield event.plain_result(info or f"查不到：{err}")
            return
        title, body, err = await _fetch(urls[0])
        if not body:
            yield event.plain_result(f"读不了：{err}")
            return
        # 指令结果直接进群、不经过 LLM，不会踩上游 content_filter，
        # 但机器人自己把时政内容贴进群同样是风险。
        if SENSITIVE_RE.search("%s %s" % (title or "", body)):
            yield event.plain_result("这是时政内容，我不聊。")
            return
        yield event.plain_result(f"《{title}》\n\n{body[:900]}")

    @filter.command("搜")
    async def cmd_search(self, event: AstrMessageEvent):
        """/搜 <关键词> —— 联网搜索。"""
        arg = self._arg(event, "搜")
        if not arg:
            yield event.plain_result("用法：/搜 AstrBot 是什么")
            return
        res, err = await _search(arg)
        if not res:
            yield event.plain_result(f"搜不到：{err}")
            return
        # 指令结果直接进群、不经过 LLM，所以不会踩上游的 content_filter，
        # 但机器人自己把时政新闻贴进群同样是风险，一并拦掉。
        res, dropped = _drop_sensitive(res)
        if not res:
            yield event.plain_result("搜出来全是时政，这个我不聊。")
            return
        lines = [f"「{arg}」搜索结果："]
        if dropped:
            lines.append(f"（摘掉 {dropped} 条时政）")
        for i, r in enumerate(res[:4], 1):
            lines.append(f"{i}. {r['title'][:52]}")
            if r["content"]:
                lines.append(f"   {r['content'][:110]}")
        yield event.plain_result("\n".join(lines))

    @filter.command("b站")
    async def cmd_bili(self, event: AstrMessageEvent):
        """/b站 <链接或BV号> —— 查 B 站视频信息。"""
        arg = self._arg(event, "b站")
        if not arg:
            yield event.plain_result("用法：/b站 BV1xx411c7mD")
            return
        info, err = await _bili_info(arg)
        yield event.plain_result(info or f"查不到：{err}")

    @filter.command("联网状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """/联网状态 —— 查看联网能力配置与连通性。"""
        res, err = await _search("测试")
        yield event.plain_result(
            f"联网：{'开' if ENABLED else '关'}\n"
            f"搜索：{SEARCH_ENGINE}｜Key：{'已配置' if ZHIPU_KEY else '未配置'}｜"
            f"连通：{'正常' if res else '失败(' + err[:40] + ')'}\n"
            f"单页上限 {MAX_CHARS} 字｜每条消息最多 {MAX_URLS} 个链接｜"
            f"超时 {TIMEOUT}s｜预算 {BUDGET:.0f}s\n"
            f"B 站走 wbi/view 端点（香港直连普通端点被 -412 拦）\n"
            f"已缓存 {len(_cache)} 个页面"
        )

    @staticmethod
    def _arg(event: AstrMessageEvent, cmd: str) -> str:
        text = (event.message_str or "").strip()
        for p in (cmd, "/" + cmd):
            if text.startswith(p):
                return text[len(p) :].strip()
        return text
