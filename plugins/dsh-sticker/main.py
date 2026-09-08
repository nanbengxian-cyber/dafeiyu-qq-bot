# dsh-sticker —— QQ 群聊「小鲸鱼」表情包贴纸插件。
#
# 小鲸鱼人格在回复中输出 [贴纸:名] 或 【贴纸:名】 标记时，插件会在
# data/stickers/<名>/ 下找到对应 GIF，把贴纸作为一条消息发到群里，
# 并把回复文字里的标记去除。
#
# 为什么用 event.send 而不是改 result_chain：
# AstrBot 的 final_llm_resp.completion_text 在设置 result_chain 后会从
# result_chain 派生(get_plain_text())，且发送路径优先用 completion_text，
# 导致把「文字+图片」放进 result_chain 时图片会被丢弃。因此贴纸走
# event.send()（独立的一条消息），文字走正常回应并去掉标记。
#
# 关键：无论贴纸名是否有效，都必须把标记从文字里去掉，避免 `[贴纸:xx]`
# 泄漏到群聊里。有效贴纸名才发送对应 GIF。
#
# ---------------------------------------------------------------------------
# v2.0（2026-09-02）两处结构性修复，起因是实测 6 小时里 `[贴纸:嘲笑]` 原样
# 漏进群聊 7 次，而同一轮最终回复的标记却剥得干干净净。
#
# 【修复一：中间步泄漏】on_llm_response 一轮只触发一次
#   OnLLMResponseEvent 只在 MainAgentHooks.on_agent_done 里触发，也就是
#   「整个 agent 跑完」那一刻。而工具循环里每一步的 assistant 文字是在
#   run_agent 内部 set_result + yield 出去的，靠调度器的洋葱递归照样走完
#   ResultDecorateStage → RespondStage 发到群里，**根本不经过
#   on_llm_response**。所以本插件旧版对「工具前的垫话」100% 不生效。
#   实测日志（逐秒对齐）：
#     14:01:11.667 Prepare to send … 得嘞，这就把群主画飞[贴纸:嘲笑]  ← 漏了
#     14:01:12.201 Agent 使用工具: ['generate_image']              ← 工具在其后
#     14:01:26.123 [贴纸] 发送 1 张 …                              ← 最终步才剥
#   修法：补一个 on_decorating_result 钩子做兜底。该钩子在
#   ResultDecorateStage 里触发，中间步同样会走到（dsh-mention 的
#   `[mention] at=...` 日志在中间步照打，已实证）。出口处总是清理——两条
#   独立防线不能塌成一条。
#   同类坑 dsh-welcome 已踩过一次（llm_generate 不走管道），教训是「凡绕过
#   管道直接出消息的地方都要自带剥离」，中间步是当时没覆盖的漏网路径。
#
# 【修复二：贴纸配额】人格里的软约束根本没被执行
#   人格写「一般一条回复最多带 1 张贴纸，只有真的想用表情表达时才加」，
#   实测 91%（51 张图 vs 56 条文本）的回复都带图 —— 软规则约束不住模型，
#   得在出口做硬闸门。这里用「最近 WINDOW 次带标记的回复里最多放行
#   MAX_IN_WINDOW 次」。
#   ⚠ 实际命中率是 1/(WINDOW+1) 而不是 1/WINDOW：放行那一次自己也留在窗口
#   里，要再过 WINDOW 次才被挤出去。本地枚举验证（见 test_sticker.py A1）：
#     WINDOW=2/MAX=1 -> T,F,F,T,F,F…   = 33%
#     WINDOW=3/MAX=1 -> T,F,F,F,T,F,F,F = 25%
#   默认取 WINDOW=2，把实测的 91% 压到约 33%（目标 ≤40%）。
#   注意：标记**永远**剥掉，配额只决定 GIF 发不发。
#   另外这一轮已经出过图/视频/语音（imagegen_done 等）时直接不发贴纸，
#   避免「工具图 + 贴纸」一次刷两张。

import os
import re
from collections import deque
from datetime import datetime

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import LLMResponse
from astrbot.core import logger
from astrbot.core.message.message_event_result import MessageChain

STICKER_DIR = os.environ.get("DSH_STICKER_DIR", "/AstrBot/data/stickers")
IMG_EXT = (".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp")
# 匹配 [贴纸:送花]、【贴纸:送花】、[貼紙:思考](繁体) 等写法
MARKER_RE = re.compile(r"[\[【]\s*(?:贴纸|貼紙|sticker)\s*[:：]\s*([^\]】]+?)\s*[\]】]")

# --- 配额旋钮 ---
# 总开关。0=不限（回到 v1 行为，每条都发）
QUOTA_ON = os.environ.get("DSH_STICKER_QUOTA", "1") != "0"
# 观察窗口：最近多少次「带贴纸标记的回复」。命中率 = 1/(WINDOW+1)
WINDOW = max(1, int(os.environ.get("DSH_STICKER_WINDOW", "2")))
# 窗口内最多放行几次。WINDOW=2/MAX=1 => 命中率 1/3
MAX_IN_WINDOW = max(1, int(os.environ.get("DSH_STICKER_MAX_IN_WINDOW", "1")))
# 一条回复最多发几张（人格也是这么写的）
MAX_PER_REPLY = max(1, int(os.environ.get("DSH_STICKER_MAX_PER_REPLY", "1")))
# [patch:dedup-v2 同一张贴纸的冷却]
# 同一张贴纸的冷却。只记「上一张真发出去的贴纸名」+「距那次过了几拍放行」，
# 同名且不满 COOLDOWN 拍就不发。实测群记录里连着 5 张「假装没伤心」，
# 观感是同一个表情刷屏。
#
# 精确语义（计时在判定**之前**加一拍，所以有个 -1）：
#   COOLDOWN=N ⇒ 同名会被连续跳过 N-1 次，第 N 拍放行。
#   因此 **COOLDOWN<=1 等于关闭**（第 1 拍就满足 since>=1，压根不挡）；
#   最小有意义的值是 2。这条别写错，否则以为调了旋钮其实没生效。
#
# ★为什么不是「最近 N 张里去重」：那样当 N ≥ 模型实际使用的贴纸种类数时会
# 永久饥饿（实测模型只用 思考/嘲笑/假装没伤心 三种，窗口 3 从第 4 张起全挡，
# 后半程一张都发不出来）。只记「上一张」的容量恒为 1，不随种类数增长，
# 所以天然无饥饿：哪怕模型只用一种贴纸，也只是退化成 发/挡/挡/发，频率降低而已。
#
# 取 3 的依据（真实序列回放，配额串联后实际发出的图）：
#   冷却 0/1（等于关闭）→ 11 张，最长连发 3
#   冷却 2             →  9 张，最长连发 2
#   冷却 3             →  8 张，最长连发 1   ← 取这个
# 设 0 关闭冷却。
DEDUP_COOLDOWN = max(0, int(os.environ.get("DSH_STICKER_DEDUP_COOLDOWN", "3")))

# 这一轮已经出过慢媒体，就别再叠贴纸
MEDIA_FLAGS = ("imagegen_done", "voice_done", "video_done")
# on_llm_response 处理过这一步的记号，防同一步被兜底钩子二次发送
STEP_FLAG = "dsh_sticker_step_done"

# 最近一次解析的记录，方便排查
_LAST_USED: dict[str, list[str]] = {}
# gid -> deque[bool]，最近 WINDOW 次带标记回复里哪几次真发了
_attempts: dict[str, deque] = {}
# gid -> (上一张真发出去的贴纸名, 距今过了几次放行)。见 patch:dedup-v2。
# 只在真发出去时更新：被配额挡掉的那次群里根本没看见，不算「间隔拉开了」。
_last_tag: dict[str, tuple] = {}
# 统计：/贴纸状态 用
_stat = {"attempt": 0, "sent": 0, "quota_drop": 0, "media_drop": 0, "unknown": 0,
         "inter_step": 0, "dedup_drop": 0}


def _resolve_sticker(tag: str):
    """根据标签(目录名)找到贴纸图片文件绝对路径，找不到返回 None。"""
    tag = tag.strip()
    if not tag:
        return None
    d = os.path.join(STICKER_DIR, tag)
    if not os.path.isdir(d):
        return None
    for f in sorted(os.listdir(d)):
        if f.lower().endswith(IMG_EXT):
            return os.path.join(d, f)
    return None


def _gid(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_group_id() or event.unified_msg_origin or "?")
    except BaseException:
        return "?"


def _has_media(event: AstrMessageEvent) -> str:
    """这一轮是不是已经发过图/视频/语音了。"""
    for flag in MEDIA_FLAGS:
        try:
            if event.get_extra(flag):
                return flag.replace("_done", "")
        except BaseException:
            pass
    return ""


def _quota_allows(gid: str) -> bool:
    """配额判定 + 记账。返回这次能不能真发贴纸。"""
    if not QUOTA_ON:
        return True
    q = _attempts.setdefault(gid, deque(maxlen=WINDOW))
    ok = sum(1 for x in q if x) < MAX_IN_WINDOW
    q.append(ok)
    return ok


def _cooldown_allows(gid: str, tag: str) -> bool:
    """同名贴纸的冷却判定。只判定，不记账（记账在真发成之后）。

    为什么不换成别的贴纸：随便换一张会答非所问（把「假装没伤心」换成「送花」
    更怪）。少一张图没人察觉，错一张图很明显。
    """
    if DEDUP_COOLDOWN <= 0:
        return True
    last, since = _last_tag.get(gid, (None, 10 ** 9))
    return not (tag.strip() == last and since < DEDUP_COOLDOWN)


def _cooldown_remember(gid: str, tag: str) -> None:
    """记下这次**真发出去的**贴纸，并把计时归零。"""
    if DEDUP_COOLDOWN <= 0:
        return
    _last_tag[gid] = (tag.strip(), 0)


def _cooldown_tick(gid: str) -> None:
    """一次「放行」记一拍。放行才计数——被配额挡掉的不算间隔。"""
    if DEDUP_COOLDOWN <= 0:
        return
    last, since = _last_tag.get(gid, (None, 10 ** 9))
    if last is not None:
        _last_tag[gid] = (last, since + 1)


async def _handle(event: AstrMessageEvent, text: str, where: str) -> str | None:
    """剥标记 + 按配额发贴纸。返回清理后的文字（无标记时返回 None）。

    标记无条件剥掉；配额只决定 GIF 发不发。
    """
    markers = MARKER_RE.findall(text or "")
    if not markers:
        return None

    cleaned = MARKER_RE.sub("", text).strip()
    gid = _gid(event)
    _stat["attempt"] += 1
    if where == "decorate":
        _stat["inter_step"] += 1

    # 判定链：已发过慢媒体 > 配额 > 贴纸名有效
    media = _has_media(event)
    if media:
        _stat["media_drop"] += 1
        logger.info(
            f"[贴纸] 不发(本轮已出{media}) tags={markers} where={where} gid={gid}"
        )
        return cleaned

    if not _quota_allows(gid):
        _stat["quota_drop"] += 1
        logger.info(
            f"[贴纸] 不发(配额 {MAX_IN_WINDOW}/{WINDOW}) tags={markers} "
            f"where={where} gid={gid} 窗口={[int(x) for x in _attempts.get(gid, [])]}"
        )
        return cleaned

    # 走到这里说明配额已放行，记一拍冷却计时（patch:dedup-v2）
    _cooldown_tick(gid)

    sent = 0
    for tag in markers:
        if sent >= MAX_PER_REPLY:
            logger.info(f"[贴纸] 超出单条上限 {MAX_PER_REPLY}，丢弃剩余: {tag!r}")
            break
        # 同名冷却：实测群里连着 5 张「假装没伤心」。
        if not _cooldown_allows(gid, tag):
            _stat["dedup_drop"] += 1
            last, since = _last_tag.get(gid, (None, -1))
            logger.info(
                f"[贴纸] 不发(同名冷却 {since}/{DEDUP_COOLDOWN}) tag={tag!r} gid={gid}"
            )
            continue
        path = _resolve_sticker(tag)
        if path:
            await event.send(MessageChain(chain=[Image.fromFileSystem(path)]))
            _cooldown_remember(gid, tag)
            sent += 1
        else:
            _stat["unknown"] += 1
            logger.warning(f"[贴纸] 未知贴纸名，已忽略: {tag!r} (dir={STICKER_DIR})")

    if sent:
        _stat["sent"] += sent
        _LAST_USED[datetime.now().isoformat()] = markers
        logger.info(
            f"[贴纸] 发送 {sent} 张, tags={markers}, where={where}, "
            f"cleaned_text={cleaned!r}"
        )
    return cleaned


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context

    @filter.on_llm_response()
    async def stickerize(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        """最终回复：识别贴纸标记，按配额发贴纸，并**总是**去除标记文字。"""
        try:
            text = response.completion_text or ""
            cleaned = await _handle(event, text, "llm_response")
            if cleaned is None:
                return
            # 只要出现过标记，就把回复文字换成去标记后的文本（即使没发成任何贴纸）
            if cleaned != text:
                try:
                    response.completion_text = cleaned
                except Exception:
                    response._completion_text = cleaned
            try:
                event.set_extra(STEP_FLAG, True)
            except BaseException:
                pass
        except BaseException as e:
            logger.error(f"[贴纸] 处理失败: {e}")

    @filter.on_decorating_result()
    async def stickerize_fallback(self, event: AstrMessageEvent) -> None:
        """兜底：中间步（工具前的垫话）不经过 on_llm_response，在这里剥。

        这个钩子对最终步也会跑一遍，但那时标记已被上面剥掉、findall 为空，
        自然空转；万一上面没剥成功，这里就是第二道防线。
        """
        try:
            result = event.get_result()
            if result is None or not result.chain:
                return

            # 上面已经处理过这一步：只剥不发，避免同一步发两张
            already = False
            try:
                already = bool(event.get_extra(STEP_FLAG))
            except BaseException:
                pass

            for comp in result.chain:
                if not isinstance(comp, Plain):
                    continue
                text = comp.text or ""
                if not MARKER_RE.search(text):
                    continue
                if already:
                    comp.text = MARKER_RE.sub("", text).strip()
                    logger.info("[贴纸] 兜底只剥不发(本步已处理)")
                    continue
                cleaned = await _handle(event, text, "decorate")
                if cleaned is not None:
                    comp.text = cleaned

            # 剥完可能只剩空 Plain，去掉以免发出空消息
            result.chain[:] = [
                c
                for c in result.chain
                if not (isinstance(c, Plain) and not (c.text or "").strip())
            ]
        except BaseException as e:
            logger.error(f"[贴纸] 兜底处理失败: {e}")

    @filter.command("贴纸状态")
    async def cmd_status(self, event: AstrMessageEvent):
        gid = _gid(event)
        win = [int(x) for x in _attempts.get(gid, [])]
        rate = (_stat["sent"] / _stat["attempt"] * 100) if _stat["attempt"] else 0.0
        yield event.plain_result(
            "贴纸配额：{}（窗口 {} 次里最多 {} 次，单条最多 {} 张）\n"
            "本群窗口：{}\n"
            "累计：想发 {} / 真发 {} = {:.0f}%\n"
            "拦下：配额 {} 次、本轮已有媒体 {} 次、未知贴纸名 {} 次\n"
            "兜底钩子在中间步剥掉标记 {} 次".format(
                "开" if QUOTA_ON else "关",
                WINDOW,
                MAX_IN_WINDOW,
                MAX_PER_REPLY,
                win or "空",
                _stat["attempt"],
                _stat["sent"],
                rate,
                _stat["quota_drop"],
                _stat["media_drop"],
                _stat["unknown"],
                _stat["inter_step"],
            )
        )
