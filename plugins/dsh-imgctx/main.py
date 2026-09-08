# dsh-imgctx —— 让机器人「看得见」最近几条消息里的图片。
#
# 问题：AstrBot 只转述**当前这条消息自带**的图片。astr_main_agent.py 收集图片的
# 循环是 `for comp in event.message_obj.message`，只看这一条；转述结果
# (<image_caption>) 也只在这一轮注入。于是群里最常见的说话方式直接失效：
#
#     群友E：[图片]
#     群友E：@大肥鱼 这什么意思
#
# 第二条消息不带图片，req.image_urls 是空的，_ensure_img_caption 压根不会跑，
# 模型对那张图一无所知，只能瞎猜——这就是「有时候不联系上下文图片」。
#
# 群聊历史那条路也堵着：platform_message_history 表里图片被存成字面量 "[Image]"，
# 不含任何内容（框架自己的说明：「暂时不支持媒体消息记录」），所以就算模型去查
# 群历史，看到的也只是一个占位符。
#
# 做法：在 on_llm_request 钩子里（框架已建好 ProviderRequest、真正发请求之前）
#   ① 若这一轮框架已经转述过图片，什么都不做——不重复花钱
#   ② 否则用 OneBot 的 get_group_msg_history 取最近 LOOKBACK 条消息
#   ③ 挑出时间窗内、别人发的图片，下载后交给视觉模型转述
#   ④ 把转述结果作为 <recent_image_context> 注入 req.extra_user_content_parts
#
# 为什么用视觉模型而不是直接把图塞给主模型：主模型 deepseek-v4-flash-0731 的
# modalities 是 ['text']，根本吃不了图片。框架自己也是这个套路——
# default_image_caption_provider_id (zhipu-vision) 先转成文字再喂主模型。
#
# 成本控制：同一张图只转述一次（按 QQ 的 file id 缓存）；每轮最多 MAX_IMAGES 张；
# 整个过程有总超时，超时就放弃注入而不是拖着回复不发——宁可这次没看懂图，
# 也不能让机器人半分钟不说话。

import asyncio
import hashlib
import io
import os
import re
import time

import aiohttp
from PIL import Image as PILImage

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType

# 总开关
ENABLED = os.environ.get("DSH_IMGCTX", "1") not in ("0", "false", "False", "")
# 往回看几条消息（用户要求 4 条）
LOOKBACK = int(os.environ.get("DSH_IMGCTX_LOOKBACK", "4"))
# 每轮最多转述几张图（防止有人连刷十张把额度烧光）
MAX_IMAGES = int(os.environ.get("DSH_IMGCTX_MAX_IMAGES", "2"))
# 图片超过这么多秒就不算「上下文」了。
# 别调太大：QQ 图片 URL 带短命 rkey，实测约 18 分钟就变 HTTP 400，
# 取历史拿到的地址过期了照样下载不到。
MAX_AGE = int(os.environ.get("DSH_IMGCTX_MAX_AGE", "300"))
# 整个「取历史+下载+转述」的总超时。超了就放弃，不能拖着回复
BUDGET = float(os.environ.get("DSH_IMGCTX_BUDGET", "15"))
# 单张图下载超时
FETCH_TIMEOUT = float(os.environ.get("DSH_IMGCTX_FETCH_TIMEOUT", "8"))
# 转述前把图缩到这个最长边。实测（125KB 原图 vs 缩到 768）：
#   原图 + 长提示词 10.4s / 原图 + 短提示词 14.9s / 缩到 768 + 短提示词 1.9s
# 快 5 倍且描述质量没有可感知的下降 —— 这一步直接决定回复会不会明显变慢。
COMPRESS_SIZE = int(os.environ.get("DSH_IMGCTX_COMPRESS", "768"))
# 动图抽几帧拼成一张再交给视觉模型。QQ 群里的图**以动图为主**（实测 40 条消息
# 里 8 张图有 6 张是 GIF，且文件名伪装成 .jpg），而视觉模型直接吃 GIF 会报
# HTTP 400「图片输入格式/解析错误」，框架的 compress_image 又对 GIF 原样返回
# —— 所以必须自己抽帧。
# 实测对比（127 帧的「小女孩玩锅」表情包）：
#   首帧   -> 「蓝发穿女仆装的小女孩」        （完全没看出在干什么）
#   中间帧 -> 「蓝色卡通人物戴金属锅帽」      （抓到一瞬，动作仍不明）
#   四帧拼 -> 「戴盆后盆掉，最后拿盆，玩盆」  （真正读出了动作）
# 表情包的意思全在动作里，所以默认拼 4 帧。设为 1 则退回单帧模式。
GIF_FRAMES = int(os.environ.get("DSH_IMGCTX_GIF_FRAMES", "4"))
# 转述用的 provider，留空则用框架配的 default_image_caption_provider_id
PROVIDER_ID = os.environ.get("DSH_IMGCTX_PROVIDER", "").strip()
# 转述提示词。刻意写短：提示词越长模型输出越啰嗦、耗时越久，
# 这里只要「图里有什么」，评论和联想留给主模型去做。
CAPTION_PROMPT = os.environ.get(
    "DSH_IMGCTX_PROMPT",
    "用中文30字内说这张图里有什么。",
)
# 动图（拼图）专用提示词 —— 必须告诉模型这是同一个动图的连续几帧，
# 否则它会当成四张无关的图分别描述。
CAPTION_PROMPT_GIF = os.environ.get(
    "DSH_IMGCTX_PROMPT_GIF",
    "这是同一个动图的{n}帧（按时间从左上到右下）。用中文30字内说这个动图在表现什么。",
)
# 是否跳过机器人自己发的图（自己画的图它本来就知道，不用再花钱看一遍）
SKIP_SELF = os.environ.get("DSH_IMGCTX_SKIP_SELF", "1") not in ("0", "false", "False")
# 图片临时存放目录
TMP_DIR = os.environ.get("DSH_IMGCTX_TMP", "/AstrBot/data/imgctx")
# 缓存最多记多少张图的转述
CACHE_MAX = int(os.environ.get("DSH_IMGCTX_CACHE", "300"))

# QQ 的 file id -> 转述文字。同一张图（尤其是被反复引用的表情包）只花一次钱。
_caption_cache: dict[str, str] = {}
# 插入顺序，用于超出 CACHE_MAX 时淘汰最旧的
_cache_order: list[str] = []

# 框架已经成功转述过图片的标记。命中任一即说明这一轮不用我们插手。
_FRAMEWORK_OK_MARKERS = (
    "<image_caption>",
    "[Image Attachment",
)
# 框架转述**失败**的标记。这不是「已处理」，而是「该我们接手」——
# 实测框架对动图必然失败（视觉模型 400「图片输入格式/解析错误」，
# 日志里 90 分钟内出现 4 次 core.astr_main_agent:744 处理图片描述失败），
# 因为它只调 compress_image，而那个函数对 GIF 原样返回。
# 群里图片以动图为主，所以这条分支恰恰是最常走到的。
_FRAMEWORK_FAIL_MARKER = "[Image Captioning Failed]"
# 框架把当前消息的图落盘后会留下 [Image Attachment: path /xxx.jpg]，
# 兜底时直接读这个本地文件，省掉一次下载、也不怕 rkey 过期。
_ATTACH_PATH_RE = re.compile(r"\[Image Attachment(?:[^\]]*?)path ([^\]]+?)\]")


def _cache_put(key: str, value: str) -> None:
    if key in _caption_cache:
        return
    _caption_cache[key] = value
    _cache_order.append(key)
    while len(_cache_order) > CACHE_MAX:
        old = _cache_order.pop(0)
        _caption_cache.pop(old, None)


def _prepare_for_vision(raw: bytes) -> tuple[bytes, int]:
    """把 QQ 图片字节变成视觉模型能吃的 JPEG，返回 (jpeg 字节, 用了几帧)。

    必须自己做，原因有两个：
      - 视觉模型对 GIF 直接报 400「图片输入格式/解析错误」
      - 框架的 compress_image 遇到 GIF 原样返回，等于没处理

    动图抽 GIF_FRAMES 帧拼成方阵，让模型能读出动作；静图只缩尺寸。
    """
    im = PILImage.open(io.BytesIO(raw))
    n_frames = getattr(im, "n_frames", 1)

    def _fit(frame, side):
        f = frame.convert("RGB")
        w, h = f.size
        scale = min(1.0, side / max(w, h))
        if scale < 1.0:
            f = f.resize((max(int(w * scale), 1), max(int(h * scale), 1)), PILImage.LANCZOS)
        return f

    def _jpeg(frame) -> bytes:
        buf = io.BytesIO()
        frame.save(buf, format="JPEG", quality=82)
        return buf.getvalue()

    # 静图，或明确要求单帧
    if n_frames <= 1 or GIF_FRAMES <= 1:
        im.seek(0)
        return _jpeg(_fit(im.copy(), COMPRESS_SIZE)), 1

    count = min(GIF_FRAMES, n_frames)
    # 等距取帧：首尾都要，中间均分
    idx = [int(i * (n_frames - 1) / (count - 1)) for i in range(count)]
    frames = []
    for i in idx:
        im.seek(i)
        frames.append(im.copy())

    cols = 2 if count <= 4 else 3
    rows = (count + cols - 1) // cols
    cell = max(COMPRESS_SIZE // cols, 128)
    grid = PILImage.new("RGB", (cell * cols, cell * rows), (255, 255, 255))
    for k, f in enumerate(frames):
        small = _fit(f, cell)
        # 居中贴，避免不同宽高比的帧顶在角上
        ox = (k % cols) * cell + (cell - small.size[0]) // 2
        oy = (k // cols) * cell + (cell - small.size[1]) // 2
        grid.paste(small, (ox, oy))
    return _jpeg(grid), count


def _framework_failed(req) -> bool:
    """框架尝试转述当前消息的图片但失败了（几乎总是因为那是张动图）。"""
    for part in getattr(req, "extra_user_content_parts", None) or []:
        if _FRAMEWORK_FAIL_MARKER in (getattr(part, "text", "") or ""):
            return True
    return False


def _file_key(path: str) -> str:
    """本地图片的缓存键 = 内容 md5。

    框架落盘的临时文件名每条消息都不一样，用文件名做键等于永不命中；
    群友重复刷同一个表情包却是最常见的情况，所以按内容算。
    """
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"md5:{h.hexdigest()}"


def _framework_image_paths(req) -> list[str]:
    """从框架留下的 [Image Attachment: path X] 里抠出本地文件路径。

    这是兜底动图时最好的图片来源：框架收消息时已经把图落到本地了
    （astr_main_agent 里 comp.convert_to_file_path()），这条标记即使
    转述失败也还留着。用本地文件比回头下载 QQ 的 URL 强得多 ——
    不花网络时间，也不受 rkey 过期影响。
    """
    paths: list[str] = []
    for part in getattr(req, "extra_user_content_parts", None) or []:
        text = getattr(part, "text", "") or ""
        for match in _ATTACH_PATH_RE.finditer(text):
            p = match.group(1).strip()
            if p and os.path.exists(p):
                paths.append(p)
    return paths


def _already_has_image_context(req) -> bool:
    """这一轮框架是否已经把图片处理好了。

    三种情况：
      - extra_user_content_parts 里有 <image_caption> / [Image Attachment] -> 成了，让路
      - req.image_urls 还非空 -> 框架待会儿自己转述，或主模型能直接看图，让路
      - 只有 [Image Captioning Failed] -> 失败了（动图必然走到这里），**不让路**
    """
    if getattr(req, "image_urls", None):
        return True
    for part in getattr(req, "extra_user_content_parts", None) or []:
        text = getattr(part, "text", "") or ""
        if any(m in text for m in _FRAMEWORK_OK_MARKERS):
            return True
    return False


def _pick_images(
    messages: list,
    self_id: str,
    cur_msg_id: str,
    include_current: bool = False,
) -> list[dict]:
    """从群历史里挑出值得转述的图片，返回 [{file, url, who, age}]，新的在前。

    messages 是 OneBot 返回的按时间升序列表。

    include_current=True 时连当前这条消息一起看 —— 只在框架转述失败
    （动图）的场合才这样，正常情况下当前消息是框架的地盘，别抢。
    """
    now = time.time()
    picked: list[dict] = []
    # 从最新往旧走，只看 LOOKBACK 条
    for msg in reversed(messages[-LOOKBACK:] if LOOKBACK > 0 else messages):
        mid = str(msg.get("message_id", ""))
        is_current = bool(cur_msg_id) and mid == cur_msg_id
        # 当前这条消息交给框架处理，不重复
        if is_current and not include_current:
            continue
        uid = str(msg.get("user_id", ""))
        if SKIP_SELF and self_id and uid == self_id:
            continue
        age = now - float(msg.get("time", 0) or 0)
        if MAX_AGE > 0 and age > MAX_AGE:
            continue

        sender = msg.get("sender") or {}
        who = sender.get("card") or sender.get("nickname") or uid or "群友"

        for seg in msg.get("message") or []:
            if seg.get("type") != "image":
                continue
            data = seg.get("data") or {}
            url = data.get("url") or ""
            if not url:
                continue
            fid = data.get("file") or hashlib.md5(url.encode()).hexdigest()
            picked.append(
                {
                    "file": str(fid),
                    "url": url,
                    "who": str(who).strip(),
                    "age": int(age),
                    "summary": (data.get("summary") or "").strip(),
                    "current": is_current,
                }
            )
            if len(picked) >= MAX_IMAGES:
                return picked
    return picked


def _describe_age(sec: int) -> str:
    if sec < 60:
        return f"{sec} 秒前"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    return f"{sec // 3600} 小时前"


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[imgctx] 已加载：开关=%s 回看=%d条 每轮最多=%d张 时效=%ds "
            "预算=%.0fs 动图抽帧=%d 缩图=%d 跳过自己=%s",
            "开" if ENABLED else "关",
            LOOKBACK,
            MAX_IMAGES,
            MAX_AGE,
            BUDGET,
            GIF_FRAMES,
            COMPRESS_SIZE,
            SKIP_SELF,
        )

    # ------------------------------------------------ 取转述用的 provider

    def _caption_provider_id(self) -> str:
        if PROVIDER_ID:
            return PROVIDER_ID
        try:
            cfg = self.context.get_config() or {}
            return (
                cfg.get("provider_settings", {}).get(
                    "default_image_caption_provider_id", ""
                )
                or ""
            )
        except Exception:
            return ""

    # ------------------------------------------------ 下载 + 转述

    async def _fetch(self, session: aiohttp.ClientSession, url: str) -> bytes | None:
        """下载图片，只返回字节。

        不落原始文件：真正需要落盘的是给视觉模型的那张 JPEG，
        原始 GIF 可能有 2MB（实测群里有 49 帧 2083KB 的），没必要写小硬盘。
        """
        async with session.get(url) as resp:
            if resp.status != 200:
                # 常见原因是 URL 里的 rkey 过期（实测约 18 分钟就变 400）
                logger.warning("[imgctx] 下载图片失败 HTTP %s", resp.status)
                return None
            body = await resp.read()
            return body if len(body) >= 64 else None

    async def _caption(self, provider_id: str, items: list[dict]) -> list[dict]:
        """给每张图配上文字描述，命中缓存的不再请求。就地写入 item['caption']。

        刻意串行：智谱那边并发两张会直接 429（实测 code 1302，第二张秒失败）。
        缩图后单张约 2~5s，MAX_IMAGES=2 也就多等一会儿，不值得为此冒 429 的风险。
        """
        todo = [it for it in items if it["file"] not in _caption_cache]
        for it in items:
            if it["file"] in _caption_cache:
                it["caption"] = _caption_cache[it["file"]]

        if not todo:
            return items

        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for it in todo:
                raw = None
                # 本地已经有这张图（框架落的盘）就直接读，省一次下载
                local = it.get("local")
                if local:
                    try:
                        with open(local, "rb") as f:
                            raw = f.read()
                    except OSError as e:
                        logger.warning("[imgctx] 读本地图失败: %s", e)
                if raw is None and it.get("url"):
                    try:
                        raw = await self._fetch(session, it["url"])
                    except Exception as e:
                        logger.warning("[imgctx] 抓图异常: %s", e)
                        continue
                if not raw:
                    continue

                # 抽帧 / 缩图。CPU 活儿放线程里，别卡住事件循环 ——
                # 127 帧的 GIF 解码不是免费的，而这台机器只有 2 核。
                try:
                    jpeg, used = await asyncio.to_thread(_prepare_for_vision, raw)
                except Exception as e:
                    logger.warning("[imgctx] 图片预处理失败: %s", e)
                    continue

                os.makedirs(TMP_DIR, exist_ok=True)
                path = os.path.join(TMP_DIR, f"ctx_{it['file'][:40]}.jpg")
                try:
                    with open(path, "wb") as f:
                        f.write(jpeg)
                except OSError as e:
                    logger.warning("[imgctx] 写临时图失败: %s", e)
                    continue

                prompt = (
                    CAPTION_PROMPT_GIF.format(n=used) if used > 1 else CAPTION_PROMPT
                )
                try:
                    resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        image_urls=[path],
                    )
                    cap = (
                        getattr(resp, "completion_text", None)
                        or getattr(resp, "_completion_text", None)
                        or ""
                    ).strip()
                except Exception as e:
                    logger.warning("[imgctx] 视觉模型转述失败: %s", e)
                    cap = ""
                finally:
                    # 转述完就删，别在小硬盘上堆图
                    try:
                        os.remove(path)
                    except OSError:
                        pass

                if cap:
                    cap = " ".join(cap.split())[:120]
                    it["caption"] = cap
                    it["animated"] = used > 1
                    _cache_put(it["file"], cap)
        return items

    # ------------------------------------------------ 主钩子

    @filter.on_llm_request()
    async def attach_recent_images(self, event: AstrMessageEvent, req) -> None:
        """把最近几条消息里的图片转成文字，附到这次 LLM 请求上。"""
        if not ENABLED:
            return
        try:
            # 框架已经把图片处理好了（本条消息自带图 / 引用了带图的消息）——别重复。
            # 注意：框架转述**失败**不算处理好，那种情况恰恰要我们兜底。
            rescue = _framework_failed(req)
            if not rescue and _already_has_image_context(req):
                return
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            group_id = event.get_group_id()
            bot = getattr(event, "bot", None)
            if not group_id or bot is None:
                return
            provider_id = self._caption_provider_id()
            if not provider_id:
                logger.warning("[imgctx] 没有可用的图片转述 provider，跳过")
                return

            # 整个流程一个总预算：宁可这次不看图，也不能让回复卡住
            await asyncio.wait_for(
                self._run(event, req, bot, group_id, provider_id, rescue),
                timeout=BUDGET,
            )
        except asyncio.TimeoutError:
            logger.warning("[imgctx] 超过 %.0fs 预算，本轮放弃图片上下文", BUDGET)
        except BaseException as e:
            # 异常的 str() 常常是空的（框架里 ActionFailed 之类就这样），
            # 只打 %s 会得到「钩子异常: 」这种废日志。类型名必须带上。
            logger.error(
                "[imgctx] 钩子异常 %s: %s", type(e).__name__, e or "(无消息)"
            )

    async def _run(self, event, req, bot, group_id, provider_id, rescue=False) -> None:
        # 兜底路径（框架转述当前消息的图失败，基本都是动图）：
        # 框架已经把图落到本地了，直接读文件就行 —— 不查历史、不发网络请求，
        # 也就不会因为 OneBot 抽风或 rkey 过期而白白丢掉这张图。
        if rescue:
            local_paths = _framework_image_paths(req)
            if local_paths:
                who = ""
                try:
                    who = (event.get_sender_name() or "").strip()
                except Exception:
                    pass
                items = []
                for p in local_paths[:MAX_IMAGES]:
                    # 用文件内容的 md5 当缓存键，而不是框架那个每条消息都不同的
                    # 临时文件名 —— 群友爱重复刷同一个表情包，内容哈希才能命中。
                    try:
                        key = await asyncio.to_thread(_file_key, p)
                    except OSError:
                        continue
                    items.append(
                        {
                            "file": key,
                            "url": "",
                            "local": p,
                            "who": who or "群友",
                            "age": 0,
                            "summary": "",
                            "current": True,
                        }
                    )
                if items:
                    await self._emit(req, provider_id, items, rescue=True)
                    return

        # 多取两条：当前消息本身会占一条，可能还有通知类消息。
        # self_id 必须显式传：aiocqhttp 的反向 WS 只在
        #   ① 传了 self_id ② 还在 websocket 上下文里 ③ 全局只有一个连接
        # 这三种情况下才知道该走哪条连接（api_impl.py:129）。这里是
        # on_llm_request 钩子，早就离开了 websocket 上下文，所以一旦接上
        # 第二个连接（第二个 QQ 号、或跑端到端测试用的假 napcat）就会抛
        # ApiNotAvailable —— 实测刷了 25 条这个异常。
        _sid = str(getattr(event.message_obj, "self_id", "") or "")
        _kw = {"self_id": int(_sid)} if _sid.isdigit() else {}
        history = await bot.get_group_msg_history(
            group_id=int(group_id), count=LOOKBACK + 2, **_kw
        )
        # aiocqhttp 的 _handle_api_result 已经剥掉了 OneBot 的 data 外层，
        # 所以正常拿到的就是 {"messages": [...]}。但为了不被某个适配器版本
        # 的差异搞挂，两种形状都认。
        data = history or {}
        if isinstance(data, dict) and "messages" not in data and isinstance(data.get("data"), dict):
            data = data["data"]
        messages = (data or {}).get("messages") or []
        if not messages:
            return

        self_id = str(getattr(event.message_obj, "self_id", "") or "")
        cur_id = str(getattr(event.message_obj, "message_id", "") or "")
        items = _pick_images(messages, self_id, cur_id, include_current=rescue)
        if not items:
            return
        await self._emit(req, provider_id, items, rescue=rescue)

    async def _emit(self, req, provider_id, items, rescue=False) -> None:
        """转述这批图并把结果注入请求。"""
        cached = sum(1 for it in items if it["file"] in _caption_cache)
        await self._caption(provider_id, items)

        described = [it for it in items if it.get("caption")]
        if not described:
            return

        # 兜底成功了，就得把框架留下的「转述失败」痕迹擦掉。
        # 不擦的话请求里会同时出现「[Image Captioning Failed]」和我们的描述，
        # 模型多半听前者的 —— 线上那几句「这图我看不清 / 加载不出来」
        # 就是这么来的。
        # 用原地删（del）而不是重新赋值：runner 每次调 provider 时都会重读
        # self.req.extra_user_content_parts（tool_loop_agent_runner:508/1423），
        # 两种写法都能生效，但原地改对「谁先拿到了这个 list 的引用」不敏感。
        if rescue:
            parts = req.extra_user_content_parts
            if parts:
                for i in range(len(parts) - 1, -1, -1):
                    if _FRAMEWORK_FAIL_MARKER in (getattr(parts[i], "text", "") or ""):
                        del parts[i]

        # 兜底当前消息的图（框架转述失败），和「回顾历史图」是两件事，
        # 说辞必须不一样：前者是「用户刚发的这张」，后者是「之前聊到的」。
        if rescue and any(it.get("current") for it in described):
            lines = [
                "下面是这条消息里图片的内容（图片本身你看不了，这是转述）。"
                "请当作你已经看到了这张图，直接就图片内容回应。"
            ]
        else:
            lines = [
                "以下是这个群最近出现过的图片内容（不是当前这条消息自带的图片），"
                "供你理解大家在聊什么。如果和当前话题无关就忽略，别刻意提起。"
            ]
        for it in described:
            who = it["who"]
            # 动图和静图要区分：说「表情包」时模型的语气会自然不同，
            # 而且它得知道那是个会动的东西才不会把描述当成静态画面来聊。
            if it.get("animated") or "表情" in (it.get("summary") or ""):
                tag = "表情包（动图）"
            else:
                tag = "图片"
            when = "刚刚" if it.get("current") else _describe_age(it["age"])
            lines.append(f"- {when} {who} 发了一张{tag}：{it['caption']}")

        req.extra_user_content_parts.append(
            TextPart(text="<recent_image_context>\n" + "\n".join(lines) + "\n</recent_image_context>")
        )
        logger.info(
            "[imgctx] 已附加 %d 张图片%s（缓存命中 %d）：%s",
            len(described),
            "（兜底当前消息的动图）" if rescue else "的上下文",
            cached,
            " | ".join(f"{it['who']}:{it['caption'][:24]}" for it in described),
        )

    # ------------------------------------------------ 指令

    @filter.command("图片上下文")
    async def cmd_status(self, event: AstrMessageEvent):
        """/图片上下文 —— 查看当前策略与缓存情况。"""
        pid = self._caption_provider_id() or "（未配置）"
        yield event.plain_result(
            f"图片上下文：{'开' if ENABLED else '关'}\n"
            f"回看最近 {LOOKBACK} 条消息，每轮最多转述 {MAX_IMAGES} 张\n"
            f"图片时效 {MAX_AGE}s，总预算 {BUDGET:.0f}s\n"
            f"动图抽 {GIF_FRAMES} 帧拼图后识别\n"
            f"转述模型：{pid}\n"
            f"已缓存 {len(_caption_cache)} 张图的描述"
        )
