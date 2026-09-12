# -*- coding: utf-8 -*-
"""dsh-guard 的动态判罚层：让情绪 / 欲望 / 关系决定「这次要不要动手、动多久」。

===========================================================================
一、这个模块解决什么

原来的 dsh-guard 是一张固定表：sev=2 警告、累犯禁 5 分钟、sev=3 直接禁
10 分钟。判据本身没问题，但那套阈值对**所有人、所有时刻都一样** ——
机器人刚被骂完正在气头上，和刚被群友夸完心情正好，处理同一句话的结果
完全相同，这不像一个真人，也浪费了已经跑在生产里的情绪/欲望插件。

这一层把「态度类」判罚的松紧交给状态：生气/警戒/关系疏远/被反复骚扰
-> 更严；心情好/玩心重/关系亲近/刚被尊重 -> 更宽。

二、硬边界（写死在代码里，心情无权改动）

  * **行为类不受心情影响**：违法(illegal)、广告(ad)、色情索要(nsfw)、
    刷屏(flood)、明确指向辱骂(direct_insult)、多人围攻(pileon) ——
    这些是**事实**不是**态度**。心情好也不能放行违法内容，
    生气也不能因为一句广告就多禁一倍。用户的原话是
    「让情绪欲望来判定是否禁言，**外加上违法、刷屏等行为**」——
    「外加」这两个字就是这两条轨道分开的意思。
  * **代码兜底判定不动**：诅咒家人 / 群体仇恨由 escalate() 抬到 sev=3，
    这是结构上确定的事实，心情不能把它降回去。
  * 一次判罚最多只能被心情推动**一档**（sev ±1、警告次数 ±1、
    时长 ×0.5~×1.5）。心情不是橡皮筋。
  * 禁言时长仍受 dsh-guard 的 MAX_BAN_SEC 硬上限与同群配额约束，
    管理员/群主/白名单照旧豁免 —— 心情一行都碰不到。

三、读别的插件的状态：只读，且失败即中性

全部用 SQLite `mode=ro` 或 JSON 只读打开，任何异常（表不存在、库被锁、
路径不对、插件没装）都退回**中性**，也就是 dsh-guard 原来的固定行为。
这与 dsh-agency 读 desire/social 的做法一致。
不写任何别人的库：写别人的表会把两个插件耦合死，而且 dsh-emotion 是
整体覆写 JSON 的，抢写必然丢状态。

四、为什么用「宽容度 leniency」而不是直接改分数

一个数（-3 ~ +3），正数=更宽容、负数=更严格，所有来源加起来。
好处是**可解释**：日志里能打出 `len=-1.2(怒3/逆反+18/烦3)`，
出问题一眼看得出是谁把这次判罚推严的，而不是「黑箱调参」。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 硬/软分类
# 行为类：心情无权改动。nsfw 放进来是因为它是「在要东西」这个行为本身
# （与 effective_severity 里 nsfw 不吃玩梗降级是同一条理由）。
HARD_KINDS = ("illegal", "ad", "nsfw")
# 态度类：这两类的边界本来就是judgment call ——
# 「骂战」在这个群是常态社交，「聊政治」用户的原话是「太过分才禁」。
# 心情只在这里生效。
SOFT_KINDS = ("politics", "attack")


def classify(f: dict) -> tuple[str, bool]:
    """返回 (类型, 是否硬类)。复用 dsh-guard decide() 的类型优先顺序。"""
    kinds = [k for k in ("politics", "nsfw", "illegal", "ad", "pileon", "attack")
             if f.get(k)]
    if not kinds:
        return "", True
    kind = kinds[0]
    # pileon（多人围攻）虽然是 attack 的一种，但它是群主明确点名要管的
    # 欺凌场景，属于事实类：心情不能把它放宽。
    hard = kind in HARD_KINDS or bool(f.get("pileon"))
    return kind, hard


# ---------------------------------------------------------------- 情绪
# 情绪 -> 对「宽容度」的贡献。正=更宽容，负=更严格。
# 依据：愤怒/警戒时人对越界更敏感（严格）；开心/得意/兴奋时更愿意接梗
# （宽容）；低落时不想管事（略宽容）；尴尬/好奇/惊讶与判罚无关。
_EMOTION_WEIGHT = {
    "angry": -1.0,
    "worried": -0.5,
    "awkward": -0.2,
    "happy": 0.6,
    "excited": 0.4,
    "proud": 0.4,
    "sad": 0.3,
    "curious": 0.2,
    "surprised": 0.0,
    "calm": 0.0,
}
# 强度 1/2/3 的缩放：强度 1 的「有点生气」不该和强度 3 一样严。
_INTENSITY_SCALE = {0: 0.0, 1: 0.5, 2: 0.75, 3: 1.0}
# 情绪是**指向自己**（刚被某人骂/被点名）时，整体再严格一点。
# 注意这是**群级**信号：情绪状态里没有「针对谁」的字段，
# 所以它只用来判断「刚被惹过，整体没耐心」，不会算到具体某个人头上。
_DIRECTED_EXTRA = -0.2

# ---------------------------------------------------------------- 欲望
# drive 的半衰期（秒），与 dsh-desire 的 HALF_LIFE 同表。
# 抄一份而不是 import：两个插件不能互相 import，只能各存一份；
# 改了记得两边一起改（与 dsh-memory / dsh-guard 里 _is_group_admin 同一约定）。
_DRIVE_HALF_LIFE = {
    "continuity": 86400.0, "integrity": 43200.0, "competence": 21600.0,
    "nociception": 2700.0, "fear": 7200.0,
    "autonomy": 10800.0, "rest": 5400.0,
    "connection": 21600.0, "recognition": 5400.0,
    "curiosity": 10800.0, "play": 7200.0, "appetite": 14400.0,
}
# (drive, 阈值, 贡献, 说明)。阈值是**基线上浮**的量，不是绝对值，
# 因为每个 drive 的基线差很多（实测生产：play 基线 16、connection 基线 16、
# continuity 基线 45、fear 基线 8）。
_DRIVE_RULES = (
    ("play", 30.0, 0.8, "玩心"),
    ("connection", 30.0, 0.3, "想搭话"),
    ("rest", 30.0, 0.3, "想潜水"),
    ("autonomy", 30.0, -0.4, "守边界"),
    ("fear", 10.0, -0.5, "戒备"),
    ("nociception", 6.0, -0.5, "类痛觉"),
)
# 欲望这一路的总贡献封顶。
#
# ★ 这个 0.5 是拿生产数据校准出来的，不是拍的 ★
# 第一版封顶 1.0，上线前拿真状态跑了一遍：机器人长期是「玩心 64 + 想搭话 100」
# （基线都是 16，connection 基本钉在上限），于是**对所有人**都恒定 +1.0 ——
# 等于把「态度类」的严重度永久降一档，骂战从「警告」直接变成「放过」且不留记录。
# 这不是「动态」，这是给整张表乘了个常数。
# 结论：欲望是**氛围**，不是**事件**。它只该是个推力（±0.5，正好落在弱信号那一档，
# 也就是「多给/少给一次警告机会」），不该单独决定动不动手；
# 情绪（生气）和关系（避让/亲近）才是能单独推动一档的强信号。
_DRIVE_CAP = 0.5
# 与 dsh-agency 同款门控：sated/cooldown 的 drive 不算数
_DRIVE_DEAD_PHASES = ("sated", "cooldown")

# ---------------------------------------------------------------- 社交关系
# 档位由 dsh-social 的 tier_of() 定义（阈值 DSH_*_THRESHOLD）。
# 加分门槛参考生产实测：style 模式下 affinity 量级很小（主群最高 ~3.0），
# 所以「亲近/熟人」在真群里极少出现 —— 这不是 bug，是关系还没攒出来，
# 逻辑正确即可，日志里能看到是谁触发的。
_TIER_LENIENCY = {"亲近": 1.0, "熟人": 0.5, "陌生": 0.0, "疏离": -0.5, "避让": -1.0}
# trust 基线 20（dsh-social 默认）。高低各给一点。
_TRUST_HIGH, _TRUST_LOW = 45.0, 10.0

# ---------------------------------------------------------------- 逆反 / 疲劳
# dsh-agency 的 reactance 基线是 8.0，单次事件 delta ∈ [-30, +35]，
# 半衰期 6h。所以用「超出基线多少」来分档，而不是绝对值。
_REACTANCE_BASE = 8.0
# dsh-fatigue 的分级是 0~3（同一人追问同一话题的次数越多越高）。
_FATIGUE_LENIENCY = {0: 0.0, 1: 0.0, 2: -0.35, 3: -0.7}

# 宽容度总范围
LEN_MIN, LEN_MAX = -3.0, 3.0


@dataclass
class Tunables:
    """全部可调项，集中在一处，方便单测与 /禁言状态 展示。

    阈值怎么定的（这是整个模块最需要解释的一组数）：
    强度 3 的生气是 -1.0、「避让」档是 -1.0、「亲近+高信任」是 +1.3 ——
    也就是说**一个足够强的信号就能推动一档**，两个弱信号叠加也行。
    一开始把线画在 ±1.5 上，结果单独一个「非常生气」只有 -1.0，
    什么都改不了（写集成测试时发现的），等于情绪白读了。
    分两档是为了让弱信号不白读：严重度只在 |len|>=1.0 时动，
    警告门槛在 |len|>=0.5 时就会动 —— 弱信号表达成「多给一次机会」，
    而不是直接改变动不动手。
    """

    weight: float = 1.0        # 总权重，0=等于关掉心情（原来的固定行为）
    max_step: int = 1          # 心情最多推动几档（sev / 警告次数 / 时长档）
    len_strict: float = 1.0    # >= 这个值：放宽一档
    len_mild_strict: float = 0.5
    len_mild_lenient: float = -0.5
    len_lenient: float = -1.0  # <= 这个值：加严一档
    dur_min: float = 0.5       # 时长倍率下限
    dur_max: float = 1.5       # 时长倍率上限
    dur_slope: float = 0.15    # 宽容度每 1.0 折算成多少倍率
    sec_floor: int = 60        # 心情算出来的禁言时长下限（秒）

    @staticmethod
    def from_env(env=None) -> "Tunables":
        e = env if env is not None else os.environ

        def num(name: str, default: float) -> float:
            try:
                return float(e.get(name, default))
            except (TypeError, ValueError):
                return default

        t = Tunables(
            weight=max(0.0, min(3.0, num("DSH_GUARD_MOOD_WEIGHT", 1.0))),
            max_step=max(0, min(2, int(num("DSH_GUARD_MOOD_MAX_STEP", 1)))),
            len_strict=num("DSH_GUARD_MOOD_LENIENT_AT", 1.0),
            len_mild_strict=num("DSH_GUARD_MOOD_MILD_LENIENT_AT", 0.5),
            len_mild_lenient=num("DSH_GUARD_MOOD_MILD_STRICT_AT", -0.5),
            len_lenient=num("DSH_GUARD_MOOD_STRICT_AT", -1.0),
            dur_min=max(0.1, min(1.0, num("DSH_GUARD_MOOD_DUR_MIN", 0.5))),
            dur_max=max(1.0, min(3.0, num("DSH_GUARD_MOOD_DUR_MAX", 1.5))),
            dur_slope=max(0.0, min(1.0, num("DSH_GUARD_MOOD_DUR_SLOPE", 0.15))),
            sec_floor=max(1, int(num("DSH_GUARD_MOOD_SEC_FLOOR", 60))),
        )
        # 阈值必须有序，否则静默失效（配置写反了要能看出来）
        if not (t.len_lenient <= t.len_mild_lenient < t.len_mild_strict <= t.len_strict):
            return Tunables(weight=t.weight, max_step=t.max_step)
        return t


@dataclass(frozen=True)
class Mood:
    """一次读取到的全部状态。任何一项没读到就是默认值（=中性）。"""

    emotion: str = "calm"
    intensity: int = 0
    directed: bool = False
    emotion_live: bool = False          # 情绪是否还没过期
    drives: dict = field(default_factory=dict)   # drive -> 衰减后的有效值
    tier: str = "陌生"
    trust: float = 20.0
    reactance: float = _REACTANCE_BASE
    fatigue: int = 0
    sources: tuple = ()                 # 成功读到的来源（日志用）
    errors: tuple = ()                  # 读失败的原因（日志用）


def neutral() -> Mood:
    return Mood()


# ---------------------------------------------------------------- 宽容度
def _emotion_leniency(mood: Mood) -> tuple[float, str]:
    if not mood.emotion_live or mood.emotion == "calm":
        return 0.0, ""
    base = _EMOTION_WEIGHT.get(mood.emotion, 0.0)
    if not base:
        return 0.0, ""
    value = base * _INTENSITY_SCALE.get(max(0, min(3, int(mood.intensity))), 0.0)
    if not value:
        return 0.0, ""
    tag = "%s%d" % (mood.emotion, int(mood.intensity))
    if mood.directed and base < 0:
        value += _DIRECTED_EXTRA
        tag += "·指向我"
    return value, tag


def _drive_leniency(mood: Mood) -> tuple[float, list[str]]:
    total, tags = 0.0, []
    for drive, margin, value, label in _DRIVE_RULES:
        row = mood.drives.get(drive)
        if not row:
            continue
        eff, base, phase = row
        if phase in _DRIVE_DEAD_PHASES:
            continue
        if eff - base >= margin:
            total += value
            tags.append("%s%.0f" % (label, eff))
    return max(-_DRIVE_CAP, min(_DRIVE_CAP, total)), tags


def _relation_leniency(mood: Mood) -> tuple[float, list[str]]:
    total, tags = 0.0, []
    tier_value = _TIER_LENIENCY.get(mood.tier, 0.0)
    if tier_value:
        total += tier_value
        tags.append(mood.tier)
    if mood.trust >= _TRUST_HIGH:
        total += 0.3
        tags.append("信任%.0f" % mood.trust)
    elif mood.trust <= _TRUST_LOW:
        total -= 0.3
        tags.append("低信任%.0f" % mood.trust)
    return max(-1.0, min(1.3, total)), tags


def _reactance_leniency(mood: Mood) -> tuple[float, str]:
    excess = float(mood.reactance) - _REACTANCE_BASE
    if excess >= 22.0:
        return -0.7, "逆反+%.0f" % excess
    if excess >= 8.0:
        return -0.35, "逆反+%.0f" % excess
    return 0.0, ""


def leniency(mood: Mood, tun: Tunables | None = None) -> tuple[float, list[str]]:
    """把状态折成一个数：>0 更宽容、<0 更严格。纯函数，好单测。

    返回 (宽容度, 因子标签)。标签是给日志和 /禁言状态 用的，
    出问题时必须能一眼看出是谁把这次判罚推严的。
    """
    t = tun or Tunables()
    if t.weight <= 0 or mood is None:
        return 0.0, []
    total, tags = 0.0, []
    value, tag = _emotion_leniency(mood)
    total += value
    if tag:
        tags.append(tag)
    value, drive_tags = _drive_leniency(mood)
    total += value
    tags += drive_tags
    value, rel_tags = _relation_leniency(mood)
    total += value
    tags += rel_tags
    value, tag = _reactance_leniency(mood)
    total += value
    if tag:
        tags.append(tag)
    fatigue_value = _FATIGUE_LENIENCY.get(max(0, min(3, int(mood.fatigue))), 0.0)
    if fatigue_value:
        total += fatigue_value
        tags.append("烦%d" % int(mood.fatigue))
    return max(LEN_MIN, min(LEN_MAX, total * t.weight)), tags


# ---------------------------------------------------------------- 应用到判罚
def raised_only(base_sev: int, new_sev: int) -> bool:
    """这次动手是**心情抬出来的**吗（原规则本来会放过）。

    ★ 这是整个动态层最重要的一条硬边界 ★
    这种情形只允许「警告」，绝不允许「禁言」。理由：警告只是说一句话，
    而禁言不可逆；更关键的是 sev=2 的第二道闸门是**累犯计数**——
    生气时如果允许翻 24 小时内的旧账，一句本来没事的话会直接吃禁言。
    误禁是这个模块唯一不能犯的错，所以这条边界写在纯函数里，单独测。
    """
    return int(base_sev) <= 1 < int(new_sev)


def severity_step(sev: int, len_value: float, tun: Tunables | None = None) -> int:
    """心情对严重度的一档调整：返回新的 sev。"""
    t = tun or Tunables()
    if t.max_step <= 0 or t.weight <= 0:
        return sev
    if len_value <= t.len_lenient and sev >= 1:
        return min(3, sev + t.max_step)
    if len_value >= t.len_strict and sev >= 2:
        # 放宽：sev2 -> 1（等于这次算了），sev3 -> 2（先警告）
        return max(0, sev - t.max_step)
    return sev


def warn_need(base_warn: int, len_value: float, tun: Tunables | None = None) -> int:
    """需要累计几次才禁。

    ★ 这一档比严重度那档更容易触发（|len|>=0.5 而不是 1.0）★
    弱信号不该直接改变动不动手，但可以改变「多给一次机会 / 少给一次机会」——
    这是把「有点烦躁」表达出来又不至于误禁的那条路。
    严格时会变成 need=1，也就是**真违规的首次就直接禁言**；这是刻意的：
    生气 + 真违规仍然先警告一次，等于情绪毫无作用。
    """
    t = tun or Tunables()
    if t.max_step <= 0 or t.weight <= 0:
        return base_warn
    if len_value <= t.len_mild_lenient:
        return max(1, base_warn - (t.max_step if len_value <= t.len_lenient else 1))
    if len_value >= t.len_mild_strict:
        return base_warn + (t.max_step if len_value >= t.len_strict else 1)
    return base_warn


def duration_factor(len_value: float, tun: Tunables | None = None) -> float:
    """时长倍率：越严格越长，越宽容越短。

    斜率 0.15 意味着宽容度满值（±3）也只到 ×0.55 ~ ×1.45 ——
    刻意不到 2 倍：把 5 分钟变成 10 分钟是「情绪」，变成 1 小时就是另一套规矩了。
    """
    t = tun or Tunables()
    if t.weight <= 0:
        return 1.0
    factor = 1.0 - t.dur_slope * float(len_value)
    return max(t.dur_min, min(t.dur_max, factor))


def apply_duration(sec: int, len_value: float, *, tun: Tunables | None = None,
                   hard: bool = False, bypass_cap: bool = False,
                   cap: int = 1800) -> int:
    """把心情算出来的倍率落到秒数上。

    hard=True（行为类）或 bypass_cap=True（刷屏一天，用户明确要的惩罚）
    时原样返回 —— 刷屏的时长是**惩罚决定**，不是态度，不该被心情砍成一半。
    """
    t = tun or Tunables()
    base = max(1, int(sec))
    if hard or bypass_cap or t.weight <= 0:
        return base
    value = int(round(base * duration_factor(len_value, t)))
    value = max(t.sec_floor, value)
    return max(1, min(value, cap))


def describe(mood: Mood, len_value: float, tags: list[str]) -> str:
    """一行给日志/指令看的状态摘要。"""
    head = "%s%d" % (mood.emotion, int(mood.intensity)) if mood.emotion_live else "无情绪"
    if mood.directed and mood.emotion_live:
        head += "(指向我)"
    return "len=%+.1f %s [%s] 来源=%s%s" % (
        len_value, head, "／".join(tags) or "无",
        "、".join(mood.sources) or "无",
        (" 失败:" + "、".join(mood.errors)) if mood.errors else "",
    )


# ---------------------------------------------------------------- 状态读取
def _read_json(path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _decay(eff: float, base: float, updated_at: float, drive: str, now: float) -> float:
    """与 dsh-desire 同款的基线衰减，避免读到几天前的残留高值。"""
    half = _DRIVE_HALF_LIFE.get(drive, 10800.0)
    elapsed = max(0.0, float(now) - float(updated_at or now))
    return float(base) + (float(eff) - float(base)) * math.pow(0.5, elapsed / half)


class MoodReader:
    """只读地把情绪/欲望/关系/逆反/疲劳汇总成一个 Mood。

    所有路径都可配；读不到就中性（等于原来的固定阈值行为）。
    带一层短缓存：guard 只在预筛命中和刷屏/辱骂路径上调用，
    频率本来就低，缓存是为了同一波消息不重复开库。
    """

    def __init__(self, tun: Tunables | None = None, env=None) -> None:
        e = env if env is not None else os.environ
        self.tun = tun or Tunables.from_env(e)
        self.enabled = str(e.get("DSH_GUARD_MOOD", "1")).strip().lower() not in {
            "0", "false", "off", "no"}
        self.emotion_path = Path(e.get("DSH_GUARD_MOOD_EMOTION",
                                      "/AstrBot/data/dsh_emotion_state.json"))
        self.interest_path = Path(e.get("DSH_GUARD_MOOD_INTEREST",
                                        "/AstrBot/data/dsh_interest_state.json"))
        self.fatigue_path = Path(e.get("DSH_GUARD_MOOD_FATIGUE",
                                       "/AstrBot/data/dsh_fatigue_state.json"))
        self.desire_db = e.get("DSH_GUARD_MOOD_DESIRE_DB", "/AstrBot/data/dsh_desire.db")
        self.agency_db = e.get("DSH_GUARD_MOOD_AGENCY_DB", "/AstrBot/data/dsh_agency.db")
        self.social_db = e.get("DSH_GUARD_MOOD_SOCIAL_DB", "/AstrBot/data/dsh_social.db")
        try:
            self.fatigue_window = float(e.get("DSH_GUARD_MOOD_FATIGUE_WINDOW", "1200"))
        except (TypeError, ValueError):
            self.fatigue_window = 1200.0
        try:
            self.cache_ttl = max(0.0, float(e.get("DSH_GUARD_MOOD_CACHE", "20")))
        except (TypeError, ValueError):
            self.cache_ttl = 20.0
        self._cache: dict = {}

    # ---- 单个来源
    def _emotion(self, gid: str, now: float) -> tuple[dict, str]:
        data = _read_json(self.emotion_path)
        if not data:
            return {}, "情绪未读到"
        row = data.get(gid)
        if not isinstance(row, dict):
            return {}, ""
        expires = float(row.get("expires_at", 0) or 0)
        live = bool(row.get("emotion") and row.get("emotion") != "calm"
                    and (expires == 0 or expires > now))
        return {
            "emotion": str(row.get("emotion") or "calm"),
            "intensity": int(row.get("intensity", 0) or 0),
            "directed": bool(row.get("directed")),
            "emotion_live": live,
        }, ""

    def _drives(self, gid: str, now: float) -> tuple[dict, str]:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self.desire_db, uri=True, timeout=1)
            try:
                rows = con.execute(
                    "SELECT drive,intensity,baseline,updated_at,phase FROM drive_state "
                    "WHERE group_id=?", (gid,)).fetchall()
            finally:
                con.close()
        except (OSError, sqlite3.Error, TypeError, ValueError) as e:
            return {}, "欲望读失败(%s)" % type(e).__name__
        out = {}
        for drive, eff, base, updated, phase in rows:
            out[str(drive)] = (_decay(float(eff), float(base), float(updated),
                                      str(drive), now),
                               float(base), str(phase))
        return out, ""

    def _relation(self, gid: str, uid: str) -> tuple[dict, str]:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self.social_db, uri=True, timeout=1)
            try:
                row = con.execute(
                    "SELECT affinity,trust,manual_tier FROM relations "
                    "WHERE group_id=? AND user_id=?", (gid, uid)).fetchone()
            finally:
                con.close()
        except (OSError, sqlite3.Error, TypeError, ValueError) as e:
            return {}, "关系读失败(%s)" % type(e).__name__
        if not row:
            return {}, ""
        affinity, trust, manual = float(row[0] or 0), float(row[1] or 20), str(row[2] or "")
        # 档位阈值与 dsh-social.tier_of 一致（那边可配，这里用默认值；
        # 真改了那边的阈值，这里跟着改）
        tier = manual if manual in _TIER_LENIENCY else (
            "亲近" if affinity >= 50 and trust >= 45 else
            "熟人" if affinity >= 15 else
            "避让" if affinity <= -50 else
            "疏离" if affinity <= -15 else "陌生")
        return {"tier": tier, "trust": trust}, ""

    def _reactance(self, gid: str, uid: str, now: float) -> tuple[float, str]:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self.agency_db, uri=True, timeout=1)
            try:
                row = con.execute(
                    "SELECT reactance,updated_at FROM agency_state "
                    "WHERE group_id=? AND user_id=?", (gid, uid)).fetchone()
            finally:
                con.close()
        except (OSError, sqlite3.Error, TypeError, ValueError) as e:
            return _REACTANCE_BASE, "逆反读失败(%s)" % type(e).__name__
        if not row:
            return _REACTANCE_BASE, ""
        value, updated = float(row[0] or _REACTANCE_BASE), float(row[1] or now)
        # 与 dsh-agency 同款衰减（半衰期 6h）
        elapsed = max(0.0, now - updated)
        value = _REACTANCE_BASE + (value - _REACTANCE_BASE) * math.pow(
            0.5, elapsed / (6 * 3600.0))
        return value, ""

    def _fatigue(self, gid: str, uid: str, now: float) -> tuple[int, str]:
        """疲劳：这个人最近是不是在反复追同一件事。

        ★ 这里**不做话题匹配** ★ dsh-fatigue 的等级是按「同一话题」算的，
        而匹配要用它自己的 clean_topic/topic_similarity —— 两个插件不能互相
        import，抄一份匹配逻辑就等于埋一个「哪天不一致」的雷。
        所以这里取一个更粗但方向一致的代理：该 uid 在所有话题里，
        **单个话题**在窗口内的追问次数最大值。它可能把「换了话题」也当成
        还在纠缠，所以它的权重刻意给得很小（最多 -0.7），
        只用来表达「这人最近老在磨同一件事，耐心低一点」。
        """
        data = _read_json(self.fatigue_path)
        topics = ((data.get("groups") or {}).get(gid)
                  if isinstance(data.get("groups"), dict) else None)
        if not isinstance(topics, list) or not topics:
            return 0, ""
        best = 0
        for item in topics:
            if not isinstance(item, dict):
                continue
            replies = item.get("replies")
            if not isinstance(replies, list):
                continue
            live = [r for r in replies if isinstance(r, dict)
                    and now - float(r.get("ts", 0) or 0) <= self.fatigue_window]
            same = sum(1 for r in live if str(r.get("uid", "")) == str(uid))
            best = max(best, same)
        # 与 dsh-fatigue 同款分档：<=1 不算，2 算 2 级，>=3 算 3 级
        return (0 if best <= 1 else 2 if best == 2 else 3), ""

    # ---- 汇总
    def read(self, gid: str, uid: str, now: float | None = None) -> Mood:
        if not self.enabled:
            return neutral()
        now = float(now if now is not None else time.time())
        key = (str(gid), str(uid))
        hit = self._cache.get(key)
        if hit and self.cache_ttl > 0 and now - hit[0] <= self.cache_ttl:
            return hit[1]
        mood = self._collect(str(gid), str(uid), now)
        if self.cache_ttl > 0:
            self._cache[key] = (now, mood)
            if len(self._cache) > 512:      # 别让缓存无限长
                for k in list(self._cache)[:256]:
                    self._cache.pop(k, None)
        return mood

    def _collect(self, gid: str, uid: str, now: float) -> Mood:
        sources, errors = [], []
        emo, err = self._emotion(gid, now)
        if emo:
            sources.append("情绪")
        if err:
            errors.append(err)
        drives, err = self._drives(gid, now)
        if drives:
            sources.append("欲望")
        if err:
            errors.append(err)
        rel, err = self._relation(gid, uid)
        if rel:
            sources.append("关系")
        if err:
            errors.append(err)
        react, err = self._reactance(gid, uid, now)
        if react != _REACTANCE_BASE:
            sources.append("逆反")
        if err:
            errors.append(err)
        fatigue, err = self._fatigue(gid, uid, now)
        if fatigue:
            sources.append("疲劳")
        if err:
            errors.append(err)
        return Mood(
            emotion=emo.get("emotion", "calm"),
            intensity=int(emo.get("intensity", 0) or 0),
            directed=bool(emo.get("directed")),
            emotion_live=bool(emo.get("emotion_live")),
            drives=drives,
            tier=str(rel.get("tier", "陌生")),
            trust=float(rel.get("trust", 20.0) or 20.0),
            reactance=float(react),
            fatigue=int(fatigue),
            sources=tuple(sources),
            errors=tuple(errors),
        )
