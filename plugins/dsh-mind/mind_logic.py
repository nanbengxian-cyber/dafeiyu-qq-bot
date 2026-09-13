# -*- coding: utf-8 -*-
"""dsh-mind 逻辑层：把散落在 11 个状态库里的「岛」读成一张快照（P0 只观测）。

===========================================================================
一、这一层要解决什么
===========================================================================

主群里现在有 11 个各自独立的状态存储（情绪 JSON、欲望 DB、社交 DB、逆反 DB、
疲劳 JSON、兴趣 JSON、利益 JSON、群感知 DB、自我认知 DB、效果 DB、以及
proactive/initiate 的行动额度 JSON），33 个插件往同一次请求里各塞各的
`extra_user_content_parts`。**没有一个地方知道这一轮到底有几个岛在说话、
它们互相矛不矛盾、加起来多少字。**

本文件就是那个「知道的地方」：只读地把它们读成一张 MindSnapshot，
再算出「岛数 / 统一草稿 / 冲突」。P0 绝不注入、绝不写别人的库。

===========================================================================
二、设计约束（照抄 dsh-guard/mood_logic.py 已经验证过的那套，别自己发明）
===========================================================================

1. **只读 + 失败即中性**。JSON 用只读打开、SQLite 一律 `mode=ro`，
   任何异常（库不存在/被锁/插件没装/字段缺）都退回中性值并记进 errors。
   绝不写别人的库 —— dsh-emotion 是整体覆写 JSON 的，抢写必然丢状态。

2. **衰减必须和所有者一致**。读的是「当前有效值」而不是库里的原始值：
   欲望按 half_life 从基线半衰（与 dsh-desire 同款）、逆反半衰期 6h
   （与 dsh-agency 同款）、情绪看 expires_at 有没有过期。抄错一处，
   整张快照就会长期偏高或偏低 —— 这是本层唯一的「数值正确性」要求。

3. **疲劳优先读事件上的 extras**。dsh-fatigue 在它自己的
   on_llm_request(priority=1800) 里把 dsh_fatigue_level/topic 写回 event，
   本插件 priority=-1 最后跑，能拿到**精确的按话题等级**；
   拿不到才退回 dsh-guard 那个更粗的代理算法（同一人在单个话题里追问次数最大值）。

4. **不存别人的原文**。观测库只记「块标签 + 字数」，不记块内容 ——
   `<scene>` 之类块里是群聊原文，复制进一个新库等于凭空多一份群聊副本。

5. **优先级 -1 = 最后跑**。AstrBot 的 star_handler 用
   `sort(key=lambda h: -priority)` 排（数字大的先跑），所以 -1 一定排在
   ctxclean/interest 等默认 0 的注入器之后，看到的是**本轮最终**的请求形状。
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 常量（抄自各自的所有者）
# 出处：dsh-guard/mood_logic.py:_DRIVE_HALF_LIFE（与 dsh-desire 一致）
DRIVE_HALF_LIFE = {
    "continuity": 86400.0, "integrity": 43200.0, "competence": 21600.0,
    "nociception": 2700.0, "fear": 7200.0,
    "autonomy": 10800.0, "rest": 5400.0,
    "connection": 21600.0, "recognition": 5400.0,
    "curiosity": 10800.0, "play": 7200.0, "appetite": 14400.0,
}
# 出处：dsh-desire/desire_logic.py:BASELINES —— **只当兜底**。
#
# ★ 别把它当权威。2026-09-13 拿生产库比对，发现这份字典已经跟线上不一致：
#   recognition 线上 8.0 / 这里 16.0，curiosity 线上 18.0 / 这里 16.0，
#   appetite 线上 14.0 / 这里 16.0。drive_state 表**自己带 baseline 列**，
#   那才是权威（dsh-desire 在改常数时会把值写进新行）。dsh-guard 也是读库里的列。
#   所以这里只在行里的 baseline 缺失或 ≤0（等于没设）时兜底，
#   免得「基线读不到」被算成「偏离极大」。
DRIVE_BASELINE = {
    "continuity": 45.0, "integrity": 32.0, "competence": 28.0,
    "nociception": 4.0, "fear": 8.0, "autonomy": 12.0, "rest": 18.0,
    "connection": 16.0, "recognition": 16.0, "curiosity": 16.0,
    "play": 16.0, "appetite": 16.0,
}


def _baseline_of(drive, row_baseline):
    """基线以库里的列为准；列缺失/为 0 时才退回常量表。"""
    try:
        base = float(row_baseline)
    except (TypeError, ValueError):
        base = 0.0
    if base > 0:
        return base
    return DRIVE_BASELINE.get(str(drive), 16.0)
# 出处：dsh-agency/main.py:_DESIRE_NAMES（展示名，保持两边一致）
DRIVE_NAMES = {
    "continuity": "存续", "integrity": "身份与记忆完整", "competence": "能力稳态",
    "nociception": "机器类痛觉", "fear": "风险警戒",
    "curiosity": "好奇", "connection": "连接", "play": "玩心", "appetite": "馋意",
    "recognition": "想被看见", "autonomy": "自主", "rest": "安静",
}
# 底层机器本能：dsh-agency 刻意不重复注入这五个，本层只记录不参与排序
FOUNDATIONAL_DRIVES = ("continuity", "integrity", "competence", "nociception", "fear")
EMOTION_NAMES = {
    "calm": "平静", "happy": "开心", "sad": "低落", "angry": "生气",
    "curious": "好奇", "awkward": "尴尬", "excited": "兴奋",
    "surprised": "震惊", "proud": "得意", "worried": "担心",
}
# 出处：dsh-agency 的 reactance 基线 8.0，半衰期 6h；dsh-guard 也按「超出基线多少」分档
REACTANCE_BASE = 8.0
REACTANCE_HALF = 6 * 3600.0
REACTANCE_ON = 8.0          # 超出基线这么多才算「在说话」
# 出处：dsh-social 的档位阈值（manual_tier 优先）
TIER_NEAR_AFFINITY, TIER_NEAR_TRUST = 50.0, 45.0
TIER_FAMILIAR = 15.0
TIER_AVOID = -50.0
TIER_DISTANT = -15.0

# 块标签：只认这种形状，认不出就记 "(无标签)"，不猜
_TAG_RE = re.compile(r"<\s*([a-z_]{3,32})\s*>")


# ---------------------------------------------------------------- 读取原语
def read_json(path) -> dict:
    """只读一个 JSON 对象；任何异常都退回 {}（=中性）。"""
    try:
        with open(str(path), encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def drive_effective(eff: float, base: float, updated_at: float, drive: str, now: float) -> float:
    """与 dsh-desire 同款的基线衰减，避免读到几天前的残留高值。"""
    half = DRIVE_HALF_LIFE.get(drive, 10800.0)
    elapsed = max(0.0, float(now) - float(updated_at or now))
    return float(base) + (float(eff) - float(base)) * math.pow(0.5, elapsed / half)


def reactance_effective(value: float, updated_at: float, now: float) -> float:
    """与 dsh-agency 同款的逆反衰减（半衰期 6h）。"""
    elapsed = max(0.0, float(now) - float(updated_at or now))
    return REACTANCE_BASE + (float(value) - REACTANCE_BASE) * math.pow(
        0.5, elapsed / REACTANCE_HALF)


def social_tier(affinity: float, trust: float, manual: str = "") -> str:
    """档位判定与 dsh-social.tier_of / dsh-guard._relation 同款阈值。"""
    manual = str(manual or "")
    if manual in ("亲近", "熟人", "陌生", "疏离", "避让"):
        return manual
    if affinity >= TIER_NEAR_AFFINITY and trust >= TIER_NEAR_TRUST:
        return "亲近"
    if affinity >= TIER_FAMILIAR:
        return "熟人"
    if affinity <= TIER_AVOID:
        return "避让"
    if affinity <= TIER_DISTANT:
        return "疏离"
    return "陌生"


def fatigue_proxy(data: dict, gid: str, uid: str, now: float, window: float) -> int:
    """粗代理：某人在**单个话题**里窗口内的追问次数最大值（0/2/3 档）。

    这是 dsh-guard 的做法，只有在读不到 event extra 时才用 —— 它会把
    「换了话题」也当成还在纠缠，所以只用来兜底。
    """
    groups = data.get("groups") if isinstance(data.get("groups"), dict) else None
    topics = (groups or {}).get(gid) if groups else None
    if not isinstance(topics, list):
        return 0
    best = 0
    for item in topics:
        if not isinstance(item, dict):
            continue
        replies = item.get("replies")
        if not isinstance(replies, list):
            continue
        same = sum(
            1 for r in replies
            if isinstance(r, dict)
            and now - float(r.get("ts", 0) or 0) <= window
            and str(r.get("uid", "")) == str(uid)
        )
        best = max(best, same)
    return 0 if best <= 1 else (2 if best == 2 else 3)


def extra_int(getter, name: str, default: int = 0) -> int:
    """从 event.get_extra 取一个整数；拿不到/不是数字都用默认值。"""
    try:
        value = getter(name)
    except BaseException:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extra_str(getter, name: str, default: str = "") -> str:
    try:
        value = getter(name)
    except BaseException:
        return default
    return str(value) if value else default


# ---------------------------------------------------------------- 本轮请求的形状
@dataclass
class Part:
    """`extra_user_content_parts` 里的一块：只留标签和字数，不留内容。"""
    tag: str
    chars: int


def scan_parts(parts) -> "list[Part]":
    """把本轮注入块读成 [Part]。

    ★ 只读标签和长度 ★ 块内容是群聊原文（<scene> 等），既不记日志也不入库。
    兼容 TextPart 与裸 str（有插件在拿不到 TextPart 时会退化成裸串）。
    """
    out = []
    for part in list(parts or []):
        text = getattr(part, "text", None)
        if not isinstance(text, str):
            text = part if isinstance(part, str) else str(part or "")
        match = _TAG_RE.search(text)
        out.append(Part(tag=match.group(1) if match else "(无标签)", chars=len(text)))
    return out


def scan_contexts(contexts) -> int:
    """历史（req.contexts）总字数。用来算「注入占整个 prompt 的多少」——
    docs/40 的根因就是历史里的注入块堆积，这个比值是它的体检指标。"""
    total = 0
    for message in list(contexts or []):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict):
                    total += len(str(piece.get("text", "")))
                else:
                    total += len(str(piece))
        elif content is not None:
            total += len(str(content))
    return total


# ---------------------------------------------------------------- 快照
@dataclass
class MindSnapshot:
    """一次读取到的全部岛。任何一项没读到就是默认值（=中性）。"""
    gid: str = ""
    uid: str = ""
    # 情绪
    emotion: str = "calm"
    intensity: int = 0
    directed: bool = False
    emotion_live: bool = False
    # 欲望：drive -> (衰减后有效值, 基线, phase)
    drives: dict = field(default_factory=dict)
    # 关系
    tier: str = "陌生"
    affinity: float = 0.0
    trust: float = 20.0
    avoid: bool = False
    # 逆反
    reactance: float = REACTANCE_BASE
    # 疲劳
    fatigue: int = 0
    fatigue_topic: str = ""
    fatigue_from: str = ""          # "extra"=精确 | "proxy"=粗代理 | ""
    # 兴趣
    mood_items: tuple = ()
    hot: tuple = ()                 # ((词, 热度), ...)
    # 自身利益
    selfworth_kinds: tuple = ()     # 窗口内冲它来的占便宜类型
    selfworth_ago: float = -1.0
    # 群感知
    awareness_recent: tuple = ()    # ((kind, count), ...)
    # 自我认知
    selfaware_fails: int = 0
    # 行动额度（主动开口类，P3 要归一的那几条链）
    action_quota: tuple = ()        # (("initiate", 已用, 上限), ...)
    # 元信息
    sources: tuple = ()             # 成功读到的来源
    errors: tuple = ()              # 读失败的原因
    unreadable: tuple = ()          # 状态不可观测的岛（现为 dsh-spine）


def neutral(gid: str = "", uid: str = "") -> MindSnapshot:
    return MindSnapshot(gid=gid, uid=uid)


# ---------------------------------------------------------------- 只读读取器
class MindReader:
    """只读地把所有岛读成一个 MindSnapshot。任何一路失败都只记 errors。"""

    def __init__(self, env=None) -> None:
        e = env if env is not None else os.environ

        def path(name: str, default: str) -> Path:
            return Path(str(e.get(name, default)))

        data = Path("/AstrBot/data")
        self.emotion_path = path("DSH_MIND_EMOTION_STATE", str(data / "dsh_emotion_state.json"))
        self.interest_path = path("DSH_MIND_INTEREST_STATE", str(data / "dsh_interest_state.json"))
        self.fatigue_path = path("DSH_MIND_FATIGUE_STATE", str(data / "dsh_fatigue_state.json"))
        self.proactive_path = path("DSH_MIND_PROACTIVE_STATE", str(data / "dsh_proactive_state.json"))
        self.initiate_path = path("DSH_MIND_INITIATE_STATE", str(data / "dsh_initiate_state.json"))
        self.selfworth_path = path("DSH_MIND_SELFWORTH_STATE",
                                   "/AstrBot/data/plugins/dsh-selfworth/data/selfworth.json")
        self.desire_db = str(e.get("DSH_MIND_DESIRE_DB", str(data / "dsh_desire.db")))
        self.social_db = str(e.get("DSH_MIND_SOCIAL_DB", str(data / "dsh_social.db")))
        self.agency_db = str(e.get("DSH_MIND_AGENCY_DB", str(data / "dsh_agency.db")))
        self.awareness_db = str(e.get("DSH_MIND_AWARENESS_DB", str(data / "dsh_awareness.db")))
        self.selfaware_db = str(e.get("DSH_MIND_SELFAWARE_DB", str(data / "dsh_selfaware.db")))

        def num(name: str, default: float) -> float:
            try:
                return float(e.get(name, default))
            except (TypeError, ValueError):
                return default

        self.fatigue_window = max(60.0, num("DSH_MIND_FATIGUE_WINDOW", 1200.0))
        self.awareness_window = max(60.0, num("DSH_MIND_AWARENESS_WINDOW", 480.0))
        self.selfworth_window = max(60.0, num("DSH_MIND_SELFWORTH_WINDOW", 900.0))
        self.selfaware_window = max(60.0, num("DSH_MIND_SELFAWARE_WINDOW", 1800.0))

    def _query(self, db: str, sql: str, args) -> list:
        """mode=ro 打开一次、查一次、关掉。失败返回 []，由调用方记 errors。"""
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=1.0)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def read(self, gid: str, uid: str, now: "float | None" = None,
             extra=None) -> MindSnapshot:
        now = float(now if now is not None else time.time())
        snap = neutral(gid, uid)
        sources, errors = [], []
        getter = extra if callable(extra) else (lambda name: None)

        # ---- 情绪（JSON） ------------------------------------------------
        try:
            row = read_json(self.emotion_path).get(gid)
            if isinstance(row, dict):
                expires = float(row.get("expires_at", 0) or 0)
                snap.emotion = str(row.get("emotion") or "calm")
                snap.intensity = int(row.get("intensity", 0) or 0)
                snap.directed = bool(row.get("directed"))
                snap.emotion_live = bool(
                    snap.emotion != "calm" and (expires == 0 or expires > now))
                sources.append("情绪")
            else:
                errors.append("情绪无该群记录")
        except (OSError, ValueError, TypeError) as exc:
            errors.append("情绪读失败(%s)" % type(exc).__name__)

        # ---- 欲望（SQLite） ---------------------------------------------
        try:
            rows = self._query(
                self.desire_db,
                "SELECT drive,intensity,baseline,updated_at,phase FROM drive_state "
                "WHERE group_id=?", (gid,))
            for drive, eff, base, updated, phase in rows:
                base = _baseline_of(drive, base)
                snap.drives[str(drive)] = (
                    drive_effective(float(eff), base, float(updated), str(drive), now),
                    base, str(phase))
            sources.append("欲望")
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            errors.append("欲望读失败(%s)" % type(exc).__name__)

        # ---- 关系（SQLite） ---------------------------------------------
        try:
            rows = self._query(
                self.social_db,
                "SELECT affinity,trust,manual_tier,avoid_until FROM relations "
                "WHERE group_id=? AND user_id=?", (gid, uid))
            if rows:
                row = rows[0]
                snap.affinity = float(row[0] or 0)
                snap.trust = float(row[1] or 20)
                snap.tier = social_tier(snap.affinity, snap.trust, str(row[2] or ""))
                snap.avoid = float(row[3] or 0) > now
                sources.append("关系")
            else:
                errors.append("关系无此人记录")
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            errors.append("关系读失败(%s)" % type(exc).__name__)

        # ---- 逆反（SQLite） ---------------------------------------------
        try:
            rows = self._query(
                self.agency_db,
                "SELECT reactance,updated_at FROM agency_state "
                "WHERE group_id=? AND user_id=?", (gid, uid))
            if rows:
                snap.reactance = reactance_effective(
                    float(rows[0][0] or REACTANCE_BASE), float(rows[0][1] or now), now)
                sources.append("逆反")
            else:
                errors.append("逆反无此人记录")
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            errors.append("逆反读失败(%s)" % type(exc).__name__)

        # ---- 疲劳（event extra 优先，其次粗代理） ------------------------
        try:
            level = extra_int(getter, "dsh_fatigue_level", -1)
            if level >= 0:
                snap.fatigue = min(3, max(0, level))
                snap.fatigue_topic = extra_str(getter, "dsh_fatigue_topic")
                snap.fatigue_from = "extra"
                sources.append("疲劳")
            else:
                data = read_json(self.fatigue_path)
                snap.fatigue = fatigue_proxy(data, gid, uid, now, self.fatigue_window)
                snap.fatigue_from = "proxy"
                sources.append("疲劳(代理)")
        except (OSError, ValueError, TypeError) as exc:
            errors.append("疲劳读失败(%s)" % type(exc).__name__)

        # ---- 兴趣（JSON） -----------------------------------------------
        try:
            data = read_json(self.interest_path)
            mood = data.get("mood") if isinstance(data.get("mood"), dict) else {}
            items = mood.get("items") if isinstance(mood.get("items"), list) else []
            snap.mood_items = tuple(str(x) for x in items[:3])
            hot = data.get("hot") if isinstance(data.get("hot"), dict) else {}
            group_hot = hot.get(gid) if isinstance(hot.get(gid), dict) else {}
            snap.hot = tuple(
                (str(k), float(v)) for k, v in
                sorted(group_hot.items(), key=lambda kv: float(kv[1]), reverse=True)[:3])
            sources.append("兴趣")
        except (OSError, ValueError, TypeError) as exc:
            errors.append("兴趣读失败(%s)" % type(exc).__name__)

        # ---- 自身利益（插件私有 JSON） -----------------------------------
        try:
            data = read_json(self.selfworth_path)
            ledger = data.get("ledger") if isinstance(data.get("ledger"), dict) else {}
            entry = ledger.get(str(uid)) if isinstance(ledger.get(str(uid)), dict) else {}
            last = float(entry.get("last", 0) or 0)
            if last and now - last <= self.selfworth_window:
                kinds = entry.get("kinds") if isinstance(entry.get("kinds"), dict) else {}
                snap.selfworth_kinds = tuple(str(k) for k in kinds)
                snap.selfworth_ago = now - last
            sources.append("自身利益")
        except (OSError, ValueError, TypeError) as exc:
            errors.append("自身利益读失败(%s)" % type(exc).__name__)

        # ---- 群感知（SQLite 事件环） -------------------------------------
        try:
            rows = self._query(
                self.awareness_db,
                "SELECT kind,count(*) FROM event WHERE group_id=? AND ts>? "
                "GROUP BY kind", (gid, now - self.awareness_window))
            snap.awareness_recent = tuple((str(r[0]), int(r[1])) for r in rows)
            sources.append("群感知")
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            errors.append("群感知读失败(%s)" % type(exc).__name__)

        # ---- 自我认知（SQLite 能力事件） ---------------------------------
        try:
            rows = self._query(
                self.selfaware_db,
                "SELECT count(*) FROM sense_event WHERE ts>? AND success=0",
                (now - self.selfaware_window,))
            snap.selfaware_fails = int(rows[0][0]) if rows else 0
            sources.append("自我认知")
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            errors.append("自我认知读失败(%s)" % type(exc).__name__)

        # ---- 行动额度（proactive / initiate 的 JSON） ---------------------
        try:
            quota = []
            for name, path in (("initiate", self.initiate_path),
                               ("proactive", self.proactive_path)):
                data = read_json(path)
                row = data.get(gid) if isinstance(data.get(gid), dict) else None
                if isinstance(row, dict):
                    quota.append((name, int(row.get("count", 0) or 0)))
            snap.action_quota = tuple(quota)
            sources.append("行动额度")
        except (OSError, ValueError, TypeError) as exc:
            errors.append("行动额度读失败(%s)" % type(exc).__name__)

        # ---- 已知不可观测的岛 -------------------------------------------
        # dsh-spine 的「连着被牵了多少轮」只存在内存里（源码里没有任何
        # 写盘/建表），任何外部读者都拿不到 —— 记下来，别假装读到了。
        snap.unreadable = ("脊梁(状态只在内存)",)

        snap.sources = tuple(sources)
        snap.errors = tuple(errors)
        return snap


# ---------------------------------------------------------------- 岛 / 强度
def _drive_top(snap: MindSnapshot):
    """返回偏离基线最大的那条**非底层**欲望：(drive, eff, base, dev)。"""
    best = None
    for drive, (eff, base, _phase) in snap.drives.items():
        if drive in FOUNDATIONAL_DRIVES:
            continue
        dev = eff - base
        if best is None or abs(dev) > abs(best[3]):
            best = (drive, eff, base, dev)
    return best


def islands(snap: MindSnapshot) -> "list[dict]":
    """哪些岛这一轮在说话。magnitude 只用于排序，不是行为决策。"""
    out = []

    if snap.emotion_live:
        name = EMOTION_NAMES.get(snap.emotion, snap.emotion)
        out.append({
            "key": "emotion", "label": "情绪",
            "detail": "%s(%d)%s" % (name, snap.intensity, "·指向我" if snap.directed else ""),
            "magnitude": min(1.5, 0.5 * max(1, snap.intensity)),
        })

    top = _drive_top(snap)
    if top:
        drive, eff, base, dev = top
        out.append({
            "key": "desire", "label": "欲望",
            "detail": "%s %.0f(基线%.0f)" % (DRIVE_NAMES.get(drive, drive), eff, base),
            "magnitude": min(1.5, abs(dev) / 30.0),
        })

    if snap.tier != "陌生":
        out.append({
            "key": "relation", "label": "关系",
            "detail": "%s(好感%.0f/信任%.0f)%s"
                      % (snap.tier, snap.affinity, snap.trust,
                         "·正在避让" if snap.avoid else ""),
            "magnitude": {"亲近": 1.0, "避让": 1.0, "熟人": 0.5, "疏离": 0.5}.get(snap.tier, 0.0),
        })

    excess = snap.reactance - REACTANCE_BASE
    if excess >= REACTANCE_ON:
        out.append({
            "key": "reactance", "label": "逆反",
            "detail": "高出基线 %.0f" % excess,
            "magnitude": min(1.2, excess / 30.0),
        })

    if snap.fatigue >= 2:
        out.append({
            "key": "fatigue", "label": "疲劳",
            "detail": "第%d档%s" % (snap.fatigue, "·" + snap.fatigue_topic if snap.fatigue_topic else ""),
            "magnitude": min(1.0, snap.fatigue / 3.0),
        })

    if snap.mood_items:
        out.append({
            "key": "interest", "label": "兴趣",
            "detail": "今日馋" + "、".join(snap.mood_items),
            "magnitude": 0.5,
        })
    if snap.hot:
        out.append({
            "key": "interest_hot", "label": "兴趣热度",
            "detail": "近期上头" + "、".join(k for k, _v in snap.hot[:2]),
            "magnitude": 0.4,
        })

    if snap.selfworth_kinds:
        out.append({
            "key": "selfworth", "label": "自身利益",
            "detail": "%.0f秒前被%s" % (snap.selfworth_ago, "/".join(snap.selfworth_kinds)),
            "magnitude": 0.6,
        })

    if snap.awareness_recent:
        total = sum(c for _k, c in snap.awareness_recent)
        out.append({
            "key": "awareness", "label": "群感知",
            "detail": "近窗口 %d 条事件(%s)"
                      % (total, ",".join("%s%d" % (k, c) for k, c in snap.awareness_recent)),
            "magnitude": 0.4,
        })

    if snap.selfaware_fails:
        out.append({
            "key": "selfaware", "label": "自我认知",
            "detail": "近窗口 %d 项能力失败" % snap.selfaware_fails,
            "magnitude": 0.4,
        })

    if snap.action_quota:
        out.append({
            "key": "action", "label": "行动额度",
            "detail": "/".join("%s今日%d次" % (n, c) for n, c in snap.action_quota),
            "magnitude": 0.3,
        })

    return out


# ---------------------------------------------------------------- 冲突
def detect_conflicts(snap: MindSnapshot, parts) -> "list[str]":
    """岛与岛互相矛盾的地方。P0 只记录，不改任何行为。

    每条规则都要求**两侧同时成立**，单侧不报 —— 这个仓库里所有
    「按词拦」的教训都是同一个：单侧判据必然误伤。
    """
    out = []

    # 1 对熟人生气/低落：情绪与关系方向相反
    if snap.emotion_live and snap.emotion in ("angry", "sad") and snap.tier in ("亲近", "熟人"):
        out.append("情绪%s(%d) × 关系%s" % (EMOTION_NAMES.get(snap.emotion), snap.intensity, snap.tier))

    # 2 又馋又烦：今日馋在，同时对同一话题已经第2档疲劳
    if snap.fatigue >= 2 and (snap.mood_items or snap.hot):
        out.append("疲劳%d × 有兴趣内容在说话" % snap.fatigue)

    # 3 想说话但烦：玩心/连接高于基线，同时疲劳已到第2档
    top = _drive_top(snap)
    if snap.fatigue >= 2 and top and top[0] in ("play", "connection") and top[3] > 0:
        out.append("疲劳%d × %s高于基线%.0f" % (snap.fatigue, DRIVE_NAMES.get(top[0], top[0]), top[3]))

    # 4 逆反高但心情好：一个在顶、一个在松
    excess = snap.reactance - REACTANCE_BASE
    if excess >= REACTANCE_ON and snap.emotion_live and snap.emotion in ("happy", "excited", "proud"):
        out.append("逆反+%.0f × 情绪%s" % (excess, EMOTION_NAMES.get(snap.emotion)))

    # 5 关系在避让，但玩心/连接还高：社交想凑上去、关系在后退
    if snap.avoid and top and top[0] in ("play", "connection") and top[3] > 0:
        out.append("关系避让中 × %s高于基线%.0f" % (DRIVE_NAMES.get(top[0], top[0]), top[3]))

    # 6 能力不稳但情绪高涨：底色与状态不一致
    if snap.selfaware_fails and snap.emotion_live and snap.emotion in ("excited", "proud"):
        out.append("近窗口%d项能力失败 × 情绪%s" % (snap.selfaware_fails, EMOTION_NAMES.get(snap.emotion)))

    return out


def annotations(snap: MindSnapshot, parts) -> "list[str]":
    """「块与活跃状态不匹配」——**不是冲突**，单独一列记。

    ★ 为什么必须跟冲突分开（2026-09-13 上线当天就撞上）：
    第一版把它当成第 7 条冲突规则，结果真实流量里**每轮都成立**
    （实测每轮 18~22 个块注入、只有 4~5 个状态岛在说话），冲突率恒等于 100%。
    一个永远为真的指标等于把这个指标废掉 —— 看的人两天后就不看它了。
    每轮都成立的量是**基线**，不是异常；只有它偏离自己的常态才值得报。

    所以这里只回答「块数和活跃岛数对得上吗」，冲突计数留给真正会互相打脸的规则。
    """
    n_blocks = len(list(parts or []))
    n_live = len(islands(snap))
    out = []
    if n_blocks > n_live + 3:
        out.append("块%d≫状态%d" % (n_blocks, n_live))
    if n_live >= 4 and n_blocks == 0:
        out.append("状态%d但零注入" % n_live)
    return out


# ---------------------------------------------------------------- 统一草稿（P1 的候选）
# 这一句是「分寸」，最多挑一条；挑不出来就只描述状态，不指导。
def _stance_sentence(snap: MindSnapshot) -> str:
    if snap.fatigue >= 2:
        return "这轮短一点，别展开。"
    excess = snap.reactance - REACTANCE_BASE
    if excess >= REACTANCE_ON:
        return "对方在压你，可以把话说直，但事情照办。"
    if snap.emotion_live and snap.emotion in ("angry", "sad") and snap.tier in ("亲近", "熟人"):
        return "是熟人，可以说真话，别客套。"
    top = _drive_top(snap)
    if top and top[0] in ("play", "connection") and top[3] > 0:
        return "想接就接，可以主动一点。"
    return ""


def render_draft(snap: MindSnapshot, budget: int = 900) -> str:
    """把活跃的岛合成**一个**块。P0 只生成不入库、不注入，用来量「合并后多少字」。

    顺序按 magnitude 从大到小；超预算就**从最小的开始丢**（不是截断字符串 ——
    截断会留下半句话，比少说一句更糟）。
    """
    items = sorted(islands(snap), key=lambda d: -d["magnitude"])
    head = "内在状态："
    stance = _stance_sentence(snap)
    tail = ("分寸：" + stance) if stance else ""

    kept = []
    for item in items:
        trial = head + "；".join(kept + [item["detail"]]) + "。"
        text = "<mind_state>%s%s%s</mind_state>" % (trial, tail, "")
        if len(text) > budget and kept:
            break
        kept.append(item["detail"])
    if not kept:
        body = "内在状态：平静。"
    else:
        body = head + "；".join(kept) + "。"
    return "<mind_state>%s%s</mind_state>" % (body, tail)


# ---------------------------------------------------------------- 一行日志
def snapshot_line(snap: MindSnapshot, parts, draft: str, conflicts, notes=()) -> str:
    """每个 LLM 轮次一行 —— 这一行就是 P0 的全部产出。

    刻意压到一行：这个仓库的日志已经 8MB 级，多一行×每轮会淹掉真正的信号。
    `notes` 是 `annotations()` 的产出（块/状态不匹配之类的观测），
    与 `conflicts`（真会互相打脸的矛盾）分开打标，免得基线被当成异常。
    """
    live = islands(snap)
    inject_chars = sum(p.chars for p in parts)
    names = "|".join(i["detail"] for i in live) or "无"
    return ("[mind] gid=%s 岛=%d/%d 注入块=%d/%d字 草稿=%d字 冲突=%d%s%s%s %s"
            % (snap.gid, len(live), 11, len(parts), inject_chars, len(draft),
               len(conflicts),
               (" [" + "；".join(conflicts) + "]") if conflicts else "",
               (" [注:" + "；".join(notes) + "]") if notes else "",
               (" 读错=%d" % len(snap.errors)) if snap.errors else "",
               names))
