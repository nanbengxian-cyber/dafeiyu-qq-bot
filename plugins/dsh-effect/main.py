# -*- coding: utf-8 -*-
"""dsh-effect -- 说完之后，看看群里到底什么反应（回复效果观察）。

---------------------------------------------------------------------------
为什么这是最大的缺口

在这之前，这个项目做的每一件事都是**开环**的：注入真人短句样本（dsh-style）、
注入黑话释义（dsh-glossary）、注入漂移档位（dsh-drift），然后祈祷。
说出去的话到底有没有落地，系统完全不知道。

真人不是这样的：讲了个笑话没人接，下次就不那么讲了。这个「说完看反应」的
回路是真人感的分水岭，也是唯一能让它**自己**变好的机制。

参考 MaiBot 的 src/maisaka/reply_effect/（judge / scoring / tracker，
EVALUATION_VERSION=6）。它的分类维度直接抄了，因为设计得确实好 ——
尤其是把「反应冲着什么」单独拆出来：冲内容、冲人设、还是跟它无关。

---------------------------------------------------------------------------
这一版**只测量，不自动改行为**

这是刻意的。拿一个还没验证过的信号去自动调机器人的行为，等于在没有仪表的
情况下拧旋钮。先积累几百条真实评分，人看过分布之后再决定怎么闭环
（最可能的第一个闭环点：`ignored` 比例长期偏高就压低主动回复率）。

所以本插件不碰任何回复路径，不注入任何东西到上下文，只写自己的库。
把它关掉对机器人的行为零影响。

---------------------------------------------------------------------------
观察窗口是免费的

这一点是本项目的结构红利：dsh-memory 已经把群里每条人类消息都写进
dsh_memory.db 的 buffer/archive。所以「它说完之后群里说了什么」不需要
额外采集，直接按时间戳查就有。（机器人自己的话不在那两张表里 ——
实测 buffer 里 group_id=100000001 的机器人消息 0 条 ——
所以它说了什么必须自己在 after_message_sent 里记下来。）

窗口内**一条人类消息都没有**时，结论直接就是 ignored，不花一次模型调用。
按真群的冷清程度，这能省掉相当一部分成本。

---------------------------------------------------------------------------
存储独立

写 /AstrBot/data/dsh_effect.db，不往 dsh_memory.db 里加表 ——
那个库是 dsh-memory 的资产，多一张表就多一处互相踩的可能。
"""

import asyncio
import json
import os
import re
import sqlite3
import time

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_EFFECT")
GROUPS = _set("DSH_EFFECT_GROUPS", "")
DB = os.environ.get("DSH_EFFECT_DB", "/AstrBot/data/dsh_effect.db")
# 语料库（只读）。窗口里群友说了什么全从这里查。
MEM_DB = os.environ.get("DSH_MEM_DB", "/AstrBot/data/dsh_memory.db")
# 观察窗口：说完之后看多久。太短看不到反应，太长会把下一个话题算进来。
WINDOW = max(30.0, float(os.environ.get("DSH_EFFECT_WINDOW", "180")))
# 窗口内最多读几条群友消息进提示词
MAX_AFTER = max(2, int(os.environ.get("DSH_EFFECT_MAX_AFTER", "8")))
# 后台结算的节拍
TICK = max(15.0, float(os.environ.get("DSH_EFFECT_TICK", "60")))
# 一次结算最多处理几条，防止积压时一口气打爆模型
BATCH = max(1, int(os.environ.get("DSH_EFFECT_BATCH", "5")))
TIMEOUT = float(os.environ.get("DSH_EFFECT_TIMEOUT", "30"))
PROVIDER = os.environ.get("DSH_EFFECT_PROVIDER", "").strip()
OWNERS = _set("DSH_EFFECT_OWNER", "")
# 太短的回复（「嗯」「好」）没有可评的策略，不记
MIN_LEN = max(1, int(os.environ.get("DSH_EFFECT_MIN_LEN", "2")))

STANCES = ("appreciation", "playful", "neutral", "confusion",
           "factual_correction", "rejection", "ignored")
TARGETS = ("bot_content", "bot_persona", "topic", "none")
CONTRIBS = ("advance", "maintain", "close", "wrong_push", "none")
STRATEGIES = ("answer", "opinion", "humor", "question", "empathy",
              "acknowledgement", "other")

_ZH = {
    "appreciation": "认可", "playful": "跟着玩", "neutral": "平淡接着说",
    "confusion": "没看懂", "factual_correction": "指出说错了",
    "rejection": "嫌烦", "ignored": "没人理",
    "bot_content": "冲内容", "bot_persona": "冲人设/说话方式",
    "topic": "跟它无关", "none": "无",
    "advance": "推进了", "maintain": "维持住", "close": "收尾", "wrong_push": "带偏了",
    "answer": "回答", "opinion": "表达看法", "humor": "开玩笑/接梗",
    "question": "反问", "empathy": "共情", "acknowledgement": "应一声",
    "other": "其它",
}

SYS = ("你是一个只输出 JSON 的观察器。只根据给到的聊天片段描述事实，"
       "不评价、不建议、不替任何人说话、不输出 JSON 以外的任何字符。")

PROMPT = """下面是一个 QQ 群里的片段。「大肥鱼」是群里的一个机器人。

大肥鱼说了这句：
{reply}

之后群里依次出现了这些消息：
{after}

只输出一个 JSON：
{{"strategy":"...","stance":"...","target":"...","contribution":"...","why":"..."}}

strategy＝大肥鱼那句话在做什么，只能取：
  answer(回答问题) opinion(表达看法) humor(开玩笑或接梗) question(反问追问)
  empathy(安慰共情) acknowledgement(单纯应一声) other
stance＝群里对**大肥鱼那句话**的态度，只能取：
  appreciation(认可或夸) playful(跟着玩起来) neutral(平淡地接着说)
  confusion(没看懂或追问什么意思) factual_correction(指出它说错了)
  rejection(嫌它烦或让它别说) ignored(没人理它，都在聊别的)
target＝群里的反应冲着什么，只能取：
  bot_content(它说的内容) bot_persona(它的说话方式或人设)
  topic(话题本身，跟它没关系) none
contribution＝大肥鱼那句话对这段对话起了什么作用，只能取：
  advance(推进了) maintain(维持住了) close(收尾了) wrong_push(带偏了) none
why＝不超过 20 字的依据，尽量引用你看到的原话。

判不出来就填 neutral / none / other，不要猜。"""


# ---------------------------------------------------------------- 存储
def _conn(path: str = None):
    con = sqlite3.connect(path or DB, timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(path: str = None) -> None:
    con = _conn(path)
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS reply (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            ts REAL NOT NULL,
            due REAL NOT NULL,
            text TEXT NOT NULL,
            addressed INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            reactions INTEGER,
            strategy TEXT, stance TEXT, target TEXT, contribution TEXT,
            why TEXT
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS ix_reply_due "
                    "ON reply(status, due)")
        con.commit()
    finally:
        con.close()


def parse_verdict(raw: str) -> dict | None:
    """从模型输出里抠出结论。取值不在枚举里就退回安全值，绝不写脏数据。

    三层，一层不成换下一层：
      ① 整段就是 JSON；
      ② 从 ```json 之类的壳里把 {...} 抠出来；
      ③ **逐字段正则捞**。第三层是上线第一天就用上的 ——
         真实失败样本是模型输出被截断在 why 里：
             {"strategy":"humor","stance":"rejection","target":"bot_persona",
              "contribution":"wrong_push","why":"某群友骂人，另一个说绷不住了，
         四个枚举字段全都在截断点**之前**，完全可以救回来，只有 why 是残句。
         没有这一层就白扔掉一条已经花过钱的评分。
    """
    text = (raw or "").strip()
    if not text:
        return None
    obj = None
    try:
        obj = json.loads(text)
    except BaseException:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except BaseException:
                obj = None
    if not isinstance(obj, dict):
        # ③ 逐字段捞。至少要捞到一个枚举字段，否则这段输出根本不是结论。
        obj = {}
        for key in ("strategy", "stance", "target", "contribution", "why"):
            hit = re.search(r'"%s"\s*:\s*"([^"]*)"' % key, text)
            if hit:
                obj[key] = hit.group(1)
        if not ({"strategy", "stance", "target", "contribution"} & set(obj)):
            return None

    def pick(key: str, allowed: tuple[str, ...], default: str) -> str:
        v = str(obj.get(key, "") or "").strip().lower()
        return v if v in allowed else default

    return {
        "strategy": pick("strategy", STRATEGIES, "other"),
        "stance": pick("stance", STANCES, "neutral"),
        "target": pick("target", TARGETS, "none"),
        "contribution": pick("contribution", CONTRIBS, "none"),
        "why": str(obj.get("why", "") or "").strip()[:60],
    }


_stat = {"recorded": 0, "settled": 0, "ignored_free": 0, "asked": 0,
         "fail": 0, "parse_fallback": 0, "skip_group": 0, "skip_short": 0}


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._task = None
        try:
            init_db()
        except BaseException as exc:
            logger.error("[effect] 建库失败，本插件停用: %r", exc)
        logger.info(
            "[effect] 已加载：%s 群=%s 窗口%.0fs 节拍%.0fs 每轮最多%d条"
            "（只测量，不改任何回复行为）",
            "开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无",
            WINDOW, TICK, BATCH,
        )

    # ------------------------------------------------------ 记下自己说了什么
    @filter.after_message_sent()
    async def note(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or gid not in GROUPS:
                _stat["skip_group"] += 1
                return
            result = event.get_result()
            if result is None or not result.chain:
                return
            # 只评模型生成的回复。指令回显（/黑话状态 之类）没有「策略」可评。
            try:
                if not result.is_model_result():
                    return
            except BaseException:
                pass
            text = (result.get_plain_text() or "").strip()
            if len(text) < MIN_LEN:
                _stat["skip_short"] += 1
                return
            now = time.time()
            con = _conn()
            try:
                con.execute(
                    "INSERT INTO reply(group_id,ts,due,text,addressed,status) "
                    "VALUES(?,?,?,?,?,'pending')",
                    (gid, now, now + WINDOW, text[:500],
                     1 if getattr(event, "is_at_or_wake_command", False) else 0),
                )
                con.commit()
            finally:
                con.close()
            _stat["recorded"] += 1
            logger.debug("[effect] 记下一条待观察：%s", text[:30])
        except BaseException as exc:
            # 观察是旁路，出任何问题都不许影响已经发出去的消息
            logger.warning("[effect] 记录失败，跳过: %r", exc)

    # ------------------------------------------------------ 后台结算
    @filter.on_astrbot_loaded()
    async def start(self) -> None:
        if not ENABLED or not GROUPS:
            return
        self._task = asyncio.create_task(self._loop())

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(TICK)
                try:
                    await self._settle()
                except BaseException as exc:   # 一轮出错不能带崩循环
                    logger.error("[effect] 结算异常: %r", exc)
        except asyncio.CancelledError:
            return
        except BaseException:
            logger.exception("[effect] 后台循环退出")

    def _due_rows(self) -> list[tuple]:
        con = _conn()
        try:
            return con.execute(
                "SELECT id,group_id,ts,due,text,addressed FROM reply "
                "WHERE status='pending' AND due<=? ORDER BY id LIMIT ?",
                (time.time(), BATCH),
            ).fetchall()
        finally:
            con.close()

    def _after(self, gid: str, start: float, end: float) -> list[tuple[str, str]]:
        """窗口内群友说了什么。只读 dsh-memory 的库，绝不写。"""
        out: list[tuple[float, str, str]] = []
        try:
            con = sqlite3.connect("file:%s?mode=ro" % MEM_DB, uri=True, timeout=3.0)
        except BaseException as exc:
            logger.warning("[effect] 打不开语料库 %s: %r", MEM_DB, exc)
            return []
        try:
            for table in ("buffer", "archive"):
                try:
                    rows = con.execute(
                        "SELECT ts,name,text FROM %s WHERE group_id=? "
                        "AND ts>? AND ts<=? ORDER BY ts" % table,
                        (gid, start, end),
                    ).fetchall()
                except sqlite3.Error:
                    continue
                for ts, name, text in rows:
                    if text and text.strip():
                        out.append((ts, str(name or "?"), text.strip()))
        finally:
            try:
                con.close()
            except BaseException:
                pass
        out.sort(key=lambda x: x[0])
        return [(n, t) for _ts, n, t in out[:MAX_AFTER]]

    def _save(self, rid: int, status: str, reactions: int,
              v: dict | None) -> None:
        con = _conn()
        try:
            con.execute(
                "UPDATE reply SET status=?,reactions=?,strategy=?,stance=?,"
                "target=?,contribution=?,why=? WHERE id=?",
                (status, reactions,
                 (v or {}).get("strategy"), (v or {}).get("stance"),
                 (v or {}).get("target"), (v or {}).get("contribution"),
                 (v or {}).get("why"), rid),
            )
            con.commit()
        finally:
            con.close()

    async def _settle(self) -> None:
        rows = self._due_rows()
        if not rows:
            return
        for rid, gid, ts, due, text, addressed in rows:
            after = self._after(gid, ts, due)
            if not after:
                # 没人说话 = 没人理。这个结论不用花模型调用。
                self._save(rid, "done", 0, {
                    "strategy": "other", "stance": "ignored",
                    "target": "none", "contribution": "none",
                    "why": "窗口内没有人发言",
                })
                _stat["settled"] += 1
                _stat["ignored_free"] += 1
                logger.info("[effect] #%d 没人理（省一次调用）：%s", rid, text[:24])
                continue
            v = await self._ask(text, after)
            if v is None:
                self._save(rid, "failed", len(after), None)
                _stat["fail"] += 1
                continue
            self._save(rid, "done", len(after), v)
            _stat["settled"] += 1
            logger.info(
                "[effect] #%d %s→%s（%s，%s）｜%s",
                rid, _ZH.get(v["strategy"], v["strategy"]),
                _ZH.get(v["stance"], v["stance"]),
                _ZH.get(v["target"], v["target"]),
                _ZH.get(v["contribution"], v["contribution"]),
                v["why"][:30],
            )

    async def _ask(self, reply: str, after: list[tuple[str, str]]) -> dict | None:
        pid = PROVIDER
        if not pid:
            try:
                # get_current_chat_provider_id 是**协程**，必须 await
                # （dsh-welcome / dsh-memory / dsh-decide 都在这栽过）。
                # 这里没有 umo（结算是后台任务，脱离了具体事件），
                # 所以传 None 让框架给默认渠道。
                pid = await self.context.get_current_chat_provider_id(None)
            except BaseException as exc:
                logger.debug("[effect] 取 provider 失败: %r", exc)
                return None
        if not pid:
            return None
        lines = "\n".join("%s：%s" % (n, t[:80]) for n, t in after)
        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=pid,
                    prompt=PROMPT.format(reply=reply[:300], after=lines),
                    system_prompt=SYS,
                    temperature=0,
                ),
                timeout=TIMEOUT,
            )
        except BaseException as exc:
            logger.warning("[effect] 问模型失败: %r", exc)
            return None
        _stat["asked"] += 1
        raw = (getattr(resp, "completion_text", "") or "").strip()
        v = parse_verdict(raw)
        if v is None:
            logger.warning("[effect] 解析不出结论，原文：%s", raw[:120])
            return None
        if not raw.lstrip().startswith("{"):
            _stat["parse_fallback"] += 1
        return v

    # ------------------------------------------------------ 看结果
    @filter.command("回复效果")
    async def cmd_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        try:
            con = _conn()
            try:
                total, done, pending, failed = con.execute(
                    "SELECT COUNT(*),"
                    "SUM(status='done'),SUM(status='pending'),SUM(status='failed')"
                    " FROM reply").fetchone()
                stance = con.execute(
                    "SELECT stance,COUNT(*) FROM reply WHERE status='done' "
                    "GROUP BY stance ORDER BY 2 DESC").fetchall()
                strat = con.execute(
                    "SELECT strategy,COUNT(*) FROM reply WHERE status='done' "
                    "GROUP BY strategy ORDER BY 2 DESC").fetchall()
                # 哪种策略最容易被接住：认可+跟着玩 算落地
                land = con.execute(
                    "SELECT strategy,"
                    "SUM(stance IN ('appreciation','playful')),COUNT(*) "
                    "FROM reply WHERE status='done' GROUP BY strategy "
                    "HAVING COUNT(*)>=3 ORDER BY 2.0/COUNT(*) DESC").fetchall()
            finally:
                con.close()
        except BaseException as exc:
            yield event.plain_result("回复效果：读库失败 %r" % exc)
            return
        lines = [
            "回复效果观察：%s｜群：%s｜窗口 %.0fs（只测量，不改行为）"
            % ("开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无", WINDOW),
            "累计记录 %d 条：已评 %d｜等窗口 %d｜失败 %d"
            % (total or 0, done or 0, pending or 0, failed or 0),
        ]
        if stance:
            lines.append("群里的反应：" + "｜".join(
                "%s %d" % (_ZH.get(s, s), n) for s, n in stance))
        if strat:
            lines.append("它用的策略：" + "｜".join(
                "%s %d" % (_ZH.get(s, s), n) for s, n in strat))
        if land:
            lines.append("哪种策略容易被接住（样本≥3）：" + "｜".join(
                "%s %.0f%%" % (_ZH.get(s, s), (g or 0) / c * 100)
                for s, g, c in land))
        lines.append("本次启动后：记录 %d｜结算 %d（其中没人理省调用 %d）｜问模型 %d｜失败 %d"
                     % (_stat["recorded"], _stat["settled"],
                        _stat["ignored_free"], _stat["asked"], _stat["fail"]))
        yield event.plain_result("\n".join(lines))
