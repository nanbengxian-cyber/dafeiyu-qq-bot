# -*- coding: utf-8 -*-
"""dsh-steal -- 偷表情包：识图理解意思，只偷「群友反复发、能看懂含义」的表情包。

---------------------------------------------------------------------------
用户钦定（2026-09-09 原话）：「不能是光偷吧，要偷那些可以理解意思的，
不是有识图模型吗直接让它识别识别出意思就可以偷了，偷那些多发的」。

也就是三步：
  ① 统计：群里每张图按 QQ file id 计数（零成本，全量钩子实时数）
  ② 识别：同一张图出现 ≥ N 次（默认 3），才花钱交给视觉模型读一次意思；
           识别不出/内容不宜公开（色情/暴力/政治等）的图不偷
  ③ 入库：能看懂含义的表达包保存到 data/stickerthief/ 并与含义一起记账
之后「来张表情包/来张图/发个表情包」以及命令 /表情包 就从库里随机发一张。

为什么「多发」才偷：随手发的照片、截图可能只出现一次，不是「群里的表情包」；
反复出现的图（转发/引用/再发）才是群友认可用法的梗图。file id 稳定（同一张
图反复转发时 QQ 给同一个 file），零成本去重/计数，错了也只是多记了一次数字。

为什么「能看懂含义」才偷：用户点名要把关 —— 识别不出来的怪图、
内容不宜公开的图（脏话/色情/暴力/时政）不入库。识别 prompt 显式要求输出
「一句话说清图的含义/适合表达什么心情」，模型回不了就放弃这张。

成本控制（照 dsh-imgctx 的教训）：
  · 识别只在计数达到阈值的**那一刻**触发一次，同一张图绝不重复花钱；
  · 下载/抽帧/识别全放后台任务，绝不拖慢群聊消息处理；
  · 总超时 20s，超时放弃（宁可少偷一张，不能卡群）；
  · 识别用视觉 provider（默认框架的 default_image_caption_provider_id，
    即 zhipu-vision / glm-4.6v-flash，dsh-imgctx 已实测可用）。

任何异常静默放行 —— 偷图是锦上添花，绝不能影响群聊本体。

旋钮（env）：
  DSH_STEAL                  总开关（默认 1=开）
  DSH_STEAL_GROUPS           作用群（默认 100000001），逗号分隔
  DSH_STEAL_MIN_COUNT        多发阈值（默认 3 次）
  DSH_STEAL_MAX_STORED       库存上限（默认 200 张，超出按最久未用淘汰）
  DSH_STEAL_PROVIDER         识图 provider（空=用 default_image_caption_provider_id）
  DSH_STEAL_HOME             库存目录（默认 /AstrBot/data/stickerthief）

命令：/表情包（随机发一张偷来的）、/表情库（看库）
"""

import asyncio
import hashlib
import io
import os
import random
import re
import sqlite3
import time

import aiohttp
import PIL.Image as PILImage

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.core import logger
from astrbot.core.platform.message_type import MessageType

ENABLED = os.environ.get("DSH_STEAL", "1").lower() not in {"0", "false", "off"}
GROUPS = {
    g.strip()
    for g in os.environ.get("DSH_STEAL_GROUPS", "100000001").split(",")
    if g.strip()
}
MIN_COUNT = max(2, int(os.environ.get("DSH_STEAL_MIN_COUNT", "3")))
MAX_STORED = max(10, int(os.environ.get("DSH_STEAL_MAX_STORED", "200")))
PROVIDER_ID = os.environ.get("DSH_STEAL_PROVIDER", "").strip()
HOME = os.environ.get("DSH_STEAL_HOME", "/AstrBot/data/stickerthief")
DB_PATH = os.path.join(HOME, "stickerthief.db")
# 单张图下载超时
FETCH_TIMEOUT = float(os.environ.get("DSH_STEAL_FETCH_TIMEOUT", "8"))
# 单张图识别总超时（下载+抽帧+识别）
BUDGET = float(os.environ.get("DSH_STEAL_BUDGET", "20"))
# 识图前最长边（照 imgctx：768 比原图快 5 倍且质量可接受）
COMPRESS_SIZE = int(os.environ.get("DSH_STEAL_COMPRESS", "768"))
# GIF 抽几帧拼一张（表情包的意思全在动作里，imgctx 实测 4 帧最佳）
GIF_FRAMES = int(os.environ.get("DSH_STEAL_GIF_FRAMES", "4"))

# 识别提示词：要它说「这图表达什么含义」，不是描述画面
CAPTION_PROMPT = os.environ.get(
    "DSH_STEAL_PROMPT",
    "这是群里常见的一张表情包/梗图。用中文30字内说它表达什么含义、"
    "适合什么场合用。若内容不适宜公开传播（色情/暴力/政治/辱骂），"
    "只回两个字：不宜。",
)
CAPTION_PROMPT_GIF = os.environ.get(
    "DSH_STEAL_PROMPT_GIF",
    "这是同一个动图的{n}帧（按时间从左到右）。这是群里常见的一张GIF表情包。"
    "用中文30字内说它表达什么含义、适合什么场合用。"
    "若内容不适宜公开传播（色情/暴力/政治/辱骂），只回两个字：不宜。",
)

_stat = {"seen": 0, "new": 0, "count_hit": 0, "skip_self": 0, "recheck": 0,
         "cap_ok": 0, "cap_bad": 0, "cap_unfit": 0, "stored": 0,
         "evicted": 0, "fail": 0, "timeout": 0}

# 进程内防重：正在识别的 file id（避免同一张图计数达阈值的瞬间并发触发两次）
_inflight: set[str] = set()


def _db() -> sqlite3.Connection:
    os.makedirs(HOME, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=3)
    con.execute(
        "CREATE TABLE IF NOT EXISTS stickers ("
        " fid TEXT PRIMARY KEY,"
        " path TEXT,"
        " caption TEXT,"
        " count INTEGER NOT NULL DEFAULT 0,"
        " first_seen REAL,"
        " last_seen REAL,"
        " last_use REAL"
        ")"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS counts ("
        " fid TEXT PRIMARY KEY,"
        " url TEXT,"
        " local TEXT,"
        " count INTEGER NOT NULL DEFAULT 0,"
        " first_seen REAL,"
        " last_seen REAL"
        ")"
    )
    return con


def _count_bump(fid: str, url: str, local: str = "") -> int:
    """记录一次出现，返回累计次数。纯同步、零网络，不拖群聊。

    local 是这条消息的本地落盘路径 —— 保存下来，识别阶段优先读它
    （不花钱、不怕 rkey 过期）。路径每次不同但无妨：只作下载兜底。
    """
    try:
        con = _db()
        try:
            now = time.time()
            row = con.execute(
                "SELECT count FROM counts WHERE fid=?", (fid,)
            ).fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO counts (fid,url,count,first_seen,last_seen)"
                    " VALUES (?,?,1,?,?)", (fid, url, now, now)
                )
                n = 1
            else:
                n = row[0] + 1
                con.execute(
                    "UPDATE counts SET count=?,url=?,last_seen=? WHERE fid=?",
                    (n, url, now, fid),
                )
            con.execute(
                "UPDATE counts SET local=? WHERE fid=?",
                (local or "", fid),
            )
            con.commit()
            return n
        finally:
            con.close()
    except BaseException:
        return 0


def _already_stored(fid: str) -> bool:
    """这张图是不是已经偷进库了（避免每次达阈值都重识别一遍）。"""
    try:
        con = _db()
        try:
            return (
                con.execute(
                    "SELECT 1 FROM stickers WHERE fid=?", (fid,)
                ).fetchone()
                is not None
            )
        finally:
            con.close()
    except BaseException:
        return False


def _image_file_key(path: str) -> str:
    """本地图片的稳定键 = 内容 md5。

    框架把 QQ 图片落盘到 data/temp/ 时，临时文件名每条消息都不同
    （media_image-xxx.png），拿文件名当 fid 等于永不命中 —— 同一张反复发
    永远计不成「多发」。内容哈希才是稳的（imgctx 同款教训）。
    """
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return ""


def _extract_images(event: AstrMessageEvent) -> list[dict]:
    """从一条群消息里抠出图片。返回 [] 或 [{fid,url,local}]。

    实测 astrbot Image 组件：type 是 ComponentType.Image 枚举，
    file 被框架换成了本地落盘路径（data/temp/media_image-xxx.png），
    url 属性保留 QQ 图床地址但 rkey 约 18 分钟过期。所以：
      · fid 必须用**内容 md5**（稳定标识同一张图）；
      · 下载优先读本地落盘文件（不花钱、不怕过期），url 兜底。
    兼容三种形状：组件对象 / dict 段 / 其他。
    """
    out: list[dict] = []
    try:
        msg = event.message_obj
        segs = getattr(msg, "message", None) or []
        for seg in segs:
            if isinstance(seg, dict):
                ty = str(seg.get("type", "")).lower()
                if "image" not in ty:
                    continue
                data = seg.get("data") or {}
                url = data.get("url") or ""
                path = data.get("file") or data.get("path") or ""
            else:
                ty = str(getattr(seg, "type", "")).lower()
                if "image" not in ty:
                    continue
                url = getattr(seg, "url", None) or ""
                path = getattr(seg, "file", None) or getattr(seg, "path", None) or ""
                if not url:
                    try:
                        d = seg.toDict().get("data") or {}
                        url = d.get("url") or ""
                        if not path:
                            path = d.get("file") or d.get("path") or ""
                    except BaseException:
                        pass
            # 本地落盘了：内容 md5 当 fid（唯一稳定键）
            local = path if (path and os.path.exists(path)) else ""
            fid = ""
            if local:
                fid = _image_file_key(local)
            if not fid and url:
                # 没有本地文件：QQ file 名（含固定部分）× url 双重退化键
                # （rkey 在 url 里，绝不能整条 url 当键）
                m = re.search(r"([0-9a-zA-Z-]+)\.[a-z]{3,5}$", path or "")
                base = m.group(1) if m else ""
                fid = "kb_" + hashlib.md5((base + url.split("&")[0][:120]).encode()).hexdigest()[:24]
            if not fid:
                continue
            out.append({"fid": fid, "url": url, "local": local})
    except BaseException:
        pass
    return out


def _prepare_for_vision(raw: bytes, n_frames: int) -> tuple[bytes, int]:
    """转述前的预处理：GIF 抽 n 帧拼一张，否则缩到 COMPRESS_SIZE。照 imgctx。"""
    buf = io.BytesIO(raw)
    buf.seek(0)
    img = PILImage.open(buf)
    if getattr(img, "is_animated", False) and n_frames > 1:
        frames = []
        for i in range(n_frames):
            try:
                img.seek(i)
                frame = img.convert("RGB")
                frame.thumbnail((COMPRESS_SIZE, COMPRESS_SIZE))
                frames.append(frame)
            except BaseException:
                break
        if len(frames) > 1:
            w = max(f.width for f in frames)
            h = sum(f.height for f in frames) + 4 * (len(frames) - 1)
            canvas = PILImage.new("RGB", (w, h), (255, 255, 255))
            y = 0
            for fr in frames:
                canvas.paste(fr, (0, y))
                y += fr.height + 4
            out = io.BytesIO()
            canvas.save(out, format="JPEG", quality=82)
            return out.getvalue(), len(frames)
    rgb = img.convert("RGB")
    rgb.thumbnail((COMPRESS_SIZE, COMPRESS_SIZE))
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=82)
    return out.getvalue(), 1


def _raw_signature(raw: bytes) -> str:
    """原图内容哈希：静态按字节，动图按前 64KB（避免整图哈希每帧动都变）。"""
    if b"GIF89a" in raw[:8] or b"GIF87a" in raw[:8]:
        return "gif_" + hashlib.md5(raw[:65536]).hexdigest()[:16]
    return hashlib.md5(raw).hexdigest()[:16]


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._lock = asyncio.Lock()
        logger.info(
            "[steal] 已加载：%s 群=%s｜多发阈值=%d次 库存上限=%d 抽帧=%d 缩图=%d",
            "开" if ENABLED else "关",
            "，".join(sorted(GROUPS)) or "无",
            MIN_COUNT, MAX_STORED, GIF_FRAMES, COMPRESS_SIZE,
        )

    def _in_group(self, event: AstrMessageEvent) -> bool:
        if not GROUPS:
            return True
        try:
            g = str(event.get_group_id() or "")
            return not g or g in GROUPS
        except BaseException:
            return True

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

    # ------------------------------------------------ 全量钩子：统计出现次数

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def observe(self, event: AstrMessageEvent) -> None:
        """每条群消息都看一眼图片：计数。识别放后台，不拖消息。"""
        if not ENABLED:
            return
        try:
            if not self._in_group(event):
                return
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            uid = str(event.get_sender_id() or "")
            sid = str(event.get_self_id() or "")
            if uid and sid and uid == sid:
                return  # 机器人自己的图（自己画的）不偷
            imgs = _extract_images(event)
            if not imgs:
                return
            _stat["seen"] += 1
            for it in imgs:
                n = _count_bump(it["fid"], it["url"], it.get("local") or "")
                if n == 1:
                    _stat["new"] += 1
                if (
                    n >= MIN_COUNT
                    and it["fid"] not in _inflight
                    and not _already_stored(it["fid"])
                ):
                    _stat["count_hit"] += 1
                    _inflight.add(it["fid"])
                    asyncio.create_task(
                        self._caption_and_store(
                            it["fid"], it["url"], it.get("local") or ""
                        )
                    )
        except BaseException as exc:
            logger.debug("[steal] 观察失败：%s", exc)

    # ------------------------------------------------ 识别 + 入库（后台任务）

    async def _caption_and_store(self, fid: str, url: str, local: str = "") -> None:
        try:
            await asyncio.wait_for(
                self._run_caption(fid, url, local), timeout=BUDGET + 5
            )
        except asyncio.TimeoutError:
            _stat["timeout"] += 1
            logger.warning("[steal] 识别超时，放弃偷这张：%s", fid[:20])
        except BaseException as exc:
            _stat["fail"] += 1
            logger.warning("[steal] 识别失败：%s（%s）", fid[:20], exc)
        finally:
            _inflight.discard(fid)

    async def _run_caption(self, fid: str, url: str, local: str = "") -> None:
        provider_id = self._caption_provider_id()
        if not provider_id:
            logger.warning("[steal] 没有可用视觉 provider，跳过")
            raise RuntimeError("no vision provider")

        tmp = None
        try:
            # 本地有落盘文件优先读（不花钱、不怕 rkey 过期）；没有才下载 url
            raw = None
            if local and os.path.exists(local):
                try:
                    with open(local, "rb") as f:
                        raw = f.read()
                except OSError:
                    raw = None
                if raw and len(raw) < 64:
                    raw = None
            if raw is None:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
                ) as session:
                    if url:
                        try:
                            async with session.get(url) as resp:
                                if resp.status == 200:
                                    body = await resp.read()
                                    if len(body) >= 64:
                                        raw = body
                        except BaseException:
                            raw = None
            if not raw:
                raise RuntimeError("download failed")

            try:
                jpeg, used = await asyncio.to_thread(
                    _prepare_for_vision, raw, GIF_FRAMES
                )
            except BaseException as exc:
                raise RuntimeError("prepare failed: %s" % exc) from exc

            os.makedirs(HOME, exist_ok=True)
            tmp = os.path.join(HOME, "tmp_%s.jpg" % fid[:24])
            with open(tmp, "wb") as f:
                f.write(jpeg)
            try:
                prompt = (
                    CAPTION_PROMPT_GIF.format(n=used)
                    if used > 1
                    else CAPTION_PROMPT
                )
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    image_urls=[tmp],
                )
                cap = (
                    getattr(resp, "completion_text", None)
                    or getattr(resp, "_completion_text", None)
                    or ""
                ).strip()
            except BaseException as exc:
                raise RuntimeError("caption failed: %s" % exc) from exc

            if not cap:
                _stat["cap_bad"] += 1
                logger.info("[steal] 识别不出含义，不偷：%s", fid[:20])
                return
            cap = " ".join(cap.split())[:120]
            if "不宜" in cap:
                _stat["cap_unfit"] += 1
                logger.info("[steal] 内容不适宜公开，不偷：%s (%s)",
                            fid[:20], cap[:20])
                return

            # 以原图落库存（静态存 JPEG 缩图；GIF 存原文件保动效）
            sig = _raw_signature(raw)
            stored_name = None
            if raw[:6] in (b"GIF89a", b"GIF87a"):
                stored_name = "s_%s_%s.gif" % (fid[:16], sig)
                with open(os.path.join(HOME, stored_name), "wb") as f:
                    f.write(raw)
            else:
                stored_name = "s_%s_%s.jpg" % (fid[:16], sig)
                with open(os.path.join(HOME, stored_name), "wb") as f:
                    f.write(jpeg)

            self._store(fid, stored_name, cap)
            _stat["cap_ok"] += 1
            logger.info("[steal] 偷到一张：%s｜含义=%s", stored_name, cap)
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _store(self, fid: str, stored_name: str, caption: str) -> None:
        """写入库存。超出上限按 last_use 最久未用的淘汰。"""
        try:
            con = _db()
            try:
                now = time.time()
                con.execute(
                    "INSERT OR REPLACE INTO stickers"
                    " (fid,path,caption,count,first_seen,last_seen,last_use)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (fid, stored_name, caption, MIN_COUNT, now, now, now),
                )
                rows = con.execute(
                    "SELECT path FROM stickers ORDER BY last_use ASC"
                ).fetchall()
                if len(rows) > MAX_STORED:
                    for (path,) in rows[: len(rows) - MAX_STORED]:
                        try:
                            os.remove(os.path.join(HOME, path))
                        except OSError:
                            pass
                        con.execute(
                            "DELETE FROM stickers WHERE path=?", (path,)
                        )
                        _stat["evicted"] += 1
                con.commit()
                _stat["stored"] += 1
            finally:
                con.close()
        except BaseException:
            pass

    # ------------------------------------------------ 用：发一张偷来的表情包

    def _pick(self) -> tuple[str, str] | None:
        """随机挑一张库存，返回 (path, caption)，顺便更新 last_use。"""
        try:
            con = _db()
            try:
                row = con.execute(
                    "SELECT path, caption FROM stickers ORDER BY "
                    "RANDOM() LIMIT 1"
                ).fetchone()
                if not row:
                    return None
                con.execute(
                    "UPDATE stickers SET last_use=? WHERE path=?",
                    (time.time(), row[0]),
                )
                con.commit()
                return (row[0], row[1] or "")
            finally:
                con.close()
        except BaseException:
            return None

    @filter.command("表情包")
    async def cmd_sticker(self, event: AstrMessageEvent):
        """随机发一张偷来的表情包（图文分开发，混一条会被丢弃）。"""
        p = self._pick()
        if not p:
            yield event.plain_result("还没偷到表情包，群里多发点梗图让我学学[贴纸:装可怜]")
            return
        path, caption = p
        # 图单独一条，含义文字放底下另一条（照 dsh-welcome 实测：图文混一条丢图）
        yield event.chain_result(
            MessageChain([Image.fromFileSystem(os.path.join(HOME, path))])
        )
        if caption:
            yield event.plain_result("这表情懂的人是懂.jpg（%s）" % caption)

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def maybe_serve(self, event: AstrMessageEvent) -> None:
        """群友说「来张表情包/来张图/发个表情」且恰好 @ 了机器人时，发一张。

        这是一条低门槛的辅助路径（command 之外的自然语言触发），
        只在「明确要图 + @ 了机器人 + 库存非空」时兜底发一张，
        不抢主模型的正常回复（主模型被 @ 照样会走完整管道）。
        """
        if not ENABLED:
            return
        try:
            if not self._in_group(event):
                return
            if not bool(getattr(event, "is_at_or_wake_command", False)):
                return
            text = (event.get_message_str() or "").strip()
            if not re.search(r"来[张个]?(表情|图)|发[张个]?(表情|图)|表情包", text):
                return
            p = self._pick()
            if not p:
                return
            path, caption = p
            # 图单独一条（照 dsh-welcome：图文混一条会被丢弃）
            try:
                await event.send(
                    MessageChain([Image.fromFileSystem(os.path.join(HOME, path))])
                )
            except BaseException as e:
                logger.warning("[steal] 表情包发送失败 %s: %s", path, e)
                return
            if caption:
                await event.send(
                    MessageChain([Plain("这表情懂的人是懂.jpg（%s）" % caption)])
                )
        except BaseException:
            pass

    @filter.command("表情库")
    async def cmd_status(self, event: AstrMessageEvent):
        try:
            con = _db()
            try:
                total = con.execute(
                    "SELECT COUNT(*) FROM stickers"
                ).fetchone()[0]
                counts = con.execute(
                    "SELECT COUNT(*), COALESCE(SUM(count),0) FROM counts"
                ).fetchone()
            finally:
                con.close()
        except BaseException:
            total, counts = 0, (0, 0)
        yield event.plain_result(
            "偷表情包：%s（阈值 %d 次，库存上限 %d）\n"
            "看过 %d 张图｜新图 %d｜达阈值触发识别 %d（识别成功 %d、"
            "不宜 %d、失败 %d）\n"
            "现有库存 %d 张｜跟踪中的图 %d 张（累计出现 %d 次）｜淘汰 %d"
            % ("开" if ENABLED else "关", MIN_COUNT, MAX_STORED,
               _stat["seen"], _stat["new"], _stat["count_hit"],
               _stat["cap_ok"], _stat["cap_unfit"], _stat["fail"],
               total, counts[0], counts[1], _stat["evicted"])
        )