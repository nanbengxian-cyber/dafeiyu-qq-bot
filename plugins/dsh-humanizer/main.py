# -*- coding: utf-8 -*-
"""dsh-humanizer -- 去AI味出口闸门：把机器人回复里残留的"AI痕迹"剥掉再发。

---------------------------------------------------------------------------
为什么

人格【AI 味黑名单】已经"教"清楚了哪些话不能说，但它靠模型自觉执行，
抓不住"教不住时"的漏网。本插件在回复发出去之前（on_decorating_result）
用一组具体的 AI 痕迹特征扫正文，命中就按模式处理——给"去AI味"补一道
leakguard 式的硬兜底（leakguard 只兜人格标题，这道兜整句/整段AI味）。

特征表与检测逻辑在 humanizer_logic.py（照抄 blader/humanizer 的 25 条
"AI 痕迹检测"里跟我们 QQ 群语境、跟人格黑名单对齐的部分）。

---------------------------------------------------------------------------
两档防误伤

  STRONG（强信号，按当前模式处理）：真人绝不会在群里自然说出来的
          AI 典型腔。命中就剥（strip）/整条拦（block）/只记日志（shadow）。
  WEAK（影子，只记日志不拦）：跟日常口语撞车、或真人偶尔也会说的软痕迹
          （首先/其次/最后、让我们…、本质上、不是…而是…、专家认为、
          破折号连用、句尾收束词等），只能记日志观察，绝不剥。

配置（env，全部可配）：
  DSH_HUMANIZER               开/关（默认 1）
  DSH_HUMANIZER_GROUPS        作用群（默认 100000001）
  DSH_HUMANIZER_OWNER         命令属主（默认 2774000001）
  DSH_HUMANIZER_MODE          strip|block|shadow（默认 strip）
  DSH_HUMANIZER_STRONG        追加强信号短语（逗号分隔）
  DSH_HUMANIZER_WEAK          追加影子短语（逗号分隔）

命令（仅群主）：
  /人味状态    配置、命中统计、最近几次处理
  /人味模式 <strip|block|shadow>  运行时切模式

--------------------------------------------------------------------------
任何异常都放行（绝不因自己出错挡掉正常回复）。只处理模型结果的 Plain
正文，混合链逐段剥，剥空才整条拦。
"""

import os
import re
import time

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger

from .humanizer_logic import (load_patterns, strip_text, shadow_hits, _norm)


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_HUMANIZER", "1")
GROUPS = _set("DSH_HUMANIZER_GROUPS", "100000001")
OWNERS = _set("DSH_HUMANIZER_OWNER", "2774000001")
MODE = os.environ.get("DSH_HUMANIZER_MODE", "strip").strip().lower()

_STRIP_PAT, _SHADOW_PAT = load_patterns()

_stat = {"seen": 0, "strip": 0, "block": 0, "shadow_strong": 0, "shadow_weak": 0}
_last: list[str] = []


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._mode = MODE if MODE in ("strip", "block", "shadow") else "strip"
        logger.info(
            "[humanizer] 已加载：%s 群=%s｜模式=%s｜strong %d 条 weak %d 条",
            "开" if ENABLED else "关",
            "、".join(sorted(GROUPS)) or "无",
            self._mode, len(_STRIP_PAT), len(_SHADOW_PAT),
        )

    # priority=300：统一去AI味管线第二步 —— 剥强信号 AI 腔。
    # 固定排在 aiflavour(400) 之后、typo(200) 之前。
    @filter.on_decorating_result(priority=300)
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

            # 强信号：命中就按模式处理
            stripped, hits = strip_text(text, _STRIP_PAT)
            if hits:
                self._handle_strong(event, text, stripped, hits)
                return

            # 影子层：只记日志
            sh = shadow_hits(text, _SHADOW_PAT)
            if sh:
                _stat["shadow_weak"] += 1
                self._note("影子「%s」｜%s" % ("、".join(sh[:3]), text[:40]))
                logger.info("[humanizer] 影子（AI痕迹，不拦）：%s｜%s",
                            "、".join(sh[:3]), text[:36])
        except BaseException as exc:
            logger.warning("[humanizer] 闸门异常，放行: %r", exc)

    def _handle_strong(self, event, text: str, stripped: str, hits: list[str]) -> None:
        mode = self._mode
        joined = "、".join(hits[:3])
        if mode == "shadow":
            _stat["shadow_strong"] += 1
            self._note("强「%s」｜%s" % (joined, text[:40]))
            logger.info("[humanizer] 影子：本要拦强「%s」：%s", joined, text[:36])
            return
        if mode == "block":
            event.clear_result()
            _stat["block"] += 1
            self._note("整条拦｜强「%s」｜%s" % (joined, text[:40]))
            logger.info("[humanizer] 整条拦下（强 AI 痕迹「%s」）", joined)
            event.stop_event()
            return
        # strip：逐段剥 Plain，剥空才整条拦
        chain = event.get_result().chain
        removed_any = False
        for comp in chain:
            if not isinstance(comp, Plain):
                continue
            orig = comp.text or ""
            cur, _hh = strip_text(orig, _STRIP_PAT)
            if _hh:
                removed_any = True
            if cur != orig:
                comp.text = cur
        total = "".join((c.text or "") for c in chain if isinstance(c, Plain))
        if not total.strip():
            event.clear_result()
            _stat["block"] += 1
            self._note("剥空整条拦｜强「%s」｜%s" % (joined, text[:40]))
            logger.info("[humanizer] 剥到正文为空，整条拦下（强「%s」）", joined)
            event.stop_event()
            return
        if removed_any:
            _stat["strip"] += 1
            self._note("剥「%s」：%s → %s" % (joined, text[:24], total[:24]))
            logger.info("[humanizer] 剥掉强 AI 痕迹「%s」：%s", joined, total[:36])

    def _note(self, brief: str) -> None:
        _last.append(time.strftime("%H:%M:%S ") + brief)
        del _last[:-8]

    @filter.command("人味状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        s = _stat
        yield event.plain_result(
            "[人味] 开=%s 群=%s 模式=%s\n"
            "strong %d / weak %d\n"
            "看过 %d 段｜剥 %d｜整条拦 %d｜影子强 %d 软 %d\n"
            "最近：%s"
            % ("开" if ENABLED else "关",
               "、".join(sorted(GROUPS)) or "无", self._mode,
               len(_STRIP_PAT), len(_SHADOW_PAT),
               s["seen"], s["strip"], s["block"],
               s["shadow_strong"], s["shadow_weak"],
               "｜".join(_last[-5:]) or "还没有"))

    @filter.command("人味模式")
    async def cmd_mode(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        args = (event.message_str or "").strip().split()
        if len(args) < 2 or args[1] not in ("strip", "block", "shadow"):
            yield event.plain_result("[人味] 用法：/人味模式 strip|block|shadow")
            return
        self._mode = args[1]
        yield event.plain_result("[人味] 已切换模式：" + self._mode)
