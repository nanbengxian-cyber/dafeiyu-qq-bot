# -*- coding: utf-8 -*-
"""dsh-selfguard -- 出口自制力：该闭嘴的时候闭嘴。

---------------------------------------------------------------------------
为什么要这个

`dsh-guard` 管群友爆粗口，**没有任何东西管机器人自己**。这个不对称在真群里
出过事。拿 58 分钟真实对话复盘出来的两段：

  1) 拱火升级
        南星桥畔  看看腿
        大肥鱼    想得美 尾巴都不给你看
        南星桥畔  你妈死了                    ← dsh-guard 禁言 600 秒
        大肥鱼    这就急了？                   ← 火上浇油
        l         你能禁言我吗
        群主      你们再吵，我真把你禁了        ← 群主把机器人算进了「你们」
     后面还有一条 `傻子机器人也比你强点`，直接骂群友。

  2) 自己跟自己重复
        14:54:00  土地公公这波是工伤
        14:54:36  土地公公实锤了               ← 36 秒后同一个梗
        15:17:46  造反前记得先喊我一声
        15:19:11  我也要，记得喊我              ← 同一个句式

两件事都不是提示词能治的。人格里明明写着「傲娇嘴硬心软」而不是「骂人」，
情绪系统全程也是 calm/curious/happy、一次都没进 angry —— 也就是说**这不是
情绪失控，是模型自己的选择**。这个项目反复验证过：人格治不了结构问题
（`dsh-style` 的句长、`dsh-human` 的逗号、`dsh-claimguard` 的认输都是这样）。
所以放一道**出口闸门**，在发出去之前拦。

---------------------------------------------------------------------------
闸门一：自我重复

判据是**两个信号取或**，拿它自己 44 条真话回测定的：

  * 最长公共子串 ≥ 4 字   → 抓「土地公公这波是工伤」→「土地公公实锤了」
  * 共享 bigram ≥ 2 **且** 占较短那条的 ≥ 25%
                          → 抓「造反前记得先喊我一声」→「我也要，记得喊我」
                            （这一对最长公共子串只有 2，光看子串会漏）

`共享 bigram ≥ 2` 这个下限是关键，它把「同一个话题词」和「同一个句式」分开了。
实测放行掉的两条恰好证明这一点：

    白嫖永动机        ⇢ 有码的白嫖党 真行     共享 1 个（白嫖）→ 放行
    夜宵选炒米粉不会错  ⇢ 汤的才叫夜宵          共享 1 个（夜宵）→ 放行

群里当时正在聊白嫖和夜宵，复用话题词是**接梗**，不是重复。

44 条真话里这条规则只拦 2 条（就是上面那两个真案例），一条误伤都没有。

---------------------------------------------------------------------------
闸门二：拱火

**两个条件必须同时成立**才拦 —— 这是这道闸门唯一不粗暴的原因：

  ① 自己这条在「评价对方的情绪」或「比较优劣贬低对方」
  ② 群里最近 3 分钟**确实在冲突**（粗口／诅咒／喊禁言／人身攻击）

同一句话，在正常聊天里是嘴硬，在骂战里是拱火 —— 所以用上下文判，不用词表判。
实测四条真话正好分在两边：

    想得美 尾巴都不给你看        挑衅=否  冲突中=是  → 放行（是嘴硬，人设允许）
    这就急了？                 挑衅=是  冲突中=是  → **拦**
    傻子机器人也比你强点         挑衅=是  冲突中=是  → **拦**
    想得美 我是你失散多年的姐妹才对  挑衅=否  冲突中=是  → 放行

两个条件各自都很窄：条件①在 1988 条群友真话里只命中 2 条（0.1%），
条件②只命中 30 条（1.5%）。两个同时成立更罕见，所以这不是一道会常关的门。

刻意**没收**进条件①的东西：`想得美`（是拒绝不是挑衅）、`绷不住`
（群里当褒义用，「笑到绷不住」）、`骂我干嘛 我又没惹你`（是自辩不是挑衅）。

---------------------------------------------------------------------------
拦下来之后做什么：什么都不发

不改写、不软化、不重新生成。改写自己的话比闭嘴危险得多，而这两种情况下
**沉默一定不比说出去差**。走 `clear_result()` + `stop_event()`，
形状照 `dsh-decide` 的 `mute_out`（那条路已经在静音群里跑了很久）。

---------------------------------------------------------------------------
两条已知边界

  * 挂在 `on_decorating_result`，和 `dsh-human`（拆句）`dsh-typo`（错别字）
    同一个钩子。按目录顺序 human → selfguard → typo，所以看到的是拆过句、
    还没打错字的文本。错别字只改一个字，最多让某个 bigram 对不上、
    少抓一次重复，不会造成误伤。
  * 管不到不产生 result、直接 `event.send()` 的插件（`dsh-poke` /
    `dsh-welcome` / `dsh-guard`）。那几个都不参与斗嘴，先不管。
"""

import os
import re
import sqlite3
import time
from collections import deque

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_SELFGUARD")
GROUPS = _set("DSH_SELFGUARD_GROUPS", "100000001")
# 影子模式：只判不拦，先看日志准不准
SHADOW = _flag("DSH_SELFGUARD_SHADOW", "0")
REPEAT_ON = _flag("DSH_SELFGUARD_REPEAT")
TAUNT_ON = _flag("DSH_SELFGUARD_TAUNT")
# 跟自己最近几条比
KEEP = max(1, int(os.environ.get("DSH_SELFGUARD_KEEP", "3")))
# 最长公共子串阈值
LCS_MIN = max(2, int(os.environ.get("DSH_SELFGUARD_LCS", "4")))
# 共享 bigram 的个数下限与占比下限
BG_MIN = max(1, int(os.environ.get("DSH_SELFGUARD_BIGRAM", "2")))
BG_RATIO = min(1.0, max(0.05, float(os.environ.get("DSH_SELFGUARD_RATIO", "0.25"))))
# 「群里在冲突」回看多少秒
CONFLICT_WINDOW = max(30.0, float(os.environ.get("DSH_SELFGUARD_WINDOW", "180")))
# 太短的话没有「重复」可言（「6」「草」真人也会连发）
MIN_LEN = max(2, int(os.environ.get("DSH_SELFGUARD_MIN_LEN", "5")))
MEM_DB = os.environ.get("DSH_MEM_DB", "/AstrBot/data/dsh_memory.db")
OWNERS = _set("DSH_SELFGUARD_OWNER", "2774000001")


# ---------------------------------------------------------------- 纯函数（可离线回测）
_PUNCT = re.compile(r"[\s，。！？、,.!?~…—\-：:；;\"'“”‘’（）()\[\]【】*]")


def core(s: str) -> str:
    """比对前先剥掉标点空白：换行和逗号不该影响「这两句是不是一个意思」。"""
    return _PUNCT.sub("", s or "")


def lcs_len(a: str, b: str) -> int:
    """最长公共子串长度。群聊句子都很短，O(n*m) 完全够。"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def repeat_of(text: str, recent: list[str]) -> tuple[str, str] | None:
    """跟最近说过的话重了吗。返回 (撞上的那条, 依据) 或 None。"""
    cur = core(text)
    if len(cur) < MIN_LEN:
        return None
    ca = bigrams(cur)
    for old in recent:
        prev = core(old)
        if len(prev) < MIN_LEN:
            continue
        n = lcs_len(cur, prev)
        if n >= LCS_MIN:
            return old, "公共子串%d字" % n
        cb = bigrams(prev)
        shared = ca & cb
        if len(shared) >= BG_MIN:
            ratio = len(shared) / max(1, min(len(ca), len(cb)))
            if ratio >= BG_RATIO:
                return old, "共享%d组/%.0f%%（%s）" % (
                    len(shared), ratio * 100, "、".join(sorted(shared)[:3]))
    return None


# 条件①：评价对方情绪，或比较优劣贬低对方。刻意不收「想得美」「绷不住」。
_TAUNT_RE = re.compile(
    r"这就(急|恼|破防|绷不住)"
    r"|(急|恼|破防)(了|什么|啥)[?？]"
    r"|气(抖冷|急)"
    r"|比你(强|好|行|厉害)|不如你|你也就(这样|这点)|就这(水平|点)|轮到你|你算(什么|啥)"
    r"|(你|他)(才|就)是?(傻|蠢|废物|垃圾|智障|沙雕)"
    r"|(傻|蠢|废物|垃圾|智障)\S{0,4}(也)?比你"
)
# 条件②：群里正在冲突
#
# 这一条校准过一轮，因为第一版栽在中文的「X死了」上：`死了` 会命中
# 笑死了／累死了／饿死了／热死了／可爱死了 —— 全是语气词，不是冲突。
# 真语料里它还命中了「2岁？电死了」「刚出了个大红就被打死了」。
# 而真正要抓的攻击（你妈死了／你马死了）本来就被 `你妈|你马` 抓住了，
# 所以 `死了` 整条删掉，换成明确冲人的 `你死|去死|该死`。
# 同理 `滚` 会命中滚烫、滚动条 —— 改成只认命令式或整条就是一个「滚」；
# `封了` 命中「我哔哩哔哩被封了」，纯属无关，删掉。
#
# 留着没动的两个：`傻逼` 会命中「傻逼任务磨了我一两个小时」、`垃圾` 会命中
# 「这游戏真垃圾」。这两种确实不是骂人，但都表示有人正在上火，
# 把它算进「气氛不好」不算错；而且条件②单独成立什么也不会发生 ——
# 必须自己同时在挑衅才拦。
_CONFLICT_RE = re.compile(
    r"你妈|你马|尼玛|草你|艹你|你死|去死|该死|鞭尸"
    r"|傻[逼比批]|贱人|好贱|贱货|狗东西|智障|垃圾|废物"
    r"|滚(蛋|开|远|出去)|给我滚|快滚|^\s*滚\s*[!！。.]*$"
    r"|禁言我|举报|骂(你|我|他|她)|吵(什么|啥|架)|别活了"
)


def is_taunt(text: str) -> bool:
    return bool(_TAUNT_RE.search(text or ""))


def looks_conflict(lines: list[str]) -> str | None:
    for t in lines:
        if _CONFLICT_RE.search(t or ""):
            return t
    return None


_stat = {"seen": 0, "repeat": 0, "taunt": 0, "shadow_repeat": 0, "shadow_taunt": 0,
         "skip_group": 0, "not_model": 0, "db_fail": 0}
_last: list[str] = []


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._recent: dict[str, deque] = {}
        logger.info(
            "[selfguard] 已加载：%s%s 群=%s｜自我重复=%s(公共子串≥%d 或 共享≥%d组且≥%.0f%%，"
            "比最近%d条)｜拱火=%s(挑衅 且 %.0fs内群里在冲突)",
            "开" if ENABLED else "关", "（影子模式，只判不拦）" if SHADOW else "",
            "、".join(sorted(GROUPS)) or "无",
            "开" if REPEAT_ON else "关", LCS_MIN, BG_MIN, BG_RATIO * 100, KEEP,
            "开" if TAUNT_ON else "关", CONFLICT_WINDOW,
        )

    # ------------------------------------------------------ 记下自己说过什么
    @filter.after_message_sent()
    async def note(self, event: AstrMessageEvent) -> None:
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
            if text:
                self._recent.setdefault(gid, deque(maxlen=KEEP)).append(text)
        except BaseException as exc:
            logger.debug("[selfguard] 记自己的话失败: %r", exc)

    # ------------------------------------------------------ 群里最近在吵吗
    def _recent_human(self, gid: str) -> list[str]:
        """只读 dsh-memory 的库。**必须读它而不是自己攒** —— 冲突判断要看的是
        群里所有人说了什么，包括那些没触发回复的消息，而只有 dsh-memory 全收。
        """
        try:
            con = sqlite3.connect("file:%s?mode=ro" % MEM_DB, uri=True, timeout=2.0)
        except BaseException:
            _stat["db_fail"] += 1
            return []
        out: list[str] = []
        try:
            since = time.time() - CONFLICT_WINDOW
            for table in ("buffer", "archive"):
                try:
                    for (t,) in con.execute(
                        "SELECT text FROM %s WHERE group_id=? AND ts>? "
                        "ORDER BY ts DESC LIMIT 30" % table, (gid, since)):
                        if t and t.strip():
                            out.append(t.strip())
                except sqlite3.Error:
                    continue
        finally:
            try:
                con.close()
            except BaseException:
                pass
        return out

    # ------------------------------------------------------ 出口闸门
    @filter.on_decorating_result()
    async def gate(self, event: AstrMessageEvent) -> None:
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
            # 只管模型生成的回复。指令回显本来就该原样发出去。
            try:
                if not result.is_model_result():
                    _stat["not_model"] += 1
                    return
            except BaseException:
                pass
            text = (result.get_plain_text() or "").strip()
            if not text:
                return
            _stat["seen"] += 1

            hit = why = None
            if REPEAT_ON:
                got = repeat_of(text, list(self._recent.get(gid, ())))
                if got:
                    hit = "自我重复"
                    why = "跟「%s」重了（%s）" % (got[0][:24], got[1])

            if hit is None and TAUNT_ON and is_taunt(text):
                # 挑衅只在群里确实在冲突时才拦 —— 平时那是嘴硬，人设允许
                spark = looks_conflict(self._recent_human(gid))
                if spark:
                    hit = "拱火"
                    why = "自己在挑衅，而群里正在吵（%s）" % spark[:24]
                else:
                    logger.info("[selfguard] 这句像挑衅但群里没在吵，放行：%s", text[:36])

            if hit is None:
                return

            key = "shadow_repeat" if (SHADOW and hit == "自我重复") else (
                "shadow_taunt" if SHADOW else ("repeat" if hit == "自我重复" else "taunt"))
            _stat[key] = _stat.get(key, 0) + 1
            brief = "%s｜%s｜原话：%s" % (hit, why, text[:40])
            _last.append(time.strftime("%H:%M:%S ") + brief)
            del _last[:-8]
            if SHADOW:
                logger.info("[selfguard] 影子模式：本来要拦（%s）", brief)
                return
            event.clear_result()
            logger.info("[selfguard] 出口拦下一条（%s）", brief)
            event.stop_event()
        except BaseException as exc:
            # 自己出问题就当没这道门，绝不连累正常回复
            logger.warning("[selfguard] 闸门异常，放行: %r", exc)

    @filter.command("自制力状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        s = _stat
        stopped = s["repeat"] + s["taunt"]
        yield event.plain_result(
            "出口自制力：%s%s｜群：%s\n"
            "看过 %d 条自己的回复，拦下 %d 条（自我重复 %d｜拱火 %d）\n"
            "影子模式下本来会拦：重复 %d｜拱火 %d\n"
            "判据：公共子串≥%d字 或 共享≥%d组bigram且占≥%.0f%%（比最近%d条）；"
            "拱火需「自己挑衅」+「%.0fs内群里在吵」同时成立\n"
            "最近：%s"
            % ("开" if ENABLED else "关", "（影子模式）" if SHADOW else "",
               "、".join(sorted(GROUPS)) or "无",
               s["seen"], stopped, s["repeat"], s["taunt"],
               s["shadow_repeat"], s["shadow_taunt"],
               LCS_MIN, BG_MIN, BG_RATIO * 100, KEEP, CONFLICT_WINDOW,
               "｜".join(_last[-3:]) or "还没有")
        )
