# dsh-fwd —— 让机器人能「点进去」看 QQ 的合并转发消息（聊天记录）。
#
# 群里那条灰色的「[转发消息]」，点开是一整段聊天记录。机器人以前对它一无所知，
# 根因不是没收到，而是**在框架里被丢掉了**：
#
#   NapCat 上报的段本来是完整的 —— {"type":"forward","data":{"id":"...","content":[97 条完整消息]}}
#   而 aiocqhttp 适配器的 _convert_handle_message_event 没有 forward 分支，
#   它落到通用 else 里执行 ComponentTypes["forward"](**m["data"])，
#   而 class Forward 只声明了 id: str —— content 那 97 条消息在构造时被静默丢弃
#   （实测 Forward(id=..., content=[...]) 之后 hasattr(o,"content") 为 False）。
#   同时 else 分支只 append 到 abm.message，从不累加 message_str。
#
# 结果：一条纯转发消息进到模型时 message_str 是空的，模型什么都看不到，
# 只能瞎猜或者说「我看不了」。这就是「大肥鱼访问不了」。
#
# 顺带说明为什么它连话都不接：ProcessStage 调 LLM 的前提是 is_at_or_wake_command，
# 而随机主动回复那条路（builtin_stars/astrbot/main.py:200）要求消息里有
# Plain|Image|Json 才算 has_context_content —— 纯 Forward 段一个都不满足。
# 所以「没人@它时对转发保持沉默」是框架的既定行为，本插件不改这一点，
# 只保证**一旦有人问起，它真的读得到全文**。
#
# 三条访问路径（用户要求「自动读」，不需要指令）：
#   ① 当前这条消息自己带转发段
#   ② 当前这条消息**引用**了一条转发消息（群里最自然的「点进去问」）
#      —— 框架的 <Quoted Message> 走的是同一个 Forward 组件，一样是空的
#   ③ 最近 LOOKBACK 条群消息里有转发（有人先转发、随后另起一条来问）
#      —— get_group_msg_history 返回的原始 JSON 里 content 是完整内联的，
#         不依赖框架那条已经丢了数据的解析链
#
# 取不到内联 content 时（旧版协议端只给 id）回落 get_forward_msg，
# 实测该接口用外层 message_id 或 forward.data.id 都能查。
#
# 嵌套：聊天记录里套聊天记录，递归展开到 MAX_DEPTH 层，超出只留一行说明。
# 长记录：保头保尾（前 HEAD 条 + 后 TAIL 条，中间标「省略 N 条」）。
# 图片：混合策略 —— QQ 自带 summary 够用就直接用（零成本），
#       否则最多挑 MAX_IMAGES 张交视觉模型转述（串行，智谱并发会 429）。
# 视频：抽第一帧转述（有大小与总预算双重上限），拿不到就退成 [视频 N MB]。
#
# 所有耗时操作共用一个总预算 BUDGET，超时立刻放弃注入而不是拖着不回复 ——
# 宁可这次只看到摘要，也不能让机器人半分钟不说话。

import asyncio
import io
import os
import re
import time

import aiohttp

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType

try:
    from PIL import Image as PILImage
except Exception:  # pragma: no cover - 容器里一定有，本地跑测试可能没有
    PILImage = None

# ----------------------------------------------------------------- 旋钮

ENABLED = os.environ.get("DSH_FWD", "1") not in ("0", "false", "False", "")
# 递归展开几层嵌套聊天记录。1 = 只展开最外层里的一层
MAX_DEPTH = int(os.environ.get("DSH_FWD_MAX_DEPTH", "3"))
# 整条记录最多解析多少条消息（防有人转发几千条把内存和预算烧光）
MAX_NODES = int(os.environ.get("DSH_FWD_MAX_NODES", "300"))
# 保头保尾：前多少条 / 后多少条
HEAD_N = int(os.environ.get("DSH_FWD_HEAD", "14"))
TAIL_N = int(os.environ.get("DSH_FWD_TAIL", "8"))
# 注入正文的字符上限（模型上下文是有限的，转发动辄上千条）
MAX_CHARS = int(os.environ.get("DSH_FWD_MAX_CHARS", "2000"))
# 单条消息正文超过这么多字就截断
LINE_MAX = int(os.environ.get("DSH_FWD_LINE_MAX", "180"))
# 往回看几条群消息找转发
LOOKBACK = int(os.environ.get("DSH_FWD_LOOKBACK", "8"))
# 回看到的转发超过这么多秒就不算「正在聊的那条」了
MAX_AGE = int(os.environ.get("DSH_FWD_MAX_AGE", "1800"))
# 最多给几张图配文字（只给「最终没被截断掉」的图花钱）
MAX_IMAGES = int(os.environ.get("DSH_FWD_MAX_IMAGES", "3"))
# 视频抽帧转述：0 关闭
VIDEO_FRAME = os.environ.get("DSH_FWD_VIDEO_FRAME", "1") not in ("0", "false", "False", "")
# 超过这个体积的视频不抽帧（下载太久）
VIDEO_MAX_MB = float(os.environ.get("DSH_FWD_VIDEO_MAX_MB", "12"))
# 最多给几个视频抽帧
MAX_VIDEOS = int(os.environ.get("DSH_FWD_MAX_VIDEOS", "1"))
# 整个「取记录 + 下载 + 转述」的总预算，秒
BUDGET = float(os.environ.get("DSH_FWD_BUDGET", "20"))
# 单个媒体下载超时
FETCH_TIMEOUT = float(os.environ.get("DSH_FWD_FETCH_TIMEOUT", "10"))
# 转述前把图缩到这个最长边（实测缩图后快 5 倍且描述质量没下降）
COMPRESS_SIZE = int(os.environ.get("DSH_FWD_COMPRESS", "768"))
# 转述用的 provider，留空则用框架配的 default_image_caption_provider_id
PROVIDER_ID = os.environ.get("DSH_FWD_PROVIDER", "").strip()
# 图片转述提示词。刻意写短：越长模型越啰嗦、越慢
CAPTION_PROMPT = os.environ.get("DSH_FWD_PROMPT", "用中文30字内说这张图里有什么。")
VIDEO_PROMPT = os.environ.get("DSH_FWD_VIDEO_PROMPT", "这是一个视频的画面，用中文30字内说画面里有什么。")

TMP_DIR = os.environ.get("DSH_FWD_TMP", "/AstrBot/data/temp")

# 「只 @机器人 + 一段聊天记录、一个字都不写」时给 message_str 补的占位符。
# 就是 QQ 自己在群里显示的那四个字，不是我们编的内容。
TEXTLESS_PLACEHOLDER = "[转发消息]"

# 转述缓存：同一张图/同一个视频只花一次钱
_cap_cache: dict[str, str] = {}
_cap_order: list[str] = []
CACHE_MAX = 400

# ----------------------------------------------------------------- 表情表
#
# QQ 表情只上报数字 id，框架和 NapCat 都没有 id→名称 表（已翻遍两个容器）。
# 这里只收录能确定的常用 id；查不到的一律渲染成 [表情]，
# 绝不猜 —— 猜错等于往模型嘴里塞假话。
_FACE_NAMES = {
    0: "惊讶", 1: "撇嘴", 2: "色", 3: "发呆", 4: "得意", 5: "流泪", 6: "害羞",
    7: "闭嘴", 8: "睡", 9: "大哭", 10: "尴尬", 11: "发怒", 12: "调皮", 13: "呲牙",
    14: "微笑", 15: "难过", 16: "酷", 18: "抓狂", 19: "吐", 20: "偷笑", 21: "可爱",
    22: "白眼", 23: "傲慢", 24: "饥饿", 25: "困", 26: "惊恐", 27: "流汗", 28: "憨笑",
    29: "悠闲", 30: "奋斗", 31: "咒骂", 32: "疑问", 33: "嘘", 34: "晕", 36: "衰",
    37: "骷髅", 38: "敲打", 39: "再见", 41: "发抖", 42: "爱情", 43: "跳跳",
    46: "猪头", 49: "拥抱", 53: "蛋糕", 56: "刀", 59: "便便", 60: "咖啡", 63: "玫瑰",
    64: "凋谢", 66: "爱心", 67: "心碎", 69: "礼物", 74: "太阳", 75: "月亮", 76: "赞",
    77: "踩", 78: "握手", 79: "胜利", 85: "飞吻", 86: "怄火", 89: "西瓜", 96: "冷汗",
    97: "擦汗", 98: "抠鼻", 99: "鼓掌", 100: "糗大了", 101: "坏笑", 102: "左哼哼",
    103: "右哼哼", 104: "哈欠", 105: "鄙视", 106: "委屈", 107: "快哭了", 108: "阴险",
    109: "亲亲", 110: "吓", 111: "可怜", 112: "菜刀", 113: "啤酒", 116: "示爱",
    118: "抱拳", 119: "勾引", 120: "拳头", 121: "差劲", 122: "爱你", 123: "NO",
    124: "OK", 172: "眨眼睛", 173: "泪奔", 174: "无奈", 175: "卖萌", 176: "小纠结",
    177: "喷血", 178: "斜眼笑", 179: "doge", 180: "惊喜", 181: "骚扰", 182: "笑哭",
    183: "我最美", 187: "幽灵", 193: "大笑", 194: "不开心", 197: "冷漠", 198: "呃",
    199: "好棒", 200: "拜托", 201: "点赞", 202: "无聊", 203: "托脸", 204: "吃",
    205: "送花", 206: "害怕", 207: "花痴", 210: "飙泪", 211: "我不看", 212: "托下巴",
    214: "啵啵", 215: "糊脸", 216: "拍头", 217: "扯一扯", 218: "舔一舔", 219: "蹭一蹭",
    222: "抱抱", 223: "暴击", 224: "开枪", 225: "撩一撩", 226: "拍桌", 227: "拍手",
    229: "干杯", 230: "嘲讽", 231: "哼", 232: "佛系", 233: "掐一掐", 234: "惊呆",
    235: "颤抖", 236: "啃头", 237: "偷看", 238: "扇脸", 239: "原谅", 240: "喷脸",
    241: "生日快乐", 262: "脑瓜疼", 263: "沧桑", 264: "捂脸", 265: "辣眼睛",
    266: "哦哟", 267: "头秃", 268: "问号脸", 269: "暗中观察", 270: "emm",
    271: "吃瓜", 272: "呵呵哒", 273: "我酸了", 274: "太南了", 277: "汪汪", 278: "汗",
    279: "打脸", 280: "击掌", 281: "无眼笑", 282: "敬礼", 283: "狂笑",
    284: "面无表情", 285: "摸鱼", 286: "魔鬼笑", 287: "哦", 289: "睁眼",
    290: "敲开心", 291: "震惊", 292: "让我康康", 294: "期待", 297: "拜谢",
    299: "牛啊", 305: "右亲亲", 306: "牛气冲天", 307: "喵喵", 314: "大怨种",
    317: "咧嘴笑", 318: "惊吓", 319: "生气", 320: "加油",
}

# summary 里这几个词等于没说，要交给视觉模型
_USELESS_SUMMARY = {"", "图片", "动画表情", "表情", "闪照", "图", "picture", "image"}


# ----------------------------------------------------------------- 纯函数区
# 下面这些不碰网络、不碰框架，test_fwd.py 直接 import 来测。


def _cache_put(key: str, val: str) -> None:
    if not key or key in _cap_cache:
        return
    _cap_cache[key] = val
    _cap_order.append(key)
    while len(_cap_order) > CACHE_MAX:
        _cap_cache.pop(_cap_order.pop(0), None)


def face_name(fid) -> str:
    """表情 id → 名字。查不到就返回空串，由调用方渲染成 [表情]。

    刻意不猜：QQ 表情 id 有几百个且随版本增删，编造名字等于往模型嘴里塞假话。
    """
    try:
        return _FACE_NAMES.get(int(fid), "")
    except (TypeError, ValueError):
        return ""


def summary_text(s: str) -> str:
    """把 QQ 自带的 summary 洗成可用的文字，没信息量的返回空串。

    实测形状：表情包是 "[/笑哭]"、普通动图是 "[动画表情]"、静图常常是 "[图片]"。
    只有第一种真的告诉了我们内容，另外两种必须交给视觉模型。
    """
    s = (s or "").strip()
    if not s:
        return ""
    core = s.strip()
    if core.startswith("[") and core.endswith("]"):
        core = core[1:-1].strip()
    if core.startswith("/"):
        core = core[1:].strip()
    if core.lower() in _USELESS_SUMMARY:
        return ""
    return core


def _human_mb(size) -> str:
    try:
        n = float(size)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n < 1024 * 1024:
        return "%.0fKB" % (n / 1024)
    return "%.1fMB" % (n / 1024 / 1024)


def node_view(node: dict) -> tuple[str, str, int, list]:
    """把千奇百怪的节点形状归一成 (昵称, QQ号, 时间戳, 消息段数组)。

    三种真实形状都要认：
      ① NapCat 内联在 forward.data.content 里的，是**完整的消息事件**
         （有 sender/message/time，见 real_seq、post_type 等字段）
      ② OneBot 标准的 node 段：{"type":"node","data":{"nickname","content"}}
      ③ get_forward_msg 返回的 messages[]，形状同 ①
    """
    if not isinstance(node, dict):
        return "", "", 0, []

    # 形状 ②：外面还套着一层 type/data
    if node.get("type") in ("node", "forward") and isinstance(node.get("data"), dict):
        node = node["data"]

    sender = node.get("sender") if isinstance(node.get("sender"), dict) else {}
    who = (
        (sender.get("card") or "").strip()
        or (sender.get("nickname") or "").strip()
        or str(node.get("nickname") or "").strip()
        or str(node.get("name") or "").strip()
    )
    uid = str(sender.get("user_id") or node.get("user_id") or node.get("uin") or "")
    try:
        ts = int(node.get("time") or 0)
    except (TypeError, ValueError):
        ts = 0

    segs = node.get("message")
    if segs is None:
        segs = node.get("content")
    if isinstance(segs, str):
        # 极少数协议端只给 CQ 码字符串。不解析 CQ 码（容易错），当纯文本用。
        segs = [{"type": "text", "data": {"text": segs}}]
    if not isinstance(segs, list):
        segs = []
    return who or "某人", uid, ts, segs


def find_forward_segs(segs) -> list[dict]:
    """从一条消息的原始段数组里挑出转发段。只看结构，不认关键字。"""
    out = []
    if not isinstance(segs, list):
        return out
    for s in segs:
        if not isinstance(s, dict):
            continue
        if s.get("type") in ("forward", "node"):
            out.append(s)
    return out


def flatten(nodes, depth: int = 0, max_depth: int = None, budget: list = None) -> list[dict]:
    """把（可能嵌套的）聊天记录压平成一串记录。

    返回 [{"who","uid","time","depth","parts","medias"}]，
    parts 是 (kind, payload) 列表，payload 对图片/视频是**可变 dict**，
    后面配好文字描述就直接写回去 —— 这样选段（保头保尾）和转述的顺序可以解耦：
    先决定哪些行留得下来，再只给留下来的行里的图花钱。
    """
    if max_depth is None:
        max_depth = MAX_DEPTH
    if budget is None:
        budget = [MAX_NODES]

    out: list[dict] = []
    if not isinstance(nodes, list):
        return out

    for node in nodes:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        who, uid, ts, segs = node_view(node)
        parts: list[tuple] = []
        nested_batches: list[list] = []

        for s in segs:
            if not isinstance(s, dict):
                continue
            t = s.get("type")
            d = s.get("data") if isinstance(s.get("data"), dict) else {}

            if t == "text":
                txt = str(d.get("text") or "")
                if txt:
                    parts.append(("text", txt))
            elif t == "face":
                parts.append(("face", face_name(d.get("id"))))
            elif t in ("image", "flash"):
                parts.append((
                    "image",
                    {
                        "kind": "image",
                        "key": str(d.get("file") or d.get("url") or "")[:120],
                        "summary": summary_text(d.get("summary")),
                        "url": str(d.get("url") or ""),
                        "local": str(d.get("path") or ""),
                        "size": d.get("file_size"),
                        "caption": "",
                    },
                ))
            elif t == "video":
                parts.append((
                    "video",
                    {
                        "kind": "video",
                        "key": str(d.get("file") or d.get("url") or "")[:120],
                        "summary": "",
                        "url": str(d.get("url") or ""),
                        "local": str(d.get("path") or ""),
                        "size": d.get("file_size"),
                        "caption": "",
                    },
                ))
            elif t == "record":
                parts.append(("record", _human_mb(d.get("file_size"))))
            elif t == "file":
                parts.append(("file", str(d.get("file") or d.get("name") or "")))
            elif t == "json":
                parts.append(("json", _json_prompt(d.get("data"))))
            elif t == "at":
                parts.append(("at", str(d.get("name") or d.get("qq") or "")))
            elif t == "mface":
                parts.append(("image", {
                    "kind": "image",
                    "key": str(d.get("emoji_id") or d.get("file") or "")[:120],
                    "summary": summary_text(d.get("summary")),
                    "url": str(d.get("url") or ""),
                    "local": "",
                    "size": None,
                    "caption": "",
                }))
            elif t in ("forward", "node"):
                # 聊天记录里套聊天记录
                inner = d.get("content")
                if depth + 1 > max_depth or not isinstance(inner, list) or not inner:
                    parts.append(("fwd_stub", str(d.get("id") or "")))
                else:
                    nested_batches.append(inner)
                    parts.append(("fwd_open", len(inner)))
            elif t in ("reply",):
                parts.append(("reply", ""))
            elif t == "markdown":
                parts.append(("text", "[卡片]"))
            # 其余类型（poke/dice/rps/contact/location/music…）一律忽略：
            # 它们在聊天记录里几乎不出现，硬渲染只会给模型添噪音。

        if parts:
            out.append({
                "who": who,
                "uid": uid,
                "time": ts,
                "depth": depth,
                "parts": parts,
            })

        for inner in nested_batches:
            out.extend(flatten(inner, depth + 1, max_depth, budget))

    return out


def _json_prompt(raw) -> str:
    """卡片消息只取 prompt 那一句（就是群里显示的灰字标题）。"""
    if isinstance(raw, dict):
        return str(raw.get("prompt") or "")[:60]
    s = str(raw or "")
    m = re.search(r'"prompt"\s*:\s*"((?:[^"\\]|\\.)*)"', s)
    if m:
        try:
            return re.sub(r"\\(.)", r"\1", m.group(1))[:60]
        except Exception:
            return m.group(1)[:60]
    return ""


def render_parts(rec: dict) -> str:
    """把一条记录的段渲染成一行正文。"""
    buf = []
    for kind, payload in rec["parts"]:
        if kind == "text":
            buf.append(payload)
        elif kind == "face":
            buf.append("[%s]" % (("表情:" + payload) if payload else "表情"))
        elif kind in ("image", "video"):
            buf.append(_media_text(payload))
        elif kind == "record":
            buf.append("[语音%s]" % ((" " + payload) if payload else ""))
        elif kind == "file":
            buf.append("[文件%s]" % ((" " + payload) if payload else ""))
        elif kind == "json":
            buf.append("[卡片%s]" % (("：" + payload) if payload else ""))
        elif kind == "at":
            buf.append("@%s" % payload if payload else "@某人")
        elif kind == "reply":
            buf.append("[回复]")
        elif kind == "fwd_stub":
            buf.append("[又一段聊天记录（层数太深，没展开）]")
        elif kind == "fwd_open":
            buf.append("[又一段聊天记录，共 %d 条，内容接在下面]" % payload)
    line = " ".join(x for x in buf if x)
    line = re.sub(r"\s+", " ", line).strip()
    if len(line) > LINE_MAX:
        line = line[:LINE_MAX] + "…"
    return line


def _media_text(m: dict) -> str:
    """一张图/一个视频渲染成什么字，取决于我们对它知道多少。"""
    cap = (m.get("caption") or "").strip()
    if m.get("kind") == "video":
        if cap:
            return "[视频：%s]" % cap
        mb = _human_mb(m.get("size"))
        return "[视频%s]" % ((" " + mb) if mb else "")
    if cap:
        return "[图片：%s]" % cap
    s = (m.get("summary") or "").strip()
    if s:
        return "[表情:%s]" % s
    return "[图片]"


def select_records(recs: list, head_n: int = None, tail_n: int = None) -> tuple[list, int, int]:
    """保头保尾。返回 (选中的记录, 中间省略了多少条, 头段有多少条)。

    为什么不做「摘要中间」：中间那段要摘要就得再问一次模型，
    既慢又可能把话说歪。聊天记录的信息量本来就集中在开头（起因）
    和结尾（结论），中间是来回拉扯 —— 明确告诉模型省了多少条，
    它需要细节时可以直接问人。
    """
    if head_n is None:
        head_n = HEAD_N
    if tail_n is None:
        tail_n = TAIL_N
    n = len(recs)
    if n <= head_n + tail_n:
        return list(recs), 0, n
    return list(recs[:head_n]) + list(recs[n - tail_n:]), n - head_n - tail_n, head_n


def _hm(ts: int) -> str:
    if not ts:
        return ""
    return time.strftime("%H:%M", time.localtime(ts))


def _ymd_hm(ts: int) -> str:
    if not ts:
        return ""
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def render_block(recs: list, total: int, omitted: int, head_cnt: int = None,
                 max_chars: int = None) -> str:
    """渲染成注入用的正文。head_cnt 是「省略标记插在第几条之前」。"""
    if max_chars is None:
        max_chars = MAX_CHARS
    if head_cnt is None:
        head_cnt = HEAD_N

    lines = []
    speakers = {(r["uid"], r["who"]) for r in recs}
    masked = len(speakers) == 1 and total > 3

    ts_all = [r["time"] for r in recs if r["time"]]
    span = ""
    if ts_all:
        a, b = min(ts_all), max(ts_all)
        span = _ymd_hm(a) if a == b else "%s~%s" % (_ymd_hm(a), _hm(b))

    head = "这是一段被转发进群的聊天记录，共 %d 条" % total
    if span:
        head += "，时间 " + span
    head += "。"
    if masked:
        # 实测：私聊记录被转发时 QQ 会把所有节点的 user_id/nickname 抹成同一个
        # （97 条全是 "QQ用户(1094950020)"）。不说清楚，模型会把两个人的话
        # 当成一个人说的，然后自信地讲错。
        head += "（注意：QQ 隐去了发言人身份，下面每行的名字都一样，"
        head += "实际可能是两个人在对话，需要你从上下文语气自己判断谁在说话。）"
    head += "图片和视频本身你看不了，方括号里是转述。"
    lines.append(head)

    used = len(head)
    body = []
    prev_depth = None
    inserted_gap = False

    for i, r in enumerate(recs):
        if omitted and i == head_cnt and not inserted_gap:
            body.append("……（中间省略 %d 条）……" % omitted)
            inserted_gap = True
            prev_depth = None  # 断点之后重新打一次时间
        txt = render_parts(r)
        if not txt:
            continue
        indent = "　" * min(r["depth"], 4)
        stamp = ""
        # 只在段落开头 / 断点后 / 嵌套层变化时打时间：
        # 既让模型知道头尾不连续，又不给每行都添噪音
        if i == 0 or r["depth"] != prev_depth:
            stamp = "[%s] " % _hm(r["time"]) if r["time"] else ""
        prev_depth = r["depth"]
        line = "%s%s%s: %s" % (indent, stamp, r["who"], txt)
        if used + len(line) + 1 > max_chars:
            body.append("……（后面还有，篇幅有限没全放）")
            break
        body.append(line)
        used += len(line) + 1

    lines.extend(body)
    return "\n".join(lines)


def pick_media(recs: list, max_images: int = None, max_videos: int = None) -> list[dict]:
    """从**留下来的**记录里挑出需要花钱转述的媒体。

    顺序很关键：先保头保尾选完再挑图，绝不给被省略掉的行里的图付费。
    自带可用 summary 的直接跳过 —— 那是 QQ 白送的答案。
    """
    if max_images is None:
        max_images = MAX_IMAGES
    if max_videos is None:
        max_videos = MAX_VIDEOS

    imgs, vids = [], []
    for r in recs:
        for kind, payload in r["parts"]:
            if kind == "image" and isinstance(payload, dict):
                if payload.get("summary") or payload.get("caption"):
                    continue
                if not (payload.get("url") or payload.get("local")):
                    continue
                imgs.append(payload)
            elif kind == "video" and isinstance(payload, dict):
                if payload.get("caption"):
                    continue
                if not (payload.get("url") or payload.get("local")):
                    continue
                vids.append(payload)

    out = []
    for m in imgs:
        if len(out) >= max_images:
            break
        out.append(m)
    nv = 0
    for m in vids:
        if nv >= max_videos:
            break
        out.append(m)
        nv += 1
    return out


# ----------------------------------------------------------------- 插件本体


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context
        logger.info(
            "[fwd] 已加载：开关=%s 嵌套=%d层 保头%d保尾%d 上限%d字 "
            "回看%d条 图%d张 视频%d个 预算%.0fs",
            "开" if ENABLED else "关",
            MAX_DEPTH, HEAD_N, TAIL_N, MAX_CHARS,
            LOOKBACK, MAX_IMAGES, MAX_VIDEOS, BUDGET,
        )

    # ------------------------------------------------------------ 开闸

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=900,
    )
    async def unblock_textless_forward(self, event: AstrMessageEvent) -> None:
        """给「只 @机器人 + 一段聊天记录、一个字都不写」开闸。

        这是第二道堵点，实测出来的：那种消息压根走不到 on_llm_request。
        internal.py:183 有个空消息闸门 ——

            has_valid_message = bool(event.message_str and event.message_str.strip())
            has_media_content = any(isinstance(c, (Image, File, Record, Video)) ...)
            has_reply         = any(isinstance(c, Reply) ...)
            if not has_provider_request and not (三者任一): return

        纯转发消息三条全不满足：message_str 是空的（适配器的 else 分支从不累加它），
        而闸门认的媒体类型里**没有 Forward**。于是整个 LLM 路径直接 return，
        展开钩子根本没机会跑。e2e 里这条用例就是这么静默失败的
        （日志只有 event_bus 收到 `[At:xxx] [转发消息]`，后面什么都没有）。

        做法：把 message_str 补成群里显示的那四个字。这不是伪造内容 ——
        QQ 自己在群里显示的就是「[转发消息]」，而真正的全文随后由
        <forward_context> 送进去。只在**确实有转发段且确实没文字**时才动手。

        为什么用 priority=900 的 event_message_type 而不是别的钩子：
        插件 handler 在 WakingCheckStage 里被激活，而 ProcessStage 先跑
        activated_handlers 再走 LLM 分支 —— 时机刚好在闸门之前。
        dsh-acl 用的是 priority=1000 的同类钩子，这里排在它后面，
        免得越权的指令还没被拦下就先被我们改了 message_str。
        """
        if not ENABLED:
            return
        try:
            if (event.message_str or "").strip():
                return
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            segs = raw.get("message") if isinstance(raw, dict) else None
            if not find_forward_segs(segs):
                return
            event.message_str = TEXTLESS_PLACEHOLDER
            event.set_extra("fwd_notext", True)
            logger.info("[fwd] 纯转发消息没有文字，已补占位符让它进得了模型")
        except Exception as e:
            # 开闸失败最多是这条消息照旧被闸门挡掉，绝不能连累别的消息
            logger.warning("[fwd] 开闸失败（已忽略）: %s", e)

    # ------------------------------------------------------------ 取 provider

    def _provider_id(self) -> str:
        if PROVIDER_ID:
            return PROVIDER_ID
        try:
            cfg = self.context.get_config() or {}
            return cfg.get("provider_settings", {}).get(
                "default_image_caption_provider_id", ""
            ) or ""
        except Exception:
            return ""

    # ------------------------------------------------------------ 取聊天记录

    @staticmethod
    def _routing(event) -> dict:
        """call_action 必须显式带 self_id。

        aiocqhttp 的反向 WS 只在「传了 self_id / 还在 websocket 上下文里 /
        全局只有一个连接」这三种情况下才知道该走哪条连接（api_impl.py:129）。
        on_llm_request 钩子早就离开了 websocket 上下文，所以一旦接上第二个
        连接（第二个 QQ 号、或跑 e2e 用的假 napcat）就会抛 ApiNotAvailable。
        """
        sid = str(getattr(getattr(event, "message_obj", None), "self_id", "") or "")
        return {"self_id": int(sid)} if sid.isdigit() else {}

    async def _fetch_nodes(self, event, seg_data: dict, msg_id) -> list:
        """拿到这段转发的节点数组。优先用内联的，取不到才发请求。"""
        inline = seg_data.get("content") if isinstance(seg_data, dict) else None
        if isinstance(inline, list) and inline:
            return inline

        bot = getattr(event, "bot", None)
        if bot is None:
            logger.info("[fwd] 没有 bot 对象，无法回落 get_forward_msg")
            return []

        kw = self._routing(event)
        # 实测 get_forward_msg 用外层 message_id 或 forward.data.id 都能查到，
        # 两个都试：不同协议端 / 不同版本认的键不一样。
        for key in (seg_data.get("id"), msg_id):
            if not key:
                continue
            try:
                r = await bot.call_action(
                    action="get_forward_msg", message_id=str(key), **kw
                )
            except Exception as e:
                logger.info("[fwd] get_forward_msg(%s) 失败: %s", key, e)
                continue
            data = r or {}
            if isinstance(data, dict) and "messages" not in data and isinstance(
                data.get("data"), dict
            ):
                data = data["data"]
            msgs = (data or {}).get("messages") or []
            if msgs:
                return msgs
        return []

    # ------------------------------------------------------------ 找转发段

    def _from_current(self, event) -> tuple[list, str]:
        """路径①：当前这条消息自己带转发段。

        必须读**原始上报**（event.message_obj.raw_message 就是 aiocqhttp 的
        Event，它是 dict 子类）：框架解析出来的 Forward 组件已经把 content 丢了。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        segs = None
        if isinstance(raw, dict):
            segs = raw.get("message")
        if not isinstance(segs, list):
            return [], ""
        hits = find_forward_segs(segs)
        if not hits:
            return [], ""
        mid = getattr(getattr(event, "message_obj", None), "message_id", "")
        return hits, str(mid or "")

    async def _from_quote(self, event) -> tuple[list, str]:
        """路径②：当前消息引用了一条转发消息（群里最自然的「点进去问」）。

        框架的 <Quoted Message> 走的是同一个已经丢了 content 的 Forward 组件，
        所以这里自己去 get_msg 拿原始 JSON。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        segs = raw.get("message") if isinstance(raw, dict) else None
        if not isinstance(segs, list):
            return [], ""
        rid = ""
        for s in segs:
            if isinstance(s, dict) and s.get("type") == "reply":
                rid = str((s.get("data") or {}).get("id") or "")
                break
        if not rid:
            return [], ""

        bot = getattr(event, "bot", None)
        if bot is None:
            return [], ""
        try:
            r = await bot.call_action(
                action="get_msg", message_id=int(rid), **self._routing(event)
            )
        except Exception as e:
            logger.info("[fwd] 取引用消息失败: %s", e)
            return [], ""
        data = r or {}
        if isinstance(data, dict) and "message" not in data and isinstance(
            data.get("data"), dict
        ):
            data = data["data"]
        hits = find_forward_segs((data or {}).get("message"))
        return (hits, rid) if hits else ([], "")

    async def _from_history(self, event) -> tuple[list, str]:
        """路径③：最近 LOOKBACK 条群消息里有转发（先转发、随后另起一条来问）。"""
        gid = event.get_group_id()
        if not gid:
            return [], ""
        bot = getattr(event, "bot", None)
        if bot is None:
            return [], ""
        try:
            r = await bot.call_action(
                action="get_group_msg_history",
                group_id=int(gid),
                count=LOOKBACK + 2,
                **self._routing(event),
            )
        except Exception as e:
            logger.info("[fwd] 取群历史失败: %s", e)
            return [], ""
        data = r or {}
        if isinstance(data, dict) and "messages" not in data and isinstance(
            data.get("data"), dict
        ):
            data = data["data"]
        msgs = (data or {}).get("messages") or []
        if not msgs:
            return [], ""

        now = int(time.time())
        cur = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        # 由近及远：只要最近的那一条转发
        for m in reversed(msgs):
            if not isinstance(m, dict):
                continue
            if str(m.get("message_id") or "") == cur:
                continue
            try:
                age = now - int(m.get("time") or 0)
            except (TypeError, ValueError):
                age = 0
            if m.get("time") and age > MAX_AGE:
                continue
            hits = find_forward_segs(m.get("message"))
            if hits:
                return hits, str(m.get("message_id") or "")
        return [], ""

    # ------------------------------------------------------------ 媒体转述

    async def _fetch_bytes(self, session, url: str) -> bytes | None:
        async with session.get(url) as resp:
            if resp.status != 200:
                # 最常见原因：URL 里的 rkey 过期（实测约 18 分钟就变 400）
                logger.info("[fwd] 下载失败 HTTP %s", resp.status)
                return None
            body = await resp.read()
            return body if len(body) >= 64 else None

    @staticmethod
    def _to_jpeg(raw: bytes) -> bytes:
        """把 QQ 图片字节变成视觉模型能吃的 JPEG。

        必须自己做：视觉模型对 GIF 直接报 400，而框架的 compress_image
        遇到 GIF 原样返回，等于没处理。这里只取首帧 —— 转发记录里的图
        主要是用来「知道发了什么」，不像 imgctx 那样要读表情包的动作。
        """
        if PILImage is None:
            return raw
        im = PILImage.open(io.BytesIO(raw))
        try:
            im.seek(0)
        except Exception:
            pass
        f = im.convert("RGB")
        w, h = f.size
        scale = min(1.0, COMPRESS_SIZE / max(w, h))
        if scale < 1.0:
            f = f.resize((max(int(w * scale), 1), max(int(h * scale), 1)), PILImage.LANCZOS)
        buf = io.BytesIO()
        f.save(buf, format="JPEG", quality=82)
        return buf.getvalue()

    @staticmethod
    async def _video_frame(url: str, out: str) -> bool:
        """让 ffmpeg 直接从 URL 抽一帧，不下整包。

        实测 3.4MB 的群视频 1 秒出帧 —— 因为 ffmpeg 只读到能解出第一帧
        就停了，不会把整个文件拖下来。注意必须加 -update 1，
        否则 image2 muxer 会抱怨文件名不是序列模式。
        """
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", "1", "-i", url,
            "-frames:v", "1", "-update", "1",
            "-vf", "scale=%d:-2" % COMPRESS_SIZE,
            out,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=FETCH_TIMEOUT + 8)
        except asyncio.TimeoutError:
            logger.info("[fwd] 视频抽帧超时")
            return False
        except Exception as e:
            logger.info("[fwd] 视频抽帧异常: %s", e)
            return False
        ok = os.path.exists(out) and os.path.getsize(out) > 512
        if not ok:
            logger.info("[fwd] 视频抽帧没产出: %s", (err or b"")[:160])
        return ok

    async def _caption_all(self, provider_id: str, medias: list, deadline: float) -> int:
        """给这批媒体配文字。串行 —— 智谱并发两张直接 429（实测 code 1302）。"""
        done = 0
        # 先吃缓存：同一张表情包在群里被反复转发是常态
        rest = []
        for m in medias:
            hit = _cap_cache.get(m.get("key") or "")
            if hit:
                m["caption"] = hit
                done += 1
            else:
                rest.append(m)
        if not rest or not provider_id:
            if rest and not provider_id:
                logger.info("[fwd] 没有配转述用的 provider，%d 个媒体只给占位符", len(rest))
            return done

        os.makedirs(TMP_DIR, exist_ok=True)
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for m in rest:
                if time.monotonic() > deadline:
                    logger.info("[fwd] 预算用完，剩下的媒体不转述了")
                    break
                path = os.path.join(
                    TMP_DIR,
                    "fwd_%s.jpg" % re.sub(r"[^A-Za-z0-9]", "", (m.get("key") or "x"))[:40],
                )
                ok = False
                if m.get("kind") == "video":
                    if not VIDEO_FRAME:
                        continue
                    try:
                        sz = float(m.get("size") or 0)
                    except (TypeError, ValueError):
                        sz = 0
                    if sz and sz > VIDEO_MAX_MB * 1024 * 1024:
                        logger.info("[fwd] 视频 %s 太大，跳过抽帧", _human_mb(sz))
                        continue
                    src = m.get("local") if (m.get("local") and os.path.exists(m["local"])) else m.get("url")
                    if not src:
                        continue
                    ok = await self._video_frame(src, path)
                else:
                    raw = None
                    local = m.get("local")
                    if local and os.path.exists(local):
                        try:
                            with open(local, "rb") as f:
                                raw = f.read()
                        except OSError:
                            raw = None
                    if raw is None and m.get("url"):
                        try:
                            raw = await self._fetch_bytes(session, m["url"])
                        except Exception as e:
                            logger.info("[fwd] 抓图异常: %s", e)
                    if not raw:
                        continue
                    try:
                        # CPU 活儿放线程里：这台机器只有 2 核，别卡事件循环
                        jpeg = await asyncio.to_thread(self._to_jpeg, raw)
                        with open(path, "wb") as f:
                            f.write(jpeg)
                        ok = True
                    except Exception as e:
                        logger.info("[fwd] 图片预处理失败: %s", e)
                        ok = False

                if not ok:
                    continue
                try:
                    resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=VIDEO_PROMPT if m.get("kind") == "video" else CAPTION_PROMPT,
                        image_urls=[path],
                    )
                    cap = (
                        getattr(resp, "completion_text", None)
                        or getattr(resp, "_completion_text", None)
                        or ""
                    ).strip()
                except Exception as e:
                    logger.info("[fwd] 视觉模型转述失败: %s", e)
                    cap = ""
                finally:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                if cap:
                    cap = " ".join(cap.split())[:100]
                    m["caption"] = cap
                    _cache_put(m.get("key") or "", cap)
                    done += 1
        return done

    # ------------------------------------------------------------ 主钩子

    @filter.on_llm_request()
    async def expand_forward(self, event: AstrMessageEvent, req) -> None:
        """把聊天记录展开成文字附到这次请求上。全程 fail-open。"""
        if not ENABLED:
            return
        t0 = time.monotonic()
        deadline = t0 + BUDGET
        try:
            # 一轮只做一次：可能有别的钩子/兜底路径重复触发
            if event.get_extra("fwd_done"):
                return

            segs, mid = self._from_current(event)
            src = "本条"
            if not segs:
                segs, mid = await self._from_quote(event)
                src = "引用"
            if not segs and event.get_message_type() == MessageType.GROUP_MESSAGE:
                segs, mid = await self._from_history(event)
                src = "回看"
            if not segs:
                # 不触发也要留证据，否则每次排查只能靠猜
                logger.debug("[fwd] 这一轮没有转发消息")
                return

            nodes = []
            for s in segs:
                d = s.get("data") if isinstance(s.get("data"), dict) else {}
                got = await self._fetch_nodes(event, d, mid)
                if got:
                    nodes.extend(got)
            if not nodes:
                logger.info("[fwd] 找到转发段（来源=%s）但取不到内容", src)
                return

            recs = flatten(nodes)
            if not recs:
                logger.info("[fwd] 转发里没有可读内容（%d 个节点）", len(nodes))
                return

            total = len(recs)
            kept, omitted, head_cnt = select_records(recs)

            medias = pick_media(kept)
            n_cap = 0
            if medias:
                n_cap = await self._caption_all(self._provider_id(), medias, deadline)

            block = render_block(kept, total, omitted, head_cnt)

            # 他这条消息本身一个字都没有（我们在 unblock_textless_forward 里
            # 给 message_str 补了「[转发消息]」才让它进得了模型）。得明确说清楚，
            # 否则模型会照着框架那句「用户只是@你但没输入内容」回一句「你想说什么」。
            #
            # 这句话必须跟正文放进**同一个带标签的块**里：
            # 不带标签的注入块 dsh-ctxclean 认不出来（它按 ^<小写标签> 判断），
            # 会被 _save_to_history 一轮轮攒进 conversations.content —— 那正是
            # 上次「答非所问」的根因。
            cur = (event.message_str or "").strip()
            if event.get_extra("fwd_notext") or not cur or cur == TEXTLESS_PLACEHOLDER:
                block += (
                    "\n（他这条消息本身没有文字，发的就是上面这段聊天记录。"
                    "请直接针对记录内容回应，不要问他想说什么。）"
                )

            req.extra_user_content_parts.append(
                TextPart(text="<forward_context>\n" + block + "\n</forward_context>")
            )
            event.set_extra("fwd_done", True)

            logger.info(
                "[fwd] 已展开聊天记录：来源=%s 节点%d 条→渲染%d 条（省略%d）"
                "转述%d/%d 个媒体 正文%d字 耗时%.1fs",
                src, len(nodes), total, omitted, n_cap, len(medias),
                len(block), time.monotonic() - t0,
            )
        except Exception as e:
            # 展开失败绝不能影响回复：这只是给模型加料，不是必需品
            logger.warning("[fwd] 展开转发消息失败（已忽略）: %s", e)

    # ------------------------------------------------------------ 状态指令

    @filter.command("聊天记录状态")
    async def show_status(self, event: AstrMessageEvent):
        lines = [
            "聊天记录展开：%s" % ("开" if ENABLED else "关"),
            "嵌套展开 %d 层，单条记录最多解析 %d 条" % (MAX_DEPTH, MAX_NODES),
            "保头 %d 条 + 保尾 %d 条，正文上限 %d 字" % (HEAD_N, TAIL_N, MAX_CHARS),
            "回看最近 %d 条群消息找转发（%d 秒内）" % (LOOKBACK, MAX_AGE),
            "每次最多转述 %d 张图 + %d 个视频，总预算 %.0f 秒" % (MAX_IMAGES, MAX_VIDEOS, BUDGET),
            "转述模型：%s" % (self._provider_id() or "（未配置，只给占位符）"),
            "已缓存 %d 条转述" % len(_cap_cache),
        ]
        yield event.plain_result("\n".join(lines))
