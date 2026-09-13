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
import random
import re
import time
from collections import deque
from datetime import datetime

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import LLMResponse
from astrbot.core import logger
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent

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
# 模型没主动写标记时，给纯聊天回复一个很低的自动补图概率。旧逻辑完全依赖模型
# 自觉写 [贴纸:x]，换模型后近 6 小时 700+ 条回复只发 1 张，体感几乎消失。
# 自动补图仍受冷却、仅限短口语回复、且一轮已有媒体时不发，避免回到 91% 刷屏。
AUTO_RATE = min(1.0, max(0.0, float(os.environ.get("DSH_STICKER_AUTO_RATE", "0.22"))))
AUTO_COOLDOWN = max(0, int(os.environ.get("DSH_STICKER_AUTO_COOLDOWN", "180")))
AUTO_MAX_CHARS = max(1, int(os.environ.get("DSH_STICKER_AUTO_MAX_CHARS", "28")))
_AUTO_TAG_RULES = (
    (re.compile(r"笑死|哈哈|绷不住|(?:^|[，。！？!?、\s])(?:乐|草|6)(?:$|[，。！？!?、\s])|离谱|逆天|抽象"), ("嘲笑", "小丑")),
    (re.compile(r"可爱|好乖|真棒|厉害|可以的|有点实力|谢谢|感谢|爱了"), ("装萌", "送花")),
    (re.compile(r"委屈|伤心|难受|哭|欺负|可怜|不理我"), ("装可怜", "假装没伤心")),
    (re.compile(r"困|熬夜|睡不着|通宵"), ("熬夜",)),
    (re.compile(r"想想|让我想|不懂|不知道|怎么回事|为啥|为什么|\?{1,3}|？{1,3}"), ("思考",)),
    (re.compile(r"看看|瞅瞅|来了|在吗|干嘛|冒泡"), ("探头",)),
    (re.compile(r"帅|稳|拿下|搞定|那必须|豪横"), ("装酷",)),
)
_AUTO_FALLBACK_TAGS = ("思考", "探头", "装萌", "装酷")
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
MEDIA_FLAGS = ("imagegen_done", "voice_done", "video_done", "steal_done")
# on_llm_response 处理过这一步的记号，防同一步被兜底钩子二次发送
STEP_FLAG = "dsh_sticker_step_done"
# 只有真正经过 LLM 最终回复钩子的无标记文本，才允许在装饰出口自动补图；
# 防止 /状态 等普通命令也被随机附上表情包。
AUTO_FLAG = "dsh_sticker_auto_candidate"

# 最近一次解析的记录，方便排查
_LAST_USED: dict[str, list[str]] = {}
# gid -> deque[bool]，最近 WINDOW 次带标记回复里哪几次真发了
_attempts: dict[str, deque] = {}
# gid -> (上一张真发出去的贴纸名, 距今过了几次放行)。见 patch:dedup-v2。
# 只在真发出去时更新：被配额挡掉的那次群里根本没看见，不算「间隔拉开了」。
_last_tag: dict[str, tuple] = {}
# gid -> 上一次自动补贴纸时间；模型主动标记走原配额，不受该时间冷却。
_last_auto: dict[str, float] = {}
# 统计：/贴纸状态 用
_stat = {"attempt": 0, "sent": 0, "quota_drop": 0, "media_drop": 0, "unknown": 0,
         "inter_step": 0, "dedup_drop": 0, "auto_attempt": 0,
         "auto_sent": 0, "auto_cooldown_drop": 0}


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


def _auto_tag(text: str) -> str:
    """给适合用表情回应的短口语挑一个已有贴纸标签；不适合则返回空。"""
    plain = (text or "").strip()
    if not plain or len(plain) > AUTO_MAX_CHARS or "\n" in plain:
        return ""
    # 正经说明、拒绝、安全/健康提醒和工具状态不自动配梗图。
    if re.search(r"https?://|因为|建议|注意|不能|无法|抱歉|出不了|失败|错误|风险|观察下|医院|医生|报警", plain):
        return ""
    for pattern, tags in _AUTO_TAG_RULES:
        if pattern.search(plain):
            return random.choice(tags)
    # 无明显情绪的短回复只以很低概率补中性贴纸，随机闸由调用方统一处理。
    return random.choice(_AUTO_FALLBACK_TAGS)


async def _maybe_auto_send(event: AstrMessageEvent, text: str) -> bool:
    """模型没有主动贴纸标记时，低频补一张；返回是否真发成功。"""
    if AUTO_RATE <= 0 or _has_media(event):
        return False
    tag = _auto_tag(text)
    if not tag:
        return False
    gid = _gid(event)
    now = time.time()
    # 先挡群级冷却再抽样，避免把冷却期内大量回复计成「尝试」，状态更好读。
    if now - _last_auto.get(gid, 0.0) < AUTO_COOLDOWN:
        _stat["auto_cooldown_drop"] += 1
        return False
    if random.random() >= AUTO_RATE:
        return False
    _stat["auto_attempt"] += 1
    _cooldown_tick(gid)
    if not _cooldown_allows(gid, tag):
        _stat["dedup_drop"] += 1
        return False
    path = _resolve_sticker(tag)
    if not path:
        _stat["unknown"] += 1
        return False
    # [patch:marker-leak-v3] 自动补图失败也必须咽下去：这个函数在兜底钩子的
    # 末尾被调用，异常会冒到外层 except，把「已经剥好的文字」的收尾工作一起带走。
    try:
        await event.send(MessageChain(chain=[Image.fromFileSystem(path)]))
    except BaseException as exc:
        _stat["send_fail"] = _stat.get("send_fail", 0) + 1
        logger.warning("[贴纸] 自动补图发送失败：%s", str(exc)[:200])
        return False
    _last_auto[gid] = now
    _cooldown_remember(gid, tag)
    _stat["sent"] += 1
    _stat["auto_sent"] += 1
    logger.info("[贴纸] 自动补发 1 张 tag=%r gid=%s text=%r", tag, gid, text[:30])
    return True


def strip_markers(text: str) -> tuple[str, list[str]]:
    """纯函数：剥掉 [贴纸:x] 标记，返回 (清理后的文字, 标记里的贴纸名)。

    [patch:marker-leak-v3] 不碰网络、不碰 event，因此**不可能失败**。
    任何要发东西的调用方都必须先拿这里的结果把文字替换掉，再去做发送。

    2026-09-12 的实测泄漏链（群里真的出现过
    「乖宝宝这称号我自己都叫上了？[贴纸:装萌]」）：旧版把「剥」和「发」
    塞在同一个 _handle 里，`event.send` 抛 ActionFailed(retcode=1200) →
    异常冒到调用方 → 调用方的 `comp.text = cleaned` /
    `response.completion_text = cleaned` 整个被跳过 → 标记原样进群。
    剥标记是纯字符串操作、不可能失败；发图是网络动作、随时会失败。
    两件事必须分开：剥的结果先落盘，再去尝试发送。
    """
    raw = text or ""
    return MARKER_RE.sub("", raw).strip(), MARKER_RE.findall(raw)


# ---------------------------------------------------------------- 出口兜底闸（v4）
#
# 为什么前两道钩子不够
# --------------------
# on_llm_response 和 on_decorating_result 都只覆盖**走管道**的那条链。
# 2026-09-13 00:53 群里真的漏出来一条，群友引用回来的原文是：
#
#     零基础上太空？先学会在群里别被禁言吧[贴纸:装酷]
#
# 而这条在 astrbot.log 的 `respond.stage:206 Prepare to send` 里
# **一次都没出现过**（全量日志里 `[贴纸:` 从没进过出站正文）——
# 说明它根本没走 respond.stage。`event.send()` 是**平台直发**：
# 不过装饰钩子、不过出口闸，标记原样进群。
# 现在至少有 5 个插件在用它（dsh-welcome / dsh-imagegen / dsh-video /
# dsh-guard / dsh-poke），以后还会加 —— 每加一个就是一条新的漏法。
#
# 所以把 `AstrMessageEvent.send` 包一层：**这是所有出站路径唯一的公共点**。
# 三条设计约束：
#   1) 只剥、不改别的 —— 复用同一个 strip_markers 纯函数，行为跟另两道完全一致；
#   2) fail-open —— 这一步抛任何异常都照原样发出去。宁可漏一个标记，
#      也绝不能因为兜底闸自己出问题而把消息吞掉；
#   3) 每次真剥到东西就 WARNING 一次。**这条日志是证据**：
#      下次再有人报「发出标记了」，直接搜它就知道是哪条路径漏的。
def guard_outgoing(message) -> int:
    """剥掉出站 MessageChain 里所有 Plain 的贴纸标记。返回剥掉的标记个数。"""
    chain = getattr(message, "chain", None)
    if not isinstance(chain, list):
        return 0
    hit = 0
    for comp in chain:
        if not isinstance(comp, Plain):
            continue
        raw = comp.text or ""
        cleaned, markers = strip_markers(raw)
        if not markers:
            continue
        comp.text = cleaned
        hit += len(markers)
        logger.warning(
            "[贴纸] 出口兜底剥标记（这条没过装饰钩子，说明走了 event.send 直发）："
            "%r → %r",
            raw[:60],
            cleaned[:60],
        )
    return hit


_ORIG_SEND = AstrMessageEvent.send
_ORIG_SEND_STREAMING = getattr(AstrMessageEvent, "send_streaming", None)


async def _guarded_send(self, message, *args, **kwargs):
    try:
        guard_outgoing(message)
    except BaseException:  # noqa: BLE001 —— fail-open，见上面第 2 条
        pass
    return await _ORIG_SEND(self, message, *args, **kwargs)


async def _guarded_send_streaming(self, generator, *args, **kwargs):
    """流式那条路收的是**异步生成器**，不是 MessageChain。

    `send_streaming(self, generator: AsyncGenerator[MessageChain, None], ...)`
    （astr_message_event.py:280）—— 对着生成器调 guard_outgoing 只会拿到
    getattr(gen, "chain", None) == None 然后返回 0，**等于没装**。
    所以要包住生成器本身，逐条链剥。（本部署流式是关的，纯粹为了别留地雷：
    哪天有人打开 streaming_response，这里必须是对的。）
    """

    async def _guarded_gen():
        async for chain in generator:
            try:
                guard_outgoing(chain)
            except BaseException:  # noqa: BLE001
                pass
            yield chain

    return await _ORIG_SEND_STREAMING(self, _guarded_gen(), *args, **kwargs)


# 只装一次：插件热重载会再执行一遍本模块，重复包装会让日志出现多层。
if not getattr(AstrMessageEvent.send, "_dsh_sticker_guard", False):
    _guarded_send._dsh_sticker_guard = True
    AstrMessageEvent.send = _guarded_send
    logger.info("[贴纸] 出口兜底闸已装：event.send 直发的链也会剥标记")
if _ORIG_SEND_STREAMING is not None and not getattr(
    _ORIG_SEND_STREAMING, "_dsh_sticker_guard", False
):
    _guarded_send_streaming._dsh_sticker_guard = True
    AstrMessageEvent.send_streaming = _guarded_send_streaming


async def _send_markers(
    event: AstrMessageEvent, markers: list[str], cleaned: str, where: str
) -> int:
    """按配额把标记对应的贴纸发出去。返回真正发出去的张数。

    **本函数绝不向外抛异常**：贴纸发不出去只是少一张图，
    绝不能连累调用方的文字处理（见 _handle 的 [patch:marker-leak-v3]）。
    """
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
        return 0

    if not _quota_allows(gid):
        _stat["quota_drop"] += 1
        logger.info(
            f"[贴纸] 不发(配额 {MAX_IN_WINDOW}/{WINDOW}) tags={markers} "
            f"where={where} gid={gid} 窗口={[int(x) for x in _attempts.get(gid, [])]}"
        )
        return 0

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
            try:
                await event.send(MessageChain(chain=[Image.fromFileSystem(path)]))
            except BaseException as exc:
                # 发不出去只是少一张图。绝不能让异常冒出去 —— 见 [patch:marker-leak-v3]。
                _stat["send_fail"] = _stat.get("send_fail", 0) + 1
                logger.warning(
                    "[贴纸] 发送失败（标记已剥，不影响文字）：%s", str(exc)[:200]
                )
                continue
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
    return sent


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context

    @filter.on_llm_response()
    async def stickerize(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        """最终回复：识别贴纸标记，按配额发贴纸，并**总是**去除标记文字。"""
        # [patch:marker-leak-v3] 顺序是刻意的：**先剥、后发**。
        # 旧版把两件事都塞进 _handle，一旦 event.send 抛 ActionFailed(retcode=1200)，
        # 异常会在 `response.completion_text = cleaned` 之前冒出来并被这里吞掉，
        # 于是标记原样进群（[贴纸:装萌]）。现在剥标记是纯字符串操作，
        # 排在发送之前且无条件执行；发送失败只记日志。
        text = response.completion_text or ""
        cleaned, markers = strip_markers(text)
        if cleaned != text:
            # 只要出现过标记，就把回复文字换成去标记后的文本（即使一张都没发成）
            try:
                response.completion_text = cleaned
            except Exception:
                response._completion_text = cleaned
        if not markers:
            try:
                event.set_extra(AUTO_FLAG, True)
            except BaseException:
                pass
            return
        try:
            event.set_extra(STEP_FLAG, True)
        except BaseException:
            pass
        try:
            await _send_markers(event, markers, cleaned, "llm_response")
        except BaseException as e:
            # _send_markers 本身不会抛，这里是最后一道保险：文字已经剥干净了，
            # 就算这里出任何事也不该回滚文字。
            logger.error(f"[贴纸] 发图出错（文字已剥干净）: {e}")

    @filter.on_decorating_result()
    async def stickerize_fallback(self, event: AstrMessageEvent) -> None:
        """兜底：中间步（工具前的垫话）不经过 on_llm_response，在这里剥。

        这个钩子对最终步也会跑一遍，但那时标记已被上面剥掉、findall 为空，
        自然空转；万一上面没剥成功，这里就是第二道防线。
        """
        try:
            result = event.get_result()
            if result is None:
                return

            # chain_result() 收的是组件 list；上游若误传 MessageChain 会形成
            # 嵌套结构。钩子顺序不保证，故本插件也独立拆平并 fail-open。
            # [patch:marker-leak-v3] 拆不平就**放弃整条链**等于放标记进群，
            # 所以这里尽量多挖几层；实在挖不动才退化成「整段文本兜底剥」。
            chain = getattr(result, "chain", None)
            for _ in range(4):
                if isinstance(chain, list) or chain is None:
                    break
                inner = getattr(chain, "chain", None)
                if inner is None:
                    break
                logger.warning("[贴纸] 检测到嵌套 MessageChain，已拆平")
                chain = inner
            if isinstance(chain, list):
                result.chain = chain
            elif chain is not None:
                # 挖不动：至少把整条结果当成一段文本剥一遍，别让标记漏出去。
                logger.error("[贴纸] result.chain 类型异常: %r", type(chain))
                raw = ""
                try:
                    raw = result.get_plain_text() or ""
                except BaseException:
                    raw = ""
                cleaned, markers = strip_markers(raw)
                if markers:
                    try:
                        result.chain = [Plain(cleaned)] if cleaned else []
                        logger.warning("[贴纸] 兜底：整段重写成剥干净的文本")
                    except BaseException:
                        pass
                return

            if not result.chain:
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
                # [patch:marker-leak-v3] 先剥、先落盘，再去发。
                # 旧版是 `cleaned = await _handle(...)` 之后才 `comp.text = cleaned`：
                # _handle 内部的 event.send 一抛，赋值就被跳过，标记原样进群。
                cleaned, markers = strip_markers(text)
                if not markers:
                    continue
                comp.text = cleaned
                if already:
                    logger.info("[贴纸] 兜底只剥不发(本步已处理)")
                    continue
                try:
                    await _send_markers(event, markers, cleaned, "decorate")
                except BaseException as e:
                    logger.error(f"[贴纸] 兜底发图出错（文字已剥干净）: {e}")

            # 剥完可能只剩空 Plain，去掉以免发出空消息
            result.chain[:] = [
                c
                for c in result.chain
                if not (isinstance(c, Plain) and not (c.text or "").strip())
            ]

            # 无显式标记的最终纯文本回复走低频自动补图。放在装饰出口而不是
            # on_llm_response：即使别的插件改写了最终文案，也按群里真正看到的文本选图。
            auto_candidate = False
            try:
                auto_candidate = bool(event.get_extra(AUTO_FLAG))
            except BaseException:
                pass
            if auto_candidate and not already and not any(
                isinstance(c, Image) for c in result.chain
            ):
                plain_text = "".join(
                    (c.text or "") for c in result.chain if isinstance(c, Plain)
                ).strip()
                if plain_text and not MARKER_RE.search(plain_text):
                    await _maybe_auto_send(event, plain_text)
        except BaseException as e:
            logger.error(f"[贴纸] 兜底处理失败: {e}")

    @filter.command("贴纸状态")
    async def cmd_status(self, event: AstrMessageEvent):
        gid = _gid(event)
        win = [int(x) for x in _attempts.get(gid, [])]
        explicit_sent = max(0, _stat["sent"] - _stat["auto_sent"])
        rate = (explicit_sent / _stat["attempt"] * 100) if _stat["attempt"] else 0.0
        yield event.plain_result(
            "贴纸配额：{}（窗口 {} 次里最多 {} 次，单条最多 {} 张）\n"
            "本群窗口：{}\n"
            "累计：想发 {} / 真发 {} = {:.0f}%\n"
            "自动补图：概率 {:.0f}% / 冷却 {}秒 / 尝试 {} / 真发 {} / 冷却拦下 {}\n"
            "拦下：配额 {} 次、本轮已有媒体 {} 次、未知贴纸名 {} 次\n"
            "兜底钩子在中间步剥掉标记 {} 次".format(
                "开" if QUOTA_ON else "关",
                WINDOW,
                MAX_IN_WINDOW,
                MAX_PER_REPLY,
                win or "空",
                _stat["attempt"],
                explicit_sent,
                rate,
                AUTO_RATE * 100,
                AUTO_COOLDOWN,
                _stat["auto_attempt"],
                _stat["auto_sent"],
                _stat["auto_cooldown_drop"],
                _stat["quota_drop"],
                _stat["media_drop"],
                _stat["unknown"],
                _stat["inter_step"],
            )
        )
