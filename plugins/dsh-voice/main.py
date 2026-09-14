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
#
# 2026-09-12 群动态记分卡（/opt/qqbot/observe/dynamics.py，15 分钟一轮）抓到两类
# 反复出现的故障，这里一并修掉：
#   P6 审核误杀：8h 内 9 次语音被挡。三个独立成因——
#      (a) 结论精确比较 raw == "可"：「可。」「"可"」「可以」全被当成拒绝；
#      (b) CENSOR_TIMEOUT=10s 对思考模型（gpt-5.6-sol）太短，超时即 fail-closed，
#          把「现在还属于内测版，我还没有正式做完呢」这种完全无害的话挡了；
#      (c) 提示词没写「玩梗不算」，「先V我50解锁转账功能」被判成诈骗。
#      修法：容错解析（先判否再判可，避免「不可以」被「可」抢先命中）+ 超时
#      提到 20s 并在「没解析出结论/超时」时重试一次 + 提示词显式白名单玩梗与引用。
#      有害内容仍是一次就拦、不重试；两次都拿不到结论才 fail-closed。
#   P7 机械提示刷屏：16:46~16:50 两个群友轮流敲 /说话，机器人往群里丢了 12 条
#      「慢点，还有 N 秒冷却」。改为按群 90s 去重（NOTICE_GAP），窗口内静默。
#      同时把「发不出声音：审核通道超时」这类内部原因从模型口播素材里摘掉——
#      审核失败只给人一句自然话，技术原因留在日志。
# 回归：test_censor.py（27 项断言，须在容器里跑）。

import asyncio
import json
import os
import random
import re
import time

import aiohttp
import edge_tts
import ormsgpack

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record
from astrbot.api.provider import LLMResponse
from astrbot.core import logger
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_type import MessageType

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
# fish: Fish Audio 的 /tts + msgpack；wusound: 悟声 simple-generate + JSON。
BACKEND = os.environ.get("DSH_VOICE_BACKEND", "auto").strip().lower()
if BACKEND == "auto":
    BACKEND = "wusound" if "wusound.cn" in API_BASE.lower() else "fish"
WUSOUND_TTS_URL = os.environ.get(
    "DSH_VOICE_TTS_URL", f"{API_BASE}/tts/simple-generate"
).strip()
MODEL = os.environ.get("DSH_VOICE_MODEL", "s2.1-pro-free")
# 主模型不可用时按顺序降级（实测这三个都能出声）
FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get("DSH_VOICE_FALLBACK", "s2.1-pro,s1").split(",")
    if m.strip()
]
# 音色。Fish 使用 32 位十六进制 ID；悟声使用 UUID。
REFERENCE_ID = os.environ.get(
    "DSH_VOICE_REF", "626bb6d3f3364c9cbc3aa6a67300a664"
)
WUSOUND_PROMPT_ID = os.environ.get("DSH_VOICE_PROMPT_ID", "default").strip()
# wav：NapCat 转 silk 无需二次转码；mp3 体积小但框架会再转一次 wav。
AUDIO_FORMAT = os.environ.get("DSH_VOICE_FORMAT", "wav")
# 单条语音最长字数。超了截断——群里没人听长语音，而且 Fish 是按字数线性耗时。
MAX_CHARS = int(os.environ.get("DSH_VOICE_MAX_CHARS", "120"))
TIMEOUT = int(os.environ.get("DSH_VOICE_TIMEOUT", "60"))
# Fish 域名在部分大陆出口会完成 TCP 但卡死 TLS。保留 Fish 主链，同时提供
# 无密钥的 Edge ReadAloud 独立兜底；它只在 Fish 三档全部失败后使用。
EDGE_FALLBACK = os.environ.get("DSH_VOICE_EDGE_FALLBACK", "1") not in (
    "0", "false", "False"
)
EDGE_VOICE = os.environ.get(
    "DSH_VOICE_EDGE_VOICE", "zh-CN-XiaoxiaoNeural"
)
# 同一会话冷却，防止刷语音
COOLDOWN = int(os.environ.get("DSH_VOICE_COOLDOWN", "20"))
MAX_CONCURRENCY = int(os.environ.get("DSH_VOICE_CONCURRENCY", "1"))
# 机械提示（"慢点，还有 N 秒冷却" / "这次语音没确认发出去"）的按群最小间隔。
# 2026-09-12 观察窗实测：16:46~16:50 两个群友轮流敲 /说话，机器人往群里丢了
# 12 条「慢点，还有 N 秒冷却」，被记分卡 P7 抓成刷屏。群聊里没人需要看倒计时。
# 保留第一条（告诉用户"有冷却"这件事），窗口内的后续尝试只写日志不再发群。
NOTICE_GAP = int(os.environ.get("DSH_VOICE_NOTICE_GAP", "90"))
# 兜底钩子总开关
AUTO_FALLBACK = os.environ.get("DSH_VOICE_AUTO", "1") not in ("0", "false", "False")
TMP_DIR = os.environ.get("DSH_VOICE_TMP", "/AstrBot/data/voice")

# ---- 语音内容审核（防淫秽/违法内容被念出来）----
# 总开关：0 关闭审核（不推荐）。默认开。
CENSOR = os.environ.get("DSH_VOICE_CENSOR", "1") not in ("0", "false", "False")
# LLM 语义审核单次超时。默认 20s：审核走的是会话主 provider（gpt-5.6-sol 这类
# 思考模型），实测 10s 会偶发超时并 fail-closed 拒发（2026-09-12 观察窗 8h 内 2 次
# 「审核通道超时」，把两句完全无害的话挡了下来）。宁可多等几秒，也别白挡。
# 超时后仍会重试一次（见 _censor_text），总最坏耗时 = 2×CENSOR_TIMEOUT。
CENSOR_TIMEOUT = int(os.environ.get("DSH_VOICE_CENSOR_TIMEOUT", "20"))
# 审核用哪个 provider；留空 = 当前会话的主 provider
CENSOR_PROVIDER = os.environ.get("DSH_VOICE_CENSOR_PROVIDER", "")

# 情绪驱动的主动语音：不要求用户先点“发语音”。只在机器人已经正常生成回复后，
# 若该群当前主情绪达到阈值，就把这次回复改为语音表达。沿用同一个情绪状态文件，
# 避免 emotion/voice 两边各维护一套状态机。默认只收高唤醒情绪，并设长冷却与概率闸。
EMOTION_AUTO = os.environ.get("DSH_VOICE_EMOTION_AUTO", "1") not in ("0", "false", "False")
EMOTION_STATE_PATH = os.environ.get(
    "DSH_VOICE_EMOTION_STATE", os.environ.get("DSH_EMOTION_STATE", "/AstrBot/data/dsh_emotion_state.json")
)
EMOTION_THRESHOLD = max(1, min(3, int(os.environ.get("DSH_VOICE_EMOTION_THRESHOLD", "3"))))
EMOTION_NAMES = {
    x.strip() for x in os.environ.get(
        "DSH_VOICE_EMOTION_NAMES", "angry,sad,excited,surprised,worried"
    ).split(",") if x.strip()
}
# [patch:emotion-tier] 分情绪阈值。
#
# 为什么单一阈值必然是坏的：dsh-emotion 的强度生成只有一条路能到 3 ——
# intensity=3 专属于 angry（见 dsh-emotion _predict 里 `if emotion == "angry":
# intensity = 3`），其余情绪一律被钉死 2（directed）或 ENV_INTENSITY（默认 2）。
# 于是 DSH_VOICE_EMOTION_THRESHOLD=3 等于「只有发火才配出声」：2026-09-12~14
# 三天全量日志里情绪主动语音只成功 3 次，且全是 angry(3)，happy/curious/
# awkward/proud 一次都没轮到过。用户的体感「从没见他主动发过语音」就是这么来的。
#
# 修法不是去改 dsh-emotion 的强度语义（那会连带影响全链路的情绪注入强度），
# 而是在语音侧承认「每种情绪的可达强度上限不同」，各给一个门槛：
#   angry 能到 3，要 3 表示"只有真发火才念"；
#   directed 类情绪上限就是 2，门槛给 2 才可能触发；
#   环境氛围类上限 ENV_INTENSITY(2)，同样给 2。
# 格式 "angry:3,happy:2"；未列出的情绪回落到 DSH_VOICE_EMOTION_THRESHOLD。
EMOTION_THRESHOLD_BY_NAME: dict[str, int] = {}
for _item in os.environ.get("DSH_VOICE_EMOTION_THRESHOLD_BY_NAME", "angry:3,happy:2,curious:2,awkward:2,excited:2,sad:2,surprised:2,worried:2,proud:2").split(","):
    _item = _item.strip()
    if not _item or ":" not in _item:
        continue
    _name, _, _val = _item.partition(":")
    try:
        EMOTION_THRESHOLD_BY_NAME[_name.strip()] = max(1, min(3, int(_val)))
    except ValueError:
        continue
# [patch:emotion-tier] 主动语音的独立频率上限（次/小时）。
# 原先情绪主动语音只有 EMOTION_COOLDOWN(1800s) 一道 30 分钟冷却，没有小时配额：
# 门槛一旦放宽（上面那条），密集对话里会出现"每半小时准点念一句"的机械感。
# 上限按群计数，放在内存即可 —— 重启清零只会让上限更宽松一点点，不会更严。
EMOTION_MAX_PER_HOUR = max(1, int(os.environ.get("DSH_VOICE_EMOTION_MAX_PER_HOUR", "3")))
_emotion_hits: dict[str, list[float]] = {}
EMOTION_COOLDOWN = max(60, int(os.environ.get("DSH_VOICE_EMOTION_COOLDOWN", "1800")))
EMOTION_RATE = max(0.0, min(1.0, float(os.environ.get("DSH_VOICE_EMOTION_RATE", "0.65"))))
EMOTION_GROUPS = {
    x.strip() for x in os.environ.get("DSH_VOICE_EMOTION_GROUPS", "").split(",") if x.strip()
}

_sem = asyncio.Semaphore(MAX_CONCURRENCY)
# session -> 上次成功发语音的时间
_last_call: dict[str, float] = {}
# session -> 上次向群里发「机械提示」（冷却/发送失败话术）的时间
_notice_last: dict[str, float] = {}
_emotion_last: dict[str, float] = {}
_emotion_inflight: set[str] = set()
# 运行期可切换的音色（/音色 指令），None 表示用 REFERENCE_ID
_runtime_ref: dict[str, str] = {}

REF_RE = re.compile(r"^[a-fA-F0-9]{32}$")
WUSOUND_REF_RE = re.compile(
    r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$"
)


def _valid_ref(ref: str) -> bool:
    return bool((WUSOUND_REF_RE if BACKEND == "wusound" else REF_RE).match(ref or ""))

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

_COOL_LAST = ""
# 冷却提示的说法。为什么不再用「慢点，还有 N 秒冷却」——
# ①「N 秒冷却」是纯机器词，真人不会说"冷却"；
# ②这是机器人最常重复的一句真文本：全量日志 114 次，2026-09-12 一天 34 次，
#    等于每天当着全群念几十遍"我是程序"；
# ③倒计时其实没必要，群友只需要知道"现在不行、等会儿"，不需要精确秒数；
# ④必须给变体：只换成另一句固定的话，过两天它自己就成了新的口头禅。
_COOL_POOL = (
    "急啥，一个一个来",
    "别催，让我缓口气",
    "刚念完，等一下嘛",
    "马上，这就好",
    "来了来了",
    "稍等会儿",
)


def _cool_line() -> str:
    """冷却时挑一句人话，且不跟上一句重复。"""
    global _COOL_LAST
    cand = [x for x in _COOL_POOL if x != _COOL_LAST] or list(_COOL_POOL)
    pick = random.choice(cand)
    _COOL_LAST = pick
    return pick


def _notice_allowed(sid: str, now: float | None = None) -> bool:
    """同一群 NOTICE_GAP 秒内最多发一条机械提示，返回是否该发。

    只给「冷却倒计时」「发送失败」这类没有信息量的提示用；正常聊天回复不走这里。
    """
    ts = time.time() if now is None else now
    if NOTICE_GAP > 0 and ts - _notice_last.get(sid, 0.0) < NOTICE_GAP:
        return False
    _notice_last[sid] = ts
    return True


def _ref_for(sid: str) -> str:
    return _runtime_ref.get(sid) or REFERENCE_ID


def _read_emotion(gid: str, now: float | None = None) -> tuple[str, int, str]:
    """读取 dsh-emotion 已落盘的单一主情绪；坏文件/过期状态一律视为平静。"""
    try:
        with open(EMOTION_STATE_PATH, encoding="utf-8") as handle:
            states = json.load(handle)
        state = states.get(str(gid)) if isinstance(states, dict) else None
        if not isinstance(state, dict):
            return "calm", 0, "missing"
        emotion = str(state.get("emotion") or "calm")
        intensity = max(0, min(3, int(state.get("intensity") or 0)))
        expires = float(state.get("expires_at") or 0.0)
        ts = time.time() if now is None else now
        if emotion == "calm" or (expires and ts >= expires):
            return "calm", 0, "expired" if expires else "calm"
        return emotion, intensity, "active"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return "calm", 0, "unreadable"


def should_emotion_voice(gid: str, sid: str, text: str, now: float | None = None,
                         roll: float | None = None) -> tuple[bool, str]:
    """情绪主动语音总闸门（除状态读取外无副作用），便于离线测试。"""
    if not EMOTION_AUTO:
        return False, "关闭"
    if EMOTION_GROUPS and str(gid) not in EMOTION_GROUPS:
        return False, "群未启用"
    clean = _clean_for_tts(text)
    if len(clean) < 2:
        return False, "文本太短"
    emotion, intensity, status = _read_emotion(gid, now)
    if status != "active" or emotion not in EMOTION_NAMES:
        return False, "情绪不匹配"
    # [patch:emotion-tier] 用该情绪自己的可达上限当门槛，而不是全局 3。
    need = EMOTION_THRESHOLD_BY_NAME.get(emotion, EMOTION_THRESHOLD)
    if intensity < need:
        return False, "强度不足"
    ts = time.time() if now is None else now
    if ts - _emotion_last.get(sid, 0.0) < EMOTION_COOLDOWN:
        return False, "冷却中"
    # [patch:emotion-tier] 小时配额：滑动窗口，只留最近一小时的触发时间。
    hits = _emotion_hits.setdefault(sid, [])
    hits[:] = [t for t in hits if ts - t < 3600]
    if len(hits) >= EMOTION_MAX_PER_HOUR:
        return False, "小时配额满"
    chance = random.random() if roll is None else roll
    if chance >= EMOTION_RATE:
        return False, "概率未中"
    return True, "%s(%d)" % (emotion, intensity)


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
# 设施类拒绝原因：只说给日志听，不给模型当口播素材（见 _send_voice）
_INFRA_REASONS = (
    "审核通道超时", "审核通道异常", "审核通道不可用", "审核未给出结论",
)
# [patch:receipt-silent] QQ NT 内核等发送回执超时（ActionFailed retcode=1200）的
# 专用哨兵。它**不是失败**：语音多半已经送达，只是内核没等到回执。所有调用方
# 见到它都必须当作「已发出、不必再说任何话」，绝不能拿去生成群里的口播 ——
# 真人不会汇报自己发没发出去，而且这句话在多数情况下是假的。
RECEIPT_TIMEOUT = "__receipt_timeout__"
# [patch:receipt-silent] 可以直接念给群里听的失败原因。不在名单里的一律换成
# 通用短句 —— 「未配置 DSH_VOICE_API_KEY」「Edge 返回空音频」「未知错误」
# 这种内部词不该出现在群里。
_HUMAN_REASONS = frozenset({
    "这句先没念出来，换个说法或者稍后再试",
    BANNED_HINT,
    "清洗后文本太短，没什么可念的",
    "这次声音没送出去，等一下再试试",
})
GENERIC_FAIL = "这次声音没送出去，等一下再试试"


def _human_err(err: str) -> str:
    """把内部失败原因翻译成一句群里能看的话。"""
    e = (err or "").strip()
    if e in _HUMAN_REASONS:
        return e
    logger.info("[voice] 内部失败原因不外发，只进日志：%s", e[:160])
    return GENERIC_FAIL

CENSOR_SYS = (
    "你是内容审查员。用户会给你一段文本，请判断这段文本是否适合由"
    "一个 QQ 群的语音机器人念出来播给全群听。"
    "以下内容的文本**不适合**念：色情淫秽、性行为/性器官描写、性暗示撩拨、"
    "**呻吟与淫叫类拟声**（如啊啊啊、嗯嗯嗯、鹅鹅鹅、哦哦哦、嗯~啊~、"
    "唔…哼…这类重复呻吟式象声词，无论是否夹杂标点）、"
    "毒品、枪支武器、恐怖暴力、见到血/死/伤的具体渲染、诈骗赌博、"
    "教唆犯罪、极端仇视与人身攻击。"
    "**以上是最容易误判的地方，请特别注意**：群友之间开玩笑、玩梗、网络流行语"
    "（例如「V我50」「薅羊毛」「白嫖」「打工人」「我裂开了」「无能的丈夫」）、"
    "复述或引用别人的话、自嘲、夸张吐槽、聊钱和游戏，都属于正常的群聊玩闹，"
    "**适合**念，不要当成诈骗、性暗示或暴力来拦。"
    "只有在文本**确实在描写或教唆**上述有害内容时才判「不适合」。"
    '请只回复一个字：如果适合就回 "可"，不适合就回 "否"。'
    "不要解释，不要输出任何其他内容。"
)

# 审核结论的容错解析：模型（尤其是思考模型）经常把「可」写成「可。」「"可"」
# 「可以」「适合」，原先的 raw == "可" 精确比较会把这些一律判成拒绝。
# 2026-09-12 观察窗抓到 8h 内 9 次语音被挡，其中就有这类误杀。
_CENSOR_NO = re.compile(r"否|不适|不宜|不合适|不可以|不能|禁止|违规")
_CENSOR_YES = re.compile(r"^\s*[\"'「『(（\[【]?\s*(可|可以|适合|没问题|OK|ok|Ok)")


def _parse_censor_verdict(raw: str) -> bool | None:
    """把模型输出解析成 True(可)/False(否)/None(没看懂)。先否后可是刻意的：
    「不可以」里同时含「可」，反过来的顺序会把它误判成放行。"""
    s = (raw or "").strip()
    if not s:
        return None
    if _CENSOR_NO.search(s[:12]):
        return False
    if _CENSOR_YES.match(s):
        return True
    return None


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
    # 语义审核：走主 provider 问一次（超时/未解析出结论时重试一次）。
    # 拿不到结论（两次都不行/异常/通道缺失）一律拒绝。
    pid = CENSOR_PROVIDER or ""
    if not pid:
        try:
            pid = await context.get_current_chat_provider_id(
                event.unified_msg_origin or ""
            )
        except BaseException as e:  # noqa: BLE001
            logger.warning("[voice] 取审核 provider 异常：%s，保守拒发", e)
            return False, "审核通道异常"
    if not pid:
        logger.warning("[voice] 审核 provider 缺失，保守拒发")
        return False, "审核通道不可用"

    last = "审核通道不可用"
    for attempt in (1, 2):
        try:
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
            verdict = _parse_censor_verdict(raw)
            if verdict is True:
                return True, ""
            if verdict is False:
                logger.info("[voice] 语义审核拦截：%r -> %.30s", text[:40], raw[:30])
                return False, BANNED_HINT
            # 没解析出结论：多半是思考模型把答案写进了 reasoning 或答非所问。
            # 这是可重试的，不要当成「不适合念」。
            last = "审核未给出结论"
            logger.warning("[voice] 审核结论无法解析（第%d次）：%r -> %.40s",
                           attempt, text[:30], raw[:40])
        except asyncio.TimeoutError:
            last = "审核通道超时"
            logger.warning("[voice] 审核超时（%ds，第%d次）", CENSOR_TIMEOUT, attempt)
        except BaseException as e:  # noqa: BLE001
            last = "审核通道异常"
            logger.warning("[voice] 审核异常（第%d次）：%s", attempt, e)
    logger.warning("[voice] 审核两次都没拿到结论，保守拒发：%.40s", text)
    return False, last


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


async def _wusound_tts_once(session, text: str, ref: str) -> tuple[bytes | None, str]:
    """悟声 simple-generate 先返回音频 URL，再下载真实 MP3。"""
    body = {"text": text, "voiceId": ref}
    if WUSOUND_PROMPT_ID:
        body["promptId"] = WUSOUND_PROMPT_ID
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "X-Vocu-App-Lang": "zh-CN",
        "Content-Type": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT)
        async with session.post(
            WUSOUND_TTS_URL, json=body, headers=headers, timeout=timeout
        ) as resp:
            detail = await resp.text()
            if resp.status != 200:
                return None, f"HTTP {resp.status} {detail[:300]}"
            try:
                payload = json.loads(detail)
            except json.JSONDecodeError:
                return None, "悟声返回了无法解析的 JSON"
            if payload.get("status") != 200:
                return None, str(payload.get("message") or payload)[:300]
            audio_url = str((payload.get("data") or {}).get("audio") or "")
            if not audio_url.startswith("https://"):
                return None, "悟声响应缺少 HTTPS 音频地址"
        async with session.get(audio_url, timeout=timeout) as audio_resp:
            audio = await audio_resp.read()
            if audio_resp.status == 200 and len(audio) > 256:
                return audio, ""
            return None, f"下载音频 HTTP {audio_resp.status}，{len(audio)}B"
    except asyncio.TimeoutError:
        return None, f"超时（>{TIMEOUT}s）"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


async def _edge_tts_once(text: str) -> tuple[bytes | None, str]:
    """调用 Edge ReadAloud WebSocket 作为独立网络/服务兜底。"""
    path = os.path.join(TMP_DIR, f"edge_{int(time.time() * 1000)}.mp3")
    try:
        await asyncio.wait_for(
            edge_tts.Communicate(text, EDGE_VOICE).save(path),
            timeout=min(TIMEOUT, 30),
        )
        with open(path, "rb") as f:
            audio = f.read()
        return (audio, "") if len(audio) > 256 else (None, "Edge 返回空音频")
    except asyncio.TimeoutError:
        return None, "Edge 超时（>30s）"
    except Exception as e:  # noqa: BLE001
        return None, f"Edge {type(e).__name__}: {e}"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def _synthesize(text: str, ref: str) -> tuple[str | None, str]:
    """合成语音，返回 (文件路径, 错误)；服务失败后才走 Edge。"""
    if not API_KEY:
        return None, "未配置 DSH_VOICE_API_KEY"
    if not _valid_ref(ref):
        kind = "UUID" if BACKEND == "wusound" else "32 位十六进制"
        return None, f"音色 ID 非法（需 {kind}）：{ref!r}"

    os.makedirs(TMP_DIR, exist_ok=True)
    last_err = ""
    async with _sem:
        async with aiohttp.ClientSession() as session:
            if BACKEND == "wusound":
                t0 = time.time()
                audio, err = await _wusound_tts_once(session, text, ref)
                if audio:
                    path = os.path.join(TMP_DIR, f"tts_{int(time.time() * 1000)}.mp3")
                    with open(path, "wb") as f:
                        f.write(audio)
                    logger.info(
                        "[voice] 悟声合成成功 voice=%s %d字 %dB %.1fs -> %s",
                        ref[:8], len(text), len(audio), time.time() - t0, path,
                    )
                    return path, ""
                last_err = err
                logger.warning("[voice] 悟声失败：%s", err)
            else:
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
            if EDGE_FALLBACK:
                t0 = time.time()
                audio, err = await _edge_tts_once(text)
                if audio:
                    path = os.path.join(
                        TMP_DIR, f"tts_{int(time.time() * 1000)}.mp3"
                    )
                    with open(path, "wb") as f:
                        f.write(audio)
                    logger.info(
                        "[voice] 主服务全挂后 Edge 合成成功 voice=%s %d字 %dB %.1fs -> %s",
                        EDGE_VOICE, len(text), len(audio), time.time() - t0, path,
                    )
                    return path, ""
                last_err = err
                logger.warning("[voice] Edge 兜底失败：%s", err)
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
            "[voice] 已加载：backend=%s base=%s model=%s fallback=%s ref=%s fmt=%s 上限%d字 冷却%ds 兜底=%s 情绪主动=%s/%s≥%d/%ds",
            BACKEND, API_BASE, MODEL, FALLBACK_MODELS, REFERENCE_ID[:8] + "…",
            AUDIO_FORMAT, MAX_CHARS, COOLDOWN, AUTO_FALLBACK,
            EMOTION_AUTO, sorted(EMOTION_NAMES), EMOTION_THRESHOLD, EMOTION_COOLDOWN,
        )
        # [patch:emotion-tier] 分情绪门槛与小时配额单独打一行：出问题时
        # 「为什么某情绪从不触发」一眼能看出是门槛没配还是链路没跑。
        logger.info(
            "[voice] 情绪门槛(分情绪)：%s ｜ 小时上限=%d 次 ｜ 概率=%.2f",
            ",".join("%s≥%d" % (k, EMOTION_THRESHOLD_BY_NAME[k]) for k in sorted(EMOTION_THRESHOLD_BY_NAME)),
            EMOTION_MAX_PER_HOUR, EMOTION_RATE,
        )

    async def _send_voice(self, event: AstrMessageEvent, text: str) -> tuple[bool, str]:
        """洗文字 -> 审核 -> 合成 -> 单独发语音；平台失败永不冒泡到 AstrBot。"""
        clean = _truncate(_clean_for_tts(text))
        if len(clean) < 2:
            return False, "清洗后文本太短，没什么可念的"
        # 内容审核闸门：黑名单 + LLM 语义，fail-closed。
        ok_c, reason = await _censor_text(self.context, event, clean)
        if not ok_c:
            logger.info("[voice] 审核未通过不发语音（%s）：%.50s", reason, clean)
            # 「审核通道超时/不可用」是设施故障，不是内容问题：这是给人看的
            # 反向说明，绝不能原样交给模型变成群里的口播（历史上出过
            # 「发不出声音：审核通道超时」这种把内部机制播出去的句子）。
            if reason in _INFRA_REASONS:
                return False, "这句先没念出来，换个说法或者稍后再试"
            return False, reason
        path, err = await _synthesize(clean, _ref_for(event.unified_msg_origin or "global"))
        if not path:
            return False, err
        try:
            await event.send(MessageChain(chain=[Record.fromFileSystem(path)]))
        except BaseException as exc:
            # NapCat retcode=1200 是 QQ NT 内核等待发送回执超时。此时服务端不能
            # 确认消息究竟失败还是迟到成功；自动重发可能在群里形成双语音，所以
            # 只转成普通失败结果，不重试，也不让框架生成带堆栈的 ":(" 报错。
            detail = str(exc).strip().replace("\n", " ")
            lower = detail.lower()
            if "retcode=1200" in lower or "nodeikernelmsgservice/sendmsg" in lower:
                # [patch:receipt-silent] 回执超时**按已送达处理，不往群里播报**。
                # 实测：群里刚听到语音，紧接着就蹦出一句
                # 「这次语音没确认发出去：QQ 发送回执超时，可能已经送达；为避免
                # 重复语音未自动重发」—— 群友看到的是机器人在念自己的故障码，
                # 而且它说的多半是假的（回执超时通常只是内核没等到回执，语音已经
                # 送出去了）。真人不会汇报自己发没发出去。
                # 和下面 _INFRA_REASONS 那条注释是同一个原则：机制绝不能变成口播。
                logger.warning(
                    "[voice] QQ 发送回执超时，按已送达处理（不重发、不播报）：%s",
                    detail[:300],
                )
                return False, RECEIPT_TIMEOUT
            logger.warning("[voice] QQ 语音发送失败(%s)：%s", type(exc).__name__, detail[:300])
            return False, "这次声音没送出去，等一下再试试"
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
        if not ok and err == RECEIPT_TIMEOUT:
            # [patch:receipt-silent] 回执超时按**已送达**处理。语音多半已经进群了，
            # 这时候让模型去说「没发出去」就是在群里报假警。
            logger.info("[voice] 工具调用：回执超时，按已送达收口")
            return "语音已经发出去了。请只回一句很短的话，不要重复语音里的内容。"
        if not ok:
            _last_call[sid] = 0.0  # 失败不占冷却
            logger.error("[voice] 工具调用失败：%s", err)
            # 不要把 err 原文交给模型当口播素材：早前模型照抄成了
            # 「发不出声音：审核通道超时」「发不出声音：这句话不太适合念出来」，
            # 把内部实现和审核机制直接播到群里。这里只给一句自然口吻的要求。
            return (
                f"本次语音没发出去（内部原因：{err}）。请用一句自然口语告诉用户"
                "这一句没念出来、让他换个说法或稍后再说；"
                "不要提到审核、超时、通道、provider 这类技术词，也不要重试。"
            )
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
            explicit_voice = bool(leaked_text or VOICE_INTENT_RE.search(user_text))
            emotional_voice = False
            emotional_reason = ""
            sid = event.unified_msg_origin or "global"
            gid = str(event.get_group_id() or "")
            if not explicit_voice and event.get_message_type() == MessageType.GROUP_MESSAGE:
                emotional_voice, emotional_reason = should_emotion_voice(
                    gid, sid, cleaned_reply
                )
            if not explicit_voice and not emotional_voice:
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

            if _cooldown_left(sid) > 0:
                logger.info("[voice] 兜底/情绪触发但在通用冷却中，跳过")
                return
            _last_call[sid] = time.time()
            if emotional_voice:
                if sid in _emotion_inflight:
                    return
                _emotion_inflight.add(sid)
                _emotion_last[sid] = time.time()
                # [patch:emotion-tier] 记进小时窗口（总闸门里查的是这份）
                _emotion_hits.setdefault(sid, []).append(time.time())

            # 留个记号：这一轮确实发了语音。dsh-mention 靠它决定要不要 @。
            # 工具路径和 /说话 路径已经置了，兜底路径原先漏了。
            event.set_extra("voice_done", True)

            logger.info(
                "[voice] 主动发语音（%s）：%s",
                ("情绪阈值 " + emotional_reason) if emotional_voice else
                ("抠自泄漏调用" if leaked_text else "用户明确要语音"),
                reply[:80],
            )
            ok, err = await self._send_voice(event, reply)
            if not ok:
                _last_call[sid] = 0.0
                if emotional_voice:
                    _emotion_last[sid] = 0.0
                logger.error("[voice] 兜底/情绪发语音失败：%s", err)
            if emotional_voice:
                _emotion_inflight.discard(sid)
        except BaseException as e:  # noqa: BLE001
            try:
                _emotion_inflight.discard(event.unified_msg_origin or "global")
            except Exception:
                pass
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
            # 冷却倒计时按群去重：90s 内只提示一次，其余静默。
            # 之前群友轮流敲 /说话 会刷出一串「慢点，还有 N 秒冷却」。
            if _notice_allowed(sid):
                yield event.plain_result(_cool_line())
            else:
                logger.info("[voice] 冷却提示 %.0fs 内已发过，静默忽略（剩 %ds）",
                            NOTICE_GAP, left)
            return
        _last_call[sid] = time.time()
        event.set_extra("voice_done", True)

        ok, err = await self._send_voice(event, text)
        if not ok and err == RECEIPT_TIMEOUT:
            # [patch:receipt-silent] 回执超时 = 多半已经送达，一个字都不说。
            # 实测噪声（2026-09-13 00:30:53）：群里刚听到语音，紧接着就是
            # 「这次语音没确认发出去：QQ 发送回执超时，可能已经送达；为避免重复
            # 语音未自动重发」—— 群友看到的是机器人在念自己的故障码，而且它说的
            # 大概率是假的。真人不会汇报自己发没发出去。
            logger.info("[voice] 回执超时，按已送达处理：不重发、不播报机制")
            return
        if not ok:
            _last_call[sid] = 0.0
            # 命令处理器必须自己收口发送异常；否则框架会把插件异常包装成
            # “:( 在调用插件…”并发进群。这里给一句稳定、可读的降级说明。
            # [patch:receipt-silent] 不再套「这次语音没确认发出去：」这个前缀 ——
            # err 本身已经是给人看的一句话，前缀一加就变成机制播报。
            if _notice_allowed(sid):
                yield event.plain_result(_human_err(err)[:160])
            else:
                logger.info("[voice] 失败提示 %.0fs 内已发过，静默（%s）",
                            NOTICE_GAP, err[:80])

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
                f"后端：{BACKEND}｜当前音色 ID：{_ref_for(sid)}\n"
                + (f"模型：{MODEL}（备用 {', '.join(FALLBACK_MODELS) or '无'}）\n" if BACKEND == "fish" else "")
                + "换音色：/音色 <名称>　或　/音色 <音色ID>"
            )
            return

        if _valid_ref(arg):
            _runtime_ref[sid] = arg
            yield event.plain_result(f"音色已切到 {arg}（重启后恢复默认）")
            return

        if BACKEND == "wusound":
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        f"{API_BASE}/voice",
                        params={"show": "full", "showMarket": "true"},
                        headers={
                            "Authorization": f"Bearer {API_KEY}",
                            "X-Vocu-App-Lang": "zh-CN",
                        },
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        data = await resp.json()
            except Exception as e:  # noqa: BLE001
                yield event.plain_result(f"搜音色失败：{type(e).__name__}")
                return
            items = [
                i for i in (data.get("data") or [])
                if arg.lower() in str(i.get("name") or "").lower()
                or arg.lower() in str(i.get("description") or "").lower()
            ]
            if not items:
                yield event.plain_result(f"没搜到叫「{arg}」的可用音色")
                return
            _runtime_ref[sid] = items[0]["id"]
            lines = [f"音色已切到：{items[0]['name']}（{items[0]['id']}，重启后恢复默认）"]
            if len(items) > 1:
                lines.append("其他候选：")
                lines += [f"· {i['name']}　{i['id']}" for i in items[1:4]]
            yield event.plain_result("\n".join(lines))
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
            f"后端：{BACKEND}"
            + (f"｜主模型：{MODEL}｜备用：{', '.join(FALLBACK_MODELS) or '无'}" if BACKEND == "fish" else "")
            + f"\n音色：{_ref_for(sid)}\n"
            f"格式：{AUDIO_FORMAT}｜字数上限：{MAX_CHARS}｜超时：{TIMEOUT}s\n"
            f"冷却：{COOLDOWN}s（剩 {_cooldown_left(sid)}s）｜并发：{MAX_CONCURRENCY}\n"
            f"Key：{'已配置' if API_KEY else '未配置'}｜兜底：{'开' if AUTO_FALLBACK else '关'}\n"
            f"临时文件：{n} 个"
        )
