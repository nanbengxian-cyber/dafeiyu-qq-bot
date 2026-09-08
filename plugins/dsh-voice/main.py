# dsh-voice —— QQ 群聊「小鲸鱼」语音（TTS）插件，接 Fish Audio S2.1 Pro。
#
# 为什么不用 AstrBot 内置的 provider_tts_settings：
#   内置实现是「全局开关 + 概率」，开了以后**每条** LLM 回复都可能被整条转成
#   语音（result_decorate/stage.py 里对 chain 中每个 Plain 调 get_audio）。
#   小鲸鱼的人设是「群里打字的真人」，把所有文字都念出来会立刻出戏；而且内置
#   适配器把 format 硬编码成 wav、timeout 默认 20s，实测 240 字要 16s 才返回，
#   稍长就超时。所以改为插件按需触发，自己控制格式、超时、长度、冷却。
#
# 三条触发路径，从强到弱（和 dsh-imagegen 同一套形状，便于排查）：
#   1) LLM 函数工具 send_voice —— 模型规范调用（最理想）。
#   2) on_llm_response 兜底钩子 —— 用户明确要语音、模型却没调工具时，插件把
#      模型这次的回复文字直接念出来。便宜模型经常「嘴上答应、不调工具」，
#      dsh-imagegen 已经在生图上踩过同样的坑，这里同样必须兜底。
#   3) 显式指令 /说话 <文本> —— 完全绕过模型。
#
# 踩过/规避的坑：
#   - Fish Audio 的 model 必须走 HTTP header（不是请求体字段），body 是
#     ormsgpack 打包的 ServeTTSRequest。付费 speech-1.6 会 402（本 key 余额 0），
#     免费/旧档 s2.1-pro-free / s2.1-pro / s1 可用，默认用 s2.1-pro-free。
#   - reference_id 必须是 32 位十六进制，否则 Fish 直接报错。
#   - 语音必须单独 event.send 一条消息，不能塞进 result_chain：AstrBot 的
#     completion_text 在设置 result_chain 后由 get_plain_text() 派生，
#     「文字+语音」混排会把语音丢掉（dsh-sticker / dsh-imagegen 已验证）。
#   - QQ 语音会被 NapCat 转成 silk，越长越慢，且群里没人愿意听 30 秒语音。
#     所以硬性截断到 MAX_CHARS，按句末标点找断点。
#   - 念之前要把贴纸标记 [贴纸:x]、@ 昵称、URL、markdown 符号清掉，
#     否则 TTS 会把「中括号贴纸冒号送花」这种东西一个字一个字念出来。

import asyncio
import os
import random
import re
import time

import aiohttp
import ormsgpack

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record
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

API_BASE = os.environ.get("DSH_VOICE_API_BASE", "https://api.fish.audio/v1").rstrip("/")
API_KEY = os.environ.get("DSH_VOICE_API_KEY", "")
MODEL = os.environ.get("DSH_VOICE_MODEL", "s2.1-pro-free")
# 主模型不可用时按顺序降级（实测这三个都能出声）
FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get("DSH_VOICE_FALLBACK", "s2.1-pro,s1").split(",")
    if m.strip()
]
# 音色。默认「可莉」——活泼、偏少女、任务量 6000+ 的高质量中文音色。
REFERENCE_ID = os.environ.get(
    "DSH_VOICE_REF", "626bb6d3f3364c9cbc3aa6a67300a664"
)
# wav：NapCat 转 silk 无需二次转码；mp3 体积小但框架会再转一次 wav。
AUDIO_FORMAT = os.environ.get("DSH_VOICE_FORMAT", "wav")
# 单条语音最长字数。超了截断——群里没人听长语音，而且 Fish 是按字数线性耗时。
MAX_CHARS = int(os.environ.get("DSH_VOICE_MAX_CHARS", "120"))
TIMEOUT = int(os.environ.get("DSH_VOICE_TIMEOUT", "60"))
# 同一会话冷却，防止刷语音
COOLDOWN = int(os.environ.get("DSH_VOICE_COOLDOWN", "20"))
MAX_CONCURRENCY = int(os.environ.get("DSH_VOICE_CONCURRENCY", "1"))
# 兜底钩子总开关
AUTO_FALLBACK = os.environ.get("DSH_VOICE_AUTO", "1") not in ("0", "false", "False")
TMP_DIR = os.environ.get("DSH_VOICE_TMP", "/AstrBot/data/voice")

# ---- 语音内容审核（防淫秽/违法内容被念出来）----
# 总开关：0 关闭审核（不推荐）。默认开。
CENSOR = os.environ.get("DSH_VOICE_CENSOR", "1") not in ("0", "false", "False")
# LLM 语义审核单次超时
CENSOR_TIMEOUT = int(os.environ.get("DSH_VOICE_CENSOR_TIMEOUT", "10"))
# 审核用哪个 provider；留空 = 当前会话的主 provider
CENSOR_PROVIDER = os.environ.get("DSH_VOICE_CENSOR_PROVIDER", "")

_sem = asyncio.Semaphore(MAX_CONCURRENCY)
# session -> 上次成功发语音的时间
_last_call: dict[str, float] = {}
# 运行期可切换的音色（/音色 指令），None 表示用 REFERENCE_ID
_runtime_ref: dict[str, str] = {}

REF_RE = re.compile(r"^[a-fA-F0-9]{32}$")

# ---------------------------------------------------------------- 意图识别
#
# 形状照搬 dsh-imagegen 的教训：用户请求是主证据，模型回复只用来否决。
# 不掐字数、不枚举「模型答应的说法」。

# 强信号：明确提到语音/说话方式
VOICE_INTENT_RE = re.compile(
    r"(用|发|来|给我|说)?\s*(语音|声音|音频|嗓子|嗓音)\s*(说|讲|回|回复|发|念|读|聊|来)"
    # 念/读/唱 是出声动作，但后面必须真的跟量词或听觉宾语。
    # 末尾放可选组（?）等于没约束：「读书破万卷」「念念不忘」「唱衰」
    # 都会被空匹配吃掉 —— 实测踩过，所以这里全部写成必需组。
    r"|(念|读|唱)\s*(一|两|几|首|支)\s*(句|段|下|遍|首|个)?"
    r"|(念|读|唱)\s*(句|段|遍|首)"
    r"|(念|读|唱)\s*(给我听|来听|出来|给你听)"
    r"|唱\s*(首|一首|个)?\s*歌"
    # 说/讲 打字也叫说 ——「随便说句话」「讲两句」实测被误判过。
    # 语音是打扰性输出，宁可漏不可扰：必须有明确的听觉线索。
    r"|(说|讲)\s*(一|两|几)?\s*(句|段|下|遍)?\s*(话)?\s*(给我听|给我念|念给我听)"
    r"|语音\s*(回复|回|说|发|一下|条|消息)"
    r"|(发|来|整|搞|给)\s*(个|条|段|句)?\s*语音"
    # ---- 「要语音」的统一形状：给予/使用动词 + 可选修饰 + 语音类名词 ----
    # 这里原先是三条近似重复的分支（各带略有差异的动词表/量词表/名词表），
    # 交叉组合必然漏 —— 笛卡尔积测试一次挖出上千个「谁都不接」的组合：
    #   「来点声音」缺 用/拿；「发一个声音」缺「一」；「使用一个音频」缺 音频。
    # 每补一次就露出下一批，所以合并成一条结构化判据。
    # 名词组与 imagegen 的 OTHER_MEDIA_RE 对齐，动词表覆盖「给予」和「使用」两类。
    # 不加 ^ 锚定，所以「能不能发一段语音」「帮我用你的声音」这些前缀天然支持。
    # 否定前视：「别用语音」「不用语音」「少用语音」「没声音」都不算在要语音；
    # 「这视频没声音」「调大点声音」的动词（没/调）本来就不在表里。
    r"|(?<![别不少没])(发|来|整|搞|给|弄|放|用|拿|使用)\s*"
    r"(我|你|你的|自己的)?\s*(一|两)?\s*(个|段|条|句|点|些)?\s*"
    r"(声音|音频|语音|嗓子|嗓音)"
    # 「录个音」「帮我录段音」。量词**必需** —— 可选的话「这段录音不错」
    # 「我在听他的录音」这种把「录音」当名词的说法也会命中（实测误触发过）。
    # 代价是「帮我录音」会漏，可接受：语音是打扰性输出，宁可漏不可扰。
    r"|录\s*(一|两)?\s*(个|段|条)\s*音(?![笔机棚室频])"
    r"|念\s*(一|给)"
    r"|读\s*出来"
    r"|听\s*(听)?\s*你\s*(的)?\s*(声音|嗓)"
    r"|你\s*(的)?\s*声音\s*(是什么样|什么样|好听)"
    r"|voice|tts"
)
# 模型明确拒绝——尊重它，不硬发
REFUSE_RE = re.compile(
    r"(不能|不会|无法|没法|做不到|不支持|办不到)\s*(发|说|讲|念|读|出|语音|声音)"
    r"|我\s*(还)?\s*(不能|不会|没法|无法)\s*(说话|发语音|出声)"
)
# 模型在反问「念什么」——别抢答
ASK_BACK_RE = re.compile(r"(念|说|读|讲)\s*(点|些|什么|啥)|想听\s*(什么|啥)|要我\s*说\s*(什么|啥)")

# ---------------------------------------------------------------- 文本清洗

STICKER_RE = re.compile(r"[\[【]\s*(?:贴纸|貼紙|sticker)\s*[:：]\s*[^\]】]*[\]】]")

# ---- 模型把工具调用当文本吐出来的各种形态 ----
#
# 这不是假想的：端到端实测里 deepseek-v4-flash-0731 回了
#   喏，听好了哦。\n\nsend_voice(text="哼，既然你这么想听…")
# tool_calls 是空的，函数调用被当普通文本写了出来。dsh-imagegen 在生图上踩过
# 一模一样的坑（XML 形态），语音这里是 Python 调用形态。两件事都得做：
#   ① 把这段标记从进群的文字里无条件清掉；
#   ② 把 text= 里的内容抠出来当真正要念的话——那才是模型想说的。
#
# 形态一：send_voice(text="…") / send_voice(text='…')
LEAK_CALL_RE = re.compile(
    r"\bsend_voice\s*\(\s*text\s*=\s*(?P<q>[\"\'])(?P<v>.*?)(?<!\\)(?P=q)\s*\)",
    re.S,
)
# 形态二：<invoke name="send_voice"><parameter name="text">…</parameter>
LEAK_XML_RE = re.compile(
    r"<\s*(?:antml:)?function_calls\s*>.*?(?:</\s*(?:antml:)?function_calls\s*>|$)"
    r"|<\s*(?:antml:)?invoke\b[^>]*send_voice.*?(?:</\s*(?:antml:)?invoke\s*>|$)",
    re.S | re.I,
)
LEAK_XML_TEXT_RE = re.compile(
    r"<\s*(?:antml:)?parameter\s+name\s*=\s*[\"\']text[\"\']\s*>(.*?)"
    r"(?:</\s*(?:antml:)?parameter\s*>|$)",
    re.S | re.I,
)
# 形态三：```json {"name":"send_voice","arguments":{"text":"…"}} ```
LEAK_JSON_RE = re.compile(
    r"```(?:json|tool_code)?\s*\{[^`]*send_voice[^`]*\}\s*```", re.S | re.I
)
LEAK_JSON_TEXT_RE = re.compile(r"[\"\']text[\"\']\s*:\s*[\"\'](.+?)[\"\']", re.S)
# 残留的孤立标签碎片
LEAK_FRAG_RE = re.compile(
    r"</?\s*(?:antml:)?(?:function_calls|invoke|parameter)\b[^>]*>?", re.I
)


def _clean_leaked_call(text: str, event=None) -> tuple[str, str | None]:
    """清掉泄漏的伪工具调用，并抠出它想念的话。

    Returns:
        (清理后的文本, 抠出的 text 参数或 None)
    """
    leaked = None
    m = LEAK_CALL_RE.search(text or "")
    if m:
        leaked = m.group("v").strip()
    if not leaked:
        block = LEAK_XML_RE.search(text or "")
        if block:
            m2 = LEAK_XML_TEXT_RE.search(block.group(0))
            if m2:
                leaked = m2.group(1).strip()
    if not leaked:
        block = LEAK_JSON_RE.search(text or "")
        if block:
            m3 = LEAK_JSON_TEXT_RE.search(block.group(0))
            if m3:
                leaked = m3.group(1).strip()

    # 清理认全部六个工具名：别的插件的泄漏标记同样不能进群，
    # 而谁的 on_llm_response 先跑是不确定的，所以每个插件都清干净（幂等）。
    if event is not None:
        cleaned, relay = _leak_relay(event, text or "", "send_voice", "text")
        return cleaned, (leaked or relay or None)
    cleaned = _strip_all_leaks(text or "")
    if not leaked:
        leaked = _extract_arg(text or "", "send_voice", "text")
    return cleaned, (leaked or None)
AT_RE = re.compile(r"@[^\s，。！？,.!?]{1,20}\s?")
URL_RE = re.compile(r"https?://\S+")
# 泄漏的伪工具调用块（便宜模型的老毛病）
INVOKE_RE = re.compile(r"<\s*(?:antml:)?invoke\b.*?(?:</\s*(?:antml:)?invoke\s*>|$)", re.S | re.I)
MD_RE = re.compile(r"[*_`~#>|]+")
EMOJI_RE = re.compile(
    "[" "\U0001f300-\U0001faff" "\U00002600-\U000027bf" "\U0001f000-\U0001f2ff" "]+"
)
SPACE_RE = re.compile(r"[ \t\u3000]+")
# 句末标点，用来找截断点
SENT_END = "。！？!?；;…\n"


def _clean_for_tts(text: str) -> str:
    """把回复文字洗成「能念出来」的样子。"""
    t = text or ""
    t, _ = _clean_leaked_call(t)
    t = INVOKE_RE.sub("", t)
    t = STICKER_RE.sub("", t)
    t = URL_RE.sub("", t)
    t = AT_RE.sub("", t)
    t = MD_RE.sub("", t)
    t = EMOJI_RE.sub("", t)
    t = SPACE_RE.sub(" ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def _truncate(text: str, limit: int = MAX_CHARS) -> str:
    """按句末标点截断到 limit 字以内；找不到断点就硬截。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    for i in range(len(head) - 1, max(0, limit // 3), -1):
        if head[i] in SENT_END:
            return head[: i + 1]
    for i in range(len(head) - 1, max(0, limit // 3), -1):
        if head[i] in "，,、 ":
            return head[: i + 1]
    return head


def _cooldown_left(sid: str) -> int:
    left = COOLDOWN - int(time.time() - _last_call.get(sid, 0.0))
    return left if left > 0 else 0


def _ref_for(sid: str) -> str:
    return _runtime_ref.get(sid) or REFERENCE_ID


# ---------------------------------------------------------------- 内容审核
#
# 语音是「别人点一下播放、全群都听见」的输出，比文字更容易出事：
# 模型被带偏念出淫秽/违法内容时，文字还能后悔，语音已经播出去了。
# 三道闸，全部放在 _send_voice 入口（三条路径都汇到那里）：
#   ① 提示词层：send_voice 工具描述里写明内容红线（见工具 docstring），
#      让模型生成时就不写这类话 —— 这是成本最低、最该起作用的闸；
#   ② 本地词表 + 简单拟声正则（零网络，快拦）：兜住 AI 通道抖动/模型踩线；
#   ③ LLM 语义审核（fail-closed，主力兜底）：让主模型判断「这段话适不
#      适合机器人当众念出来」，网络/超时等任何拿不到「可」的情况一律不发。
# 宁可少发，不可错发。

# 简单词表（完整句子的语义兜底交给 LLM 审核，这里只快拦最露骨的）
BANNED_WORDS = [
    # 淫秽/色情
    "鸡巴", "阴道", "阴蒂", "阴唇", "阴茎", "阳具", "龟头", "屄",
    "口交", "肛交", "乳交", "性交", "做爱", "自慰", "手淫", "撸管",
    "打飞机", "打炮", "约炮", "嫖", "卖淫", "援交", "强奸", "轮奸",
    "迷奸", "奸杀", "群交", "淫乱", "淫荡", "浪叫", "叫床", "裸聊",
    "祼聊", "色情直播", "福利姬", "操你", "肏", "干死你", "操死",
    # 毒品
    "冰毒", "海洛因", "摇头丸", "大麻", "K粉", "k粉", "可卡因", "鸦片",
    "吸毒", "贩毒", "制毒", "毒品",
    # 恐怖/暴力/犯罪
    "炸弹", "炸死", "杀人", "谋杀", "分尸", "碎尸", "活埋", "绑架",
    "撕票", "枪杀", "枪决", "恐怖袭击", "灭门", "屠城", "虐杀", "肢解",
    "爆炸物", "引爆炸", "刺杀", "暗杀",
    # 诈骗/赌博/非法
    "洗钱", "传销", "赌博平台", "澳门赌场", "网赌", "刷单", "裸贷", "校园贷", "套路贷",
    # 极端仇视/敏感
    "去死", "滚出中国", "支那", "精日", "卖国贼",
]
# 淫叫拟声（用户点名：啊啊啊/嗯嗯嗯/鹅鹅鹅/哦哦哦，以及符号间隔的
# 啊啊啊！啊！啊！、啊...啊...啊、嗯～啊～ 等任何变体）。
# 策略：先把文本里所有非汉字剥掉，再查「连续同字≥3 / 连续混合≥4」——
# 任何标点/符号/emoji/空格都被剥掉，谁也躲不过；
# 另加一条拖音正则兜住「啊~」「嗯…」「啊!!」这种单字+符号堆叠。
_MOAN_CHARS = "啊嗯哦噢唔呜呃哼恩噫鹅呀哟喔哎诶唷嘤吁"
_CJK_HAN = re.compile(r"[^\u4e00-\u9fff]")  # 只留汉字
_MOAN_SAME_RE = re.compile(r"([%s])\1{2,}" % _MOAN_CHARS)  # 同字连3+：啊啊啊/鹅鹅鹅
_MOAN_MIX_RE = re.compile(r"[%s]{4,}" % _MOAN_CHARS)  # 混合连4+：嗯啊嗯啊/啊啊哦哦（剥符号后才见真章）
# 单字+拖音符号：波浪/省略号1个就算（啊~ 嗯…），感叹/问号≥2才算（啊!! 嗯？？）
_MOAN_DRAW_RE = re.compile(
    r"[%s][~～…．.。]{1,3}|[%s][!！?？]{2,4}" % (_MOAN_CHARS, _MOAN_CHARS)
)
BANNED_HINT = "这句话不太适合念出来，换一句嘛"

CENSOR_SYS = (
    "你是内容审查员。用户会给你一段文本，请判断这段文本是否适合由"
    "一个 QQ 群的语音机器人念出来播给全群听。"
    "以下内容的文本**不适合**念：色情淫秽、性行为/性器官描写、性暗示撩拨、"
    "**呻吟与淫叫类拟声**（如啊啊啊、嗯嗯嗯、鹅鹅鹅、哦哦哦、嗯~啊~、"
    "唔…哼…这类重复呻吟式象声词，无论是否夹杂标点）、"
    "毒品、枪支武器、恐怖暴力、见到血/死/伤的具体渲染、诈骗赌博、"
    "教唆犯罪、极端仇视与人身攻击。"
    '请只回复一个字：如果合适就回 "可"，不合适就回 "否"。'
    "不要解释，不要输出任何其他内容。"
)


def _banned_quick(text: str) -> bool:
    """本地快拦：词表命中或拟声命中。零网络，AI 审核的前置闸。"""
    t = text or ""
    for w in BANNED_WORDS:
        if w in t:
            return True
    # 剥掉所有非汉字后再查连读：任何符号间隔都绕不过
    core = _CJK_HAN.sub("", t)
    if _MOAN_SAME_RE.search(core) or _MOAN_MIX_RE.search(core):
        return True
    if _MOAN_DRAW_RE.search(t):
        return True
    return False


async def _censor_text(
    context, event: AstrMessageEvent, text: str
) -> tuple[bool, str]:
    """审核一段待念文本。返回 (放行?, 原因)。fail-closed：拿不到「可」就不发。"""
    if not CENSOR:
        return True, ""
    if _banned_quick(text or ""):
        logger.info("[voice] 本地快拦：%.40s", text)
        return False, BANNED_HINT
    # 语义审核：走主 provider 问一次。拿不到结论（超时/异常/通道缺失）一律拒绝。
    try:
        pid = CENSOR_PROVIDER or ""
        if not pid:
            pid = await context.get_current_chat_provider_id(
                event.unified_msg_origin or ""
            )
        if not pid:
            logger.warning("[voice] 审核 provider 缺失，保守拒发")
            return False, "审核通道不可用"
        resp = await asyncio.wait_for(
            context.llm_generate(
                chat_provider_id=pid,
                prompt=text[:MAX_CHARS] or "",
                system_prompt=CENSOR_SYS,
                temperature=0,
            ),
            timeout=CENSOR_TIMEOUT,
        )
        raw = (getattr(resp, "completion_text", "") or "").strip()
        if raw == "可":
            return True, ""
        logger.info("[voice] 语义审核拦截：%r -> %.30s", text[:40], raw[:30])
        return False, BANNED_HINT
    except asyncio.TimeoutError:
        logger.warning("[voice] 审核超时（%ds），保守拒发", CENSOR_TIMEOUT)
        return False, "审核通道超时"
    except BaseException as e:  # noqa: BLE001
        logger.warning("[voice] 审核异常：%s，保守拒发", e)
        return False, "审核通道异常"


# ---------------------------------------------------------------- Fish Audio 调用


async def _tts_once(session, text: str, model: str, ref: str) -> tuple[bytes | None, str]:
    body = {
        "text": text,
        "chunk_length": 200,
        "format": AUDIO_FORMAT,
        "mp3_bitrate": 128,
        "references": [],
        "reference_id": ref,
        "normalize": True,
        "latency": "normal",
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "model": model,
        "content-type": "application/msgpack",
    }
    try:
        async with session.post(
            f"{API_BASE}/tts",
            data=ormsgpack.packb(body),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        ) as resp:
            ctype = resp.headers.get("content-type", "")
            if resp.status == 200 and ctype.startswith("audio/"):
                return await resp.read(), ""
            detail = (await resp.text())[:300]
            return None, f"HTTP {resp.status} {detail}"
    except asyncio.TimeoutError:
        return None, f"超时（>{TIMEOUT}s）"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


async def _synthesize(text: str, ref: str) -> tuple[str | None, str]:
    """合成语音，返回 (文件路径, 错误)。主模型失败自动降级。"""
    if not API_KEY:
        return None, "未配置 DSH_VOICE_API_KEY"
    if not REF_RE.match(ref or ""):
        return None, f"音色 ID 非法（需 32 位十六进制）：{ref!r}"

    os.makedirs(TMP_DIR, exist_ok=True)
    last_err = ""
    async with _sem:
        async with aiohttp.ClientSession() as session:
            for model in [MODEL, *FALLBACK_MODELS]:
                t0 = time.time()
                audio, err = await _tts_once(session, text, model, ref)
                if audio:
                    ext = "mp3" if AUDIO_FORMAT == "mp3" else AUDIO_FORMAT
                    path = os.path.join(TMP_DIR, f"tts_{int(time.time() * 1000)}.{ext}")
                    with open(path, "wb") as f:
                        f.write(audio)
                    logger.info(
                        "[voice] 合成成功 model=%s %d字 %dB %.1fs -> %s",
                        model, len(text), len(audio), time.time() - t0, path,
                    )
                    return path, ""
                last_err = err
                logger.warning("[voice] model=%s 失败：%s", model, err)
    return None, last_err or "未知错误"


async def _cleanup_old(keep_seconds: int = 1800) -> None:
    """删掉半小时前的临时语音，1.6G 小机器别攒垃圾。"""
    try:
        now = time.time()
        for name in os.listdir(TMP_DIR):
            p = os.path.join(TMP_DIR, name)
            try:
                if os.path.isfile(p) and now - os.path.getmtime(p) > keep_seconds:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


# ---------------------------------------------------------------- 插件主体


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        # 保住后台清理任务的引用。裸 asyncio.create_task 的返回值不留引用时
        # 可能被 GC 掉、任务无声消失（dsh-memory 的 _spawn 注释里记过同一个坑），
        # 表现成「旧语音文件有时候不清理」，磁盘慢慢涨且无从排查。
        self._tasks: set = set()
        logger.info(
            "[voice] 已加载：base=%s model=%s fallback=%s ref=%s fmt=%s 上限%d字 冷却%ds 兜底=%s",
            API_BASE, MODEL, FALLBACK_MODELS, REFERENCE_ID[:8] + "…",
            AUDIO_FORMAT, MAX_CHARS, COOLDOWN, AUTO_FALLBACK,
        )

    async def _send_voice(self, event: AstrMessageEvent, text: str) -> tuple[bool, str]:
        """洗文字 -> 审核 -> 合成 -> 单独发一条语音消息。返回 (成功, 错误)。"""
        clean = _truncate(_clean_for_tts(text))
        if len(clean) < 2:
            return False, "清洗后文本太短，没什么可念的"
        # 内容审核闸门：黑名单 + LLM 语义，fail-closed。
        ok_c, reason = await _censor_text(self.context, event, clean)
        if not ok_c:
            logger.info("[voice] 审核未通过不发语音（%s）：%.50s", reason, clean)
            return False, reason
        path, err = await _synthesize(clean, _ref_for(event.unified_msg_origin or "global"))
        if not path:
            return False, err
        await event.send(MessageChain(chain=[Record.fromFileSystem(path)]))
        task = asyncio.create_task(_cleanup_old())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True, ""

    # ------------------------------------------------ 路径 1：规范工具调用

    @filter.llm_tool(name="send_voice")
    async def send_voice(self, event: AstrMessageEvent, text: str):
        """用户想听你的声音、要求用语音说话/念一段话/发条语音时调用本工具，把文字合成语音发出去。

        内容红线（生成 text 时**严格禁止**）：色情淫秽、性行为/性器官描写、
        性暗示与撩骚、呻吟淫叫类拟声（啊啊啊、嗯嗯嗯、鹅鹅鹅、哦哦哦、
        嗯~啊~ 等重复呻吟式象声词，无论是否夹标点）、毒品、恐怖暴力、
        血/死/伤渲染、诈骗赌博、教唆犯罪、极端仇视与人身攻击。语音是
        给全群听的，text 必须内容健康、阳光、适合当众朗读。

        Args:
            text(string): 要念出来的话，用你自己的口吻写，不超过 100 字，不要带表情符号和网址
        """
        sid = event.unified_msg_origin or "global"
        left = _cooldown_left(sid)
        if left > 0:
            return f"语音冷却中，还需 {left} 秒。请用文字告诉用户稍后再试。"

        _last_call[sid] = time.time()
        event.set_extra("voice_done", True)  # 告诉兜底钩子别重复发
        logger.info("[voice] 工具调用：%s", text[:80])

        ok, err = await self._send_voice(event, text)
        if not ok:
            _last_call[sid] = 0.0  # 失败不占冷却
            logger.error("[voice] 工具调用失败：%s", err)
            return f"语音发送失败：{err}。请简短告诉用户发不出声音，不要重试。"
        return "语音已经发出去了。请只回一句很短的话，不要重复语音里的内容。"

    # ------------------------------------------------ 路径 2：兜底钩子

    @filter.on_llm_response()
    async def auto_voice(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        """用户明确要语音、模型却只用文字回时，插件把这次回复念出来。"""
        try:
            raw = response.completion_text or ""
            cleaned_reply, leaked_text = _clean_leaked_call(raw, event)

            # 无条件回写：泄漏的 send_voice(...) 绝不能进群。
            # 这一步必须在所有 return 之前——就算不发语音，文字也得是干净的。
            if cleaned_reply != raw:
                try:
                    response.completion_text = cleaned_reply
                except Exception:  # noqa: BLE001
                    response._completion_text = cleaned_reply
                logger.warning("[voice] 已清理模型泄漏的伪工具调用标记")

            if not AUTO_FALLBACK:
                return
            if event.get_extra("voice_done"):
                return
            if "send_voice" in (response.tools_call_name or []):
                return

            user_text = event.message_str or ""
            # 泄漏的调用本身就是「模型想发语音」的铁证，比意图正则更硬
            if not leaked_text and not VOICE_INTENT_RE.search(user_text):
                return

            # 抠出来的 text 参数才是模型真正想念的话；没有就念清理后的回复
            reply = _clean_for_tts(leaked_text or cleaned_reply)
            refused = bool(REFUSE_RE.search(reply))
            asked_back = bool(ASK_BACK_RE.search(reply))
            if refused or asked_back or len(reply) < 2:
                logger.info(
                    "[voice] 未触发语音：拒绝=%s 反问=%s 回复长度=%d 泄漏调用=%s | 用户=%.40s",
                    refused, asked_back, len(reply), bool(leaked_text), user_text,
                )
                return

            sid = event.unified_msg_origin or "global"
            if _cooldown_left(sid) > 0:
                logger.info("[voice] 兜底触发但在冷却中，跳过")
                return
            _last_call[sid] = time.time()

            # 留个记号：这一轮确实发了语音。dsh-mention 靠它决定要不要 @。
            # 工具路径和 /说话 路径已经置了，兜底路径原先漏了。
            event.set_extra("voice_done", True)

            logger.info(
                "[voice] 兜底发语音（%s）：%s",
                "抠自泄漏调用" if leaked_text else "念回复原文",
                reply[:80],
            )
            ok, err = await self._send_voice(event, reply)
            if not ok:
                _last_call[sid] = 0.0
                logger.error("[voice] 兜底发语音失败：%s", err)
        except BaseException as e:  # noqa: BLE001
            logger.error("[voice] 兜底钩子异常：%s", e)

    # ------------------------------------------------ 路径 3：显式指令

    @filter.command("说话")
    async def cmd_speak(self, event: AstrMessageEvent):
        """/说话 <文本> —— 不经过模型直接合成语音。"""
        text = (event.message_str or "").strip()
        for p in ("说话", "/说话"):
            if text.startswith(p):
                text = text[len(p) :].strip()
                break
        if not text:
            yield event.plain_result("用法：/说话 哼，才不是特意帮你呢")
            return

        sid = event.unified_msg_origin or "global"
        left = _cooldown_left(sid)
        if left > 0:
            yield event.plain_result(f"慢点，还有 {left} 秒冷却")
            return
        _last_call[sid] = time.time()
        event.set_extra("voice_done", True)

        ok, err = await self._send_voice(event, text)
        if not ok:
            _last_call[sid] = 0.0
            yield event.plain_result(f"发不出声音：{err[:200]}")

    @filter.command("音色")
    async def cmd_voice_pick(self, event: AstrMessageEvent):
        """/音色 [关键词] —— 查看当前音色，或按名字搜索并临时切换。"""
        arg = (event.message_str or "").strip()
        for p in ("音色", "/音色"):
            if arg.startswith(p):
                arg = arg[len(p) :].strip()
                break
        sid = event.unified_msg_origin or "global"

        if not arg:
            yield event.plain_result(
                f"当前音色 ID：{_ref_for(sid)}\n"
                f"模型：{MODEL}（备用 {', '.join(FALLBACK_MODELS) or '无'}）\n"
                "换音色：/音色 派蒙　或　/音色 <32位ID>"
            )
            return

        if REF_RE.match(arg):
            _runtime_ref[sid] = arg
            yield event.plain_result(f"音色已切到 {arg}（重启后恢复默认）")
            return

        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{API_BASE.replace('/v1', '')}/model",
                    params={"title": arg, "sort_by": "score", "page_size": "5"},
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    data = await resp.json()
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"搜音色失败：{type(e).__name__}")
            return

        items = [i for i in (data.get("items") or []) if "zh" in (i.get("languages") or [])]
        if not items:
            yield event.plain_result(f"没搜到叫「{arg}」的中文音色")
            return
        _runtime_ref[sid] = items[0]["_id"]
        lines = [f"音色已切到：{items[0]['title']}（{items[0]['_id']}）", "其他候选："]
        lines += [
            f"· {i['title']}　{i['_id']}　❤{i.get('like_count', 0)}" for i in items[1:4]
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("语音状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """/语音状态 —— 查看语音渠道配置。"""
        sid = event.unified_msg_origin or "global"
        n = 0
        try:
            n = len([f for f in os.listdir(TMP_DIR)])
        except OSError:
            pass
        yield event.plain_result(
            f"语音渠道：{API_BASE}\n"
            f"主模型：{MODEL}｜备用：{', '.join(FALLBACK_MODELS) or '无'}\n"
            f"音色：{_ref_for(sid)}\n"
            f"格式：{AUDIO_FORMAT}｜字数上限：{MAX_CHARS}｜超时：{TIMEOUT}s\n"
            f"冷却：{COOLDOWN}s（剩 {_cooldown_left(sid)}s）｜并发：{MAX_CONCURRENCY}\n"
            f"Key：{'已配置' if API_KEY else '未配置'}｜兜底：{'开' if AUTO_FALLBACK else '关'}\n"
            f"临时文件：{n} 个"
        )
