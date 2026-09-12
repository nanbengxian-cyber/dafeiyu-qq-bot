"""dsh-initiate 的主动新话题候选池：只读既有插件状态，不导入 AstrBot。"""

import hashlib
import json
import re
import sqlite3
from pathlib import Path


_SENSITIVE_RE = re.compile(
    r"(?:密码|口令|token|密钥|验证码|身份证|银行卡|手机号|住址|地址|定位|私聊|病史|欠款|工资|学校|公司)"
    r"|(?:https?://|www\.)"
    r"|(?:\b(?:\d[ -]?){6,}\b)",
    re.IGNORECASE,
)
_BAD_KINDS = {"身份", "关系", "个人", "隐私", "称呼", "边界", "忌讳"}
_UNSEARCHABLE_RE = re.compile(r"(?:群主|群友|群员|成员|本群|机器人|个人档案|私聊)")
_SEARCH_NOISE_RE = re.compile(r"^(?:以|用|讨论|做|喜欢聊|常聊|曾聊)(.*)$")
_CLEAN_PREFIX_RE = re.compile(r"^(?:群里|群内|大家|本群)(?:曾|最近|常|经常|流行|讨论|喜欢|会|在)?")


def clean_topic(text: str, limit: int = 60) -> str:
    value = " ".join(str(text or "").split()).strip(" ，。！？；:：")
    return value[: max(0, int(limit))]


def safe_group_fact(kind: str, content: str) -> bool:
    text = clean_topic(content)
    if not text or str(kind or "").strip() in _BAD_KINDS:
        return False
    if len(text) < 4 or _SENSITIVE_RE.search(text):
        return False
    return True


def topic_key(text: str) -> str:
    normalized = re.sub(r"[\W_]+", "", clean_topic(text).lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16] if normalized else ""


def render_fact_topic(content: str) -> str:
    text = _CLEAN_PREFIX_RE.sub("", clean_topic(content)).strip(" ，。！？；:：")
    return text or clean_topic(content)


def search_query(topic: str) -> str:
    """把群记忆句改成保守搜索词；拒绝过短、敏感或纯内部称呼。"""
    text = clean_topic(topic, 36)
    match = _SEARCH_NOISE_RE.match(text)
    if match and len(match.group(1).strip()) >= 2:
        text = match.group(1).strip()
    text = re.sub(r"(?:指代|的说法|等)$", "", text).strip()
    if len(text) < 2 or _SENSITIVE_RE.search(text) or _UNSEARCHABLE_RE.search(text):
        return ""
    return text


def load_group_fact_topics(db_path: str, gid: str, limit: int = 8) -> list[dict]:
    rows = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute(
                "SELECT kind,content,weight,updated_at FROM facts "
                "WHERE group_id=? AND user_id='' ORDER BY weight DESC,updated_at DESC LIMIT ?",
                (str(gid), max(8, int(limit) * 3)),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    for kind, content, weight, updated_at in rows:
        if not safe_group_fact(str(kind), str(content)):
            continue
        text = render_fact_topic(str(content))
        out.append({
            "source": "memory",
            "topic": text,
            "hint": "群共同记忆：" + text,
            "key": topic_key(text),
            "search_query": search_query(text),
            "weight": max(1.0, float(weight or 1.0)),
            "updated_at": float(updated_at or 0),
        })
        if len(out) >= limit:
            break
    return out


def load_interest_topics(path: str, gid: str, limit: int = 5) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    rows = []
    mood = data.get("mood", {}) if isinstance(data, dict) else {}
    for item in mood.get("items", []) if isinstance(mood, dict) else []:
        text = clean_topic(item, 30)
        if text and not _SENSITIVE_RE.search(text):
            rows.append({"source": "interest", "topic": text, "hint": "你现在自己有点想聊：" + text,
                         "key": topic_key(text), "search_query": search_query(text), "weight": 1.3})
    hot = data.get("hot", {}).get(str(gid), {}) if isinstance(data, dict) else {}
    if isinstance(hot, dict):
        for item, weight in sorted(hot.items(), key=lambda x: -float(x[1] or 0)):
            text = clean_topic(item, 30)
            if text and not _SENSITIVE_RE.search(text):
                rows.append({"source": "interest", "topic": text, "hint": "群里近期兴趣热度：" + text,
                             "key": topic_key(text), "search_query": search_query(text),
                             "weight": max(1.0, float(weight or 1.0))})
    seen = set()
    return [x for x in rows if not (x["key"] in seen or seen.add(x["key"]))][:limit]


def load_self_topics(path: str, limit: int = 3) -> list[dict]:
    """只暴露最近的能力状态主题，不暴露主机/IP/磁盘等运维细节。"""
    labels = {"chat": "聊天状态", "vision": "看图能力", "web": "联网能力",
              "imagegen": "画图能力", "voice": "语音能力", "video": "视频能力"}
    rows = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            raw = con.execute(
                "SELECT capability,status,ts FROM sense_event ORDER BY ts DESC,id DESC LIMIT 50"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return rows
    seen = set()
    for capability, status, ts in raw:
        capability = str(capability or "")
        status = str(status or "")
        if capability in seen or capability not in labels or status not in {"ok", "recovered", "degraded"}:
            continue
        seen.add(capability)
        topic = labels[capability]
        rows.append({"source": "self", "topic": topic,
                     "hint": "%s最近%s" % (topic, "恢复了" if status == "recovered" else "有变化"),
                     "key": topic_key(topic), "search_query": "", "weight": 1.0,
                     "updated_at": float(ts or 0)})
        if len(rows) >= limit:
            break
    return rows


def choose_topic(candidates: list[dict], recent_keys: dict, now: float, ttl: float) -> dict | None:
    usable = []
    for item in candidates:
        key = str(item.get("key") or topic_key(item.get("topic", "")))
        last = float((recent_keys or {}).get(key, 0) or 0)
        if not key or (last and now - last < ttl):
            continue
        row = dict(item); row["key"] = key
        usable.append(row)
    if not usable:
        return None
    # 分值主导，日序只负责在同分档轮换，避免每天永远拿同一条。
    usable.sort(key=lambda x: (-float(x.get("weight", 1)), str(x.get("key"))))
    best = float(usable[0].get("weight", 1))
    tier = [x for x in usable if float(x.get("weight", 1)) >= best - 0.25]
    day_slot = int(now // 86400) % len(tier)
    return tier[day_slot]


def collect_topics(db_path: str, interest_path: str, self_db: str, gid: str, limit: int = 16) -> list[dict]:
    rows = (load_group_fact_topics(db_path, gid, 8)
            + load_interest_topics(interest_path, gid, 5)
            + load_self_topics(self_db, 3))
    seen = set(); out = []
    for item in rows:
        key = item.get("key")
        if not key or key in seen:
            continue
        seen.add(key); out.append(item)
    return out[:limit]
