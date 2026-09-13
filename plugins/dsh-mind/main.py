# -*- coding: utf-8 -*-
"""dsh-mind —— 内在状态总线（P0：只读聚合观测，绝不注入）。

═══════════════════════════════════════════════════════════════════
一、要解决什么（群主原话：「有没有一种方案可以整合目前的主动行为，
    比如说情绪欲望等，而不再是独立分支的独岛」）
═══════════════════════════════════════════════════════════════════

现状（2026-09-13 实测）：

  · 11 个各自独立的状态存储：情绪 JSON、欲望 DB、社交 DB、逆反 DB、
    疲劳 JSON、兴趣 JSON、自身利益 JSON、群感知 DB、自我认知 DB、
    效果 DB、主动开口额度 JSON。
  · 33 个插件往同一次请求里各塞各的 `extra_user_content_parts`，
    约 50 种块标签。
  · 跨插件读已经是**网状**：dsh_memory.db 有 15 个读者、dsh_social.db 6 个、
    dsh_interest_state.json 5 个、dsh_emotion_state.json 4 个。
    —— 总线其实早就存在了，只是以最差的形态存在：N×M 点对点读文件，
    每个读者自己再实现一遍解析和「失败即中性」。
  · **没有任何一处代码知道**这一轮有几个岛在说话、它们互相矛不矛盾、
    加起来占多少字。

P0 就是把那个「知道的地方」建起来，而且**零行为变更**：不注入、不写别人的库、
不发消息、不 stop_event。每个 LLM 轮次只产出一行日志 + 一行观测记录。

═══════════════════════════════════════════════════════════════════
二、为什么先做观测而不是直接合并（这个仓库的既定纪律）
═══════════════════════════════════════════════════════════════════

dsh-initiate 合成事件上线时要为 decide / ctxclean / mention 各打一处兼容补丁；
docs/71 的「识图等待该不该省」是一次**被自己的反事实回放推翻**的改动
（按分组算出来能省 81%，实测只省 15%）。教训是同一条：
**先量，再改。** 所以 P0 只回答三个问题：

  1. 一次对话里到底有几个岛在说话？（岛数分布）
  2. 合并成一个块会是多少字？现在各自为政又是多少字？（收益上限）
  3. 岛与岛之间有多少条真冲突？（整合的必要性）

答不出这三点就动手合，等于把 docs/40 的「注入块堆积」重演一遍。

═══════════════════════════════════════════════════════════════════
三、硬边界（写死在代码里）
═══════════════════════════════════════════════════════════════════

1. **绝不注入**。本插件不 import TextPart、不碰 `req`。`DSH_MIND_MODE=inject`
   在 P0 会被明确拒绝并退回 observe（写一行 WARNING），不允许「悄悄生效」。
2. **绝不写别人的状态**。所有外部状态一律 JSON 只读 / SQLite `mode=ro`。
   dsh-emotion 是整体覆写 JSON 的，抢写必然丢状态。
3. **失败即中性**。读不到就当中性并记 errors，绝不抛、绝不阻止回复。
4. **不存别人的原文**。观测库只记「块标签 + 字数」——`<scene>` 之类块里是
   群聊原文，复制进新库等于凭空多一份群聊副本。
5. **权限**。`/心智状态` 含各家状态口径，与 `/禁言状态` 同理属于不该公开的
   信息（公开就等于给群友一份攻略），只对群主/群管理/机器人管理员开放。
6. **优先级 -1**。AstrBot 用 `sort(key=lambda h: -priority)`，数字大的先跑，
   所以 -1 一定排在 ctxclean/interest 等默认 0 的注入器之后 —— 看到的是
   **本轮最终**的请求形状，而不是半成品。

═══════════════════════════════════════════════════════════════════
四、环境变量
═══════════════════════════════════════════════════════════════════

  DSH_MIND            总开关（默认 1）
  DSH_MIND_MODE       observe（P0 唯一支持值）
  DSH_MIND_GROUPS     生效群号，逗号分隔
  DSH_MIND_OWNER      群主 QQ，逗号分隔（看 /心智状态）
  DSH_MIND_DB         观测库路径，默认 /AstrBot/data/dsh_mind.db
  DSH_MIND_BUDGET     统一草稿预算（字），默认 900
  DSH_MIND_KEEP_DAYS  观测保留天数，默认 14
  DSH_MIND_MAX_ROWS   观测库行数上限，默认 30000
  DSH_MIND_SAMPLE     采样率 0~1，默认 1.0（群里太吵时可降到 0.3）
  DSH_MIND_*_DB / DSH_MIND_*_STATE   各状态源路径覆盖（见 mind_logic.MindReader）
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.platform.message_type import MessageType

from .mind_logic import (
    MindReader,
    annotations,
    detect_conflicts,
    islands,
    render_draft,
    scan_contexts,
    scan_parts,
    snapshot_line,
)


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no", ""}


def _set(name: str, default: str = "") -> set:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_MIND")
MODE = os.environ.get("DSH_MIND_MODE", "observe").strip().lower()
GROUPS = _set("DSH_MIND_GROUPS", "100000001")
OWNERS = _set("DSH_MIND_OWNER", "2774000001")
DB_PATH = Path(os.environ.get("DSH_MIND_DB", "/AstrBot/data/dsh_mind.db"))
BUDGET = max(200, min(2000, int(os.environ.get("DSH_MIND_BUDGET", "900"))))
KEEP_DAYS = max(1, min(90, int(os.environ.get("DSH_MIND_KEEP_DAYS", "14"))))
MAX_ROWS = max(1000, int(os.environ.get("DSH_MIND_MAX_ROWS", "30000")))
try:
    SAMPLE = min(1.0, max(0.0, float(os.environ.get("DSH_MIND_SAMPLE", "1.0"))))
except (TypeError, ValueError):
    SAMPLE = 1.0

_stat = {"turns": 0, "skipped_group": 0, "skipped_sample": 0, "skip_synthetic": 0,
         "rows": 0, "fail": 0, "islands": 0, "conflicts": 0}


def _is_admin(event) -> bool:
    """群主/群管理/机器人管理员。与 dsh-guard._is_group_admin 逐字一致
    （两个插件不能互相 import，只能各存一份；改了记得两边一起改）。

    不用框架的 PermissionType.ADMIN：它只认 cmd_config.json 的 admins_id
    （这台机器上是 ['astrbot'] 一个 WebUI 账号），群里任何人包括群主都会被拒。
    """
    try:
        if getattr(event, "role", "") == "admin":
            return True
    except BaseException:
        pass
    try:
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            role = str((raw.get("sender") or {}).get("role") or "")
        else:
            role = str(getattr(getattr(raw, "sender", None), "role", "") or "")
        if role in ("owner", "admin"):
            return True
    except BaseException:
        pass
    try:
        return str(event.get_sender_id() or "") in OWNERS
    except BaseException:
        return False


class MindStore:
    """只属于本插件的观测库。别人不读它，它也不写别人。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self._since_prune = 0
        self._prepare()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=3.0)
        con.execute("PRAGMA busy_timeout=3000")
        return con

    def _prepare(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            old = os.umask(0o077)
            try:
                with self._connect() as con:
                    con.executescript(
                        """
                        PRAGMA journal_mode=WAL;
                        CREATE TABLE IF NOT EXISTS observe(
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            ts REAL NOT NULL,
                            gid TEXT NOT NULL,
                            uid TEXT NOT NULL,
                            n_blocks INTEGER NOT NULL,
                            inject_chars INTEGER NOT NULL,
                            ctx_chars INTEGER NOT NULL,
                            n_islands INTEGER NOT NULL,
                            draft_len INTEGER NOT NULL,
                            n_conflicts INTEGER NOT NULL,
                            islands TEXT NOT NULL DEFAULT '[]',
                            conflicts TEXT NOT NULL DEFAULT '[]',
                            notes TEXT NOT NULL DEFAULT '[]',
                            errors TEXT NOT NULL DEFAULT '[]',
                            flags TEXT NOT NULL DEFAULT ''
                        );
                        CREATE INDEX IF NOT EXISTS idx_observe_ts ON observe(ts);
                        CREATE TABLE IF NOT EXISTS observe_block(
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            obs_id INTEGER NOT NULL,
                            tag TEXT NOT NULL,
                            chars INTEGER NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS idx_block_obs ON observe_block(obs_id);
                        """
                    )
                self._migrate()
            finally:
                os.umask(old)
        except (OSError, sqlite3.Error) as exc:
            logger.warning("[mind] 观测库不可用，本插件退化为只打日志：%s", exc)

    def _migrate(self) -> None:
        """补上后加的列。CREATE TABLE IF NOT EXISTS 对既有库无效，
        而生产库在第一次上线当天就已经有真实数据了（不能靠删库重建）。"""
        with self._connect() as con:
            have = {r[1] for r in con.execute("PRAGMA table_info(observe)").fetchall()}
            for column, ddl in (("notes", "TEXT NOT NULL DEFAULT '[]'"),):
                if column not in have:
                    con.execute("ALTER TABLE observe ADD COLUMN %s %s" % (column, ddl))

    def add(self, row: dict, parts) -> None:
        with self.lock:
            try:
                with self._connect() as con:
                    cur = con.execute(
                        "INSERT INTO observe(ts,gid,uid,n_blocks,inject_chars,ctx_chars,"
                        "n_islands,draft_len,n_conflicts,islands,conflicts,notes,errors,flags) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (row["ts"], row["gid"], row["uid"], row["n_blocks"],
                         row["inject_chars"], row["ctx_chars"], row["n_islands"],
                         row["draft_len"], row["n_conflicts"], row["islands"],
                         row["conflicts"], row["notes"], row["errors"], row["flags"]))
                    obs_id = cur.lastrowid
                    con.executemany(
                        "INSERT INTO observe_block(obs_id,tag,chars) VALUES(?,?,?)",
                        [(obs_id, p.tag, p.chars) for p in parts])
                _stat["rows"] += 1
                self._since_prune += 1
                if self._since_prune >= 200:
                    self._since_prune = 0
                    self.prune()
            except (OSError, sqlite3.Error) as exc:
                _stat["fail"] += 1
                logger.debug("[mind] 写观测库失败：%s", exc)

    def prune(self) -> None:
        with self.lock:
            try:
                cutoff = time.time() - KEEP_DAYS * 86400
                with self._connect() as con:
                    con.execute("DELETE FROM observe_block WHERE obs_id IN "
                                "(SELECT id FROM observe WHERE ts<?)", (cutoff,))
                    con.execute("DELETE FROM observe WHERE ts<?", (cutoff,))
                    total = con.execute("SELECT count(*) FROM observe").fetchone()[0]
                    if total > MAX_ROWS:
                        keep_from = con.execute(
                            "SELECT ts FROM observe ORDER BY id DESC LIMIT 1 OFFSET ?",
                            (MAX_ROWS,)).fetchone()
                        if keep_from:
                            con.execute("DELETE FROM observe_block WHERE obs_id IN "
                                        "(SELECT id FROM observe WHERE ts<?)", (keep_from[0],))
                            con.execute("DELETE FROM observe WHERE ts<?", (keep_from[0],))
            except (OSError, sqlite3.Error) as exc:
                logger.debug("[mind] 清理观测库失败：%s", exc)

    def recent(self, since: float, limit: int = 5000) -> list:
        with self.lock:
            with self._connect() as con:
                con.row_factory = sqlite3.Row
                return con.execute(
                    "SELECT * FROM observe WHERE ts>? ORDER BY id DESC LIMIT ?",
                    (since, limit)).fetchall()

    def block_stats(self, since: float) -> list:
        with self.lock:
            with self._connect() as con:
                return con.execute(
                    "SELECT b.tag, count(*), sum(b.chars), avg(b.chars) "
                    "FROM observe_block b JOIN observe o ON o.id=b.obs_id "
                    "WHERE o.ts>? GROUP BY b.tag ORDER BY sum(b.chars) DESC",
                    (since,)).fetchall()


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self.reader = MindReader()
        self.store = MindStore(DB_PATH)
        if MODE != "observe":
            logger.warning("[mind] P0 只支持 observe，配置的 MODE=%s 被拒绝（绝不注入）", MODE)
        logger.info(
            "[mind] 已加载：%s 模式=%s 群=%s 预算=%d字 采样=%.2f 观测库=%s "
            "（P0 只观测：不注入、不写别人的状态）",
            "开" if ENABLED else "关", "observe", ",".join(sorted(GROUPS)) or "无",
            BUDGET, SAMPLE, DB_PATH)

    # ------------------------------------------------------------ 观测
    @filter.on_llm_request(priority=-1)
    async def observe(self, event: AstrMessageEvent, req) -> None:
        """排在所有注入器之后（priority=-1），看本轮最终的请求形状。

        全程 fail-open：本插件出任何问题都不允许影响这一次回复。
        """
        if not ENABLED or MODE != "observe":
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or gid not in GROUPS:
                _stat["skipped_group"] += 1
                return
            # 合成事件（主动开口/探头）是同一套管道的另一条入口，单独计数，
            # 不混进「正常对话」的统计里 —— 否则岛数会被他们稀释。
            synthetic = ""
            try:
                if event.get_extra("dsh_initiate"):
                    synthetic = "initiate"
                elif event.get_extra("dsh_proactive"):
                    synthetic = "proactive"
            except BaseException:
                pass
            if synthetic:
                _stat["skip_synthetic"] += 1
            # 采样只影响**入库**，不影响那行日志：日志才是 P0 的主要产物，
            # 群里太吵时先降采样保住日志，而不是把观测一起关掉。
            sampled_out = False
            if SAMPLE < 1.0:
                key = "%s|%s" % (gid, event.get_message_str() or "")
                if (abs(hash(key)) % 1000) / 1000.0 > SAMPLE:
                    sampled_out = True
                    _stat["skipped_sample"] += 1

            now = time.time()
            uid = str(event.get_sender_id() or "")

            def extra(name, default=None, _e=event):
                try:
                    value = _e.get_extra(name)
                except BaseException:
                    return default
                return default if value is None else value

            parts = scan_parts(getattr(req, "extra_user_content_parts", None))
            snap = self.reader.read(gid, uid, now, extra=extra)
            snap.gid = gid
            draft = render_draft(snap, BUDGET)
            conflicts = detect_conflicts(snap, parts)
            # 观测（块/状态不匹配）与冲突分开：前者每轮都成立，混进冲突率就没意义了
            notes = annotations(snap, parts)
            live = islands(snap)

            flags = []
            if synthetic:
                flags.append(synthetic)
            try:
                if getattr(event, "is_at_or_wake_command", False):
                    flags.append("at")
            except BaseException:
                pass
            text = (event.get_message_str() or "").strip()
            if text.startswith("/"):
                flags.append("cmd")

            line = snapshot_line(snap, parts, draft, conflicts, notes)
            if synthetic:
                line += " [合成事件:%s]" % synthetic
            logger.info(line)

            _stat["turns"] += 1
            _stat["islands"] += len(live)
            _stat["conflicts"] += len(conflicts)
            if not synthetic and not sampled_out:
                self.store.add({
                    "ts": now, "gid": gid, "uid": uid,
                    "n_blocks": len(parts),
                    "inject_chars": sum(p.chars for p in parts),
                    "ctx_chars": scan_contexts(getattr(req, "contexts", None)),
                    "n_islands": len(live),
                    "draft_len": len(draft),
                    "n_conflicts": len(conflicts),
                    "islands": json.dumps([i["detail"] for i in live], ensure_ascii=False),
                    "conflicts": json.dumps(conflicts, ensure_ascii=False),
                    "notes": json.dumps(notes, ensure_ascii=False),
                    "errors": json.dumps(list(snap.errors), ensure_ascii=False),
                    "flags": ",".join(flags),
                }, parts)
        except BaseException as exc:  # 观测层永远不许影响回复
            _stat["fail"] += 1
            logger.debug("[mind] 观测失败：%s", exc)

    # ------------------------------------------------------------ 状态
    @filter.command("心智状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """/心智状态 —— 只有群主/群管理/机器人管理员能看到。

        和 /禁言状态 同一个理由：这份输出把各家的状态口径摊开了，
        公开就等于给群里一份「怎么影响它」的攻略。
        """
        if not _is_admin(event):
            yield event.plain_result("群里正常聊就行。")
            return
        now = time.time()
        since = now - 86400
        try:
            rows = self.store.recent(since)
            blocks = self.store.block_stats(since)
        except (OSError, sqlite3.Error) as exc:
            yield event.plain_result("观测库读不了：%s" % exc)
            return

        def pct(seq, q):
            if not seq:
                return 0
            data = sorted(seq)
            idx = min(len(data) - 1, int(q * (len(data) - 1)))
            return data[idx]

        inject = [r["inject_chars"] for r in rows]
        nblocks = [r["n_blocks"] for r in rows]
        nislands = [r["n_islands"] for r in rows]
        drafts = [r["draft_len"] for r in rows]
        ctxs = [r["ctx_chars"] for r in rows]
        conf = 0
        note_hist = {}
        errs = {}
        island_hist = {}
        for r in rows:
            conf += r["n_conflicts"]
            for note in json.loads((r["notes"] if "notes" in r.keys() else "[]") or "[]"):
                note_hist[note] = note_hist.get(note, 0) + 1
            island_hist[r["n_islands"]] = island_hist.get(r["n_islands"], 0) + 1
            for e in json.loads(r["errors"] or "[]"):
                errs[e] = errs.get(e, 0) + 1

        lines = [
            "内在状态总线（P0 只观测，不注入）",
            "--- 近 24 小时 ---",
            "轮次 %d｜岛数 中位 %.1f P90 %d｜注入块 中位 %.1f P90 %d"
            % (len(rows), pct(nislands, 0.5), pct(nislands, 0.9),
               pct(nblocks, 0.5), pct(nblocks, 0.9)),
            "注入字数 中位 %d P90 %d｜历史字数 中位 %d｜块/历史 = %.1f%%"
            % (pct(inject, 0.5), pct(inject, 0.9), pct(ctxs, 0.5),
               (sum(inject) / max(1, sum(ctxs))) * 100),
            "合并草稿 中位 %d P90 %d 字 → 若合并，注入字数约 %s"
            % (pct(drafts, 0.5), pct(drafts, 0.9),
               ("省 %.0f%%" % ((1 - (sum(drafts) / max(1, sum(inject)))) * 100))
               if sum(inject) else "无可比数据"),
            "岛数分布：" + "｜".join("%d岛=%d" % (k, v) for k, v in sorted(island_hist.items())),
            "冲突 %d 条（%.0f%% 的轮次）｜观测 %s"
            % (conf, (conf / max(1, len(rows))) * 100,
               "、".join("%s×%d" % (k, v) for k, v in
                         sorted(note_hist.items(), key=lambda kv: -kv[1])[:3]) or "无"),
            "--- 注入块 Top（近 24h，按总字数） ---",
        ]
        for tag, cnt, total, avg in blocks[:12]:
            lines.append("  %-22s %5d次 %7d字 均%.0f" % (tag, cnt, total, avg))
        if errs:
            lines.append("--- 读不到的状态（按次数） ---")
            for key, cnt in sorted(errs.items(), key=lambda kv: -kv[1])[:8]:
                lines.append("  %s ×%d" % (key, cnt))
        lines.append("--- 本进程 ---")
        lines.append("轮次 %d｜跳过(非生效群) %d｜合成事件 %d｜写库失败 %d"
                     % (_stat["turns"], _stat["skipped_group"],
                        _stat["skip_synthetic"], _stat["fail"]))
        lines.append("草稿预览：" + render_draft(self.reader.read(
            str(event.get_group_id() or ""), str(event.get_sender_id() or ""), now,
            extra=lambda n, d=None: None), BUDGET))
        yield event.plain_result("\n".join(lines))


def _selfcheck() -> None:
    """加载时自检一次：状态源能不能读到。读不到要说清楚，不能静默中性。"""
    reader = MindReader()
    snap = reader.read("0", "0", time.time(), extra=lambda n, d=None: None)
    missing = [e for e in snap.errors if "无该群" in e or "无此人" in e]
    real = [e for e in snap.errors if e not in missing]
    logger.info("[mind] 自检：读到 %s；无记录项 %d；读取失败 %d %s",
                ",".join(snap.sources) or "无", len(missing), len(real),
                ("｜" + "；".join(real)) if real else "")


if ENABLED:
    _selfcheck()
