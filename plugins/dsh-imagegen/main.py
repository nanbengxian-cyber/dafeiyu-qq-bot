# dsh-imagegen —— QQ 群聊 AI 文生图插件（Agnes AI 渠道）。
#
# 三条触发路径，从强到弱：
#   1) LLM 函数工具 generate_image —— 模型规范调用（最理想）。
#   2) on_llm_response 兜底钩子 —— 模型「嘴上答应画、但没真调工具」时，插件
#      自己识别意图并出图。这是本插件最关键的一层，原因见下。
#   3) 显式指令 /画图 <描述> —— 完全绕过模型。
#
# 为什么必须有第 2 层：
#   deepseek-v4-flash-0731 这类便宜模型在长群聊上下文里经常「假装调用」——
#   正经的 tool_calls 字段是空的，却把 Anthropic 风格的 <invoke name="..."> 
#   块当成普通文本吐出来，或者干脆只回一句「在画了，等着」然后什么也不做。
#   实测：单轮干净上下文下它 5 次里 4 次能正确调用，但一旦带上完整人格提示词
#   + 250 条历史，就退化成纯文本承诺。所以必须由插件兜底。
#
# 其它设计要点（都是踩过的坑）：
#   - 该渠道 /v1/images/generations 必须显式带 model，否则默认 dall-e 并返回
#     503 model_not_found（No available channel for model dall-e）。
#   - 返回体是 {"data":[{"url": ...,"b64_json":""}]}，url 与 b64_json 二者
#     其一有值，两种都要兼容。
#   - 图片走 event.send() 单独发一条消息，不要塞进 result_chain：AstrBot 的
#     completion_text 在设置 result_chain 后会由 get_plain_text() 派生，
#     「文字+图片」混排会把图片丢掉（dsh-sticker 插件已验证过此行为）。
#   - 泄漏的 <invoke> 标记必须无条件从文字里清掉，否则群里会看到一堆 XML。
#   - 生图耗时 5~15s，用较长 timeout，主备模型自动降级。
#   - 并发上限用信号量兜住，1.6G 内存的小机器别被刷爆。

import asyncio
import base64
import os
import random
import re
import time

import aiohttp

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.provider import LLMResponse
from astrbot.core import logger
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

API_BASE = os.environ.get("DSH_IMG_API_BASE", "https://apihub.agnes-ai.com/v1")
API_KEY = os.environ.get("DSH_IMG_API_KEY", "")
MODEL = os.environ.get("DSH_IMG_MODEL", "agnes-image-2.5-flash")
SIZE = os.environ.get("DSH_IMG_SIZE", "1024x1024")
TIMEOUT = int(os.environ.get("DSH_IMG_TIMEOUT", "120"))
# 备用模型：主模型渠道拥塞时按顺序降级
FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get(
        "DSH_IMG_FALLBACK", "agnes-image-2.1-flash,agnes-image-2.0-flash"
    ).split(",")
    if m.strip()
]
SAVE_DIR = os.environ.get("DSH_IMG_SAVE_DIR", "/AstrBot/data/imagegen")
# 默认画风：二次元可爱简笔画。无论 prompt 来自模型还是 /画图 指令，都会追加，
# 保证群里出图风格统一。想临时换风格改这个环境变量即可。
STYLE_SUFFIX = os.environ.get(
    "DSH_IMG_STYLE",
    "anime style, cute chibi character, kawaii, simple clean line art, "
    "minimal flat colors, soft pastel palette, thin outlines, sketch-like, "
    "white or very simple background, lots of negative space, "
    "no photorealism, no 3d render, no heavy shading, no complex background",
)
# 同时最多几个生图请求在跑
MAX_CONCURRENCY = int(os.environ.get("DSH_IMG_CONCURRENCY", "2"))
# 同一个会话两次生图之间的最小间隔（秒），防刷
COOLDOWN = int(os.environ.get("DSH_IMG_COOLDOWN", "8"))
# 是否启用「模型嘴上答应了但没调工具」的兜底出图
AUTO_FALLBACK = os.environ.get("DSH_IMG_AUTO", "1") not in ("0", "false", "False")
# 小鲸鱼自己的形象，用户说「画你自己/自拍」时用
SELF_PORTRAIT = os.environ.get(
    "DSH_IMG_SELF",
    "小鲸鱼 DeepSeek 娘，蓝白配色的可爱少女，蓝色长发，头上有鲸鱼耳朵，"
    "身后有鲸鱼尾巴，闭着眼睛微笑，上半身特写",
)

_sem = asyncio.Semaphore(MAX_CONCURRENCY)
_last_call: dict[str, float] = {}


# ---------------------------------------------------------------- 意图识别

# 模型把工具调用当文本吐出来时的各种形态，一律清掉。
# 形态一：<function_calls><invoke name="generate_image"><parameter name="prompt">…
LEAK_INVOKE_RE = re.compile(
    r"<\s*(?:antml:)?function_calls\s*>.*?(?:</\s*(?:antml:)?function_calls\s*>|$)"
    r"|<\s*(?:antml:)?invoke\b.*?(?:</\s*(?:antml:)?invoke\s*>|$)",
    re.S | re.I,
)
# 形态二：```json {"name":"generate_image","arguments":{"prompt":"…"}} ```
LEAK_JSON_RE = re.compile(
    r"```(?:json|tool_code)?\s*\{[^`]*generate_image[^`]*\}\s*```", re.S | re.I
)
# 从泄漏文本里抠出 prompt 参数
LEAK_PROMPT_RE = re.compile(
    r"<\s*(?:antml:)?parameter\s+name\s*=\s*[\"']prompt[\"']\s*>(.*?)"
    r"(?:</\s*(?:antml:)?parameter\s*>|$)",
    re.S | re.I,
)
LEAK_PROMPT_JSON_RE = re.compile(r"[\"']prompt[\"']\s*:\s*[\"'](.+?)[\"']", re.S)

# 用户在要图 —— 强信号：动词 + 明确的图片类名词。
# 「发个照片」「给张图」也算：用户要的是图，至于是画还是找，对他没差别。
# 实测漏过「@大肥鱼 发个照片」，机器人回了「发了！说好的偷藏一张自拍」却没图。
DRAW_INTENT_RE = re.compile(
    r"(画|绘|生成|做|整|来|搞|弄|发|给|甩|扔|放|晒|p|P)\s*(?:一)?\s*(?:张|个|幅|副|下|点)?\s*"
    r"[^。！？\n]{0,12}?(图|图片|照|照片|画|画像|壁纸|头像|表情包|自拍|插画|海报|封面|立绘|简笔画)"
    r"|画画|画个|画一|画张|画只|画条|画头|画只|自拍|拍一张|拍张|拍个|拍照"
    r"|生成图|出图|作图|画图|来点图|上图|看看你|长什么样|长啥样|什么样子"
)

# 弱信号：动词 + 量词，但宾语是具体事物而不是「图」这个字。
#
# 实测群里最常见的要图说法恰恰属于这一类：「给我生成某个群友」「帮我生成一个懒羊羊」
# 「去给我画点」「弄个猫娘」——句子里根本没有「图/照/壁纸」这些名词，
# 于是强信号正则全部落空，哪怕模型已经明确答应「行，等着」也不出图。
# 这是「有时候不生图」的主因。
#
# 拆成两级是为了控制误伤：
#   画/绘/生成/P图 本身就偏向作画，配上量词已经足够明确；
#   做/整/搞/弄 太泛（做饭、整理、搞定），额外要求模型回复里也提到画/图。
WEAK_DRAW_RE = re.compile(
    r"(画|绘|生成|p图|P图|ps|PS)\s*(?:一)?\s*(?:张|个|幅|副|下|点|只|条|头|位|只)"
    r"|画点|画些|生成点|生成些"
)
WEAK_LOOSE_RE = re.compile(r"(做|整|搞|弄)\s*(?:一)?\s*(?:张|个|幅|副|只|条|头|位)")
# 明显不是画面的东西。「弄个猫娘」该出图，「弄个表格」不该——差别在宾语。
# 与其枚举画得出来的东西（无穷），不如排除明显画不出的（有限且稳定）。
NON_VISUAL_RE = re.compile(
    r"(表格|表单|投票|问卷|文件|文档|链接|账号|名单|统计|报表|总结|摘要|方案|计划|"
    r"清单|群|机器人|脚本|代码|程序|插件|饭|菜|外卖|奶茶|咖啡|安排|时间|日程|提醒|"
    r"教程|攻略|规则|公告|通知|记录|笔记|翻译|数据|报告|建议|主意|办法)"
)

# 别的插件的活儿。这一条要**否决强信号**，所以不能塞进 NON_VISUAL_RE
# （那个只作用于弱信号）。
#
# 实测两处翻车：
#   「生成一段动画」——「动画」里带个「画」字，DRAW_INTENT_RE 的名词表里有「画」，
#     于是强信号命中，本插件抢着发了一张静态图；
#   「拍个视频」——DRAW_INTENT_RE 的字面分支里就有「拍个」，同样命中。
# 用户要的是视频，收到一张图属于答错，比不回还糟。
OTHER_MEDIA_RE = re.compile(
    r"(视频|动画|短片|影片|片子|小电影|录像|语音|声音|音频|念一?[句遍段]|说话|唱)"
)
# 但「做个视频封面」「动画风格的壁纸」这类确实是要图 —— 判据是句里有
# 明确的图片名词。注意这里**不能收「画」**：正是「动画」里的「画」引发了误判。
EXPLICIT_IMG_RE = re.compile(
    r"(图|图片|照|照片|画像|壁纸|头像|表情包|自拍|插画|海报|封面|立绘|简笔画|"
    r"画画|画图|出图|作图|画一张|画张|画个)"
)
# 模型自己的回复里提到在画什么 —— 这是最可靠的旁证：
# 用户说「给我生成某个群友」，模型回「画个某个群友是吧？行，等着」，
# 模型已经把请求理解成画图了，插件没理由再怀疑。
# 也要认「画给你看」「画来了」这种不带量词的说法。
ASSIST_DRAW_RE = re.compile(
    r"(画|绘|生成)\s*(?:一)?\s*(?:张|个|幅|副|下|点|只|条|头|位|好)"
    r"|画给|画来|画了|给你画|帮你画|画一"
)

# 「你自己」类请求 → 用小鲸鱼形象
SELF_REF_RE = re.compile(r"(你的|你自己|自己的|自拍|你本人|本人|小鲸鱼|大肥鱼)")
# 模型嘴上答应要画。
# 「画给你看」「瞧好了」这类没有「等」字的答应之前全漏了——
# 实测「行，画给你看！蓝的，胖的…」就因此没触发出图。
PROMISE_RE = re.compile(
    r"(在画|画了|这就画|马上画|就画|给你画|帮你画|画给|画来|开画|画好|"
    r"等着|等会|等下|稍等|马上来|来了|这就来|安排|上图|出图中|画着呢|正在画|等我|马上|"
    r"好了给你|行，等|行等|瞧好|看好了|拿去|收好|给你看|来一张|来张)"
)
# 模型明确拒绝画（傲娇拒绝时别硬塞图）
REFUSE_RE = re.compile(r"(不画|不给画|不想画|画不了|不会画|拒绝画|才不画|没法画|不能画)")
# 模型在反问「画什么」——它在等用户补充细节，这时别自作主张出图
ASK_BACK_RE = re.compile(
    r"(画什么|画啥|画个啥|想画|要画什么|画成什么|什么样的|啥样的|"
    r"具体点|说清楚|描述一下)[?？]?"
)
# 从请求里剥掉的祈使词，剩下的才是画面主体。
# 动词表要与 DRAW_INTENT_RE 的动词表保持同步，否则「发个照片」会剥成「发照片」，
# 被当成画面主体拿去生图。
STRIP_WORDS_RE = re.compile(
    r"(帮我|给我|替我|麻烦|请|来|画|绘|生成|做|整|搞|弄|拍|发|甩|扔|放|晒|给|你|"
    r"一张|一个|一幅|一副|"
    r"张|个|幅|副|下|吧|呗|啊|呀|吗|么|的话|谢谢|快|再|然后|现在|马上|去|点|些)"
)
# 量词（判断剥完名字后还剩不剩真正的内容时用）
QUANTIFIER_RE = re.compile(r"[一二三两四五六七八九十]?\s*(只|条|头|位|个|张|幅|副|群|窝|堆)")
# 纯粹的「图片」类词，本身不构成画面主体。
# 「发个照片」剥完只剩「照片」，说明用户没指定画什么 —— 那就画机器人自己。
BARE_PIC_RE = re.compile(
    r"(图片|照片|图|照|自拍|头像|壁纸|表情包|动图|动态图|看看|"
    r"长什么样|长啥样|什么样子|样子)"
)


def _clean_leaked_markup(text: str, event=None) -> tuple[str, str | None]:
    """清掉模型泄漏的伪工具调用标记。

    Returns:
        (清理后的文本, 从标记里抠出的 prompt 或 None)
    """
    # 清理认全部六个工具名（见 _strip_all_leaks 的说明）：
    # 实测「画一张小鲸鱼」时模型泄漏的是 generate_image(prompt=没有引号的中文，
    # 一直到结尾都没有右括号 —— 老正则只认 XML 和 ```json 两种形态，整条漏掉。
    # 走接力：别的插件可能已经把标记清掉了，原文在 event.extra 里
    if event is not None:
        return _leak_relay(event, text, "generate_image", "prompt")
    cleaned = _strip_all_leaks(text)
    return cleaned, _extract_arg(text, "generate_image", "prompt")


def _derive_prompt(user_text: str, assistant_text: str) -> str:
    """从用户请求里推出画面描述（模型没给 prompt 时用）。"""
    req = (user_text or "").strip()
    # 自称类请求：画你自己 / 自拍
    if SELF_REF_RE.search(req):
        if re.search(r"(自拍|你的|你自己|你本人)", req):
            return SELF_PORTRAIT

        # 「大肥鱼」既可能是在叫人，也可能就是要画的东西：
        #   「大肥鱼给我生成某个群友」-> 叫人，主体是某个群友
        #   「给我生成一条大肥鱼」    -> 主体就是大肥鱼本人
        # 判据是把名字和祈使词、量词都剥掉之后还剩不剩内容。
        stripped = re.sub(r"(大肥鱼|小鲸鱼)", "", req)
        residue = STRIP_WORDS_RE.sub("", stripped)
        residue = QUANTIFIER_RE.sub("", residue)
        residue = residue.strip(" ，,。.!！?？~、的")
        if len(residue) < 2:
            return SELF_PORTRAIT
        req = stripped

    subject = STRIP_WORDS_RE.sub("", req).strip(" ，,。.!！?？~、")

    # 顺序要紧：**先**判断整句是不是只剩「图/照片/头像」这类词，说明用户没指定
    # 画什么，是在跟机器人本人要照片 —— 画小鲸鱼自己最贴切。
    # 判据是「剥完一个字都不剩」而不是「不足两个字」：「猫」只有一个字，
    # 但它是完全合法的画面主体。
    # 实测踩坑：这一步原先放在剥量词之后，「帮我画个头像」剥成「头像」再被
    # 开头量词规则切成「像」，BARE_PIC_RE 认不出「像」，于是拿一个「像」字去生图。
    if subject and not BARE_PIC_RE.sub("", subject).strip(" ，,。.!！?？~、的"):
        return SELF_PORTRAIT

    # 剥掉开头残留的量词：「画只猫」剥掉「画」后剩「只猫」，主体其实是「猫」。
    subject = re.sub(r"^[一二三两四五六七八九十]?\s*(只|条|头|位|群|窝|堆|把|件)", "", subject)
    subject = subject.strip(" ，,。.!！?？~、")

    # 剥完量词又可能露出图片类词（「画个头照」之类），再判一次。
    if subject and not BARE_PIC_RE.sub("", subject).strip(" ，,。.!！?？~、的"):
        return SELF_PORTRAIT
    if subject:
        return subject

    # 用户原话里挖不出主体（例如只说了「画点」），退而从模型回复里找：
    # 模型常会复述「画个某个群友是吧？」，那个宾语正是要画的东西。
    m = re.search(
        r"(?:画|绘|生成)\s*(?:一)?\s*(?:张|个|幅|副|下|点|只|条|头|位)?\s*"
        r"([^，。！？\n、的是吧呢啊呀吗么]{2,20})",
        assistant_text or "",
    )
    if m:
        cand = m.group(1).strip()
        if len(cand) >= 2:
            return cand

    return SELF_PORTRAIT


# ---------------------------------------------------------------- 底层调用


async def _request_once(session: aiohttp.ClientSession, model: str, prompt: str):
    """请求一次生图接口，返回 (图片本地路径, 错误信息)，成功时错误信息为 None。"""
    url = f"{API_BASE.rstrip('/')}/images/generations"
    full_prompt = f"{prompt.strip()}, {STYLE_SUFFIX}" if STYLE_SUFFIX else prompt
    payload = {"model": model, "prompt": full_prompt, "n": 1, "size": SIZE}
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    async with session.post(url, json=payload, headers=headers) as resp:
        text = await resp.text()
        if resp.status != 200:
            return None, f"HTTP {resp.status}: {text[:300]}"
        try:
            data = await resp.json(content_type=None)
        except Exception:
            return None, f"返回不是合法 JSON: {text[:300]}"

    if data.get("error"):
        return None, str(data["error"])[:300]
    items = data.get("data") or []
    if not items:
        return None, f"接口未返回图片: {text[:200]}"

    item = items[0]
    os.makedirs(SAVE_DIR, exist_ok=True)
    stamp = f"{int(time.time() * 1000)}"
    path = os.path.join(SAVE_DIR, f"img_{stamp}.png")

    b64 = item.get("b64_json") or ""
    if b64:
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return path, None

    img_url = item.get("url") or ""
    if not img_url:
        return None, "接口返回项既无 url 也无 b64_json"

    # 下载到本地再发，避免 NapCat 直接取远程 URL 失败/超时
    async with session.get(img_url) as r2:
        if r2.status != 200:
            return None, f"下载图片失败 HTTP {r2.status}"
        with open(path, "wb") as f:
            f.write(await r2.read())
    return path, None


async def _generate(prompt: str):
    """按主模型 + 备用模型顺序尝试生图。返回 (path, err)。"""
    if not API_KEY:
        return None, "未配置生图 API Key（环境变量 DSH_IMG_API_KEY）"

    models = [MODEL] + [m for m in FALLBACK_MODELS if m != MODEL]
    errors = []
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    async with _sem:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for model in models:
                try:
                    path, err = await _request_once(session, model, prompt)
                except TimeoutError:
                    err = f"{model} 请求超时（>{TIMEOUT}s）"
                    path = None
                except Exception as e:  # 网络异常等
                    err = f"{model} 请求异常: {e}"
                    path = None
                if path:
                    if model != MODEL:
                        logger.warning("[imagegen] 主模型不可用，已降级到 %s", model)
                    return path, None
                errors.append(f"{model} -> {err}")
                logger.warning("[imagegen] %s 生图失败: %s", model, err)
    return None, "；".join(errors)


def _cooldown_left(key: str) -> int:
    last = _last_call.get(key, 0.0)
    left = COOLDOWN - int(time.time() - last)
    return left if left > 0 else 0


# ---------------------------------------------------------------- 插件主体


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[imagegen] 已加载：base=%s model=%s fallback=%s auto=%s style=%s...",
            API_BASE,
            MODEL,
            FALLBACK_MODELS,
            AUTO_FALLBACK,
            STYLE_SUFFIX[:40],
        )

    # ------------------------------------------------ 路径 1：规范工具调用

    @filter.llm_tool(name="generate_image")
    async def generate_image(self, event: AstrMessageEvent, prompt: str):
        """画图、生成图片、出图、做壁纸/头像/表情包/插画、拍照片时调用本工具，生成并发送图片。只要用户想要一张图就调用它。

        Args:
            prompt(string): 图片内容的详细描述，写清主体、动作、表情、场景，越具体越好
        """
        sid = event.unified_msg_origin or "global"
        left = _cooldown_left(sid)
        if left > 0:
            return f"生图冷却中，还需等待 {left} 秒，请告诉用户稍后再试。"

        _last_call[sid] = time.time()
        event.set_extra("imagegen_done", True)  # 告诉兜底钩子别重复画
        logger.info("[imagegen] 工具调用生图: %s", prompt[:120])

        path, err = await _generate(prompt)
        if not path:
            _last_call[sid] = 0.0  # 失败不占用冷却
            return f"生图失败：{err}。请把失败原因简短告诉用户，不要重复尝试。"

        await event.send(MessageChain(chain=[Image.fromFileSystem(path)]))
        # 图片已经单独发出，返回值只用于让模型说一句话，别再描述图片内容
        return "图片已经生成并发送给用户了。请只用一句简短的话回应，不要描述图片细节。"

    # ------------------------------------------------ 路径 2：兜底钩子

    @filter.on_llm_response()
    async def auto_draw(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        """模型只是嘴上答应画图 / 把工具调用吐成了文本时，插件自己把图画出来。"""
        try:
            text = response.completion_text or ""
            cleaned, leaked_prompt = _clean_leaked_markup(text, event)

            # 无条件回写清理后的文字：泄漏的 XML 绝不能进群
            if cleaned != text:
                try:
                    response.completion_text = cleaned
                except Exception:
                    response._completion_text = cleaned
                logger.warning("[imagegen] 已清理模型泄漏的伪工具调用标记")

            if not AUTO_FALLBACK:
                return
            # 工具已经正常跑过了，别画第二张
            if event.get_extra("imagegen_done"):
                return
            if "generate_image" in (response.tools_call_name or []):
                return

            user_text = event.message_str or ""

            # 让路给 dsh-video / dsh-voice：用户点名要视频或语音，且句里
            # 没有明确的图片名词时，本插件一概不出手。放在最前面，
            # 强信号、弱信号、「模型自称在画」三条路一起否决。
            if OTHER_MEDIA_RE.search(user_text) and not EXPLICIT_IMG_RE.search(user_text):
                logger.info(
                    "[imagegen] 让路：用户要的是视频/语音而不是图 | 用户=%.40s", user_text
                )
                return

            strong = bool(DRAW_INTENT_RE.search(user_text))
            promised = bool(PROMISE_RE.search(cleaned))
            refused = bool(REFUSE_RE.search(cleaned))
            asked_back = bool(ASK_BACK_RE.search(cleaned))
            # 模型回复里自己说在画什么，是理解成画图请求的可靠旁证
            assist_draw = bool(ASSIST_DRAW_RE.search(cleaned))
            # 弱信号：「生成某个群友」「弄个猫娘」这类没有「图」字的说法。
            #
            # 取舍：与其枚举「画得出来的东西」（无穷），不如排除「明显画不出的」
            # （表格/投票/报表/饭…，有限且稳定）。宾语不在黑名单里就认作要图。
            # 代价是遇到没列举的非画面宾语会误发一张图；但漏检更恼人——用户
            # 明确要图却毫无反应，比多发一张图糟糕得多。黑名单可随时补。
            non_visual = bool(NON_VISUAL_RE.search(user_text))
            weak = not non_visual and (
                bool(WEAK_DRAW_RE.search(user_text))
                or bool(WEAK_LOOSE_RE.search(user_text))
            )
            has_intent = strong or weak

            # 触发条件。核心判断很简单：
            #   **用户明确要图 + 模型没拒绝 + 模型不是在反问细节 => 就该出图。**
            #
            # 早先版本还要求模型「说了答应的话」或「回复短于 30 字」，
            # 结果一句 32 字的「行，画给你看！蓝的，胖的，闭着眼翻白眼那种！」
            # 就因为超一个字而漏掉——用户明明看到机器人答应了却等不到图。
            # 模型答应的说法千变万化，靠枚举关键词或掐字数都不可靠；
            # 用户的请求意图才是真正该看的东西，模型回复只用来否决
            #（明确拒绝 / 反问画什么）。
            if leaked_prompt:
                prompt = leaked_prompt
                reason = "模型泄漏 prompt"
            elif has_intent and not refused and not asked_back:
                prompt = _derive_prompt(user_text, cleaned)
                bits = []
                if not strong:
                    bits.append("弱信号")
                if promised:
                    bits.append("模型答应")
                if assist_draw:
                    bits.append("模型提到画")
                reason = "用户要图" + ("（" + "、".join(bits) + "）" if bits else "")
            elif assist_draw and promised and not refused and not asked_back:
                # 用户原话没被认出，但模型自己说在画 X —— 采信模型的理解
                prompt = _derive_prompt(user_text, cleaned)
                reason = "模型自称在画"
            else:
                # 不触发也留个痕：只要出现了任一信号却没出图，就记一条，
                # 否则下次「有时候不生图」还得靠猜。日志量可控——
                # 完全无关的闲聊不会命中任何信号，压根不会走到这里。
                if strong or weak or assist_draw or promised:
                    logger.info(
                        "[imagegen] 未触发出图：强=%s 弱=%s 模型提到画=%s 答应=%s"
                        " 拒绝=%s 反问细节=%s 非画面宾语=%s | 用户=%.40s | 回复=%.40s",
                        strong, weak, assist_draw, promised, refused,
                        asked_back, non_visual, user_text, cleaned,
                    )
                return

            sid = event.unified_msg_origin or "global"
            if _cooldown_left(sid) > 0:
                logger.info("[imagegen] 兜底触发但在冷却中，跳过")
                return
            _last_call[sid] = time.time()

            # 留个记号：这一轮确实产出了慢媒体。
            # dsh-mention 靠这个决定要不要 @（生图 5~15s，回来时人早翻页了）。
            # 工具路径和 /画图 路径本来就会置这个 extra，兜底路径漏了，
            # 于是「模型嘴上答应、插件自己动手」这条最常见的路径反而 @ 不上。
            event.set_extra("imagegen_done", True)

            logger.info("[imagegen] 兜底出图（%s）: %s", reason, prompt[:120])
            path, err = await _generate(prompt)
            if not path:
                _last_call[sid] = 0.0
                logger.error("[imagegen] 兜底出图失败: %s", err)
                return
            await event.send(MessageChain(chain=[Image.fromFileSystem(path)]))
            logger.info("[imagegen] 兜底出图已发送: %s", path)
        except BaseException as e:
            logger.error("[imagegen] 兜底钩子异常: %s", e)

    # ------------------------------------------------ 路径 3：显式指令

    @filter.command("画图")
    async def cmd_draw(self, event: AstrMessageEvent):
        """/画图 <描述> —— 不经过模型直接生图（兜底入口）。"""
        text = (event.message_str or "").strip()
        for p in ("画图", "/画图"):
            if text.startswith(p):
                text = text[len(p) :].strip()
                break
        if not text:
            yield event.plain_result("用法：/画图 一只戴墨镜的橘猫")
            return

        sid = event.unified_msg_origin or "global"
        left = _cooldown_left(sid)
        if left > 0:
            yield event.plain_result(f"慢点，还有 {left} 秒冷却")
            return
        _last_call[sid] = time.time()
        event.set_extra("imagegen_done", True)

        yield event.plain_result("在画了，稍等十几秒…")
        path, err = await _generate(text)
        if not path:
            _last_call[sid] = 0.0
            logger.error("[imagegen] /画图 失败: %s", err)
            yield event.plain_result(f"画失败了：{err[:200]}")
            return
        yield event.chain_result([Image.fromFileSystem(path)])

    @filter.command("画图状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """/画图状态 —— 查看生图渠道配置与连通性。"""
        ok_key = "已配置" if API_KEY else "未配置"
        yield event.plain_result(
            f"生图渠道：{API_BASE}\n主模型：{MODEL}\n备用：{', '.join(FALLBACK_MODELS) or '无'}\n"
            f"尺寸：{SIZE}｜Key：{ok_key}｜并发：{MAX_CONCURRENCY}｜冷却：{COOLDOWN}s\n"
            f"兜底出图：{'开' if AUTO_FALLBACK else '关'}\n画风：{STYLE_SUFFIX[:60]}…"
        )
