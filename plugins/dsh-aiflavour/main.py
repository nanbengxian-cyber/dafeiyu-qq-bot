# -*- coding: utf-8 -*-
"""dsh-aiflavour -- 动态 AI 味拦截（解释/总结高频腔自动学习加强）。

---------------------------------------------------------------------------
为什么（在 dsh-humanizer 之外再加一层）

humanizer 是"固定词表"闸门——只有词表里写死的词会拦，新出现的 AI 味
套话抓不住。实证：机器人回复里"解释一下/我来解释/简单来说"这类说教腔
出现频率很高，但词表里只有"综上所述"几个总结腔，解释类一个都没有。

本插件在回复发出去之前（on_decorating_result）做三层检查：

  1. 静态强信号整词（STRONG）——真人不会在群里说的 AI 典型腔，
     含"解释一下/总结一下/简单来说/换句话说"等高教学腔，命中就剥。
  2. 静态影子层（WEAK）——撞日常口语的软痕迹，只记日志不拦。
  3. 动态学习（核心）——一组"AI 腔词根"（解释/总结/说明/意味着/
     简单来说/综上所述……）。正文命中词根即累计热度，同一词根同群
     ≥ DSH_AIFLAVOUR_LEARN_MIN 次（默认 3）自动升级进剥除名单，
     之后命中直接剥；连续 ≥ DSH_AIFLAVOUR_DEACTIVATE_DAYS 天
     （默认 7）没再命中自动降级回观察，防误伤累积。
  会话刹车：同一群 SHORT_WINDOW（默认 180s）内剥除名单词根再次命中
     → 整条 block（模型在同轮里还在说教，治本）。

配置（env，全部可配）：
  DSH_AIFLAVOUR                开/关（默认 1）
  DSH_AIFLAVOUR_GROUPS         作用群（默认 100000001）
  DSH_AIFLAVOUR_OWNER          命令属主（默认 2774000001）
  DSH_AIFLAVOUR_MODE           strip|block|shadow（默认 strip）
  DSH_AIFLAVOUR_STRONG         静态强词追加（逗号分隔）
  DSH_AIFLAVOUR_WEAK           静态影词追加（逗号分隔）
  DSH_AIFLAVOUR_ROOTS          学习词根追加（逗号分隔）
  DSH_AIFLAVOUR_LEARN_MIN      升级阈值（默认 3）
  DSH_AIFLAVOUR_DEACTIVATE_DAYS 自动降级天数（默认 7）
  DSH_AIFLAVOUR_SHORT_WINDOW   会话刹车窗口秒（默认 180）

命令（仅群主）：
  /AI味状态    配置、统计、当前剥除名单、最近处理
  /AI味模式 strip|block|shadow  运行时切模式
  /AI味名单    当前动态剥除名单明细（词根+次数+最后命中）
  /AI味清名单  清空动态学习状态（防误伤累积后手动复位）

状态持久化：插件目录 data/aiflavour_dyn.json，重启不丢。
任何异常都放行（绝不因自己出错挡掉正常回复）。
"""

import os
import time

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger

from .aiflavour_logic import (
    _flag, _int, _set, _norm,
    STRONG_BASE, WEAK_BASE, ROOT_BASE,
    DynState, Matcher,
)


ENABLED = _flag("DSH_AIFLAVOUR", "1")
GROUPS = _set("DSH_AIFLAVOUR_GROUPS", "100000001")
OWNERS = _set("DSH_AIFLAVOUR_OWNER", "2774000001")
MODE = os.environ.get("DSH_AIFLAVOUR_MODE", "strip").strip().lower()

STRONG = STRONG_BASE | {_norm(x) for x in _set("DSH_AIFLAVOUR_STRONG")}
WEAK = WEAK_BASE | {_norm(x) for x in _set("DSH_AIFLAVOUR_WEAK")}
ROOTS = ROOT_BASE | {_norm(x) for x in _set("DSH_AIFLAVOUR_ROOTS")}
LEARN_MIN = _int("DSH_AIFLAVOUR_LEARN_MIN", 3)
DEACTIVATE_DAYS = _int("DSH_AIFLAVOUR_DEACTIVATE_DAYS", 7)
SHORT_WINDOW = _int("DSH_AIFLAVOUR_SHORT_WINDOW", 180)

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_DYN_PATH = os.path.join(_PLUGIN_DIR, "data", "aiflavour_dyn.json")

_stat = {"seen": 0, "strip": 0, "block": 0, "upgraded": 0, "brake": 0, "shadow": 0}
_last: list[str] = []


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._mode = MODE if MODE in ("strip", "block", "shadow") else "strip"
        self._dyn = DynState(_DYN_PATH)
        self._dyn.load()
        self._m = Matcher(STRONG, WEAK, ROOTS, self._dyn,
                          min_count=LEARN_MIN,
                          deactivate_days=DEACTIVATE_DAYS,
                          short_window=float(SHORT_WINDOW))
        logger.info(
            "[aiflavour] 已加载：%s 群=%s｜模式=%s｜强词%d 影词%d 词根%d 升级阈值%d 降级%d天 刹车%.0fs｜动态名单%d个",
            "开" if ENABLED else "关",
            "、".join(sorted(GROUPS)) or "无",
            self._mode, len(STRONG), len(WEAK), len(ROOTS),
            LEARN_MIN, DEACTIVATE_DAYS, SHORT_WINDOW,
            len(self._dyn.active_roots()),
        )

    # priority=400：统一去AI味管线第一步 —— 先剥 AI 味词 + 会话刹车。
    # 框架 append 按 -priority 排序，越大越先跑。管线顺序：
    # aiflavour(400) → humanizer(300) → typo(200) → noise(100)。
    @filter.on_decorating_result(priority=400)
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
            text = result.get_plain_text() or ""
            if not text.strip():
                return
            _stat["seen"] += 1
            ts = time.time()

            info = self._m.inspect(text, gid, ts)
            # 动态刚升级：记一笔，且本轮就当命中处理
            if info["dyn_upgraded"]:
                _stat["upgraded"] += 1
                self._note("新学到AI腔「%s」→ 进剥除名单" % info["dyn_upgraded"])
                logger.info("[aiflavour] 动态升级「%s」（已累计≥%d次）", info["dyn_upgraded"], LEARN_MIN)

            # 会话刹车：窗口内重复命中升级词 → 整条拦（说教一而再再而三）
            if info["brake"] and (info["dyn_hits"] or info["strong_hits"]):
                _stat["brake"] += 1
                if self._mode == "shadow":
                    self._note("刹车(影子)｜%s" % text[:36])
                    logger.info("[aiflavour] 刹车(影子)：%s", text[:36])
                else:
                    event.clear_result()
                    _stat["block"] += 1
                    self._note("整条拦｜词根重复说教｜%s" % text[:36])
                    logger.info("[aiflavour] 刹车整条拦（%s 反复说教）", "、".join(info["dyn_hits"][:3]))
                    event.stop_event()
                    return

            # 静态强信号 + 剥除名单词根：剥
            stripped, hits = self._m.strip_strong(text)
            if hits:
                self._handle_strong(event, text, stripped, hits)
                return

            # 影子层：只记日志
            if info["shadow_hits"]:
                _stat["shadow"] += 1
                self._note("影子「%s」｜%s" % ("、".join(info["shadow_hits"][:3]), text[:36]))
                logger.info("[aiflavour] 影子（AI痕迹，不拦）：%s｜%s",
                            "、".join(info["shadow_hits"][:3]), text[:36])
        except BaseException as exc:
            logger.warning("[aiflavour] 闸门异常，放行: %r", exc)

    def _handle_strong(self, event, text: str, stripped: str, hits: list[str]) -> None:
        mode = self._mode
        joined = "、".join(hits[:3])
        if mode == "shadow":
            self._note("强(影子)「%s」｜%s" % (joined, text[:36]))
            logger.info("[aiflavour] 影子：本要拦强「%s」：%s", joined, text[:36])
            return
        if mode == "block":
            event.clear_result()
            _stat["block"] += 1
            self._note("整条拦｜强「%s」｜%s" % (joined, text[:36]))
            logger.info("[aiflavour] 整条拦下（强 AI 痕迹「%s」）", joined)
            event.stop_event()
            return
        # strip：逐段剥 Plain，剥空才整条拦
        chain = event.get_result().chain
        removed_any = False
        for comp in chain:
            if not isinstance(comp, Plain):
                continue
            orig = comp.text or ""
            cur, _hh = self._m.strip_strong(orig)
            if _hh:
                removed_any = True
            if cur != orig:
                comp.text = cur
        total = "".join((c.text or "") for c in chain if isinstance(c, Plain))
        if not total.strip():
            event.clear_result()
            _stat["block"] += 1
            self._note("剥空整条拦｜强「%s」｜%s" % (joined, text[:36]))
            logger.info("[aiflavour] 剥到正文为空，整条拦下（强「%s」）", joined)
            event.stop_event()
            return
        if removed_any:
            _stat["strip"] += 1
            self._note("剥「%s」：%s → %s" % (joined, text[:20], total[:20]))
            logger.info("[aiflavour] 剥掉 AI 痕迹「%s」：%s", joined, total[:36])

    def _note(self, brief: str) -> None:
        _last.append(time.strftime("%H:%M:%S ") + brief)
        del _last[:-8]

    def _fmt_dyn(self) -> str:
        rows = []
        for root, e in sorted(self._dyn.data.items()):
            if e.get("active"):
                rows.append("%s×%d(最后%s)" % (
                    root, int(e.get("count", 0)),
                    time.strftime("%m-%d %H:%M", time.localtime(float(e.get("last_ts", 0)))) if e.get("last_ts") else "-",
                ))
        return "；".join(rows) if rows else "（空）"

    @filter.command("AI味状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        s = _stat
        yield event.plain_result(
            "[AI味] 开=%s 群=%s 模式=%s\n"
            "强词%d 影词%d 词根%d｜升级阈值%d 降级%d天 刹车%.0fs\n"
            "看过%d段｜剥%d｜整条拦%d｜新学%d｜刹车%d｜影子%d\n"
            "剥除名单(%d)：%s\n"
            "最近：%s"
            % ("开" if ENABLED else "关",
               "、".join(sorted(GROUPS)) or "无", self._mode,
               len(STRONG), len(WEAK), len(ROOTS),
               LEARN_MIN, DEACTIVATE_DAYS, SHORT_WINDOW,
               s["seen"], s["strip"], s["block"],
               s["upgraded"], s["brake"], s["shadow"],
               len(self._dyn.active_roots()), self._fmt_dyn(),
               "｜".join(_last[-5:]) or "还没有"))

    @filter.command("AI味模式")
    async def cmd_mode(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        args = (event.message_str or "").strip().split()
        if len(args) < 2 or args[1] not in ("strip", "block", "shadow"):
            yield event.plain_result("[AI味] 用法：/AI味模式 strip|block|shadow")
            return
        self._mode = args[1]
        yield event.plain_result("[AI味] 已切换模式：" + self._mode)

    @filter.command("AI味名单")
    async def cmd_list(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        yield event.plain_result("[AI味] 动态剥除名单（%d个）：\n%s" % (
            len(self._dyn.active_roots()), self._fmt_dyn()))

    @filter.command("AI味清名单")
    async def cmd_reset(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        n = len(self._dyn.data)
        self._dyn.data = {}
        self._dyn.save()
        yield event.plain_result("[AI味] 已清空动态学习状态（%d 条记录）" % n)
