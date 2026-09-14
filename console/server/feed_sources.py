# -*- coding: utf-8 -*-
"""实时观察数据源 —— 只读聚合，给控制台 /api/console/live|mind|log|rawconfig 用。

设计原则（和 console_api 一致）：
  · **只读**。这个模块绝不写任何生产文件/数据库；出任何错都回空/降级，
    不让观察层把控制台搞挂 —— 观察是附加能力，不是生命线。
  · **诚实标注**。每条数据都带来源（src 字段）：手机上看到的「机器人想法」
    是 dsh-mind 的内在状态块和 dsh-effect 的决策判词，**不是**主模型的原始
    思维链（reasoning_content 不落盘、拿不到）；界面上必须这么标，不装。

数据源（默认全在生产路径，测试用 CONSOLE_FEED_* 环境变量改指临时文件）：
  astrbot.log      插件活性/错误 + 日志尾巴（增量扫描，见下）
  dsh_memory.db    buffer 表：群友消息流（每组滚动 240 条）
  dsh_effect.db    reply 表：机器人发出的话 + 群里的反应判词（带 group_id）
  dsh_mind.db      observe/observe_block：每轮注入了哪些内在状态块
  dsh_selfaware.db self_revision：能力状态迁移；sense_event：能力事件
  dynamics.json    群体记分卡（P1~P7，qqbot-dynamics.timer 每 15 分钟写）
  cmd_config.json  全量配置（脱敏后给手机看）
  imagegen.env     插件 env（脱敏后给手机看）

两个必须写在前面的坑：
  · **日志时区**。astrbot.log 的时间戳是 UTC+8 挂钟（实测 host UTC 02:39 时
    日志打 10:39），而 buffer.ts / docker StartedAt 是绝对 epoch。要可比，
    日志墙钟必须按 +8 折算：epoch = timegm(墙钟) - 8h。
  · **增量扫描**。日志 11~20MB 且一直在长；手机 15 秒一轮询，每次全量重读
    会把这台 2 核小机拖住。日志只增不改（轮转是换文件名），所以按
    (inode, byte offset) 缓存，只解析新追加的字节。
"""

import calendar
import json
import os
import re
import sqlite3
import time

ASTRBOT_LOG = os.environ.get("CONSOLE_FEED_LOG", "/opt/qqbot/astrbot/data/logs/astrbot.log")
MEM_DB = os.environ.get("CONSOLE_FEED_MEMDB", "/opt/qqbot/astrbot/data/dsh_memory.db")
EFFECT_DB = os.environ.get("CONSOLE_FEED_EFFECTDB", "/opt/qqbot/astrbot/data/dsh_effect.db")
MIND_DB = os.environ.get("CONSOLE_FEED_MINDB", "/opt/qqbot/astrbot/data/dsh_mind.db")
SELF_DB = os.environ.get(
    "CONSOLE_FEED_SELFAWAREDB", "/opt/qqbot/astrbot/data/dsh_selfaware.db")
DYNAMICS = os.environ.get("CONSOLE_FEED_DYNAMICS", "/opt/qqbot/observe/dynamics.json")
OBSERVE_STATE = os.environ.get(
    "CONSOLE_FEED_OBSERVE_STATE", "/opt/qqbot/observe/state.json")
CFG_PATH = os.environ.get("CONSOLE_CFG", "/opt/qqbot/astrbot/data/cmd_config.json")
ENV_PATH = os.environ.get("CONSOLE_ENV", "/opt/qqbot/imagegen.env")

BOT_LABEL = "机器人"

# 日志时间戳的时区偏移（小时）。默认 +8：AstrBot 容器 TZ 设了 Asia/Shanghai。
LOG_TZ_OFF_H = int(os.environ.get("CONSOLE_FEED_LOG_TZ", "8"))

# 行环形缓冲上限：log_tail 最多看 24h，但内存里只留最近的行。
# 超出时 truncated=True，界面如实说「更早的被截断了」。
ROW_RING = 8000

# ---------------------------------------------------------------- 日志解析
#
# astrbot.log 的行形状（实测）：
#   [2026-09-13 19:41:07.075] [Core] [INFO] [star.star_manager:1123]: Loading plugin dsh-clarify ...
#   [2026-09-13 19:41:36.364] [Plug] [INFO] [dsh-voice.main:1089]: [voice] 已加载：backend=wusound ...
#   [2026-09-13 19:41:37.377] [Core] [INFO] [star.star_manager:1207]: Plugin dsh-voice (1.2.5) by 大肥鱼: ...

TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})")
LEVEL_RE = re.compile(r"\[(ERRO|CRIT|WARN|INFO|DEBUG)\]")
TAG_RE = re.compile(r"\[([\w\-]+)\.main:(\d+)\]")
LOAD_RE = re.compile(r"Loading plugin (\S+) \.\.\.")
VER_RE = re.compile(r"Plugin (\S+) \(([\w\.]+)\) by")
TRACEBACK = "Traceback (most recent call last)"


def _line_epoch(ts_match):
    """日志墙钟（UTC+8）→ 绝对 epoch 秒。

    calendar.timegm 把墙钟当 UTC 解，而它实际是 +8 区的，所以减 8 小时。
    不做这一步，日志事件会比 buffer.ts「晚 8 小时出现」——所有新鲜度判断全错。
    """
    try:
        t = time.strptime(ts_match.group(1) + " " + ts_match.group(2),
                          "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return calendar.timegm(t) - LOG_TZ_OFF_H * 3600


class LogScan(object):
    """一次（增量）扫描的产物：插件活性累计 + 带时间戳的行环形缓冲。

    plugins 里的计数器是**进程内累计**的（自模块加载起），够用：
    「本次容器启动以来有没有出错」才是问题，跨重启的历史不重要。
    """

    def __init__(self):
        self.plugins = {}      # name -> {last_ts, events, errors, warns, version}
        self.rows = []         # (epoch, ts_text, level, tag, text)，时间升序
        self.oldest_kept = None  # 环形缓冲里最老一行的 epoch（截断证据）
        self.offset = 0        # 已消化的字节偏移
        self.ino = None

    def eat_line(self, ln):
        """消化一行。不带时间戳的行（Traceback 续行等）直接丢弃计数之外。"""
        m = TS_RE.match(ln)
        if not m:
            return
        ts_text = m.group(1) + " " + m.group(2)
        epoch = _line_epoch(m)
        if epoch is None:
            return
        rest = ln[m.end():]
        lvl_m = LEVEL_RE.search(rest[:40])
        level = lvl_m.group(1) if lvl_m else ""
        tag_m = TAG_RE.search(rest)
        tag = tag_m.group(1) if tag_m else ""

        if tag:
            info = self.plugins.setdefault(tag, {
                "last_ts": epoch, "events": 0, "errors": 0, "warns": 0,
                "version": None})
            info["last_ts"] = max(info.get("last_ts") or 0, epoch)
            if level in ("ERRO", "CRIT"):
                info["errors"] += 1
            elif level == "WARN":
                info["warns"] += 1
            else:
                info["events"] += 1
        load_m = LOAD_RE.search(rest)
        if load_m:
            info = self.plugins.setdefault(load_m.group(1), {
                "last_ts": epoch, "events": 0, "errors": 0, "warns": 0,
                "version": None})
            info["last_ts"] = max(info.get("last_ts") or 0, epoch)
        ver_m = VER_RE.search(rest)
        if ver_m:
            info = self.plugins.setdefault(ver_m.group(1), {
                "last_ts": epoch, "events": 0, "errors": 0, "warns": 0,
                "version": None})
            info["version"] = ver_m.group(2)

        self.rows.append((epoch, ts_text, level, tag, ln.strip()))
        if len(self.rows) > ROW_RING:
            drop = len(self.rows) - ROW_RING
            del self.rows[:drop]
            self.oldest_kept = self.rows[0][0]

    def runtime_plugins(self, started_epoch, now=None):
        """插件活性画像。started_epoch 是 astrbot 容器本次启动的绝对 epoch。

        状态口径（宁可说不确定，不硬猜）：
          active  本次启动后有插件日志且无错误
          error   本次启动后有错误日志（最要紧的信号）
          silent  加载过、启动后没有任何运行日志（很多插件安静是正常的）
          stale   最后一条日志早于本次容器启动 —— 插件没加载/加载失败
          unknown 连加载行都没见过（本模块启动晚于容器启动时可能误判，
                  界面上按「无数据」展示，不按坏插件展示）
        """
        now = now if now is not None else time.time()
        out = []
        for name, info in sorted(self.plugins.items()):
            last = info.get("last_ts")
            err_n = info.get("errors", 0)
            runtime_n = info.get("events", 0) + err_n + info.get("warns", 0)
            if last is None:
                status, note = "unknown", "日志里没见过"
            elif started_epoch and last < started_epoch:
                status, note = "stale", "本次启动后没有任何日志（含加载行）"
            elif runtime_n <= 0:
                status, note = "silent", "已加载，之后没有运行日志"
            elif err_n > 0:
                status, note = "error", "有 %d 条错误日志" % err_n
            else:
                age = int(now - last)
                status = "active"
                note = "%.0f 分钟内有活动" % (max(age, 0) / 60.0)
            out.append({
                "name": name,
                "status": status,
                "note": note,
                "last_age_s": (int(now - last) if last else None),
                "events": runtime_n,
                "errors": err_n,
                "version": info.get("version"),
            })
        return out


_scan_cache = {"ino": None, "scan": None}


def _stat_key(path):
    st = os.stat(path)
    return st.st_ino, st.st_size


def scan_log(force=False):
    """增量扫描日志：只解析上次之后新追加的字节。

    换了文件（轮转）或文件变短了（被截）→ 整个重来，计数器重置 ——
    重置比拿着错位的 offset 解出乱码强。
    """
    path = ASTRBOT_LOG
    try:
        ino, size = _stat_key(path)
    except OSError:
        return LogScan()
    scan = _scan_cache["scan"]
    fresh = (scan is not None and _scan_cache["ino"] == ino
             and 0 <= scan.offset <= size)
    if fresh and not force:
        pass  # 复用，往下只补新字节
    else:
        scan = LogScan()
        _scan_cache["ino"] = ino
        _scan_cache["scan"] = scan

    if scan.offset == size:
        return scan
    try:
        with open(path, "rb") as fh:
            fh.seek(scan.offset)
            chunk = fh.read(size - scan.offset)
    except OSError:
        return scan
    if not chunk:
        return scan
    # 末尾可能是半行（正被写）——只消化到最后一整个换行，剩下的下次再说。
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return scan
    consumed = chunk[:cut + 1]
    scan.offset += len(consumed)
    for raw in consumed.split(b"\n"):
        if not raw.strip():
            continue
        try:
            scan.eat_line(raw.decode("utf-8", "replace").rstrip("\r"))
        except Exception:  # noqa: BLE001 —— 单行解析失败绝不拖垮整轮
            continue
    return scan


# ---------------------------------------------------------------- 数据库读取


def _rows(db_path, sql, args=()):
    """打开→查→关。读不到（文件不在/表不在/被锁）就回空列表，绝不抛。

    mode=ro + busy_timeout：WAL 库并发读安全，也不挡正在写的 AstrBot。
    """
    if not db_path or not os.path.exists(db_path):
        return []
    con = None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        con.execute("PRAGMA busy_timeout=3000")
        con.row_factory = sqlite3.Row
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


# ---------------------------------------------------------------- 群聊合并流


def group_list():
    """buffer + effect 里出现过的群，按最近活跃排序。群名库里没有，只有 id ——
    显示「群 <id>」，不猜名字（AstrBot 配置里的群列表不含名称映射）。"""
    agg = {}
    for r in _rows(MEM_DB, "select group_id, count(*) n, max(ts) last from buffer group by group_id"):
        d = agg.setdefault(str(r["group_id"]), {})
        d["msgs"] = r["n"]
        d["last"] = r["last"]
    for r in _rows(EFFECT_DB, "select group_id, count(*) n, max(ts) last from reply group by group_id"):
        d = agg.setdefault(str(r["group_id"]), {})
        d["bot"] = r["n"]
        d["last"] = max(d.get("last") or 0, r["last"])
    now = time.time()
    out = []
    for gid, d in agg.items():
        out.append({
            "id": gid,
            "label": "群 " + gid,
            "user_msgs": d.get("msgs", 0),
            "bot_msgs": d.get("bot", 0),
            "last_ts": d.get("last"),
            "last_age_s": int(now - d["last"]) if d.get("last") else None,
        })
    out.sort(key=lambda g: -(g["last_ts"] or 0))
    return out


def _clean_text(text, cap=300):
    if text is None:
        return ""
    text = str(text).strip()
    return text if len(text) <= cap else text[:cap] + "…"


def dialog(group_id=None, limit=60, before=None):
    """合并群友消息（memory.buffer）和机器人发言（effect.reply），新的在前。

    bot 消息为什么从 effect 拿而不是从日志拿：effect 带 group_id（日志的
    Prepare to send 不带群号），而且 reactions/strategy 是现成的判词。
    buffer 里没有 bot 自己的行（实测 user_id=机器人 的行数为 0），不会重。
    """
    limit = max(1, min(int(limit or 60), 200))
    groups = group_list()
    if not groups:
        return {"groups": [], "group": None, "dialog": [], "more": False}
    if not group_id:
        group_id = groups[0]["id"]  # 默认最近活跃的群
    group_id = str(group_id)

    feed = []
    if before is not None:
        rows = _rows(
            MEM_DB,
            "select ts, user_id, name, text from buffer "
            "where group_id=? and ts<? order by ts desc limit ?",
            (group_id, float(before), limit + 1))
    else:
        rows = _rows(
            MEM_DB,
            "select ts, user_id, name, text from buffer "
            "where group_id=? order by ts desc limit ?",
            (group_id, limit + 1))
    for r in rows:
        feed.append({
            "ts": r["ts"], "who": r["name"] or str(r["user_id"]),
            "qq": str(r["user_id"]), "dir": "in",
            "text": _clean_text(r["text"]), "src": "memory",
        })

    if before is not None:
        erows = _rows(
            EFFECT_DB,
            "select ts, text, status, reactions from reply "
            "where group_id=? and ts<? order by ts desc limit ?",
            (group_id, float(before), limit + 1))
    else:
        erows = _rows(
            EFFECT_DB,
            "select ts, text, status, reactions from reply "
            "where group_id=? order by ts desc limit ?",
            (group_id, limit + 1))
    for r in erows:
        item = {
            "ts": r["ts"], "who": BOT_LABEL, "qq": "", "dir": "out",
            "text": _clean_text(r["text"]), "src": "effect",
        }
        # reactions 观察窗到期后回填：None=还没到期，数字=群里跟了 N 条
        if r["reactions"] is not None:
            item["reactions"] = r["reactions"]
        feed.append(item)

    feed.sort(key=lambda x: -x["ts"])
    more = len(feed) > limit
    for item in feed:  # epoch 取整，别把浮点尾数漏给手机
        item["ts"] = round(float(item["ts"]))
    return {"groups": groups, "group": group_id,
            "dialog": feed[:limit], "more": more}


# ---------------------------------------------------------------- 内在状态


def _parse_json_list(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def mind_snapshot(limit=12):
    """dsh-mind 观测：每轮 LLM 请求注入了哪些内在状态块（islands/notes）。

    这是「它在想什么」最接近的**已落盘**证据 —— 模型的原始 reasoning 拿不到，
    界面上必须如实标注这一点，不能把 islands 说成思维链原文。
    """
    rows = _rows(
        MIND_DB,
        "select ts, gid, uid, n_blocks, inject_chars, ctx_chars, n_islands, "
        "draft_len, n_conflicts, islands, conflicts, notes, errors, flags "
        "from observe order by id desc limit ?",
        (int(limit),))
    now = time.time()
    recent = []
    for r in rows:
        recent.append({
            "ts": round(float(r["ts"])),
            "age_s": int(now - r["ts"]),
            "gid": r["gid"],
            "blocks": r["n_blocks"],
            "inject_chars": r["inject_chars"],
            "ctx_chars": r["ctx_chars"],
            "islands": _parse_json_list(r["islands"]),
            "conflicts": _parse_json_list(r["conflicts"]),
            "notes": _parse_json_list(r["notes"]),
            "errors": _parse_json_list(r["errors"]),
            "flags": r["flags"] or "",
        })
    blocks = []
    for r in _rows(
            MIND_DB,
            "select b.tag, count(*) n, sum(b.chars) chars from observe_block b "
            "join observe o on o.id=b.obs_id where o.ts>? group by b.tag "
            "order by chars desc limit 20",
            (now - 86400,)):
        blocks.append({"tag": r["tag"], "n": r["n"], "chars": r["chars"]})
    return {"recent": recent, "blocks_24h": blocks}


def effect_recent(limit=15):
    """dsh-effect：机器人说了什么 + 群里反应如何（AI 观察窗的判词）。"""
    rows = _rows(
        EFFECT_DB,
        "select group_id, ts, due, text, status, reactions, strategy, stance, "
        "contribution, why from reply order by id desc limit ?",
        (int(limit),))
    now = time.time()
    out = []
    for r in rows:
        out.append({
            "gid": r["group_id"],
            "ts": round(float(r["ts"])),
            "age_s": int(now - r["ts"]),
            "text": _clean_text(r["text"], 120),
            "status": r["status"],
            "reactions": r["reactions"],
            "strategy": r["strategy"],
            "stance": r["stance"],
            "contribution": r["contribution"],
            "why": _clean_text(r["why"], 160),
        })
    return out


def selfaware_recent(limit=10):
    """dsh-selfaware：能力状态迁移（vision 从 degraded 恢复 available 这类）。"""
    revisions = []
    for r in _rows(
            SELF_DB,
            "select ts, layer, subject, old_value, new_value, reason "
            "from self_revision order by id desc limit ?",
            (int(limit),)):
        revisions.append({
            "ts": round(float(r["ts"])),
            "layer": r["layer"], "subject": r["subject"],
            "old": r["old_value"], "new": r["new_value"],
            "reason": _clean_text(r["reason"], 120),
        })
    senses = []
    for r in _rows(
            SELF_DB,
            "select ts, capability, status, success, latency_ms, source, detail "
            "from sense_event order by id desc limit ?", (int(limit),)):
        senses.append({
            "ts": round(float(r["ts"])),
            "capability": r["capability"], "status": r["status"],
            "success": r["success"],
            "latency_ms": r["latency_ms"],
            "source": r["source"],
            "detail": _clean_text(r["detail"], 100),
        })
    return {"revisions": revisions, "senses": senses}


def _read_json_file(path):
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def dynamics_summary():
    """群体记分卡摘要。dynamics.json 在就给 P1~P7 数字；
    不在就回落到 observe/state.json 里的告警信息。"""
    d = _read_json_file(DYNAMICS)
    if d:
        return {
            "generated": d.get("generated"),
            "window_h": d.get("window_h"),
            "n_user_msg": d.get("n_user_msg"),
            "n_bot_msg": d.get("n_bot_msg"),
            "n_mention": d.get("n_mention"),
            "p1_miss_n": len(d.get("p1_mention_miss") or []),
            "p2_owner_ignored_n": len(d.get("p2_owner_ignored") or []),
            "p3_share": d.get("p3_share"),
            "p4_top": (d.get("p4_repeat") or [])[:3],
            "p5_burst_n": len(d.get("p5_burst") or []),
            "p6_fails": d.get("p6_fails"),
            "p7_mech": d.get("p7_mech"),
            "top_talkers": (d.get("top_talkers") or [])[:5],
        }
    st = _read_json_file(OBSERVE_STATE)
    if st:
        return {"observe_state": {
            "last_r1_ts": st.get("last_r1_ts"),
            "alert": (st.get("alerts") or {}).get("last_body"),
        }}
    return None


def mind_feed():
    return {
        "mind": mind_snapshot(),
        "effect": effect_recent(),
        "selfaware": selfaware_recent(),
        "dynamics": dynamics_summary(),
        "honesty_note": "「内在状态」是 dsh-mind 每轮注入的状态块与 dsh-effect 的观察判词；"
                        "主模型的原始思维链不落盘，这里拿不到，也不假装拿得到。",
    }


# ---------------------------------------------------------------- 日志尾巴


LOG_LEVELS = ("all", "err", "warn", "info")


def log_tail(minutes=180, level="all", name=None, limit=200):
    """按时间/等级/插件名过滤的日志尾巴，新的在前。

    err 档把 Traceback 行也算上（它们不带等级标记，但人一定想看见）。
    """
    minutes = max(1, min(int(minutes or 180), 1440))
    limit = max(1, min(int(limit or 200), 1000))
    level = level if level in LOG_LEVELS else "all"
    name = (name or "").strip().lower()

    scan = scan_log()
    since = time.time() - minutes * 60
    out = []
    for epoch, ts_text, lvl, tag, text in reversed(scan.rows):
        if epoch < since:
            continue
        if level == "err" and lvl not in ("ERRO", "CRIT") and TRACEBACK not in text:
            continue
        if level == "warn" and lvl not in ("ERRO", "CRIT", "WARN"):
            continue
        if level == "info" and lvl not in ("INFO", "WARN", "ERRO", "CRIT"):
            continue
        if name and name not in text.lower():
            continue
        out.append({"ts": ts_text, "level": lvl or "?", "tag": tag, "text": text[:400]})
        if len(out) >= limit:
            break
    truncated = bool(scan.oldest_kept is not None and scan.oldest_kept > since)
    return {"lines": out, "minutes": minutes, "level": level,
            "name": name, "truncated": truncated}


# ---------------------------------------------------------------- 配置脱敏

_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api_?key|apikey|authorization|credential|"
    r"(?:^|_)key$|^key$)", re.I)
_SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASS|PASSWORD|AUTH)$")
# 值形状防线：不管键名叫什么，长得像凭据的值一律掩码。键名防线
# 挡不住 provider_sources[].key 这种「键名就叫 key」的字段 —— 实测
# 就是从这里漏出去六个真 sk- 密钥。宁可误掩（值恰好像密钥的普通串
# 很少见），不可漏一个。
_SECRET_VALUE_RE = re.compile(
    r"^(sk-[A-Za-z0-9_-]{8,}"          # OpenAI 风格
    r"|ghp_[A-Za-z0-9]{10,}"            # GitHub PAT
    r"|github_pat_[A-Za-z0-9_]{10,}"
    r"|xox[baprs]-[A-Za-z0-9-]+"        # Slack
    r"|AIza[A-Za-z0-9_-]{10,}"          # Google
    r"|eyJ[A-Za-z0-9_-]{10,}\."         # JWT 开头
    r"|[A-Fa-f0-9]{32,}$"               # 32+ 位纯 hex（私钥/哈希/secret）
    r")")


def _redact_value(val):
    """值非空 → 掩码。空值保持空（区分「没设」和「设了」）。"""
    if val is None:
        return None
    s = str(val)
    return "***已设置***" if s else ""


def _looks_secret(key, val):
    """键名像密钥 或 值长得像凭据 → True。两层都要：键名层语义准，
    值形状层兜底（键名千奇百怪，sk- 长相不会骗人）。"""
    if not isinstance(val, str):
        return False
    if _SECRET_KEY_RE.search(str(key)):
        return True
    v = val.strip()
    if not v:
        return False
    if _SECRET_VALUE_RE.match(v):
        # 纯数字长串（QQ 号、时间戳、ID）不是密钥，别误掩
        if v.isdigit():
            return False
        return True
    return False


def _redact(obj, key_hint=""):
    """递归脱敏：键名像密钥或值像凭据的，换成掩码。列表里的 dict 同样处理。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out[k] = _redact(v, key_hint=str(k))
            elif _looks_secret(k, v) or (key_hint and _looks_secret(key_hint, v)):
                out[k] = _redact_value(v)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        # key_hint 会把「父键叫 key」的列表一并兜住（provider_sources[].key[0]）
        return [_redact(x, key_hint) for x in obj]
    # 列表里的字符串值也要走值形状防线 —— 漏掉这一步就是把 sk- 直接吐出去
    if key_hint and _looks_secret(key_hint, obj):
        return _redact_value(obj)
    return obj


def _redact_env_line(line):
    """env 行级脱敏：KEY=value 里变量名像密钥的掩码，注释/空行原样。"""
    m = re.match(r"^([A-Za-z_][\w]*)=(.*)$", line)
    if not m:
        return line
    var, val = m.group(1), m.group(2)
    if _SECRET_ENV_RE.search(var) and val.strip():
        return var + "=***已设置***"
    return line


def raw_config():
    """全量配置（cmd_config.json + imagegen.env），密钥一律掩码。

    8088 是明文 HTTP —— 明文里能出现什么，按「会被邻居路由器看到」来定。
    所以宁可多掩一层：键名沾 password/secret/token/key 的都不出服务器。
    """
    cfg = None
    err = ""
    try:
        raw = open(CFG_PATH, "rb").read()
        cfg = _redact(json.loads(raw.decode("utf-8-sig")))
    except (OSError, ValueError) as exc:
        err = "cmd_config.json 读不了：%s" % exc
    env_lines = []
    try:
        with open(ENV_PATH, "r", encoding="utf-8", errors="replace") as fh:
            env_lines = [_redact_env_line(ln.rstrip("\n")) for ln in fh]
    except OSError as exc:
        env_lines = ["# imagegen.env 读不了：%s" % exc]
    return {"config": cfg, "env": env_lines, "error": err or None}
