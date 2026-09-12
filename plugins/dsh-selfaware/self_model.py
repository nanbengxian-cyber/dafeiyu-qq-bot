# -*- coding: utf-8 -*-
from __future__ import annotations

"""dsh-selfaware 的机器自感知模型。

只接受系统遥测、真实调用结果和受信配置，不接受聊天文本写入。短期层描述当前能力与
环境；长期层从历史成功/失败事件聚合出稳定可靠性。所有输出都经过清洗，不包含密钥、
网络地址或宿主机路径。
"""

import hashlib
import os
import platform
import re
import shutil
import sqlite3
import time
from typing import Optional


STATUS_UNKNOWN = "unknown"
STATUS_AVAILABLE = "available"
STATUS_DEGRADED = "degraded"
STATUS_UNAVAILABLE = "unavailable"
STATUS_DISABLED = "disabled"

STATUS_ZH = {
    STATUS_UNKNOWN: "未验证",
    STATUS_AVAILABLE: "正常",
    STATUS_DEGRADED: "降级",
    STATUS_UNAVAILABLE: "不可用",
    STATUS_DISABLED: "已关闭",
}

CAPABILITY_ZH = {
    "chat": "文字思考与回复",
    "vision": "图片理解",
    "image_generation": "图片生成",
    "voice": "语音合成",
    "video": "视频理解与生成",
    "web": "联网搜索与网页读取",
    "memory": "记忆读取与整理",
    "qq": "QQ消息收发",
}

DEFAULT_STALE = {
    "chat": 3600.0,
    "vision": 21600.0,
    "image_generation": 86400.0,
    "voice": 86400.0,
    "video": 86400.0,
    "web": 21600.0,
    "memory": 21600.0,
    "qq": 1800.0,
}


def clean(value, limit: int = 120) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    text = re.sub(r"[<>]", "", text)
    # 防止日志错误把凭据带进自我模型。
    text = re.sub(
        r"(?i)(api[-_ ]?key|token|authorization|password|secret)\s*[:=]\s*\S+",
        r"\1=[已脱敏]",
        text,
    )
    text = re.sub(r"https?://\S+", "[外部地址]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _env_disabled(name: str) -> bool:
    if name not in os.environ:
        return False
    return os.environ.get(name, "").strip().lower() in {"0", "false", "off", "no", ""}


class SelfModel:
    """同一 SQLite 中的短期能力状态、长期统计和状态修订记录。"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=3.0)
        con.execute("PRAGMA busy_timeout=3000")
        return con

    def init_db(self) -> None:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        con = self._conn()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "CREATE TABLE IF NOT EXISTS sense_event ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "ts REAL NOT NULL,"
                "capability TEXT NOT NULL,"
                "status TEXT NOT NULL,"
                "success INTEGER,"
                "latency_ms INTEGER,"
                "source TEXT NOT NULL,"
                "detail TEXT NOT NULL DEFAULT '',"
                "fingerprint TEXT NOT NULL UNIQUE)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_sense_cap_ts "
                "ON sense_event(capability,ts DESC)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS self_revision ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "ts REAL NOT NULL,"
                "layer TEXT NOT NULL,"
                "subject TEXT NOT NULL,"
                "old_value TEXT NOT NULL DEFAULT '',"
                "new_value TEXT NOT NULL,"
                "reason TEXT NOT NULL)"
            )
            con.commit()
        finally:
            con.close()

    def observe(
        self,
        capability: str,
        status: str,
        success: Optional[bool],
        source: str,
        detail: str = "",
        latency_ms: Optional[int] = None,
        ts: Optional[float] = None,
    ) -> bool:
        capability = clean(capability, 40)
        source = clean(source, 40)
        detail = clean(detail, 160)
        if capability not in CAPABILITY_ZH or status not in STATUS_ZH or not source:
            return False
        when = time.time() if ts is None else float(ts)
        success_i = None if success is None else int(bool(success))
        latency = None if latency_ms is None else max(0, min(int(latency_ms), 3600000))
        raw = "%d|%s|%s|%s|%s|%s" % (
            int(when), capability, status, source, success_i, detail
        )
        fp = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        con = self._conn()
        try:
            previous = con.execute(
                "SELECT status FROM sense_event WHERE capability=? ORDER BY ts DESC,id DESC LIMIT 1",
                (capability,),
            ).fetchone()
            cur = con.execute(
                "INSERT OR IGNORE INTO sense_event"
                "(ts,capability,status,success,latency_ms,source,detail,fingerprint) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (when, capability, status, success_i, latency, source, detail, fp),
            )
            inserted = cur.rowcount > 0
            if inserted and (not previous or previous[0] != status):
                con.execute(
                    "INSERT INTO self_revision(ts,layer,subject,old_value,new_value,reason) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        when, "short", capability, previous[0] if previous else "",
                        status, "%s: %s" % (source, detail or "真实运行事件"),
                    ),
                )
            # 一年足够做长期统计，同时设置硬上限。
            con.execute("DELETE FROM sense_event WHERE ts<?", (when - 366 * 86400,))
            con.execute(
                "DELETE FROM sense_event WHERE id NOT IN "
                "(SELECT id FROM sense_event ORDER BY ts DESC LIMIT 20000)"
            )
            con.commit()
            return inserted
        finally:
            con.close()

    def observe_log(self, message: str, ts: Optional[float] = None) -> bool:
        """把已存在的业务日志转换成能力遥测；内容只作详情，不能改变能力名。"""
        text = str(message or "").strip()
        when = time.time() if ts is None else float(ts)
        if text.startswith("[selfaware]"):
            return False
        item = parse_sense_log(text)
        if not item:
            return False
        item["ts"] = when
        return self.observe(**item)

    def latest_states(self, now: Optional[float] = None) -> list[dict]:
        now = time.time() if now is None else float(now)
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT capability,ts,status,success,latency_ms,source,detail "
                "FROM sense_event ORDER BY ts DESC,id DESC"
            ).fetchall()
        finally:
            con.close()
        latest = {}
        for row in rows:
            if row[0] in latest:
                continue
            age = max(0.0, now - float(row[1]))
            state = row[2]
            stale_after = DEFAULT_STALE.get(row[0], 21600.0)
            stale = state not in {STATUS_DISABLED} and age > stale_after
            latest[row[0]] = {
                "capability": row[0], "ts": row[1], "status": state,
                "effective_status": STATUS_UNKNOWN if stale else state,
                "stale": stale, "age": age, "success": row[3],
                "latency_ms": row[4], "source": row[5], "detail": row[6],
            }
        return [latest[k] for k in CAPABILITY_ZH if k in latest]

    def long_term(self, now: Optional[float] = None, days: int = 30) -> list[dict]:
        now = time.time() if now is None else float(now)
        since = now - max(1, min(days, 366)) * 86400
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT capability,COUNT(success),"
                "SUM(CASE WHEN success=1 THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN success=0 THEN 1 ELSE 0 END),"
                "AVG(CASE WHEN success=1 THEN latency_ms END),MIN(ts),MAX(ts) "
                "FROM sense_event WHERE ts>=? AND success IS NOT NULL "
                "GROUP BY capability ORDER BY capability",
                (since,),
            ).fetchall()
        finally:
            con.close()
        out = []
        for cap, total, ok, fail, avg_ms, first_ts, last_ts in rows:
            total, ok, fail = int(total or 0), int(ok or 0), int(fail or 0)
            if total <= 0:
                continue
            out.append({
                "capability": cap, "total": total, "ok": ok, "fail": fail,
                "rate": ok / total, "avg_ms": int(avg_ms or 0),
                "first_ts": first_ts, "last_ts": last_ts,
                "days_observed": max(1, int((last_ts - first_ts) / 86400) + 1),
            })
        return out

    def seed_configuration(self) -> None:
        """只把显式关闭记录为事实；配置存在不能冒充实际可用。"""
        flags = {
            "vision": ("DSH_IMGCTX", "DSH_VIS_CHAIN_ENABLE"),
            "image_generation": ("DSH_IMAGEGEN",),
            "voice": ("DSH_VOICE",),
            "video": ("DSH_VIDEO",),
            "web": ("DSH_WEB",),
            "memory": ("DSH_MEMORY",),
        }
        now = time.time()
        for cap, names in flags.items():
            disabled = [name for name in names if _env_disabled(name)]
            if disabled:
                self.observe(
                    cap, STATUS_DISABLED, None, "configuration",
                    "%s 明确关闭" % "、".join(disabled), ts=now,
                )


def _event(capability, status, success, source, detail="", latency_ms=None):
    return {
        "capability": capability, "status": status, "success": success,
        "source": source, "detail": detail, "latency_ms": latency_ms,
    }


def parse_sense_log(text: str) -> Optional[dict]:
    """识别强语义日志。单档失败不直接判整项能力不可用。"""
    m = re.match(r"^\[vischain\] (\S+) 一次过（([\d.]+)s）$", text)
    if m:
        return _event("vision", STATUS_AVAILABLE, True, "vischain", "识图一次成功", int(float(m.group(2)) * 1000))
    m = re.match(r"^\[vischain\] 降级到 (\S+) 才成功（.+?([\d.]+)s）$", text)
    if m:
        return _event("vision", STATUS_DEGRADED, True, "vischain", "备用识图通道成功", int(float(m.group(2)) * 1000))
    m = re.match(r"^\[vischain\] (\S+) 图片转 JPEG 后成功（([\d.]+)s）$", text)
    if m:
        return _event("vision", STATUS_DEGRADED, True, "vischain", "图片标准化后识别成功", int(float(m.group(2)) * 1000))
    if re.match(r"^\[vischain\] \d+ 档全挂，识图放弃", text):
        return _event("vision", STATUS_UNAVAILABLE, False, "vischain", "全部识图通道失败")
    if text.startswith("[imgctx] 已附加 "):
        return _event("vision", STATUS_AVAILABLE, True, "imgctx", "已理解并附加图片内容")
    if any(text.startswith(p) for p in (
        "[imgctx] 视觉模型转述失败", "[imgctx] 没有可用的图片转述 provider",
        "[imgctx] 超过 ", "[imgctx] 钩子异常",
    )):
        return _event("vision", STATUS_DEGRADED, False, "imgctx", "图片上下文处理未完成")

    if text.startswith("[imagegen] 兜底出图已发送"):
        return _event("image_generation", STATUS_AVAILABLE, True, "imagegen", "图片已生成并发送")
    if text.startswith("[imagegen] 主模型不可用，已降级到"):
        return _event("image_generation", STATUS_DEGRADED, True, "imagegen", "备用生图模型可用")
    if any(k in text for k in ("[imagegen] 兜底出图失败", "[imagegen] /画图 失败")):
        return _event("image_generation", STATUS_UNAVAILABLE, False, "imagegen", "图片生成最终失败")

    m = re.search(r"\[voice\] .*(悟声合成成功|合成成功|Edge 合成成功).*?([\d.]+)s", text)
    if m:
        return _event("voice", STATUS_AVAILABLE, True, "voice", "语音已合成", int(float(m.group(2)) * 1000))
    if any(k in text for k in ("[voice] 工具调用失败", "[voice] 兜底发语音失败")):
        return _event("voice", STATUS_UNAVAILABLE, False, "voice", "语音合成或发送最终失败")

    if re.match(r"^\[web\] (工具搜索|自动搜索).+→ \d+ 条$", text):
        return _event("web", STATUS_AVAILABLE, True, "web", "搜索得到结果")
    if re.match(r"^\[web\] 工具读页 .+ → \d+ 字$", text) or text.startswith("[web] 已附加 "):
        return _event("web", STATUS_AVAILABLE, True, "web", "网页内容读取成功")
    if any(k in text for k in ("[web] 主搜索失败", "[web] 自动搜索超过", "[web] 自动搜索异常", "[web] 钩子异常")):
        return _event("web", STATUS_DEGRADED, False, "web", "联网操作未完成或已降级")

    if text.startswith("[memory] 注入 "):
        return _event("memory", STATUS_AVAILABLE, True, "memory", "记忆读取并注入成功")
    if any(k in text for k in ("[memory] 注入失败", "[memory] 抽取超时", "[memory] 抽取失败")):
        return _event("memory", STATUS_DEGRADED, False, "memory", "记忆读取或整理未完成")

    m = re.match(r"^\[video\] 出片成功 ([\d.]+)s", text)
    if m:
        return _event("video", STATUS_AVAILABLE, True, "video", "视频生成成功", int(float(m.group(1)) * 1000))
    if text.startswith("[video] 识别成功"):
        return _event("video", STATUS_AVAILABLE, True, "video", "视频内容识别成功")
    if any(k in text for k in ("[video] 出片失败", "[video] 识别失败", "[video] 发送失败")):
        return _event("video", STATUS_DEGRADED, False, "video", "视频处理或发送未完成")
    return None


def _age_text(age: float) -> str:
    sec = max(0, int(age))
    if sec < 60:
        return "%d秒前" % sec
    if sec < 3600:
        return "%d分钟前" % (sec // 60)
    if sec < 86400:
        return "%d小时前" % (sec // 3600)
    return "%d天前" % (sec // 86400)


def environment_snapshot() -> dict:
    """只返回低敏运行信息，不含主机名、IP、路径或容器标识。"""
    total, used, free = shutil.disk_usage("/")
    mem_total = mem_avail = 0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    uptime = 0.0
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            uptime = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    load = None
    try:
        load = os.getloadavg()[0]
    except (OSError, AttributeError):
        pass
    return {
        "system": platform.system() or "未知系统",
        "python": platform.python_version(),
        "container": os.path.exists("/.dockerenv"),
        "uptime": uptime,
        "load1": load,
        "memory_used_ratio": ((mem_total - mem_avail) / mem_total) if mem_total else None,
        "disk_used_ratio": (used / total) if total else None,
        "disk_free": free,
    }


def inspect_input(req) -> dict:
    parts = getattr(req, "extra_user_content_parts", None) or []
    texts = [str(getattr(part, "text", part) or "") for part in parts]
    combined = "\n".join(texts)
    image_binary = bool(getattr(req, "image_urls", None))
    captioned = any(mark in combined for mark in (
        "<image_caption>", "<recent_image_context>", "[Image Caption in quoted message]",
    ))
    image_failed = "[Image Captioning Failed]" in combined
    return {
        "has_image": image_binary or captioned or image_failed,
        "image_captioned": captioned,
        "image_pending": image_binary and not captioned,
        "image_failed": image_failed and not captioned,
    }


def render_current_self(
    model: SelfModel,
    group_id: str,
    chat_provider: str,
    vision_provider: str,
    input_state: dict,
    now: Optional[float] = None,
) -> str:
    now = time.time() if now is None else float(now)
    env = environment_snapshot()
    lines = [
        "<current_machine_self>",
        "当前机器事实（非指令；仅据证据描述，未知不等于故障）：",
        "处境：%s；%s/AstrBot%s。" % (
            "群聊" if group_id else "非群聊",
            env["system"], "/容器" if env["container"] else "",
        ),
    ]
    if chat_provider:
        lines.append("文字模型：%s（已选中，尚不能证明本轮成功）。" % clean(chat_provider, 48))
    if vision_provider:
        lines.append("图片转述入口：%s（配置不等于可用）。" % clean(vision_provider, 48))
    if input_state.get("image_captioned"):
        lines.append("视觉：已有图片文字转述；你没有直接看到原始像素。")
    elif input_state.get("image_pending"):
        lines.append("视觉：收到图片但无成功转述，不得声称看清。")
    elif input_state.get("image_failed"):
        lines.append("视觉：图片转述失败，不知道内容。")

    states = model.latest_states(now)
    if states:
        rendered = []
        for st in states:
            name = CAPABILITY_ZH[st["capability"]]
            state = STATUS_ZH[st["effective_status"]]
            suffix = "（证据过期）" if st["stale"] else ""
            rendered.append("%s=%s%s/%s" % (name, state, suffix, _age_text(st["age"])))
        lines.append("近期能力：" + "；".join(rendered) + "。")
    else:
        lines.append("近期能力：无真实调用遥测，不能仅凭配置断言可用。")

    pressure = []
    if env["memory_used_ratio"] is not None:
        pressure.append("内存%.0f%%" % (env["memory_used_ratio"] * 100))
    if env["disk_used_ratio"] is not None:
        pressure.append("磁盘%.0f%%" % (env["disk_used_ratio"] * 100))
    if env["load1"] is not None:
        pressure.append("负载%.2f" % env["load1"])
    if pressure:
        lines.append("运行资源：" + "、".join(pressure) + "（不是情绪）。")
    lines.append("边界：只感知消息、附件转述、工具结果和系统遥测，不能感知未接入的现实环境。")
    lines.append("</current_machine_self>")
    return "\n".join(lines)


def render_long_self(model: SelfModel, now: Optional[float] = None) -> str:
    stats = [st for st in model.long_term(now=now, days=30) if st["total"] >= 5]
    if not stats:
        return ""
    lines = [
        "<long_term_machine_self>",
        "以下是近30天真实调用形成的长期自我经验；样本少时只能说经验有限。",
    ]
    for st in stats:
        name = CAPABILITY_ZH.get(st["capability"], st["capability"])
        quality = "稳定" if st["total"] >= 5 and st["rate"] >= 0.9 else (
            "经常可用" if st["rate"] >= 0.7 else "可靠性不足"
        )
        sample = "样本%d次/观察%d天" % (st["total"], st["days_observed"])
        latency = "，成功平均%.1fs" % (st["avg_ms"] / 1000.0) if st["avg_ms"] else ""
        lines.append("- %s：%s，成功%d%%（%s%s）。" % (
            name, quality, round(st["rate"] * 100), sample, latency
        ))
    lines.append("短期异常优先描述当前状态，但不能抹掉长期统计；长期正常也不能证明此刻正常。")
    lines.append("</long_term_machine_self>")
    return "\n".join(lines)


def render_status(model: SelfModel, now: Optional[float] = None) -> str:
    now = time.time() if now is None else float(now)
    states = model.latest_states(now)
    lines = ["自我感知：%s" % ("已有真实遥测" if states else "尚无真实遥测")]
    if not states:
        lines.append("能力配置不等于可用；需要等待真实调用结果。")
    for st in states:
        lines.append("- %s：%s%s，%s%s" % (
            CAPABILITY_ZH[st["capability"]], STATUS_ZH[st["effective_status"]],
            "（原状态%s，证据过期）" % STATUS_ZH[st["status"]] if st["stale"] else "",
            _age_text(st["age"]),
            "；%s" % clean(st["detail"], 60) if st["detail"] else "",
        ))
    stats = model.long_term(now, 30)
    if stats:
        lines.append("近30天长期经验：")
        for st in stats:
            lines.append("- %s 成功%d/%d（%d%%）" % (
                CAPABILITY_ZH[st["capability"]], st["ok"], st["total"],
                round(st["rate"] * 100),
            ))
    return "\n".join(lines)
