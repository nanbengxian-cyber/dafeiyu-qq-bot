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

# 模型自造的伪媒体标记。[语音]/[图片]/[视频] 这类舞台提示，实测长在行首。
# 三重收窄防误伤：① 只认行首；② 括号里只有那个词；③ 带冒号的一律不碰
# （[贴纸:嘲笑] 有自己的链，[At:123] 是框架的）。
_MEDIA_TAG_RE = re.compile(
    r"(?m)^[ \t\u3000]*[\[【]\s*"
    r"(?:语音|語音|音频|音頻|voice|audio|图片|圖片|image|photo|pic"
    r"|视频|視頻|video|表情|动图|動圖|gif)"
    r"\s*[\]】][ \t\u3000]*"
)
# 形态 V：模型自己加的旁白 —— 「（图片自动发给你）」「（语音已发送）」。
# 不是伪调用，但同属「把机制说出来」这类。条件：内容 ≤14 字 + 媒体名词 +
# 发送动作词，三者齐了才清。
_STAGE_NOTE_RE = re.compile(
    r"[（(]"
    r"(?=[^）)\n]{0,14}[）)])"
    r"(?=[^）)\n]*(?:图片|图|语音|音频|视频|表情包|贴纸))"
    r"[^）)\n]*(?:自动发|已发|发给|发送|附上|见下|在下面|随后发|马上发)"
    r"[^）)\n]*[）)]"
)
# 形态 W：不带参数的裸标记 —— `[tool_call]`、`[工具调用]`、`[函数调用]`。
_BARE_CALL_RE = re.compile(
    r"[\[【]\s*(?:tool_call|tool_calls|function_call|function_calls|"
    r"工具调用|函数调用|调用工具|工具call)\s*[\]】]",
    re.I,
)
# 清理完一个字都不剩时的回落短话
_EMPTY_FALLBACKS = ("这就来", "等着", "来了", "行", "好嘞")
# 恰好两个连续反引号 = 空的行内代码段
_EMPTY_SPAN = re.compile(r"(?<!`)``(?!`)")
_LONE_FENCE_LINE = re.compile(r"(?m)^[ \t]*`{1,3}[ \t]*$\n?")
# 形态 Z：中文工具名 + 方括号 —— `[生成图片: 一只猫]`。
_CN_ACTS = "生成|发送|调用|播放|合成|搜索|联网|读取|查看|获取|制作|画"
_LEAK_Z = re.compile(
    rf"[\[【]\s*(?:{_CN_ACTS})[^\]】\n]{{0,10}}[:：][^\]】\n]*[\]】]?"
)
# 中文译名 -> 英文工具名（_extract_arg 用）
_CN_TOOL_ALIAS = {
    "generate_image": r"生成图片|生成图像|画图|生成一张图|作图|绘图|制作图片",
    "send_voice": r"发送语音|语音合成|合成语音|播放语音|发语音",
    "generate_video": r"生成视频|制作视频|生成动画|做视频",
    "web_search": r"联网搜索|网页搜索|搜索网页|搜索",
    "read_webpage": r"读取网页|查看网页|打开网页",
    "bilibili_video": r"查看视频信息|获取视频信息|查B站",
}
# markdown 图片：![alt](url)。QQ 不渲染，一定原样进群。
_MD_IMG_RE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
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
    """从泄漏的调用里抠出某个参数的值。带引号、不带引号、XML、JSON 都认。"""
    src = text or ""
    m = re.search(
        rf"\b{tool}\s*\([^)]*?\b{arg}\s*=\s*(?P<q>[\"\'])(?P<v>.*?)(?<!\\)(?P=q)",
        src, re.S,
    )
    if m:
        v = (m.group("v") or "").strip()
        if v:
            return v
    m = re.search(
        rf"<\s*(?:antml:)?parameter\s+name\s*=\s*[\"\']{arg}[\"\']\s*>(.*?)"
        r"(?:</\s*(?:antml:)?parameter\s*>|$)",
        src, re.S | re.I,
    )
    if m:
        v = m.group(1).strip()
        if v:
            return v
    m = re.search(
        rf"[\"\']{arg}[\"\']\s*:\s*[\"\'](.+?)[\"\']", src, re.S
    )
    if m:
        v = m.group(1).strip()
        if v:
            return v
    m = re.search(
        rf"\b{tool}\s*\(\s*(?:\w+\s*=\s*[^,)]*,\s*)*?{arg}\s*=\s*"
        r"(?P<v>[^\n]*?)\s*(?:[\)）]|$)",
        src, re.S,
    )
    if m:
        v = (m.group("v") or "").strip().strip("\"\'")
        if v:
            return v
    m = re.search(
        rf"<\s*{tool}\s*>(.*?)(?:</\s*{tool}\s*>|$)", src, re.S | re.I
    )
    if m:
        v = (m.group(1) or "").strip().strip("\"\'")
        if v:
            return v
    m = re.search(
        rf"[\[【]\s*{tool}\s*[:：]([^\]】\n]*)[\]】]?", src, re.I
    )
    if m:
        v = (m.group(1) or "").strip().strip("\"\'")
        if v:
            return v
    alias = _CN_TOOL_ALIAS.get(tool)
    if alias:
        m = re.search(
            rf"[\[【]\s*(?:{alias})\s*[:：]([^\]】\n]*)[\]】]?", src
        )
        if m:
            v = (m.group(1) or "").strip().strip("\"\'")
            if v:
                return v
    return None


def _leak_relay(event, raw, tool, arg):
    """清理 + 抠参数，并在插件之间接力原文。

    坑：四个插件的 on_llm_response 钩子按**插件加载顺序**依次跑，谁先跑谁就把
    泄漏标记清掉了，后面的插件想从标记里抠参数就什么都拿不到。所以第一个发现
    泄漏的插件把**原文**存进 event.extra，后面的都从这儿读。清理本身幂等。
    """
    raw = raw or ""
    cleaned = _strip_all_leaks(raw)
    if raw.strip() and not cleaned.strip():
        cleaned = random.choice(_EMPTY_FALLBACKS)
    src = raw
    if cleaned != raw:
        try:
            if not event.get_extra("dsh_leak_raw"):
                event.set_extra("dsh_leak_raw", raw)
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            src = event.get_extra("dsh_leak_raw") or raw
        except Exception:  # noqa: BLE001
            src = raw
    leaked = _extract_arg(src, tool, arg) if tool else None
    return cleaned, leaked


# ---------------------------------------------------------------- 配置

ENABLED = os.environ.get("DSH_WEB", "1") not in ("0", "false", "False", "")
# 单次抓取正文最多保留多少字
MAX_CHARS = int(os.environ.get("DSH_WEB_MAX_CHARS", "6000"))
# 一条消息最多自动抓几个链接
MAX_URLS = int(os.environ.get("DSH_WEB_MAX_URLS", "3"))
# 单次请求超时（秒）
TIMEOUT = int(os.environ.get("DSH_WEB_TIMEOUT", "15"))
# 一次消息处理的总预算（秒）：宁可这轮不抓，也不能把群聊卡住
BUDGET = float(os.environ.get("DSH_WEB_BUDGET", "35"))
# 同一条指令最多同时几个链接
SEARCH_COUNT = int(os.environ.get("DSH_WEB_SEARCH_COUNT", "5"))
SEARCH_SNIPPET = int(os.environ.get("DSH_WEB_SNIPPET", "160"))
SEARCH_ENGINE = os.environ.get("DSH_WEB_SEARCH_ENGINE", "zhipu").strip().lower()

ZHIPU_BASE = os.environ.get(
    "DSH_WEB_ZHIPU_BASE", "https://open.bigmodel.cn/api/paas/v4"
).rstrip("/")
ZHIPU_KEY = os.environ.get("DSH_WEB_ZHIPU_KEY", "")
DDG_BASE = os.environ.get("DSH_WEB_DDG_BASE", "https://html.duckduckgo.com/html/")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 抓不到的站点特征（香港实测）——抓到这些就老实说抓不到，别让模型编
KNOWN_BLOCKED = ("mp.weixin.qq.com", "weixin", "juejin", "36kr", "baijiahao",
                 "weibo", "zhihu", "wikipedia.org", "b23.tv", "bilibili")

# path -> 页面正文缓存（同一轮/邻近轮次复用）
_cache: dict[str, str] = {}
_stat = {"inject": 0, "fetch": 0, "fail": 0, "watch": 0, "no_urls": 0}

# ---------------------------------------------------------------- SSRF 防护

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("::1"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _ok_net(ip_s: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_s.strip())
    except ValueError:
        return False
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return False
    return all(not ip in n for n in _PRIVATE_NETS)


async def _resolve(url: str) -> str | None:
    host = urlparse(url).hostname
    if not host:
        return None
    try:
        infos = await asyncio.get_event_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
    except OSError:
        return None
    for info in infos:
        ip = info[4][0]
        if _ok_net(ip):
            return ip
    return None


async def _fetch(url: str) -> tuple[str, str]:
    """抓一个 URL 的正文纯文本。返回 (text, error)。"""
    if url in _cache:
        return _cache[url], ""
    par = urlparse(url)
    if par.scheme not in ("http", "https"):
        return "", "只支持 http/https"
    ip = await _resolve(url)
    if not ip:
        return "", f"解析不了 {par.hostname}（或不是公网地址）"

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        ) as session:
            async with session.get(
                url,
                headers={"User-Agent": UA},
                allow_redirects=True,
                ssl=False,
            ) as resp:
                final = str(resp.url)
                if urlparse(final).hostname != urlparse(url).hostname:
                    # 重定向跳走了：重新校验目标 IP
                    ip2 = await _resolve(final)
                    if not ip2:
                        return "", "重定向目标不是公网地址"
                if resp.status != 200:
                    return "", f"HTTP {resp.status}"
                ctype = resp.headers.get("Content-Type", "").lower()
                if ctype and "html" not in ctype and "text" not in ctype and "json" not in ctype:
                    return "", f"不是网页（{ctype[:40]}）"
                body = await resp.text(errors="replace")
    except asyncio.TimeoutError:
        return "", f"抓取超时（>{TIMEOUT}s）"
    except aiohttp.ClientError as e:
        return "", f"{type(e).__name__}: {e}"

    text = _html_to_text(body)
    if not text.strip():
        return "", "页面没有可读正文（可能是前端渲染）"
    text = text[:MAX_CHARS]
    _cache[url] = text
    _stat["fetch"] += 1
    return text, ""


def _html_to_text(html: str) -> str:
    """把 HTML 转成能读的纯文本：剥标签、收空白、拆段落。"""
    import html as _html

    s = _html.unescape(html or "")
    s = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<style[^>]*>.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "\n", s)
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


# ---------------------------------------------------------------- 搜索

async def _search_zhipu(query: str) -> tuple[list[dict], str]:
    """智谱 web_search。每条自带摘要，最稳的一条路。"""
    if not ZHIPU_KEY:
        return [], "没配智谱 key"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        ) as session:
            async with session.post(
                f"{ZHIPU_BASE}/web_search",
                headers={
                    "Authorization": f"Bearer {ZHIPU_KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": "web_search", "query": query},
            ) as resp:
                if resp.status != 200:
                    return [], f"HTTP {resp.status}"
                data = await resp.json(content_type=None)
    except (asyncio.TimeoutError, aiohttp.ClientError, ValueError) as e:
        return [], f"{type(e).__name__}: {e}"
    results = (data.get("search_result") or [])[:SEARCH_COUNT]
    # content 本身带标签，剥成干净文本
    out = []
    for r in results:
        c = re.sub(r"<[^>]+>", "", r.get("content") or "")
        out.append(
            {
                "title": r.get("title") or "",
                "link": r.get("link") or r.get("url") or "",
                "content": c.strip()[:SEARCH_SNIPPET * 3],
                "date": "",
            }
        )
    return out, "" if out else "没有结果"


async def _search_ddg(query: str) -> tuple[list[dict], str]:
    """DuckDuckGo html 端点兜底。"""
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        ) as session:
            async with session.get(
                DDG_BASE,
                params={"q": query},
                headers={"User-Agent": UA},
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    return [], f"HTTP {resp.status}"
                html = await resp.text(errors="replace")
    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        return [], f"{type(e).__name__}: {e}"

    from html.parser import HTMLParser

    links, snips = [], []
    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self._cur = None
        def handle_starttag(self, tag, attrs):
            if tag == "a":
                d = dict(attrs)
                cls = d.get("class", "")
                if "result__a" in cls:
                    self._cur = {"href": d.get("href", "")}
            elif tag == "a" and self._cur is None:
                pass
        def handle_endtag(self, tag):
            if tag == "a" and self._cur:
                links.append(self._cur)
                self._cur = None
        def handle_data(self, data):
            if self._cur is not None:
                self._cur["text"] = (self._cur.get("text") or "") + data
    p = _P()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001
        pass

    # 摘要块：result__snippet
    snips = []
    class _S(HTMLParser):
        def __init__(self):
            super().__init__()
            self._on = False
            self._buf = []
        def handle_starttag(self, tag, attrs):
            if dict(attrs).get("class", "") == "result__snippet":
                self._on = True
        def handle_endtag(self, tag):
            if tag == "p" and self._on:
                snips.append("".join(self._buf).strip())
                self._on = False
                self._buf = []
        def handle_data(self, data):
            if self._on:
                self._buf.append(data)
    s = _S()
    try:
        s.feed(html)
    except Exception:  # noqa: BLE001
        pass

    out = []
    for a, sn in list(zip(links, snips))[:SEARCH_COUNT]:
        out.append(
            {
                "title": a.get("text", "").strip(),
                "link": a.get("href", ""),
                "content": sn[:SEARCH_SNIPPET],
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
    """b23.tv 短链换成真实地址。带重试：B 站 CDN 会对香港出口做分钟级风控，
    连接可能被掐到超时（约几分钟后自行恢复），重试可吸收这类抖动。"""
    last = url
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12)
            ) as session:
                async with session.get(
                    url, headers={"User-Agent": UA}, allow_redirects=True, ssl=False
                ) as resp:
                    return str(resp.url)
        except Exception:  # noqa: BLE001
            last = url
            if attempt < 2:
                await asyncio.sleep(2)
    return last


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
    data = None
    last_err = ""
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12)
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
                        last_err = f"HTTP {resp.status}"
                    else:
                        data = await resp.json(content_type=None)
                        break
        except asyncio.TimeoutError:
            last_err = "超时"
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        if attempt < 2:
            await asyncio.sleep(2)
    if data is None:
        return "", last_err

    if data.get("code") != 0:
        return "", f"B站返回 code {data.get('code')}"
    d = data.get("data") or {}
    owner = (d.get("owner") or {}).get("name") or "未知UP"
    lines = [
        f"标题：{d.get('title') or '未知'}",
        f"UP：{owner}｜时长：{_fmt_dur(d.get('duration') or 0)}",
        f"播放：{_fmt_cnt(d.get('stat', {}).get('view') or 0)}｜"
        f"点赞：{_fmt_cnt(d.get('stat', {}).get('like') or 0)}｜"
        f"弹幕：{_fmt_cnt(d.get('stat', {}).get('danmaku') or 0)}",
        f"简介：{d.get('desc') or '（无）'}",
    ]
    return "\n".join(lines), ""


BV_RE = re.compile(r"BV[0-9A-Za-z]{8,12}")
AV_RE = re.compile(r"av(\d+)")

# 链接提取：http(s) 或裸域名/BV 号
URL_RE = re.compile(
    r"https?://[^\s<>\"']+"
    r"|[A-Za-z0-9-]+\.[a-z]{2,}(?:/[^\s<>\"']*)?"
    r"|BV[0-9A-Za-z]{8,12}"
    r"|b23\.tv/\S+"
)


def _urls(text: str) -> list[str]:
    found = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0)
        if "." not in u and not u.startswith("BV") and "b23" not in u:
            continue
        if not u.startswith("http"):
            u = "https://" + u
        if u not in found:
            found.append(u)
    return found[:MAX_URLS]


def _is_bili(u: str) -> bool:
    return any(k in u for k in ("bilibili", "b23.tv", "BV", "av"))


# ---------------------------------------------------------------- 插件主体


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[web] 已加载：%s 抓正文≤%d字 每消息≤%d链接 超时%ds 预算%.0fs 搜索=%s",
            "开" if ENABLED else "关", MAX_CHARS, MAX_URLS, TIMEOUT, BUDGET,
            SEARCH_ENGINE,
        )

    # ------------------------------------------------ 主力：钩子自动抓链接

    @filter.on_llm_request()
    async def auto_fetch(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED:
            return
        try:
            text = event.message_str or ""
            urls = _urls(text)
            if not urls:
                _stat["no_urls"] += 1
                return
            # 只看本群的消息；别在私聊/别的平台瞎抓
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            await asyncio.wait_for(
                self._run_fetch(req, urls), timeout=BUDGET
            )
        except asyncio.TimeoutError:
            logger.warning("[web] 超过 %.0fs 预算，本轮放弃抓取", BUDGET)
        except BaseException as e:  # noqa: BLE001
            logger.error("[web] 钩子异常：%s", e)

    async def _run_fetch(self, req, urls: list[str]) -> None:
        parts = []
        for u in urls:
            if _is_bili(u):
                txt, err = await _bili_info(u)
                flag = "B站"
            else:
                txt, err = await self._lookup_page(u)
                flag = "网页"
            if txt:
                _stat["inject"] += 1
                parts.append(f"[{flag}] {u}\n{txt}")
            else:
                _stat["fail"] += 1
                logger.warning("[web] 抓取失败 %s：%s", u, err)
            # 顺序抓，间隔一下，别把对方 CDN 打疼
            await asyncio.sleep(0.3)
        if not parts:
            # 一个都拿不到也要给模型一句实话，不许它编
            req.extra_user_content_parts.append(
                TextPart(
                    text=f"<web_note>用户发了链接但插件一个都抓不到（{urls[0]}）。"
                    "你的回复不要说看了链接内容，就说你这边看不到。"
                    "</web_note>"
                )
            )
            return
        block = (
            "<web_content>\n下面是群消息里链接的内容（插件抓的，可能有截断）：\n"
            + "\n\n".join(parts)
            + "\n</web_content>"
        )
        req.extra_user_content_parts.append(TextPart(text=block))
        logger.info("[web] 注入 %d 个链接内容（%d 字）", len(parts), len(block))

    async def _lookup_page(self, url: str) -> tuple[str, str]:
        if any(k in url for k in KNOWN_BLOCKED):
            return "", "该站香港直连抓不了正文"
        txt, err = await _fetch(url)
        if txt:
            return txt, ""
        # 抓不到正文：拿 URL 里的词去搜索，拿结果当兜底
        host = urlparse(url).hostname or ""
        words = host.replace("www.", "").split(".")[0]
        res, serr = await _search(words)
        if res:
            return _fmt_search(words, res), ""
        return "", f"{err}；搜索兜底也失败：{serr}"

    # ------------------------------------------------ 显式指令

    @filter.command("看网页")
    async def cmd_read(self, event: AstrMessageEvent):
        arg = self._arg(event, "看网页")
        if not arg:
            yield event.plain_result("用法：/看网页 <url>")
            return
        txt, err = await self._lookup_page(arg)
        if txt:
            yield event.plain_result(txt[:2000])
        else:
            yield event.plain_result(f"看不了：{err}")

    @filter.command("搜")
    async def cmd_search(self, event: AstrMessageEvent):
        q = self._arg(event, "搜")
        if not q:
            yield event.plain_result("用法：/搜 <关键词>")
            return
        res, err = await _search(q)
        if res:
            yield event.plain_result(_fmt_search(q, res)[:2000])
        else:
            yield event.plain_result(f"搜不到：{err}")

    @filter.command("b站")
    async def cmd_bili(self, event: AstrMessageEvent):
        arg = self._arg(event, "b站")
        if not arg:
            yield event.plain_result("用法：/b站 <BV号或链接>")
            return
        txt, err = await _bili_info(arg)
        yield event.plain_result(txt if txt else f"查不到：{err}")

    # ------------------------------------------------ LLM 函数工具

    @filter.llm_tool(name="web_search")
    async def tool_search(self, event: AstrMessageEvent, query: str):
        """联网搜索关键词，返回带摘要的结果列表。

        Args:
            query(string): 搜索词
        """
        res, err = await _search(query or "")
        if res:
            return _fmt_search(query, res)
        return f"搜索失败：{err}"

    @filter.llm_tool(name="read_webpage")
    async def tool_read(self, event: AstrMessageEvent, url: str):
        """抓一个网页的正文（自动转纯文本并截断）。

        Args:
            url(string): 要看的网页地址
        """
        txt, err = await self._lookup_page(url or "")
        if txt:
            return txt[:MAX_CHARS]
        return f"看不了：{err}"

    @filter.llm_tool(name="bilibili_video")
    async def tool_bili(self, event: AstrMessageEvent, url_or_bvid: str):
        """查 B 站视频信息（标题/UP主/时长/播放/点赞）。

        Args:
            url_or_bvid(string): B站链接或BV号
        """
        txt, err = await _bili_info(url_or_bvid or "")
        return txt if txt else f"查不到：{err}"

    # ------------------------------------------------ 兜底清理

    @filter.on_llm_response()
    async def clean_leaks(self, event: AstrMessageEvent, response) -> None:
        """把模型泄漏到回复里的伪工具调用清掉（全部六个工具）。"""
        try:
            raw = getattr(response, "completion_text", "") or ""
            if not raw.strip():
                return
            cleaned, _ = _leak_relay(event, raw, None, None)
            if cleaned != raw:
                try:
                    response.completion_text = cleaned
                except Exception:  # noqa: BLE001
                    response._completion_text = cleaned
                logger.warning("[web] 已清理泄漏的伪工具调用标记")
        except BaseException as e:  # noqa: BLE001
            logger.warning("[web] 清理钩子异常：%s", e)

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
