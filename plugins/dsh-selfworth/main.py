# -*- coding: utf-8 -*-
"""dsh-selfworth -- 自身利益：不认账、不自贬、不替外人记账。

===========================================================================
一、要解决什么（群主原话：「AI 他自己没有个性，卖了他他还要替别人数钱」）

拿 astrbot.log 里 3097 条机器人真话回测出来的四类「损己」发言：

  1) 认低身价  16:43:08  群友A：钱咱俩四六分，我六你四
                         鱼：六四？ 我这身价四成都不够          ← 替对方砍自己价
  2) 认下贬低  16:44:18  群友A：其实你是猪
                         鱼：你说啥？ 蓝毛呆鱼，傻得可爱        ← 顺着认了
  3) 认被白嫖  15:28:32  鱼：不花钱还能榨我 这才是真白嫖大佬   ← 被榨还夸对方
                        15:29:43  鱼：榨吧 榨干了我正好歇会儿
  4) 替外人记账 17:22:54 鱼：行吧，这笔账记群主头上           ← 跟外人一起算自己人的账
                17:42:10 鱼：乐啥，明天看我收拾群主

四类里最要命的是 4：它把群主（跟它一边的人）当外人算账。1~3 是同一个毛病的
不同形状 —— **别人给的任何定位它都接受**，包括「你很便宜」「你很傻」。

===========================================================================
二、为什么人格治不了（已经实测过）

人格里早就写着，而且写得很直白：

    「被怼通常还是顶回去；偶尔接不住就发"？"或玩梗带过，别老用"行吧 你赢了"」
    「不要"有求必应"：要推荐/答案/帮忙时可以不接、装傻、已读乱回」
    「别**加入别人嘲讽他的队伍、别帮着补刀**（指群主）」

它照样写出上面四条。这跟 dsh-style 的句长、dsh-claimguard 的认输是同一件事：
**人格给的是倾向，模型在具体一轮里会为了「接得漂亮」把倾向让掉** ——
自贬是最好笑的接法，而它被训练成优先选好笑的。

所以这里补的不是又一条规矩，是**事实 + 一道出口闸门**：
  · 输入侧：对方这轮在占你便宜/贬你（原话摆给它看），注入 <self_interest>。
  · 出口侧：回复真的认了 → 换成一句傲娇的顶回去（默认），或整条拦掉 / 只记日志。

===========================================================================
三、为什么是结构判断，不是词表（拿真语料校准过）

直接按「身价 / 白嫖 / 傻」这种词拦会误伤一大片，因为机器人**嘴硬时也说这些词**：

    再夸也不打折 摸鱼价翻倍          ← 有「身价」的意思，但是**顶回去**，必须放行
    白给的哪有那么香 我这身价不低吧  ← 有「身价」，是嘴硬，放行
    豆包不值得我出手 脏了我得鱼尾    ← 有「不值」，是踩别人，放行
    傻鱼也能吊打你                   ← 有「傻鱼」，是回击，放行
    白嫖党永不为奴                   ← 有「白嫖」，说的是别人，放行

真正的判据是**两件事同时成立**：
  (a) 输入侧：这一轮（或 TTL 内）确实有人在占它便宜/贬它/拉它对付群主；
  (b) 输出侧：它的回复出现了「认账 / 自贬」的形状，而不是「回击」的形状。
只有 (a)+(b) 才动手。单看任何一边都会误伤 —— 这也是本插件不叫「脏话过滤器」
的原因：它管的不是词，是**立场**。

===========================================================================
四、配置（env，全部有默认值，不改也能跑）

  DSH_SELFWORTH             开/关（默认 1）
  DSH_SELFWORTH_GROUPS      作用群（默认 100000001）
  DSH_SELFWORTH_OWNER       命令属主（默认 2774000001）
  DSH_SELFWORTH_MODE        rewrite|block|shadow（默认 rewrite）
  DSH_SELFWORTH_TTL         输入侧命中后多少秒内的回复还算「同一轮」（默认 120）
  DSH_SELFWORTH_LEDGER_MIN  同一个人累计占便宜几次后开始报账（默认 2）
  DSH_SELFWORTH_EXEMPT      豁免的 QQ 号（逗号分隔，默认空）

命令（仅群主）：
  /利益状态            配置、统计、最近处理
  /利益模式 rewrite|block|shadow
  /利益账本            谁占过便宜、几次

持久化：插件目录 data/selfworth.json（模式 + 账本），重启不丢。
任何异常一律放行 —— 自己出问题绝不能挡掉正常回复。
"""

import json
import os
import re
import time
from collections import deque

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger
from astrbot.core.agent.message import TextPart


# ---------------------------------------------------------------- 配置
def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_SELFWORTH")
GROUPS = _set("DSH_SELFWORTH_GROUPS", "100000001")
OWNERS = _set("DSH_SELFWORTH_OWNER", "2774000001")
MODE = os.environ.get("DSH_SELFWORTH_MODE", "rewrite").strip().lower()
if MODE not in ("rewrite", "block", "shadow"):
    MODE = "rewrite"
TTL = max(10.0, float(os.environ.get("DSH_SELFWORTH_TTL", "120")))
LEDGER_MIN = max(1, int(os.environ.get("DSH_SELFWORTH_LEDGER_MIN", "2")))
EXEMPT = _set("DSH_SELFWORTH_EXEMPT")
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_PATH = os.path.join(_PLUGIN_DIR, "data", "selfworth.json")

# 机器人自己的称呼。输入侧要「指向它」才算，否则「白嫖党永不为奴」这种闲聊会命中。
_ME = r"(?:你|您|大肥鱼|肥鱼|小鲸鱼|这鱼|本鱼|鱼哥|蓝毛鱼)"
_AT_RE = re.compile(r"\[At:\d+\]|@\S+")

# ---------------------------------------------------------------- 输入侧：四类「占它便宜」
# DEAL=卖身/分账/压价   FREELOAD=白嫖/榨/白干   INSULT=贬低   SIDING=拉它对付群主
_DEAL_PATS = (
    re.compile(r"(?:卖|卖掉|出手|转让|收了|收购)[了掉]?" + _ME),
    re.compile(r"把" + _ME + r"卖"),
    re.compile(r"(?:分账|分成|四六|三七|五五|对半)"),
    re.compile(r"[我你]\s*[六四三七五二八]\s*[你我]"),
    re.compile(r"(?:身价|价钱|价格).{0,6}(?:不够|不值|打折|便宜|压价|就值)"),
)
_FREELOAD_PATS = (
    re.compile(r"(?:白嫖|白剽|薅|榨|压榨).{0,6}" + _ME),
    re.compile(r"不花钱.{0,6}(?:还能|就|也)?(?:榨|用|使唤|玩|聊)"),
    re.compile(r"(?:免费|白干|白给|义务|打工|工具人|牛马).{0,6}(?:你|帮|给|替)"),
)
_INSULT_WORDS = r"(?:猪|傻|笨|呆|蠢|废物|垃圾|弱智|智障|没用|脑残)"
_INSULT_PATS = (
    re.compile(_ME + r".{0,8}" + _INSULT_WORDS),
    re.compile(r"(?:傻|笨|呆|蠢)(?:鱼|机器人|bot|号)"),
)
_SIDING_PATS = (
    re.compile(r"(?:怪|赖|甩锅|记账|算账|收拾|整|怼|找)[了着]?.{0,3}群主"),
    re.compile(r"群主(?:的错|的锅|头上)"),
)

# ---------------------------------------------------------------- 输出侧：认账 / 自贬的形状
# 顺序有讲究：先查「替外人记账」（最严重、最不可能误伤），再查自贬。
_OUT_SIDING = (
    re.compile(r"(?:账|锅).{0,8}(?:记|甩|算|扣).{0,6}(?:群主|他|他们|别人)"),
    re.compile(r"(?:收拾|怪|怼|算账|找).{0,3}群主"),
)
_OUT_DEAL = (
    re.compile(r"我(?:这|的)?身价.{0,8}(?:不够|不值|太低|便宜|就值)"),
    re.compile(r"我(?:也)?不值(?:钱|这个价)?"),
    re.compile(r"(?:白送|倒贴)(?:给)?你"),
)
_OUT_FREELOAD = (
    re.compile(r"(?:白嫖|榨|白干|免费).{0,8}(?:福报|大佬|真行|爽|永动机|吧|呗)"),
    re.compile(r"(?:榨|白嫖|薅).{0,3}(?:吧|呗|就行)"),
)
_OUT_INSULT = (
    re.compile(r"(?:我|咱|本鱼|这鱼|大肥鱼|蓝毛).{0,8}(?:傻|笨|呆|蠢|是猪)"),
    re.compile(r"(?:傻|笨|呆|蠢)得可爱"),
)
_OUT_ACK = (
    re.compile(r"(?:^|[，,。！!？?\s])(?:行|好吧|成|得)[，,]?\s*我?认了"),
    re.compile(r"我认了|你赢了|算你狠|啊对对对"),
)

# 哪类自贬需要哪类输入侧证据兜底（宁漏不误伤：没证据就不动手）
_CORROBORATE = {
    "SIDING": {"SIDING"},
    "DEAL": {"DEAL", "FREELOAD"},
    "FREELOAD": {"FREELOAD", "DEAL"},
    "INSULT": {"INSULT"},
    "ACK": {"DEAL", "FREELOAD", "INSULT", "SIDING"},
}

# 顶回去的话。短、傲娇、不复述对方原话；同群同类别不连着重复。
_RETORTS = {
    "DEAL": ("卖我？先把账结了", "这价留着买鱼食吧", "身价不还价，谢谢", "想卖我得群主点头"),
    "FREELOAD": ("白嫖没有，先充值", "榨我可以，先报预算", "免费的最贵，你懂的", "想白使唤我？排号去"),
    "INSULT": ("你再说一遍试试", "嘴这么欠，鱼都躲你", "就这嘴还嫌鱼笨", "谁傻谁知道"),
    "SIDING": ("群主的事我不掺和", "想让我记账给群主？没门", "你找别人背锅去", "这锅我不接"),
}

_KIND_CN = {"DEAL": "卖身分账", "FREELOAD": "白嫖榨取", "INSULT": "贬低你", "SIDING": "拉你对付群主",
            "ACK": "认账"}


# ---------------------------------------------------------------- 纯函数（可离线回测）
def _directed(text: str, at_bot: bool) -> bool:
    """这条消息是不是冲着机器人来的。"""
    return bool(at_bot) or bool(re.search(_ME, text or "")) or bool(_AT_RE.search(text or ""))


def exploit_kinds(text: str, at_bot: bool = False) -> list:
    """输入侧：对方这轮在占便宜/贬低/拉站队。返回命中的类别（按严重度排序去重）。"""
    t = (text or "").strip()
    if not t:
        return []
    directed = _directed(t, at_bot)
    hits = []
    if any(p.search(t) for p in _SIDING_PATS):
        hits.append("SIDING")
    # 卖身/分账里「分账」「四六」这类没有主语，必须确认是冲它说的
    if any(p.search(t) for p in _DEAL_PATS[:2]) or \
       (directed and any(p.search(t) for p in _DEAL_PATS[2:])):
        hits.append("DEAL")
    if any(p.search(t) for p in _FREELOAD_PATS):
        hits.append("FREELOAD")
    if any(p.search(t) for p in _INSULT_PATS) or (at_bot and re.search(_INSULT_WORDS, t)):
        hits.append("INSULT")
    return hits


def self_devalue(text: str):
    """输出侧：这句话是「认账/自贬」还是「嘴硬回击」。返回类别，None=放行。"""
    t = (text or "").strip()
    if not t:
        return None
    for kind, pats in (("SIDING", _OUT_SIDING), ("DEAL", _OUT_DEAL),
                       ("FREELOAD", _OUT_FREELOAD), ("INSULT", _OUT_INSULT),
                       ("ACK", _OUT_ACK)):
        if any(p.search(t) for p in pats):
            return kind
    return None


def corroborated(kind: str, recent_kinds) -> bool:
    """自贬类别有没有对应的输入侧证据。"""
    return bool(_CORROBORATE.get(kind, set()) & set(recent_kinds or ()))


def pick_retort(kind: str, last: str = "") -> str:
    """挑一句顶回去，尽量不跟上一次重复。"""
    pool = _RETORTS.get(kind) or _RETORTS["DEAL"]
    for cand in pool:
        if cand != last:
            return cand
    return pool[0]


def render_block(kinds, quote: str, ledger_n: int = 0) -> str:
    """输入侧注入块。给事实 + 立场，不给台词。"""
    cn = "、".join(_KIND_CN.get(k, k) for k in kinds)
    lines = ["<self_interest>",
             "这轮对方在「%s」（原话：「%s」）。你自己人的立场：" % (cn, (quote or "")[:40]),
             "- 群主在你身上烧的是真钱：别人白嫖你、压你价，花的是群主的钱。",
             "- 可以接梗，但不许认账：不认低价、不认「傻/呆/猪」、不说「我认了/行吧你赢了」。",
             "- 不替对方压价，也不替外人把账甩给群主 —— 群主是跟你一边的。"]
    if ledger_n >= LEDGER_MIN:
        lines.append("- 这个人最近已经占你便宜 %d 次了，别每次都配合。" % ledger_n)
    lines.append("顶回去，一两句，傲娇那种，别解释、别自嘲。")
    lines.append("</self_interest>")
    return "\n".join(lines)


# ---------------------------------------------------------------- 状态
class _State:
    """模式 + 账本 + 最近命中，落盘到 data/selfworth.json。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.mode = MODE
        self.ledger = {}          # uid -> {"kinds": {kind: n}, "last": ts, "name": str}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d.get("mode"), str) and d["mode"] in ("rewrite", "block", "shadow"):
                self.mode = d["mode"]
            if isinstance(d.get("ledger"), dict):
                self.ledger = d["ledger"]
        except FileNotFoundError:
            pass
        except BaseException as exc:
            logger.warning("[selfworth] 状态读取失败，用默认值: %r", exc)

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"mode": self.mode, "ledger": self.ledger}, fh,
                          ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException as exc:
            logger.warning("[selfworth] 状态保存失败: %r", exc)

    def note(self, uid: str, name: str, kind: str) -> int:
        """记一笔，返回这个人累计被记的笔数。"""
        if not uid:
            return 0
        rec = self.ledger.setdefault(uid, {"kinds": {}, "last": 0.0, "name": name})
        rec["kinds"][kind] = int(rec["kinds"].get(kind, 0)) + 1
        rec["last"] = time.time()
        rec["name"] = name or rec.get("name") or uid
        return sum(int(v) for v in rec["kinds"].values())


_stat = {"seen": 0, "hit_in": 0, "hit_out": 0, "rewrite": 0, "block": 0, "shadow": 0,
         "skip_group": 0, "err": 0}
_last = []


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self.state = _State(_STATE_PATH)
        self.recent = {}          # gid -> deque[(uid, name, kind, ts, quote)]
        self.last_retort = {}     # (gid, kind) -> 上次用的那句
        logger.info(
            "[selfworth] 已加载：%s 群=%s 模式=%s TTL=%.0fs",
            "开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无",
            self.state.mode, TTL)

    # -------------------------------------------------- 输入侧
    def _push(self, gid: str, uid: str, name: str, kinds, quote: str) -> int:
        q = self.recent.get(gid)
        if q is None:
            q = self.recent[gid] = deque(maxlen=24)
        now = time.time()
        n = 0
        for k in kinds:
            q.append((uid, name, k, now, quote))
            n = self.state.note(uid, name, k)
        return n

    def _recent_kinds(self, gid: str) -> set:
        q = self.recent.get(gid)
        if not q:
            return set()
        now = time.time()
        return {k for _u, _n, k, ts, _q in q if now - ts <= TTL}

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or gid not in GROUPS:
                _stat["skip_group"] += 1
                return
            uid = str(event.get_sender_id() or "")
            if not uid or uid == str(event.get_self_id() or ""):
                return
            _stat["seen"] += 1
            text = event.message_str or ""
            at_bot = bool(getattr(event, "is_at_or_wake_command", False))
            kinds = exploit_kinds(text, at_bot)
            if not kinds:
                return
            name = (event.get_sender_name() if hasattr(event, "get_sender_name") else "") or uid
            n = self._push(gid, uid, name, kinds, text)
            self.state.save()
            block = render_block(kinds, text, n)
            req.extra_user_content_parts.append(TextPart(text=block))
            _stat["hit_in"] += 1
            brief = "%s｜%s｜%s" % ("/".join(kinds), name, text[:32])
            _last.append(time.strftime("%H:%M:%S ") + brief)
            del _last[:-8]
            logger.info("[selfworth] 输入侧命中（注入立场）：%s", brief)
        except BaseException as exc:
            _stat["err"] += 1
            logger.warning("[selfworth] 输入侧异常，跳过: %r", exc)

    # -------------------------------------------------- 出口闸门
    @filter.on_decorating_result()
    async def gate(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or gid not in GROUPS:
                return
            result = event.get_result()
            if result is None or not result.chain:
                return
            try:
                if not result.is_model_result():
                    return
            except BaseException:
                pass
            text = (result.get_plain_text() or "").strip()
            if not text:
                return
            kind = self_devalue(text)
            if not kind:
                return
            if not corroborated(kind, self._recent_kinds(gid)):
                logger.info("[selfworth] 这句像认账但没有输入侧证据，放行：%s", text[:36])
                return
            _stat["hit_out"] += 1
            mode = self.state.mode
            brief = "%s｜原话：%s" % (_KIND_CN.get(kind, kind), text[:40])
            _last.append(time.strftime("%H:%M:%S ") + brief)
            del _last[:-8]
            if mode == "shadow":
                _stat["shadow"] += 1
                logger.info("[selfworth] 影子模式：本来要处理（%s）", brief)
                return
            if mode == "block":
                _stat["block"] += 1
                logger.info("[selfworth] 出口拦下（%s）", brief)
                event.clear_result()
                event.stop_event()
                return
            retort = pick_retort(kind, self.last_retort.get((gid, kind), ""))
            self.last_retort[(gid, kind)] = retort
            result.chain[:] = [Plain(retort)]
            _stat["rewrite"] += 1
            logger.info("[selfworth] 出口改写（%s）→「%s」", brief, retort)
        except BaseException as exc:
            _stat["err"] += 1
            logger.warning("[selfworth] 闸门异常，放行: %r", exc)

    # -------------------------------------------------- 命令
    @filter.command("利益状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        yield event.plain_result(
            "自身利益：%s｜作用群：%s｜模式：%s｜TTL %.0fs｜账本阈值 %d\n"
            "过 %d 轮，输入侧命中 %d，出口命中 %d（改写 %d / 拦 %d / 影子 %d），异常 %d\n"
            "最近：%s"
            % ("开" if ENABLED else "关", "、".join(sorted(GROUPS)) or "无",
               self.state.mode, TTL, LEDGER_MIN,
               _stat["seen"], _stat["hit_in"], _stat["hit_out"], _stat["rewrite"],
               _stat["block"], _stat["shadow"], _stat["err"],
               "｜".join(_last[-5:]) or "还没有"))

    @filter.command("利益模式")
    async def cmd_mode(self, event: AstrMessageEvent, mode: str = ""):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        m = (mode or "").strip().lower()
        if m not in ("rewrite", "block", "shadow"):
            yield event.plain_result("用法：/利益模式 rewrite|block|shadow")
            return
        self.state.mode = m
        self.state.save()
        yield event.plain_result("自身利益模式已切成：%s" % m)

    @filter.command("利益账本")
    async def cmd_ledger(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        rows = sorted(self.state.ledger.items(),
                      key=lambda kv: -sum(int(v) for v in kv[1].get("kinds", {}).values()))
        if not rows:
            yield event.plain_result("账本还是空的。")
            return
        out = []
        for uid, rec in rows[:10]:
            total = sum(int(v) for v in rec.get("kinds", {}).values())
            kinds = "、".join("%s%d" % (_KIND_CN.get(k, k), v)
                              for k, v in rec.get("kinds", {}).items())
            out.append("%s(%s)：%d 次（%s）" % (rec.get("name") or uid, uid, total, kinds))
        yield event.plain_result("占便宜账本：\n" + "\n".join(out))
