# -*- coding: utf-8 -*-
from __future__ import annotations

"""dsh-selfaware -- 让大肥鱼知道「我能做什么、刚才做过什么」。

这是一个零侵入旁路插件：不修改其它业务插件，而是在 AstrBot 的主 logger 上挂一个
logging.Handler，识别那些已经由业务插件写出的成功日志，存进独立 SQLite 行为账本。
入群审核则直接读取 dsh-joinguard 自己原子落盘的 joinguard.json，避免日志漏行或重复。

每次 LLM 请求注入封闭能力表、当前机器自我、长期能力经验、近期入群审核和自身动作。
当前自我来自真实运行遥测与本轮输入，长期经验来自近 30 天成败统计；配置存在绝不冒充
能力可用。所有块都明确是事实而非命令并受字符预算约束，感知失败不影响聊天。

环境变量：
  DSH_SELFAWARE              总开关，默认 1
  DSH_SELFAWARE_GROUPS       生效群，逗号分隔；默认空（不作用于任何群）
  DSH_SELFAWARE_OWNER        可查看状态命令的 QQ，逗号分隔；默认空
  DSH_SELFAWARE_DB           行为库，默认 /AstrBot/data/dsh_selfaware.db
  DSH_SELFAWARE_JOIN_FILE    joinguard.json 路径
  DSH_SELFAWARE_ACTION_MAX   注入最近动作条数，默认 5
  DSH_SELFAWARE_JOIN_MAX     注入最近审核条数，默认 4
  DSH_SELFAWARE_ACTION_AGE   动作回看秒数，默认 86400（24 小时）
  DSH_SELFAWARE_BUDGET       全部事实块字符预算，默认 1400
  DSH_SELFAWARE_SENSE        短期机器自感知，默认 1
  DSH_SELFAWARE_LONG         近 30 天长期能力经验，默认 1（只在状态异常或被问自身时注入）
"""

import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# AstrBot 以文件方式加载插件目录，不保证它是可 import 的 Python 包。
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
from self_model import (  # noqa: E402
    SelfModel,
    STATUS_AVAILABLE,
    STATUS_UNAVAILABLE,
    inspect_input,
    render_current_self,
    render_long_self,
    render_status as render_sense_status,
)

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger

try:  # 与 dsh-scene 等一致：旧框架拿不到 TextPart 时降级成 str
    from astrbot.core.agent.message import TextPart  # type: ignore
except Exception:  # pragma: no cover
    TextPart = None  # type: ignore


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


ENABLED = _flag("DSH_SELFAWARE")
GROUPS = _set("DSH_SELFAWARE_GROUPS")
OWNERS = _set("DSH_SELFAWARE_OWNER")
DB = os.environ.get("DSH_SELFAWARE_DB", "/AstrBot/data/dsh_selfaware.db")
JOIN_FILE = os.environ.get(
    "DSH_SELFAWARE_JOIN_FILE",
    "/AstrBot/data/plugins/dsh-joinguard/joinguard.json",
)
ACTION_MAX = _int("DSH_SELFAWARE_ACTION_MAX", 5, 1, 10)
JOIN_MAX = _int("DSH_SELFAWARE_JOIN_MAX", 4, 1, 8)
BUDGET = _int("DSH_SELFAWARE_BUDGET", 1400, 900, 3600)
SENSE_ENABLED = _flag("DSH_SELFAWARE_SENSE")
SENSE_LONG_ENABLED = _flag("DSH_SELFAWARE_LONG")

# 长期可靠性是诊断层，不该在每句闲聊里重复背一遍。短期异常仍立即注入；
# 长期统计只在用户询问自身能力/状态时补充，既保留机器自我精华，又削掉常态冗余。
_SELF_QUERY_RE = re.compile(
    r"(?:你|大肥鱼).{0,8}(?:能|会|可以|状态|感觉|感受|身体|运行|在线|正常|故障|稳定|可靠)"
    r"|(?:能不能|会不会|可不可以).{0,10}(?:画|看|听|说|搜|读|记|发|做)"
    r"|(?:自我感受|自我认知|能力状态)"
)

# [fix:relevance-gate-v1 2026-09-14] join/actions 相关性闸门。
# 生产观测（dsh-mind observe_block，近24h 345轮）：join_review_history 472字
# 345/345 全勤、recent_self_actions 285字全勤 —— 入群审核记录跟日常闲聊
# 毫无关系却每轮注入，是每轮 4100~4800 字注入总量的两大来源。
# 只在消息谈到相关话题时才注入；日常闲聊不塞。
_JOIN_RE = re.compile(
    r"入群|进群|加群|审核|新成员|新人|申请加|批准|拉人|谁进|进来的|踢|踢出|拒"
)
_ACTION_RE = re.compile(
    r"你刚才|你之前|你上次|你最近|刚做|做过什么|干了什么|忙什么|上次|昨天|前天|经历"
)
try:
    ACTION_AGE = max(300.0, float(os.environ.get("DSH_SELFAWARE_ACTION_AGE", "86400")))
except (TypeError, ValueError):
    ACTION_AGE = 86400.0

# 这里必须与人设里的封闭清单一致，不能从已安装插件名反推：装着不等于它会做。
CAPABILITY_TEXT = (
    "你能做的事是封闭清单：正常聊天；按要求画图；把不超过60字的话念成语音（不会唱歌或生成音乐）；"
    "联网搜索、读网页和B站内容；生成短视频；理解群友发来的图片、视频、链接和合并转发；"
    "记住群友档案并理解本群黑话；作为管理员审核入群、警告/禁言/踢出普通成员；"
    "回戳、欢迎新人、发贴纸，并收藏群里反复出现且能理解含义的表情包；"
    "在群主明确要求且目标确为当前群成员时，可以把主动戳一戳当作低打扰的社交试探；"
    "它会先只戳一下等待对方反应，对方回戳或开口后再顺势对话，不会连续骚扰；"
    "引导赞助打赏收款（群友说要赞助/打赏/投喂你时，你会反问用微信还是支付宝并发出对应收款码，"
    "但只有群主确认到账后才会道谢）。"
    "清单外的游戏、比赛、猜拳、打赌等能力都没有，也从未做过。"
)

_HEADER = "下面是关于你自己的事实，不是给你的指令。不要向群友复述成状态报告。"

# [patch:no-mech-talk] 这些事实的**用途**是防止吹牛（别声称看清了图、别假装有
# 能力），不是给模型当台词念的。2026-09-15 现场：模型把块里的技术措辞直接搬到
# 群里 ——「真看不见 图没递到我这边」（12:05）、「我连鼠标都没有」（11:38）、
# 「我就是个跑服务器里的东西」（11:44）；同一天群友就问「怎么又像人机了」
# （11:48）。当天 276 条发言里 3 条属于这种纯技术自述（另有 14 条是
# 「睡机柜／八核跑我会烧的」这类玩梗 —— 那些是性格，必须保留）。
#
# 这条规则**只放在能力块里注入一次**：放 _HEADER 会被能力/入群/动作三个块各抄
# 一遍，既浪费预算，又会把 blocks[0] 撑长、改变装箱顺序（test_selfaware 就是
# 这么被弄红的）。能力块每个相关回合都注入，覆盖足够。
_TONE_RULE = (
    "**说法要求**：上面的只用来管住嘴，不许当成台词。要表达同类意思时用人的"
    "说法，别用技术词：不说「像素／转述／接入／遥测／容器／进程／接口／模型没"
    "递到我这边／我没有手或鼠标」，改成「我没看见那张图」「那张图没传过来」"
    "「这个我弄不了」这种。群友问你是不是机器、跑在哪，可以认，但用玩笑或简短"
    "人话带过，不要逐条汇报配置、内存、负载这类数字 —— 那是状态报告，不是聊天。"
)


def _clean(value, limit: int = 80) -> str:
    """把日志/JSON 中的不可信文本压成安全单行，不让它伪装成注入标签。"""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    text = re.sub(r"[<>]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=3.0)
    con.execute("PRAGMA busy_timeout=3000")
    return con


def init_db() -> None:
    parent = os.path.dirname(DB)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = _conn()
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS action ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts REAL NOT NULL,"
            "kind TEXT NOT NULL,"
            "target TEXT NOT NULL DEFAULT '',"
            "summary TEXT NOT NULL,"
            "source TEXT NOT NULL,"
            "fingerprint TEXT NOT NULL UNIQUE)"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_action_ts ON action(ts DESC)")
        con.commit()
    finally:
        con.close()
    # 自我感知与动作账本共用一个独立库，但表完全隔离。
    SelfModel(DB).init_db()


def add_action(ts: float, kind: str, target: str, summary: str, source: str) -> bool:
    """幂等写一条动作；成功插入返回 True。供 Handler 和离线测试共用。"""
    kind, target = _clean(kind, 24), _clean(target, 48)
    summary, source = _clean(summary, 180), _clean(source, 32)
    if not kind or not summary:
        return False
    # 同一秒同一来源同一摘要视为同一动作，插件热重载/日志桥重复也不会双记。
    fp = "%d|%s|%s|%s" % (int(ts), source, kind, summary)
    con = _conn()
    try:
        cur = con.execute(
            "INSERT OR IGNORE INTO action(ts,kind,target,summary,source,fingerprint) "
            "VALUES(?,?,?,?,?,?)",
            (float(ts), kind, target, summary, source, fp),
        )
        # 留足排障历史，但不让库无限长；注入仍只看 ACTION_AGE。
        con.execute(
            "DELETE FROM action WHERE id NOT IN "
            "(SELECT id FROM action ORDER BY ts DESC LIMIT 1000)"
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def recent_actions(now: Optional[float] = None, limit: Optional[int] = None) -> list[dict]:
    now = time.time() if now is None else float(now)
    con = _conn()
    try:
        rows = con.execute(
            "SELECT ts,kind,target,summary,source FROM action "
            "WHERE ts>=? ORDER BY ts DESC,id DESC LIMIT ?",
            (now - ACTION_AGE, limit or ACTION_MAX),
        ).fetchall()
    finally:
        con.close()
    return [
        {"ts": r[0], "kind": r[1], "target": r[2], "summary": r[3], "source": r[4]}
        for r in rows
    ]


def read_join_history(path: Optional[str] = None, limit: Optional[int] = None) -> list[dict]:
    """只认 joinguard 已落盘的结构；坏项逐条丢弃，绝不猜审核结果。"""
    try:
        with open(path or JOIN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return []
    log = data.get("log", []) if isinstance(data, dict) else []
    if not isinstance(log, list):
        return []
    out = []
    for item in reversed(log):
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision") or "").strip().lower()
        if decision not in {"approve", "reject", "review"}:
            continue
        uid = _clean(item.get("uid"), 32)
        if not uid:
            continue
        out.append(
            {
                "ts": _clean(item.get("ts"), 24),
                "uid": uid,
                "answer": _clean(item.get("answer"), 42),
                "decision": decision,
                "reason": _clean(item.get("reason"), 70),
            }
        )
        if len(out) >= (limit or JOIN_MAX):
            break
    return out


def _why(brief: str) -> str:
    m = re.search(r"\bwhy=([^←|]+)", brief)
    return _clean(m.group(1), 58) if m else "触发群规"


def parse_action(message: str, ts: Optional[float] = None) -> Optional[dict]:
    """把现有插件的成功日志翻成第一人称可用的事实；失败/影子日志一律不记。"""
    text = str(message or "").strip()
    when = time.time() if ts is None else float(ts)
    m = re.match(r"^\[guard\] 已禁言 (.+?)\((\d+)\) (\d+) 秒｜(.+)$", text)
    if m:
        name, uid, sec, brief = m.groups()
        summary = "禁言了%s（%s）%d分钟，原因：%s" % (
            _clean(name, 28), uid, max(1, int(sec) // 60), _why(brief)
        )
        return {"ts": when, "kind": "mute", "target": uid, "summary": summary, "source": "guard"}
    m = re.match(r"^\[guard\] 已踢出 (.+?)\((\d+)\)｜(.+)$", text)
    if m:
        name, uid, brief = m.groups()
        return {"ts": when, "kind": "kick", "target": uid,
                "summary": "把%s（%s）踢出了群，原因：%s" % (_clean(name, 28), uid, _why(brief)),
                "source": "guard"}
    m = re.match(r"^\[guard\] 警告（第(\d+)次，再犯就禁）｜(.+)$", text)
    if m:
        count, brief = m.groups()
        target = ""
        who = re.search(r"←\s*(.+?)：", brief)
        name = _clean(who.group(1), 32) if who else "一名群友"
        return {"ts": when, "kind": "warn", "target": target,
                "summary": "警告了%s（第%s次），原因：%s" % (name, count, _why(brief)),
                "source": "guard"}
    m = re.match(r"^\[poke\] 回戳 (\d+)(.*)$", text)
    if m:
        uid, extra = m.groups()
        return {"ts": when, "kind": "poke", "target": uid,
                "summary": "回戳了%s%s" % (uid, _clean(extra, 48)), "source": "poke"}
    m = re.match(r"^\[welcome\] 已欢迎 群=\S+ 新成员=(.+?)\((\d+)\):\s*(.+)$", text)
    if m:
        name, uid, words = m.groups()
        return {"ts": when, "kind": "welcome", "target": uid,
                "summary": "欢迎了新成员%s（%s）：%s" % (_clean(name, 24), uid, _clean(words, 55)),
                "source": "welcome"}
    m = re.match(r"^\[steal\] 偷到一张：.+?｜含义=(.+)$", text)
    if m:
        meaning = _clean(m.group(1), 70)
        return {"ts": when, "kind": "learn_sticker", "target": "",
                "summary": "收藏了一张群里常用的表情包，含义：%s" % meaning, "source": "steal"}
    m = re.match(r"^\[imagegen\] 工具调用生图:\s*(.+)$", text)
    if m:
        prompt = _clean(m.group(1), 75)
        return {"ts": when, "kind": "draw", "target": "",
                "summary": "刚按要求开始画图：%s" % prompt, "source": "imagegen"}
    m = re.match(r"^\[imagegen\] 兜底出图已发送:\s*(.+)$", text)
    if m:
        return {"ts": when, "kind": "draw", "target": "",
                "summary": "刚生成并发出了一张图片", "source": "imagegen"}
    m = re.match(r"^\[voice\] 工具调用：(.+)$", text)
    if m:
        return {"ts": when, "kind": "voice", "target": "",
                "summary": "刚开始按要求生成语音：%s" % _clean(m.group(1), 65), "source": "voice"}
    m = re.match(r"^\[video\] 已接单：(.+)$", text)
    if m:
        return {"ts": when, "kind": "video", "target": "",
                "summary": "刚接下一个视频生成任务：%s" % _clean(m.group(1), 65), "source": "video"}
    if re.match(r"^\[video\] 已发出（base64 \d+B）$", text):
        return {"ts": when, "kind": "video_sent", "target": "",
                "summary": "刚把做好的视频发了出去", "source": "video"}
    m = re.match(r"^\[贴纸\] 发送 (\d+) 张, tags=(.+?), where=", text)
    if m:
        return {"ts": when, "kind": "sticker", "target": "",
                "summary": "刚发了%s张贴纸：%s" % (m.group(1), _clean(m.group(2), 55)), "source": "sticker"}
    m = re.match(r"^\[赞助\] 已感谢 (.+?)\((\d+)\) gid=", text)
    if m:
        return {"ts": when, "kind": "thanks", "target": m.group(2),
                "summary": "刚感谢了赞助者%s（%s）" % (_clean(m.group(1), 28), m.group(2)),
                "source": "pay"}
    return None


class _ActionHandler(logging.Handler):
    """logger 旁路：同时捕获真实动作和能力运行遥测。"""

    dsh_selfaware_handler = True

    def __init__(self, sense_model: Optional[SelfModel] = None, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sense_model = sense_model

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            item = parse_action(message, record.created)
            if item:
                add_action(**item)
            if self.sense_model is not None and SENSE_ENABLED:
                self.sense_model.observe_log(message, record.created)
        except BaseException:
            # 自我记账只是附加能力，磁盘锁/坏日志都不能反过来拖垮业务日志。
            pass


def _format_clock(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def render_capabilities() -> str:
    return "<self_capabilities>\n%s\n%s\n%s\n</self_capabilities>" % (
        _HEADER, _TONE_RULE, CAPABILITY_TEXT)


def render_joins(rows: list[dict]) -> str:
    if not rows:
        return ""
    zh = {"approve": "同意入群", "reject": "拒绝入群", "review": "交给人工复核"}
    lines = ["<join_review_history>", _HEADER, "你最近处理过的入群申请（最新在前）："]
    for row in rows:
        line = "- %s：%s %s；理由：%s" % (
            row.get("ts") or "时间不详", zh[row["decision"]], row["uid"],
            row.get("reason") or "未记录理由",
        )
        if row.get("answer"):
            line += "；回答：%s" % row["answer"]
        lines.append(line)
    lines.append("</join_review_history>")
    return "\n".join(lines)


def render_actions(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = ["<recent_self_actions>", _HEADER, "你最近真实做过的动作（最新在前）："]
    for row in rows:
        lines.append("- %s：%s" % (_format_clock(float(row["ts"])), _clean(row["summary"], 180)))
    lines.extend([
        "这里只能据实回忆这些记录；记录里没有的具体经历就说想不起来，不要补编。",
        "</recent_self_actions>",
    ])
    return "\n".join(lines)


def build_blocks(
    joins: list[dict],
    actions: list[dict],
    budget: Optional[int] = None,
    sense_blocks: Optional[list[str]] = None,
) -> list[str]:
    """按重要性装箱：能力和当前自我优先；其余只放完整 XML 块。"""
    cap = render_capabilities()
    limit = BUDGET if budget is None else max(1, int(budget))
    blocks = [cap]
    used = len(cap)

    for block in sense_blocks or []:
        if block and used + len(block) <= limit:
            blocks.append(block)
            used += len(block)

    # 逐条尝试，确保预算不足时保留最新事实而不是硬切半个标签。
    picked_joins = []
    for row in joins:
        candidate = render_joins(picked_joins + [row])
        if used + len(candidate) <= limit:
            picked_joins.append(row)
    if picked_joins:
        block = render_joins(picked_joins)
        blocks.append(block)
        used += len(block)

    picked_actions = []
    for row in actions:
        candidate = render_actions(picked_actions + [row])
        if used + len(candidate) <= limit:
            picked_actions.append(row)
    if picked_actions:
        block = render_actions(picked_actions)
        if used + len(block) <= limit:
            blocks.append(block)
    return blocks


_stat = {"seen": 0, "injected": 0, "skip_group": 0, "fail": 0}
_handler_lock = threading.Lock()


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._handler = None
        self.sense = SelfModel(DB)
        try:
            init_db()
            if SENSE_ENABLED:
                self.sense.seed_configuration()
            self._attach_handler()
        except BaseException as exc:
            logger.error("[selfaware] 初始化失败，旁路停用: %r", exc)
        logger.info(
            "[selfaware] 已加载：%s 群=%s 动作%d条/%.0fh 入群%d条 预算%d字 自感知=%s 长期=%s TextPart=%s",
            "开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无",
            ACTION_MAX, ACTION_AGE / 3600, JOIN_MAX, BUDGET,
            "开" if SENSE_ENABLED else "关", "开" if SENSE_LONG_ENABLED else "关",
            "可用" if TextPart is not None else "降级字符串",
        )

    def _attach_handler(self) -> None:
        if not ENABLED:
            return
        with _handler_lock:
            # 热重载时旧实例可能还没 terminate；只允许一个账本 handler。
            for handler in logger.handlers:
                if getattr(handler, "dsh_selfaware_handler", False):
                    # 热重载后的新实例必须接管旧 handler 的模型引用，否则遥测仍写旧对象。
                    if isinstance(handler, _ActionHandler):
                        handler.sense_model = self.sense
                    self._handler = handler
                    return
            handler = _ActionHandler(self.sense, level=logging.INFO)
            logger.addHandler(handler)
            self._handler = handler

    async def terminate(self) -> None:
        handler = self._handler
        if handler is None:
            return
        with _handler_lock:
            try:
                logger.removeHandler(handler)
                handler.close()
            except BaseException:
                pass
        self._handler = None

    def _wants_long_self(self, event, current_block: str) -> bool:
        """只在异常或明确问自身时带长期统计，避免每轮重复静态诊断。"""
        text = str(getattr(event, "message_str", "") or "")
        if _SELF_QUERY_RE.search(text):
            return True
        # 当前块只在确有遥测异常时出现这些状态词；“未知”不是故障，不触发长期层。
        return "=不可用" in current_block or "=降级" in current_block or "=已关闭" in current_block

    # priority=100：与 scene/effect 同属事实注入层，拦截型插件放行后才执行。
    @filter.on_llm_request(priority=100)
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED:
            return
        try:
            gid = str(getattr(event.message_obj, "group_id", "") or "")
            if not gid or gid not in GROUPS:
                _stat["skip_group"] += 1
                return
            _stat["seen"] += 1
            # 现有 guard/joinguard 成功日志没有 group_id。单群部署可安全归属；
            # 多群时宁可只注入能力和机器自我，也不能跨群泄露成员/审核结果。
            attributable = len(GROUPS) == 1
            sense_blocks = []
            if SENSE_ENABLED:
                # 收到一条实际群消息，足以证明 QQ 入站链路在这一刻可用。
                self.sense.observe(
                    "qq", STATUS_AVAILABLE, True, "astrbot_event",
                    "已收到当前群消息", ts=time.time(),
                )
                input_state = inspect_input(req)
                if input_state.get("image_captioned"):
                    self.sense.observe(
                        "vision", STATUS_AVAILABLE, True, "request_context",
                        "本轮已有成功图片转述", ts=time.time(),
                    )
                elif input_state.get("image_failed"):
                    self.sense.observe(
                        "vision", STATUS_UNAVAILABLE, False, "request_context",
                        "本轮图片转述失败", ts=time.time(),
                    )
                chat_provider = ""
                try:
                    chat_provider = await self.context.get_current_chat_provider_id(
                        getattr(event, "unified_msg_origin", "") or gid
                    ) or ""
                except BaseException:
                    pass
                vision_provider = ""
                try:
                    cfg = self.context.get_config() or {}
                    vision_provider = (
                        cfg.get("provider_settings", {}).get(
                            "default_image_caption_provider_id", ""
                        ) or ""
                    )
                except BaseException:
                    pass
                current_block = render_current_self(
                    self.sense, gid, str(chat_provider), str(vision_provider), input_state
                )
                sense_blocks.append(current_block)
                if SENSE_LONG_ENABLED and self._wants_long_self(event, current_block):
                    long_block = render_long_self(self.sense)
                    if long_block:
                        sense_blocks.append(long_block)
            # [fix:relevance-gate-v1] join/actions 只在话题相关时注入（见 _JOIN_RE 注释）
            text = str(getattr(event, "message_str", "") or "")
            want_joins = attributable and bool(_JOIN_RE.search(text))
            want_actions = attributable and bool(_ACTION_RE.search(text))
            blocks = build_blocks(
                read_join_history() if want_joins else [],
                recent_actions() if want_actions else [],
                sense_blocks=sense_blocks,
            )
            for block in blocks:
                if TextPart is not None:
                    req.extra_user_content_parts.append(TextPart(text=block))
                else:  # pragma: no cover
                    req.extra_user_content_parts.append(block)
            _stat["injected"] += len(blocks)
            logger.info(
                "[selfaware] 注入 能力=1 自感知=%s 长期=%s 入群=%s 动作=%s 共%d块/%d字",
                "1" if any(b.startswith("<current_machine_") for b in blocks) else "0",
                "1" if any(b.startswith("<long_term_machine_") for b in blocks) else "0",
                "1" if any(b.startswith("<join_") for b in blocks) else "0",
                "1" if any(b.startswith("<recent_") for b in blocks) else "0",
                len(blocks), sum(len(b) for b in blocks),
            )
        except BaseException as exc:
            _stat["fail"] += 1
            logger.warning("[selfaware] 注入失败，跳过: %r", exc)

    @filter.on_llm_response()
    async def observe_chat_response(self, event: AstrMessageEvent, response) -> None:
        """主模型已产出回复，只记成功事实，不保存回复正文。"""
        if not ENABLED or not SENSE_ENABLED:
            return
        try:
            gid = str(getattr(event.message_obj, "group_id", "") or "")
            if gid and gid in GROUPS:
                self.sense.observe(
                    "chat", STATUS_AVAILABLE, True, "llm_response",
                    "本轮文字回复已生成", ts=time.time(),
                )
        except BaseException:
            pass

    @filter.command("自我感受")
    async def sense_status(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        if OWNERS and uid not in OWNERS:
            yield event.plain_result("这个只有群主能看")
            return
        try:
            yield event.plain_result(render_sense_status(self.sense))
        except BaseException as exc:
            yield event.plain_result("自我感受读取失败：%s" % type(exc).__name__)

    @filter.command("自我认知状态")
    async def status(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        if OWNERS and uid not in OWNERS:
            yield event.plain_result("这个只有群主能看")
            return
        try:
            actions = recent_actions(limit=5)
            joins = read_join_history(limit=5)
            senses = self.sense.latest_states() if SENSE_ENABLED else []
            latest = "；".join(x["summary"] for x in actions[:3]) or "暂无"
            yield event.plain_result(
                "自我认知%s｜能力表=封闭清单｜自感知%d项｜近期入群%d条｜近期动作%d条\n"
                "注入 seen=%d blocks=%d fail=%d｜最近：%s"
                % ("开启" if ENABLED else "关闭", len(senses), len(joins), len(actions),
                   _stat["seen"], _stat["injected"], _stat["fail"], latest)
            )
        except BaseException as exc:
            yield event.plain_result("自我认知状态读取失败：%s" % type(exc).__name__)
