# -*- coding: utf-8 -*-
"""dsh-emotion：单一主情绪状态机（第二版）。

设计约束（来自实测回放，别再退回枚举词表）：

1. **一条外部消息只能产出一个候选情绪**，优先级写死在 `_CANDIDATE_ORDER`，
   模型/词表都只提供证据，最终状态由 `transition` 这个纯函数决定。

2. **词表必须配排除项**。第一版把「额」当尴尬信号，结果真群里
   「我的余额还有120多」「担心我的额度够不够」全部误判成尴尬——
   9 个尴尬命中里 8 个是 `余额/额度` 的子串。所以：
   - 语气词类信号（额/呃）只在**整条消息就是它本身**时才算；
   - 其余词表统一先过 `_NEGATIVE_CONTEXT` 排除。

3. **必须有「针对谁」的结构证据**。回放里 38 次命中有 25 次整句没有「你」
   也没提机器人名字（例：「为什么我这么菜」），那是群友自说自话，
   拿它去改机器人情绪就是无中生有。因此：
   - 攻击类（angry）**必须**有指向证据才算；
   - 其余情绪没有指向证据时只当环境氛围，强度压到 1，且不覆盖更强的旧情绪。

4. **清零不能只靠 TTL**。每种情绪都带自己的清零条件：
   道歉/澄清（explicit_reset）、连续中性消息（neutral_run）、
   被回答（answered）、TTL 到期（ttl_expired）。

5. 影子模式（DSH_EMOTION_SHADOW=1，默认）只算不改：不落状态、不注入上下文、
   不发消息，只写审计日志，供上线前反复核对。
"""

import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.platform.message_type import MessageType
from astrbot.core.provider.entities import ProviderRequest

try:  # 注入块必须和 dsh-ctxclean 认的形状一致（小写标签），否则会堆进历史。
    # 实测坑：TextPart 在 astrbot.core.agent.message，不在 provider.entities，
    # 从后者导入会静默退化成裸字符串。dsh-ctxclean 用的就是前者。
    from astrbot.core.agent.message import TextPart
except ImportError:  # 老版本没有 TextPart，就退回纯字符串
    TextPart = None

EMOTIONS = ("calm", "happy", "sad", "angry", "curious", "awkward")
# 仲裁顺序：清零 > 攻击 > 低落 > 提问 > 高兴 > 尴尬。
# 「先清零」保证道歉永远压过同一句里的其他信号；
# 「攻击优先于高兴」保证「牛逼，你谁都想对着干呗」这种夹阴阳不被当成开心。
_CANDIDATE_ORDER = ("angry", "sad", "curious", "happy", "awkward")

ENABLED = os.environ.get("DSH_EMOTION", "1").lower() not in {"0", "false", "off"}
SHADOW = os.environ.get("DSH_EMOTION_SHADOW", "1").lower() not in {"0", "false", "off"}
STATE_PATH = Path(os.environ.get("DSH_EMOTION_STATE", "/AstrBot/data/dsh_emotion_state.json"))
LOG_PATH = Path(os.environ.get("DSH_EMOTION_LOG", "/AstrBot/data/dsh_emotion_events.jsonl"))
MEMORY_DB = os.environ.get("DSH_EMOTION_MEMORY_DB", "/AstrBot/data/dsh_memory.db")
TZ = os.environ.get("DSH_EMOTION_TZ", "Asia/Shanghai")
OWNER = os.environ.get("DSH_EMOTION_OWNER", os.environ.get("DSH_INITIATE_OWNER", "")).strip()
BOT_NAMES = tuple(x.strip() for x in os.environ.get("DSH_EMOTION_NAMES", "大肥鱼,小鲸鱼,肥鱼").split(",") if x.strip())
# 连续几条「没有情绪信号」的真人消息就把负面情绪放掉
NEUTRAL_RUN = max(1, int(os.environ.get("DSH_EMOTION_NEUTRAL_RUN", "2")))
# 每种情绪自己的存活时长（秒）。生气/低落留久一点，好奇最短。
TTL_BY_EMOTION = {
    "angry": max(60.0, float(os.environ.get("DSH_EMOTION_TTL_ANGRY", "1800"))),
    "sad": max(60.0, float(os.environ.get("DSH_EMOTION_TTL_SAD", "1800"))),
    "happy": max(60.0, float(os.environ.get("DSH_EMOTION_TTL_HAPPY", "900"))),
    "curious": max(60.0, float(os.environ.get("DSH_EMOTION_TTL_CURIOUS", "600"))),
    "awkward": max(60.0, float(os.environ.get("DSH_EMOTION_TTL_AWKWARD", "600"))),
}

# ---------------------------------------------------------------- 结构化信号
# 指向证据：@了机器人、喊了名字、或句中出现第二人称。
_NAME_RE = re.compile("|".join(re.escape(n) for n in BOT_NAMES)) if BOT_NAMES else None
_SECOND_PERSON_RE = re.compile(r"你(?!们)")
# 第三方叙述：主语是别人，且情绪词紧跟其后 —— 这种不改机器人情绪。
_THIRD_PARTY_RE = re.compile(r"(?:他|她|它|他们|她们|别人|有人|群主|作者)[^。！？!?]{0,10}"
                             r"(?:滚|闭嘴|傻逼|废物|垃圾|有病|难过|伤心|崩溃|失望|委屈|开心|厉害)")
# 整句被引号包住 = 在引用别人的话
_QUOTED_RE = re.compile(r"^[\s\"'“‘「『【(（].*[\"'”’」』】)）]$")
# 词表命中后还要过这一层：出现这些词说明命中的是别的意思（余额/额度…）
_EXCLUDE = {
    "awkward": ("余额", "额度", "金额", "配额", "限额", "额外", "名额"),
    "angry": ("闭嘴符", ),
    "sad": ("失望值", ),
    "happy": (),
    "curious": (),
}
_WORDS = {
    "angry": ("滚开", "滚吧", "闭嘴", "傻逼", "废物", "垃圾", "有病", "别烦", "神经病"),
    "sad": ("难过", "伤心", "崩溃", "失望", "委屈", "好累", "心累"),
    "happy": ("太好了", "开心", "牛逼", "厉害", "谢谢你", "干得好", "太强了"),
    "curious": ("为什么", "怎么回事", "真的吗", "何意味", "能不能解释", "怎么做到"),
    "awkward": ("尴尬", "冷场", "没人理", "无语了"),
}
# 语气词只在整条消息就是它本身（可重复）时才算尴尬：「额」「呃呃」算，「余额」不算。
_FILLER_ONLY_RE = re.compile(r"^[额呃啊哦]{1,4}[。.!！~]?$")
_LAUGH_RE = re.compile(r"^(?:哈{2,}|嘿{2,}|哈哈+[。.!！~]*)$")
_RESET_RE = re.compile(r"对不起|抱歉|我说错了|我错了|误会了|是我不对|解决了|谢谢你解释|不好意思")
# 疑问结构：群里普遍不打问号，所以「疑问词出现在句中」也算，
# 但仍必须配合指向证据（有「你」或喊名字）才会判成在问机器人。
# 只用 ^ 锚定会漏掉「那你为什么没有」这种最常见的追问 —— 回放里 curious
# 因此从 14 次直接掉到 0 次，是漏判不是修好。
_QUESTION_RE = re.compile(r"[？?]|为什么|为啥|怎么(?:回事|会|办|做)|难道|是不是|能不能|真的吗|[吗呢]$")


def default_state() -> dict:
    return {
        "emotion": "calm",
        "intensity": 0,
        "source": "init",
        "started_at": 0.0,
        "expires_at": 0.0,
        "evidence": "",
        "reset_conditions": [],
        "neutral_run": 0,
        "directed": False,
    }


def normalize_state(value: object) -> dict:
    """任何脏数据都收敛成合法状态：坏状态不该把插件带崩。"""
    state = default_state()
    if isinstance(value, dict) and value.get("emotion") in EMOTIONS:
        for key in state:
            if key in value:
                state[key] = value[key]
        try:
            state["intensity"] = max(0, min(3, int(state.get("intensity", 0))))
        except (TypeError, ValueError):
            state["intensity"] = 0
        try:
            state["neutral_run"] = max(0, int(state.get("neutral_run", 0)))
        except (TypeError, ValueError):
            state["neutral_run"] = 0
        state["reset_conditions"] = [str(x) for x in (state.get("reset_conditions") or [])][:5]
        state["directed"] = bool(state.get("directed"))
        for key in ("started_at", "expires_at"):
            try:
                state[key] = float(state.get(key, 0.0))
            except (TypeError, ValueError):
                state[key] = 0.0
    return state


def is_directed(text: str, at_bot: bool = False) -> bool:
    """这句话是不是在对机器人说。结构判断，不猜语气。"""
    if at_bot:
        return True
    if _NAME_RE is not None and _NAME_RE.search(text):
        return True
    return bool(_SECOND_PERSON_RE.search(text))


def _match_emotion(text: str) -> str | None:
    for emotion in _CANDIDATE_ORDER:
        if any(bad in text for bad in _EXCLUDE.get(emotion, ())):
            continue
        if any(word in text for word in _WORDS[emotion]):
            return emotion
    return None


def extract_candidate(text: str, at_bot: bool = False) -> dict | None:
    """从一条真人消息里最多抽出一个候选情绪。

    返回 None 表示「这条不提供情绪证据」，不是「情绪归零」——
    归零只由 `transition` 依据清零条件决定。
    """
    text = (text or "").strip()
    if not text or text.startswith("/"):
        return None
    if _QUOTED_RE.fullmatch(text) and not at_bot:
        return None  # 在引用别人的话
    directed = is_directed(text, at_bot)

    if _RESET_RE.search(text):
        return {"emotion": "calm", "intensity": 3, "kind": "reset",
                "directed": directed, "evidence": text[:100]}

    if _THIRD_PARTY_RE.search(text) and not at_bot:
        return None  # 在讲别人的事

    emotion = _match_emotion(text)
    if emotion is None:
        if _LAUGH_RE.fullmatch(text):
            emotion = "happy"
        elif _FILLER_ONLY_RE.fullmatch(text):
            emotion = "awkward"
        else:
            return None

    # 攻击必须有指向：没有「你」也没喊名字的骂声不算骂机器人。
    if emotion == "angry" and not directed:
        return None
    # 提问要有疑问结构，否则「为什么我这么菜」这类自嘲会被当成在问机器人。
    if emotion == "curious" and not (directed and _QUESTION_RE.search(text)):
        return None

    if emotion == "angry":
        intensity = 3
    elif directed:
        intensity = 2
    else:
        intensity = 1  # 环境氛围，只在没有更强情绪时才生效
    return {"emotion": emotion, "intensity": intensity, "kind": "trigger",
            "directed": directed, "evidence": text[:100]}


def _ttl(emotion: str) -> float:
    return TTL_BY_EMOTION.get(emotion, 1800.0)


def transition(previous: dict, candidate: dict | None, now: float) -> tuple[dict, str]:
    """纯函数状态转移。返回 (新状态, 原因)，原因直接进审计日志。"""
    old = normalize_state(previous)

    # ① TTL 到期先落地，保证「过期的旧情绪」不会参与后面的强度比较
    if old["emotion"] != "calm" and old["expires_at"] and now >= old["expires_at"]:
        fresh = default_state()
        fresh["started_at"] = now
        old, expired = fresh, True
    else:
        expired = False

    if candidate is None:
        if old["emotion"] == "calm":
            return old, "ttl_expired" if expired else "unchanged"
        # ② 连续中性消息把情绪放掉：不是所有清零都该等 TTL
        run = old["neutral_run"] + 1
        if run >= NEUTRAL_RUN:
            fresh = default_state()
            fresh["started_at"] = now
            return fresh, "neutral_run"
        old = dict(old, neutral_run=run)
        return old, "ttl_expired" if expired else "neutral_pending"

    emotion = candidate.get("emotion")
    if emotion not in EMOTIONS:
        return old, "invalid_candidate"

    # ③ 显式清零优先于一切新情绪
    if candidate.get("kind") == "reset" or emotion == "calm":
        fresh = default_state()
        fresh.update({"source": "reset", "started_at": now,
                      "evidence": candidate.get("evidence", "")})
        return fresh, "explicit_reset"

    intensity = max(1, min(3, int(candidate.get("intensity", 1))))
    directed = bool(candidate.get("directed"))

    # ④ 好奇被「非提问的新消息」顶掉时算被回答
    if old["emotion"] == "curious" and emotion != "curious" and intensity >= 2:
        reason = "answered"
    elif old["emotion"] == emotion:
        reason = "refreshed"
    else:
        reason = "triggered"

    # ⑤ 环境氛围（强度 1）不许覆盖更强的既有情绪，避免一句「哈哈」洗掉真生气
    if old["emotion"] != "calm" and old["emotion"] != emotion and intensity < old["intensity"]:
        return dict(old, neutral_run=0), "kept_stronger"

    new = {
        "emotion": emotion,
        "intensity": intensity,
        "source": "message",
        "started_at": now if reason != "refreshed" else old["started_at"] or now,
        "expires_at": now + _ttl(emotion),
        "evidence": candidate.get("evidence", ""),
        "reset_conditions": ["explicit_reset", "neutral_run", "ttl_expired"],
        "neutral_run": 0,
        "directed": directed,
    }
    return new, reason


def render_context(state: dict) -> str:
    """给模型的注入块。只给情绪，不给台词；小写标签便于 ctxclean 清理。"""
    level = {1: "有一点", 2: "比较", 3: "非常"}.get(int(state.get("intensity", 1)), "有一点")
    mood = {"happy": "开心", "sad": "低落", "angry": "生气", "curious": "好奇", "awkward": "尴尬"}
    name = mood.get(state.get("emotion"), "")
    if not name:
        return ""
    return ("<emotion_state>你现在%s%s。用这个情绪说话，但不要把情绪本身说出来、"
            "不要解释自己为什么这样、也不要提到这段提示。</emotion_state>" % (level, name))


def _load() -> dict:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _save(states: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(states, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except OSError as exc:
        logger.warning("[emotion] 保存状态失败: %s", exc)


def _audit(record: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("[emotion] 写审计日志失败: %s", exc)


def memory_context(group_id: str, limit: int = 6) -> dict:
    """只读地借 dsh-memory 的近期消息与档案做背景；失败一律空。"""
    try:
        con = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute(
                "SELECT user_id,name,text,ts FROM buffer WHERE group_id=? ORDER BY id DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
            facts = con.execute(
                "SELECT user_id,kind,content FROM facts WHERE group_id=? ORDER BY updated_at DESC LIMIT 8",
                (group_id,),
            ).fetchall()
        finally:
            con.close()
        return {
            "recent": [{"user_id": str(r[0]), "name": str(r[1] or ""), "text": str(r[2] or "")[:120]}
                       for r in reversed(rows)],
            "facts": [{"user_id": str(r[0]), "kind": str(r[1]), "content": str(r[2])[:160]}
                      for r in facts],
        }
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {"recent": [], "facts": []}


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self.states = _load()
        self._lock = asyncio.Lock()
        logger.info(
            "[emotion] 已加载：%s 影子=%s 连续中性清零=%d条 TTL(生气/低落/开心/好奇/尴尬)=%.0f/%.0f/%.0f/%.0f/%.0fs",
            "开" if ENABLED else "关", SHADOW, NEUTRAL_RUN,
            _ttl("angry"), _ttl("sad"), _ttl("happy"), _ttl("curious"), _ttl("awkward"),
        )

    # ---- 路径 1：每条真人群消息都更新（影子模式下只预测）情绪状态
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def observe(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            if event.get_extra("dsh_initiate"):
                return  # 主动开口的合成事件不许反过来改情绪
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            if not gid or not uid or uid == str(event.get_self_id() or ""):
                return
            text = (event.get_message_str() or "").strip()
            if not text:
                return
            at_bot = bool(getattr(event, "is_at_or_wake_command", False))
            candidate = extract_candidate(text, at_bot)
            now = time.time()
            async with self._lock:
                previous = normalize_state(self.states.get(gid))
                predicted, reason = transition(previous, candidate, now)
                if not SHADOW:
                    self.states[gid] = predicted
                    _save(self.states)
                _audit({
                    "ts": now,
                    "day": datetime.fromtimestamp(now, ZoneInfo(TZ)).isoformat(timespec="seconds"),
                    "group_id": gid, "user_id": uid, "shadow": SHADOW,
                    "at_bot": at_bot, "text": text[:160], "candidate": candidate,
                    "before": previous, "predicted": predicted, "reason": reason,
                })
            if candidate or reason not in ("unchanged", "neutral_pending"):
                logger.info(
                    "[emotion] gid=%s 影子=%s %s(%d) -> %s(%d) 原因=%s 指向=%s",
                    gid, SHADOW, previous["emotion"], previous["intensity"],
                    predicted["emotion"], predicted["intensity"], reason, at_bot or bool(candidate and candidate.get("directed")),
                )
        except BaseException as exc:  # 情绪系统永远不该影响群聊
            logger.debug("[emotion] 观察失败：%s", exc)

    # ---- 路径 2：请求 LLM 前注入情绪（影子模式下**不注入**）
    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not ENABLED or SHADOW:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            gid = str(event.get_group_id() or "")
            if not gid:
                return
            async with self._lock:
                state = normalize_state(self.states.get(gid))
                # 注入前也要过一次 TTL，别把过期情绪塞给模型
                state, reason = transition(state, None, time.time())
                if reason in ("ttl_expired", "neutral_run"):
                    self.states[gid] = state
                    _save(self.states)
            block = render_context(state)
            if not block:
                return
            req.extra_user_content_parts.append(TextPart(text=block) if TextPart else block)
            logger.info("[emotion] gid=%s 注入情绪=%s(%d)", gid, state["emotion"], state["intensity"])
        except BaseException as exc:
            logger.debug("[emotion] 注入失败：%s", exc)

    @filter.command("情绪状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id()) != OWNER:
            return
        gid = str(event.get_group_id() or "")
        state = normalize_state(self.states.get(gid))
        age = int(time.time() - state["started_at"]) if state["started_at"] else 0
        yield event.plain_result(
            "情绪系统：%s｜影子：%s\n当前：%s（强度 %d，已持续 %d 秒）\n证据：%s\n清零条件：%s"
            % ("开" if ENABLED else "关", "开（只判不改）" if SHADOW else "关（真生效）",
               state["emotion"], state["intensity"], age,
               state["evidence"] or "无", "、".join(state["reset_conditions"]) or "无")
        )
