# dsh-listen —— 「听」群里所有语音与视频里的声音（AssemblyAI 转写）。
#
# 要解决什么
# ----------
# 群里发的语音条，框架只留下 `[Audio Attachment: path X]` 这类路径标记，
# 聊天模型看不到声音内容，只能看到路径，于是回出「我听不了语音」或者瞎猜。
# 本插件把语音转成文字换进上下文，机器人就能听懂群友的语音条。
#
# 为什么挂在「消息收集」而不是「LLM 请求」上
# ------------------------------------------
# 第一版挂在 on_llm_request：只有机器人决定回复（且 dsh-decide 没判潜水
# 掐掉传播）时才跑。群里没人@、decide 判沉默 stop_event 后，语音永远没人听
# —— 日志实证：语音消息到了（[ComponentType.Record]），但插件零日志。
# 现在学 dsh-memory 的做法，用 platform_adapter_type(ALL) 在**每条群消息**进
# 来时就把它带的声音（语音条 / 视频音轨）后台转写进缓存；on_llm_request 只
# 负责把缓存里已听好的结果注入上下文。机器人潜水与否都不影响「听」。
#
# 三个来源，一条链：
#   ① 当前消息直接带的语音条
#   ② 引用消息里的语音（框架的 [Audio Attachment in quoted message] 标记）
#   ③ 视频里的声音（抽音轨转写，补 dsh-video「只看得见画面、听不到声音」）
# 画面描述归 dsh-video，声音描述归本插件，各自追加、互不覆盖。
#
# 为什么选 AssemblyAI 异步（/v2）而不是它的 Sync API
# --------------------------------------------------
# 官方给的 sync.assemblyai.com/transcribe 实测四个姿势全部 404
# （官方 curl 姿势 / octet-stream / multipart / 带 X-AAI-Model header），
# 疑似该功能对当前账号未开放或路由未上线；而 /v2/upload -> /v2/transcript
# -> 轮询整条链路实测 200 通畅，中文（language_code=zh）可用。
#
# 为什么不把整个文件直接 post 给 /v1/speech-to-text 之类：
# AssemblyAI 的转写都走 /v2（upload + transcript），没有免费单请求端点。

import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import time
import uuid

import aiohttp

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record, Video
from astrbot.core import logger
from astrbot.core.platform.message_type import MessageType
from astrbot.core.agent.message import TextPart

# ---------------------------------------------------------------- 配置

ENABLED = os.environ.get("DSH_LISTEN_ENABLE", "1") not in ("0", "false", "False", "")
# AssemblyAI API Key
KEY = os.environ.get("DSH_LISTEN_KEY", "")
BASE = os.environ.get("DSH_LISTEN_BASE", "https://api.assemblyai.com/v2").rstrip("/")
# 转写语言。默认 zh。AssemblyAI 语言码：zh/en/ja/ko/yue 等
LANG = os.environ.get("DSH_LISTEN_LANG", "zh")
# 单次请求超时
HTTP_TIMEOUT = float(os.environ.get("DSH_LISTEN_TIMEOUT", "120"))
# 轮询间隔与最长时间（异步任务一般 2~10s 完成）
POLL_INTERVAL = float(os.environ.get("DSH_LISTEN_POLL", "2.0"))
POLL_MAX = float(os.environ.get("DSH_LISTEN_POLL_MAX", "120"))
# on_llm_request 注入时的预算：宁可这轮不带，也不能让群里等
INJECT_BUDGET = float(os.environ.get("DSH_LISTEN_BUDGET", "15"))
# 抽音轨临时目录
TMP_DIR = os.environ.get("DSH_LISTEN_TMP", "/AstrBot/data/listen")
# 同一文件不重复转写
CACHE_MAX = int(os.environ.get("DSH_LISTEN_CACHE", "300"))
# 并发转写上限（多语音同时来时的礼貌值）
MAX_CONCURRENT = int(os.environ.get("DSH_LISTEN_MAX_CONC", "2"))

# 框架留下的语音附件标记（含引用消息版）
_AUDIO_RE = re.compile(
    r"\[Audio Attachment(?: in quoted message)?:\s*path\s+([^\]]+)\]"
)
# 视频附件标记（引用消息版）
_QUOTED_VIDEO_RE = re.compile(
    r"\[Video Attachment in quoted message:\s*name\s+[^,]+,\s*path\s+([^\]]+)\]"
)

# path -> 转写文本。同一个文件（含同一条语音在收集与 LLM 两条路上）只转一次。
_cache: dict[str, str] = {}
# 收集路径上正在转写的任务：path -> Task，防并发重复
_pending: dict[str, asyncio.Task] = {}
_pending_lock = asyncio.Lock()
# 并发闸
_sem = asyncio.Semaphore(MAX_CONCURRENT)
# 战绩统计
_stat = {"ok": 0, "fail": 0, "sec": 0.0, "last_err": ""}


def _extract_audio_paths(path: str) -> list[str]:
    """把一份音频/视频文件变成可转写的 wav 列表。

    - 纯音频（wav/mp3 等）直接用原路径；
    - 视频（mp4 等）先抽音轨到临时 wav。
    返回 [] 表示这条路没戏（会带日志）。
    """
    os.makedirs(TMP_DIR, exist_ok=True)
    low = (path or "").lower()
    is_video = re.search(r"\.(mp4|mov|mkv|avi|flv|webm|ts|m4v)$", low) is not None
    if not is_video:
        return [path]
    out = os.path.join(TMP_DIR, f"a_{uuid.uuid4().hex[:12]}.wav")
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", path,
        "-vn", "-ac", "1", "-ar", "16000", "-t", "240", out,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 1024:
            logger.warning("[listen] 抽视频音轨失败（%s）：%.120s",
                           os.path.basename(path),
                           r.stderr.decode("utf-8", "replace")[-120:])
            return []
        logger.info("[listen] 抽音轨 OK（%s）", os.path.basename(path))
        return [out]
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("[listen] 抽音轨异常：%s", e)
        return []


async def _transcribe_once(session: aiohttp.ClientSession, wav: str) -> tuple[str, str]:
    """单份 wav：upload -> transcript -> poll。返回 (text, 错误)。"""
    try:
        with open(wav, "rb") as f:
            data = f.read()
    except OSError as e:
        return "", f"读文件失败: {e}"
    if len(data) < 100:
        return "", "音频文件太小（可能是空录音或损坏）"

    # 1) upload
    try:
        async with session.post(
            f"{BASE}/upload",
            headers={"Authorization": KEY, "Content-Type": "application/octet-stream"},
            data=data,
        ) as resp:
            if resp.status != 200:
                return "", f"上传 HTTP {resp.status}"
            up = ((await resp.json()) or {}).get("upload_url") or ""
            if not up:
                return "", "上传没拿到 upload_url"
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return "", f"上传失败: {type(e).__name__}"

    # 2) 建转写任务
    body = {"audio_url": up, "language_code": LANG}
    try:
        async with session.post(
            f"{BASE}/transcript",
            headers={"Authorization": KEY, "Content-Type": "application/json"},
            json=body,
        ) as resp:
            if resp.status not in (200, 201):
                return "", f"建任务 HTTP {resp.status}"
            tid = ((await resp.json()) or {}).get("id") or ""
            if not tid:
                return "", "建任务没拿到 id"
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return "", f"建任务失败: {type(e).__name__}"

    # 3) 轮询
    deadline = time.time() + POLL_MAX
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        if time.time() > deadline:
            return "", f"等太久（>{POLL_MAX:.0f}s）"
        try:
            async with session.get(
                f"{BASE}/transcript/{tid}", headers={"Authorization": KEY}
            ) as resp:
                if resp.status == 200:
                    d = await resp.json()
                else:
                    continue
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue
        st = (d.get("status") or "").lower()
        if st == "completed":
            text = (d.get("text") or "").strip()
            return (text, "") if text else ("", "转写完成但没文本")
        if st in ("error", "failed"):
            return "", f"转写失败: {d.get('error') or st}"


async def _describe(path: str) -> tuple[str, str]:
    """完整流程：抽音轨(若视频) -> 转写。返回 (文本, 错误)。不改缓存。"""
    if not os.path.exists(path):
        return "", "文件不在了"
    targets = _extract_audio_paths(path)
    if not targets:
        return "", "抽不出音频（文件损坏或不是音视频）"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        ) as session:
            async with _sem:
                return await _transcribe_once(session, targets[0])
    finally:
        # 抽出来的临时 wav 用完就删（原始语音文件是框架的，不碰）
        for t in targets:
            if t != path and t.startswith(TMP_DIR) and os.path.exists(t):
                try:
                    os.remove(t)
                except OSError:
                    pass


async def _get_cached(path: str) -> str:
    """取缓存里的转写文本；没有且正在转写就等它；都没有就现转。"""
    if path in _cache:
        return _cache[path]
    task = None
    async with _pending_lock:
        task = _pending.get(path)
    if task is not None:
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=POLL_MAX)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return ""
    cap, err = await _describe(path)
    _record_stats(cap, err)
    if cap:
        _cache[path] = cap
    return cap


def _record_stats(cap: str, err: str) -> None:
    if cap:
        _stat["ok"] += 1
    else:
        _stat["fail"] += 1
        _stat["last_err"] = (err or "")[:100]


def _trim_cache() -> None:
    if len(_cache) > CACHE_MAX:
        for k in list(_cache)[: CACHE_MAX // 2]:
            _cache.pop(k, None)


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        os.makedirs(TMP_DIR, exist_ok=True)
        state = "关"
        if not ENABLED:
            state = "总开关关"
        elif not KEY:
            state = "没配 Key"
        else:
            state = "开"
        logger.info(
            "[listen] 已加载：开关=%s 语言=%s 注入预算=%.0fs 并发=%d 缓存上限=%d",
            state, LANG, INJECT_BUDGET, MAX_CONCURRENT, CACHE_MAX,
        )

    # ---------------------------------------------------------------- 收集路径

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def collect(self, event: AstrMessageEvent) -> None:
        """每条群消息进来都听一遍：里面有语音条/视频就后台转写进缓存。"""
        if not (ENABLED and KEY):
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
                return  # 机器人自己发的语音不回听（会自我强化）
            comps = getattr(event.message_obj, "message", None) or []
            jobs: list[str] = []
            for comp in comps:
                if isinstance(comp, (Record, Video)):
                    try:
                        p = await comp.convert_to_file_path()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[listen] 取语音路径失败：%s", e)
                        continue
                    if p:
                        owned = await self._own_copy(p)
                        if owned:
                            jobs.append(owned)
            if not jobs:
                return
            logger.info("[listen] collect 拿到 %d 条声音：%s",
                        len(jobs),
                        ", ".join(os.path.basename(j) for j in jobs))
            for p in jobs:
                await self._ensure_job(p)
        except BaseException as e:  # noqa: BLE001
            logger.error("[listen] 收集钩子异常：%s", e)

    async def _own_copy(self, src: str) -> str:
        """把 convert 出来的临时文件复制进自己的目录，避免被框架清理。

        实测（2026-09-09）：convert_to_file_path() 返回的路径在第 N 秒还
        存在（框架自己那条链生成的文件留在 temp/），但 collect 钩子拿到的
        路径文件随后就被清掉了——拿到的更像「幽灵路径」。这里立刻整份复制
        到 DSH_LISTEN_TMP，文件名按源路径哈希命名，重复转发的同一段语音
        还会自动复用到同一份副本。
        """
        if not src or not os.path.exists(src):
            logger.warning("[listen] 源文件不在（%.80s），转写跳过", str(src)[-80:])
            return ""
        os.makedirs(TMP_DIR, exist_ok=True)
        key = hashlib.sha1(src.encode("utf-8", "replace")).hexdigest()[:16]
        low = src.lower()
        ext = ".wav" if re.search(r"\.(wav|mp3|m4a|aac|ogg|flac|opus|amr|silk|sln)$", low) else ".bin"
        dst = os.path.join(TMP_DIR, f"v_{key}{ext}")
        try:
            if not os.path.exists(dst):
                await asyncio.to_thread(self._copy_file, src, dst)
            logger.info("[listen] 已接管声音 %s -> %s", os.path.basename(src), os.path.basename(dst))
            return dst
        except OSError as e:
            logger.warning("[listen] 复制声音失败：%s", e)
            return ""

    @staticmethod
    def _copy_file(src: str, dst: str) -> None:
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)

    @staticmethod
    def _owned_path(src: str) -> str:
        """源路径 -> 本插件的 owned 副本路径（纯计算，不复制）。"""
        key = hashlib.sha1(src.encode("utf-8", "replace")).hexdigest()[:16]
        low = src.lower()
        ext = ".wav" if re.search(r"\.(wav|mp3|m4a|aac|ogg|flac|opus|amr|silk|sln)$", low) else ".bin"
        return os.path.join(TMP_DIR, f"v_{key}{ext}")

    async def _ensure_job(self, path: str) -> None:
        """path 没转写过就起一个后台任务转写；已在转/已转完就不动。"""
        if path in _cache:
            logger.info("[listen] 已转写过（缓存命中）%s", os.path.basename(path))
            return
        async with _pending_lock:
            if path in _pending:
                logger.info("[listen] 正在转写中（跳过）%s", os.path.basename(path))
                return
            task = asyncio.create_task(self._job(path))
            _pending[path] = task
            task.add_done_callback(lambda t, p=path: self._job_done(p, t))
        logger.info("[listen] 已接声音 %s（后台转写）", os.path.basename(path))

    async def _job(self, path: str) -> None:
        try:
            t0 = time.time()
            cap, err = await _describe(path)
            dt = time.time() - t0
            if cap:
                _cache[path] = cap
                _stat["sec"] += dt
                _record_stats(cap, "")
                _trim_cache()
                logger.info("[listen] 转写成功 %s（%.1fs）-> %.40s",
                            os.path.basename(path), dt, cap[:40])
            else:
                _record_stats("", err)
                logger.warning("[listen] 转写失败 %s（%.1fs）：%s",
                               os.path.basename(path), dt, err[:100])
        except BaseException as e:  # noqa: BLE001
            _record_stats("", str(e))
            logger.error("[listen] 转写任务异常 %s：%s", os.path.basename(path), e)
        finally:
            # owned 副本（v_ 前缀、在自己的目录里）转写完即删：
            # 成功时文字已进缓存，失败时留副本也没用
            if path.startswith(TMP_DIR) and os.path.basename(path).startswith("v_"):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _job_done(self, path: str, task: asyncio.Task) -> None:
        async def _cleanup():
            async with _pending_lock:
                _pending.pop(path, None)
        try:
            asyncio.create_task(_cleanup())
        except BaseException:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 注入路径

    @filter.on_llm_request()
    async def listen(self, event: AstrMessageEvent, req) -> None:
        """把上下文里出现的语音/视频声音，用已转好的文字替掉路径标记。"""
        if not (ENABLED and KEY):
            return
        try:
            parts = getattr(req, "extra_user_content_parts", None) or []
            hits: list[tuple[int, str]] = []

            # ① 语音条标记（当前 + 引用）
            for i, part in enumerate(parts):
                text = getattr(part, "text", "") or ""
                for m in _AUDIO_RE.finditer(text):
                    hits.append((i, m.group(1).strip()))
            # ② 引用消息里的视频标记（画面归 dsh-video，我只补声音）
            for i, part in enumerate(parts):
                text = getattr(part, "text", "") or ""
                for m in _QUOTED_VIDEO_RE.finditer(text):
                    hits.append((i, m.group(1).strip()))
            if not hits:
                return

            seen: set[str] = set()
            uniq = []
            for idx, p in hits:
                if p in seen:
                    continue
                seen.add(p)
                uniq.append((idx, p))

            lines: list[str] = []
            drop_parts: set[int] = set()
            for idx, p in uniq:
                # 统一用 owned 路径当缓存 key：收集路径缓存的就是它
                owned = self._owned_path(p)
                try:
                    cap = await asyncio.wait_for(_get_cached(owned), timeout=INJECT_BUDGET)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    cap = ""
                if not cap:
                    # 缓存没有（收集路径没赶上/没跑）：尝试接管源文件现转
                    # （预算内）。源文件可能已被框架清掉，失败就放弃。
                    try:
                        owned2 = await self._own_copy(p)
                        if owned2:
                            cap = await asyncio.wait_for(
                                _get_cached(owned2), timeout=INJECT_BUDGET
                            )
                    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                        cap = ""
                if cap:
                    lines.append(f"- 有条语音（或视频里的声音），内容是：{cap}")
                    drop_parts.add(idx)
                else:
                    # 转写不了/还在转/超预算：至少把路径拿掉避免模型去瞎猜
                    drop_parts.add(idx)
                    logger.warning("[listen] 注入时无文本（%s）", os.path.basename(p)[:40])
            if not lines:
                return
            for i in sorted(drop_parts, reverse=True):
                if i < 0 or i >= len(parts):
                    continue
                text = getattr(parts[i], "text", "") or ""
                stripped = _AUDIO_RE.sub("", text).strip()
                if stripped:
                    try:
                        parts[i].text = stripped
                    except Exception:  # noqa: BLE001
                        del parts[i]
                else:
                    del parts[i]
            req.extra_user_content_parts.append(
                TextPart(
                    text="<voice_context>\n"
                    "下面是这段音频内容（语音条或视频音轨转写，可能有个别字听错）。"
                    "当作你已经听到了，直接就内容回应。\n"
                    + "\n".join(lines)
                    + "\n</voice_context>"
                )
            )
        except asyncio.TimeoutError:
            logger.warning("[listen] 注入超过 %.0fs 预算", INJECT_BUDGET)
        except BaseException as e:  # noqa: BLE001
            logger.error("[listen] 注入钩子异常：%s", e)

    # ---------------------------------------------------------------- 指令

    @filter.command("听语音状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """/听语音状态 —— 查看语音识别配置与战绩。"""
        avg = (_stat["sec"] / _stat["ok"]) if _stat["ok"] else 0.0
        err = ""
        if _stat["last_err"]:
            err = "｜最近错误: " + _stat["last_err"][:60]
        yield event.plain_result(
            f"听语音：{'开' if (ENABLED and KEY) else '关'}｜语言 {LANG}｜"
            f"注入预算 {INJECT_BUDGET:.0f}s\n"
            f"成功 {_stat['ok']} 次 / 失败 {_stat['fail']} 次｜"
            f"{('平均 %.1fs' % avg) if _stat['ok'] else '尚无成功记录'}{err}"
            f"｜缓存 {len(_cache)} 条｜转写中 {len(_pending)} 条"
        )