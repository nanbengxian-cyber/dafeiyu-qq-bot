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

# ---------------------------------------------------------------- 配置

ENABLED = os.environ.get("DSH_VOICE", "1") not in ("0", "false", "False")
# 语音接口（Fish Audio）。router 是转发/负载均衡，正式接口在后面。
API_BASE = os.environ.get(
    "DSH_VOICE_BASE", "https://api.fish.audio/v1/tts"
).rstrip("/")
API_KEY = os.environ.get("DSH_VOICE_KEY", "")
# 主模型。免费档 s2.1-pro-free / s1，付费的 speech-1.6 会 402（key 余额 0）。
# 实测 s2.1-pro 有时也 402（visual_api_key 没配），所以放着 s1 兜底。
MODEL = os.environ.get("DSH_VOICE_MODEL", "s2.1-pro-free")
FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get("DSH_VOICE_FALLBACKS", "s2.1-pro,s1").split(",")
    if m.strip()
]
# reference_id 必须 32 位十六进制。留空 = 用默认音色
TTS_REFERENCE_ID = os.environ.get("DSH_VOICE_REFERENCE_ID", "").strip()
# 输出格式：mp3；QQ 会再转一次 silk。
AUDIO_FORMAT = os.environ.get("DSH_VOICE_FORMAT", "mp3")
TIMEOUT = int(os.environ.get("DSH_VOICE_TIMEOUT", "40"))
# 一条语音最多多少字。QQ 语音转 silk 后越长越慢，25 字约 3~4s 可接受。
MAX_CHARS = int(os.environ.get("DSH_VOICE_MAX_CHARS", "80"))
# 一个会话多久内不能连发（秒）；打开发语音一次顶八十字，别让人连点
COOLDOWN = float(os.environ.get("DSH_VOICE_COOLDOWN", "60"))
# 最多同时几个语音请求
MAX_CONCURRENCY = int(os.environ.get("DSH_VOICE_CONCURRENCY", "3"))
# 兜底开关：只在用户明确要语音时才拦截
AUTO_FALLBACK = os.environ.get("DSH_VOICE_FALLBACK", "1") not in ("0", "false", "False")
# 兜底触发后，把整条回复念出来之前先加一句说明，听的人才知道这不是 bug
VOICE_NOTE_TEXT = os.environ.get("DSH_VOICE_NOTE", "（语音版：）")
# 预算：一次兜底从识别到发完最多多少秒
VOICE_BUDGET = float(os.environ.get("DSH_VOICE_BUDGET", "30"))

TMP_DIR = os.environ.get("DSH_VOICE_TMP", "/AstrBot/data/voice")

# 兜底触发的意图：用户明确要语音的内容。
# 常见说法经过层叠：说粤语/英语 → 只是语言方言，不是要语音；
# 要「音频/视频链接」→ 是链接，不是 TTS；
# 「唱首歌」→ 唱歌是内容要求，仍要 TTS 说出来（虽然不会唱，那是后话）。
VOICE_RE = re.compile(
    r"(说|念|读|讲|语音说|用语音|整段说|用嘴说|读出来|念出来|说出来|讲出来|读一下|念一下|说一下|讲一下)"
    r"\s*"
    r"(?![粤语|英语|英文|日语|日文|韩语|韩文|德语|法语|西班牙语|意大利语|俄语|外语|方言])"
    r"[^\n]{0,14}?"
    r"(一?遍|一?句|一段|一声|个)?"
    r"(语音|音频|声音|话|给我听|来听听|听听)"
    r"|(来|发|整|搞)(段|条|个)?(语音|音频|声音)"
    r"|用语音\s*",
)
# 唱 K —— 收录进来，送给 TTS 说（实测 TTS 能念出歌词，很喜感）
SING_RE = re.compile(r"(唱|来一首|来段|来句|哼|来一下)\s*(歌|小曲|几句)")
# 措辞里有「不再用语音」，或明确只要链接不要音频
VOICE_REFUSE_RE = re.compile(r"(别|不要|不用|别再|不要再用)\s*(发)?\s*语音")
# 附上 URL 的「看这个链接」：要的是链接，不是 TTS
URL_ONLY_RE = re.compile(r"(https?://|www\.|b23\.tv|BV[0-9A-Za-z]{8,})")

# 每个会话(cid)的冷却和并发
_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
_sent_at: dict[str, float] = {}
_dummy = 0.0
_stat = {"llm": 0, "hook": 0, "cmd": 0, "fail": 0, "timeout": 0, "cooldown": 0, "skip": 0}


def _cooldown_left(cid: str) -> float:
    d = _sent_at.get((cid or "") + "_m")
    if not d:
        return 0.0
    left = COOLDOWN - (time.time() - d)
    return max(0.0, left)


def _mark_sent(cid: str) -> None:
    _sent_at[(cid or "") + "_m"] = time.time()


def _cid(event) -> str:
    return (event.unified_msg_origin or "global") + "_" + str(
        getattr(event, "get_sender_id", lambda: "")() or ""
    )


def _ref_for(cid: str) -> str:
    """本轮该用的 reference_id：cmd 用 /音色 换过就以那条为准（/音色 名字带
    前缀 s_，所以和字符数无关），否则用全局默认。"""
    k = "ref_" + cid
    v = _sent_at.get(k)
    if v:
        return str(v)
    return TTS_REFERENCE_ID


def _set_ref(cid: str, rid: str) -> None:
    _sent_at["ref_" + cid] = rid


def _shorten(text: str, limit: int = MAX_CHARS) -> str:
    """按句末标点/空格截断到 limit 附近；找不到断点就硬切。"""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    head = t[:limit]
    cut = 0
    for sep in ("。", "！", "？", "…", ";", "；", "\n", " ", ",", "，"):
        j = head.rfind(sep)
        if j > limit * 0.6:
            cut = j + 1
            break
    return (head[:cut] if cut else head).strip()


def _clean_for_tts(text: str) -> str:
    """去掉不该被念出来的东西：@、贴纸标记、URL、markdown、CQ 码。"""
    t = text or ""
    t = re.sub(r"@\S+", "", t)
    t = re.sub(r"[\[【]\s*(?:贴纸|貼紙|sticker)\s*[:：][^\]】]*[\]】]", "", t)
    t = re.sub(r"\[CQ:[^\]]*\]", "", t)
    t = re.sub(r"https?://\S+|www\.\S+", "", t)
    t = re.sub(r"[`*_~>#|\-]", "", t)          # markdown 痕迹
    t = re.sub(r"\n{2,}", "\n", t)
    return (t or "").strip()


async def _tts_once(text: str, model: str, rid: str, fmt: str) -> bytes | None:
    """单次合成。失败返回 None（不区分原因，调用方统一降级）。"""
    url = f"{API_BASE}/speech"
    # model 必须走 header（cbb 字段是 body 的一部分，不是顶层参数）
    body = {
        "text": text,
        "format": fmt,
        **({"reference_id": rid} if rid else {}),
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/msgpack",
        "model": model,
    }
    payload = ormsgpack.packb(body)
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        ) as session:
            async with session.post(url, data=payload, headers=headers) as resp:
                if resp.status != 200:
                    body_txt = await resp.text()
                    logger.warning(
                        "[voice] %s HTTP %s: %s", model, resp.status, body_txt[:160]
                    )
                    return None
                return await resp.read()
    except asyncio.TimeoutError:
        logger.warning("[voice] %s 超时(%ss)", model, TIMEOUT)
        return None
    except aiohttp.ClientError as e:
        logger.warning("[voice] %s 网络错误: %s", model, e)
        return None


async def _tts(text: str, cid: str) -> tuple[bytes | None, str]:
    """主模型 + 备胎，全挂返回 None。"""
    rid = _ref_for(cid)
    models = [MODEL] + [m for m in FALLBACK_MODELS if m != MODEL]
    for m in models:
        data = await _tts_once(text, m, rid, AUDIO_FORMAT)
        if data:
            return data, m
        logger.info("[voice] %s 失败，换下一个模型", m)
    return None, ""


async def _tts_loop(text: str, cid: str, path: str) -> int:
    """合成 + 落盘。返回字节数；失败/空返回 0。"""
    data, used = await _tts(text, cid)
    if not data:
        return 0
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


async def _send_speech(event, cid: str, text: str, note: str = "", path: str = "") -> bool:
    """整个链路：截断 → 清理 → 合成 → 单独一条发出去。"""
    if len(text) > MAX_CHARS:
        text = _shorten(text)
    text = _clean_for_tts(text)
    if not text:
        return False

    if path:
        os.makedirs(TMP_DIR, exist_ok=True)
    else:
        import tempfile

        path = os.path.join(
            TMP_DIR, f"v_{int(time.time())}_{random.randint(1000, 9999)}.{AUDIO_FORMAT}"
        )
        os.makedirs(TMP_DIR, exist_ok=True)

    n = await _tts_loop(text, cid, path)
    if n == 0:
        logger.warning("[voice] 合成失败，没有可用模型")
        return False

    try:
        # 语音必须单独一条。混进正常回复会被 completion_text 的重新推导抹掉。
        if note:
            await event.send(MessageChain(chain=[Plain(note)]))
        await event.send(MessageChain(chain=[Record(file=path)]))
    except Exception as e:  # noqa: BLE001
        logger.warning("[voice] 发送失败: %s", e)
        return False
    finally:
        if not path:
            try:
                os.remove(path)
            except OSError:
                pass
    _mark_sent(cid)
    _stat[path] = 1
    return True


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self.concurrency = MAX_CONCURRENCY
        logger.info(
            "[voice] 已加载：%s 模型[%s→%s] 冷却%.0fs 上限%d字 并发%d",
            "开" if ENABLED else "关", MODEL, ",".join(FALLBACK_MODELS),
            COOLDOWN, MAX_CHARS, MAX_CONCURRENCY,
        )

    def _in_group(self, event) -> bool:
        gid = getattr(event, "get_group_id", lambda: None)()
        return gid is not None

    def _record_used(self, status, where):
        """复用 _stat 原 key 的计数：llm/hook/cmd 分别是调用方，
        真实用途由布尔开关区分开。"""
        if where == "llm":
            _stat["llm"] += 1
        elif where == "hook":
            _stat["hook"] += 1
        elif where == "cmd":
            _stat["cmd"] += 1

    @filter.llm_tool(name="send_voice")
    async def send_voice(self, event: AstrMessageEvent, text: str):
        """用语音（TTS）把一段话发到群里时调用本工具。

        Args:
            text(string): 要念出来的一句话
        """
        if not (ENABLED and API_KEY):
            return "语音没开，请告诉用户无法发送语音。"
        cid = _cid(event)
        left = _cooldown_left(cid)
        if left > 0:
            return f"语音冷却中，还要等 {int(left)} 秒，别重复调用。"
        self._record_used(0, "llm")
        try:
            async with _semaphore:
                ok = await _send_speech(
                    event, cid, text, note=VOICE_NOTE_TEXT
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("[voice] llm tool 发送失败: %s", e)
            ok = False
        if not ok:
            return "语音合成失败，请用文字回复。"
        return "语音已发送。"

    @filter.command("说话")
    async def cmd_say(self, event: AstrMessageEvent):
        """/说话 <文本> —— 直接用语音把话念出来。"""
        if not (ENABLED and API_KEY):
            yield event.plain_result("语音没开")
            return
        cid = _cid(event)
        left = _cooldown_left(cid)
        if left > 0:
            yield event.plain_result(f"冷却中，还要等 {int(left)} 秒")
            return
        arg = self._arg(event, "说话")
        if not arg:
            yield event.plain_result("用法：/说话 我想听你说句话")
            return
        self._record_used(0, "cmd")
        async with _semaphore:
            ok = await _send_speech(event, cid, arg)
        yield event.plain_result("已发送语音" if ok else "语音合成失败")

    @filter.command("音色")
    async def cmd_ref(self, event: AstrMessageEvent):
        """切换音色（reference_id）。

        用法：/音色 或 /音色 <新id>。不给参数=查看当前；给了就换，
        只对**本会话**生效（next 接 cid，不改全局）。
        全局默认值在 env DSH_VOICE_REFERENCE_ID。
        """
        cid = _cid(event)
        arg = self._arg(event, "音色")
        if not arg:
            cur = _ref_for(cid)
            yield event.plain_result(
                f"当前音色：{cur or '(默认)'}\n"
                f"全局默认：{TTS_REFERENCE_ID or '(未设置，用 Fish 默认)'}\n"
                "换音色：/音色 <32位hex reference_id>（这条只对本群生效）"
            )
            return
        arg = arg.strip()
        if len(arg) != 32 or not re.fullmatch(r"[0-9a-fA-F]{32}", arg):
            yield event.plain_result("reference_id 必须是 32 位十六进制（鱼声 API 返回的字符串）")
            return
        _set_ref(cid, arg)
        yield event.plain_result(f"音色已切换为 {arg[:8]}…（本群生效）")

    @filter.on_llm_response()
    async def voice_fallback(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        """兜底：用户明确要语音、模型只答应没调工具时，把模型的回复念出来。"""
        if not (ENABLED and API_KEY and AUTO_FALLBACK):
            return
        try:
            text = getattr(response, "completion_text", "") or ""
            if not text:
                return
            # 模型把 send_voice(...) 当正文写出来了：直接抠参数拿来兜底。
            raw_src = getattr(event, "get_extra", None) and event.get_extra("dsh_leak_raw")
            arg = None
            if raw_src:
                # 从接力原文里找 send_voice( 的 text=…
                m = re.search(
                    r"send_voice\s*\([^)]*?\btext\s*=\s*[\"\'](.+?)[\"\']",
                    raw_src, re.S,
                )
                if m:
                    arg = m.group(1)
            if not arg:
                # 没有泄漏调用可抠：按意图正则判断。
                umsg = event.get_message_str() or ""
                if VOICE_REFUSE_RE.search(umsg):
                    return
                hit = VOICE_RE.search(umsg) or SING_RE.search(umsg)
                if not hit:
                    return
                if URL_ONLY_RE.search(umsg):
                    return
            # 冷却
            cid = _cid(event)
            if _cooldown_left(cid) > 0:
                return
            # 预算：整条链路最慢 25s 合成，发送 5s
            try:
                await asyncio.wait_for(
                    self._do_fallback(event, cid, arg or text), timeout=VOICE_BUDGET
                )
            except asyncio.TimeoutError:
                _stat["timeout"] += 1
                logger.warning("[voice] 兜底超时，放弃")
        except BaseException as e:  # noqa: BLE001
            logger.warning("[voice] 兜底钩子异常: %s", e)

    async def _do_fallback(self, event, cid: str, text: str) -> None:
        self._record_used(0, "hook")
        async with _semaphore:
            ok = await _send_speech(
                event, cid, text, note=VOICE_NOTE_TEXT
            )
        if not ok:
            _stat["fail"] += 1

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

    @staticmethod
    def _arg(event: AstrMessageEvent, cmd: str) -> str:
        text = (event.message_str or "").strip()
        for p in (cmd, "/" + cmd):
            if text.startswith(p):
                return text[len(p) :].strip()
        return text
