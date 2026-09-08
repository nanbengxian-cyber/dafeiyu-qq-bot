# -*- coding: utf-8 -*-
"""dsh-leakguard -- 拦住机器人把人格系统提示词直接抄进回复发出去。

---------------------------------------------------------------------------
为什么

群里 2026-09-07 抓到一条实证：机器人在回怼的末尾把人格里的**系统指令标题**
原样抄出来发了出去：

    [11:45] 群友C: @大肥鱼 好的，你哭丧
    [11:45] 大肥鱼 → 群友C: 哭你个头，我打错字你当真了是吧
            **同轮只办一件事，轮到了这条先办这条。** 我收回那句，没别的意思

模型没把「同轮只办一件事」当作要执行的规则，反而当成了自己想说的一句话
写进回复。这种泄露在真人眼里就是「机器人露馅了」——它把**内部指令原文**
摊给群友看。本插件在回复发出去之前（on_decorating_result）检查正文里有没有
嵌入人格 system_prompt 里的专属指令标题/句子，命中就按模式处理。

---------------------------------------------------------------------------
怎么判，避免误伤

泄露最可靠的指纹是【…】标题（「同轮只办一件事」「最高原则」「群聊铁律」
这类浓缩规则标签），正常人不会在 QQ 群里说出这几个词。但标题里也有跟日常
口语撞车、绝不该拦的：

- 「你在哪」「语言」「说话方式」—— 群友天天问/说这几个字。
- 「不认账」「时政不聊」「工具规则」等 —— 也可能被群友顺口说出来。

所以标题分三档，全部可配：
  1. strong（强信号，按模式处理）：“系统味”最足、群里绝不会自然出现的标题。
     内置一套，可用 env DSH_LEAKGUARD_STRONG 追加。
  2. common（直接忽略）：跟日常口语撞车的标题，绝不当泄露。
     内置一套，可用 env DSH_LEAKGUARD_COMMON 追加。
  3. weak（影子，只记日志不拦）：其余标题。防误伤的保险档 —— 今后改人格
     新增的标题先走 weak 记日志观察，别一上来就拦。

命中判断用“归一化子串”：去掉所有空白后再比对，所以「同轮只办一件事」和
「同轮只办 一件事」都能命中。strong 档会给带括号/冒号后缀的标题（如
【AI 味黑名单（出现即失败）】）提取主词「AI味黑名单」做锚点，因为泄露时
往往只带主词不带括号后缀。

另有一个永远只记日志的**指令句影子层**：人格里每条 `- 开头` 的指令子弹
是否被逐字抄进正文。它容易误伤（用户也可能引用），所以只记日志不拦。

---------------------------------------------------------------------------
三种模式（env DSH_LEAKGUARD_MODE，默认 strip）

  - strip：只删掉正文里命中的标题片段，其余照发。混合链（夹图片/@ 等）
           逐段剥 Plain，剥到整条正文空了才整条拦。
  - block：命中 strong 标题就整条 clear_result，一句话都不发。
  - shadow：只打日志，什么都不改（用于上线前观察）。

命令（仅群主 DSH_LEAKGUARD_OWNER）：
  /泄露guard状态    配置、命中统计、最近几次处理
  /泄露guard刷新    重新取一次人格 system_prompt，更新标题表
  /泄露guard模式 <strip|block|shadow>   运行时切模式

---------------------------------------------------------------------------
读取人格的 recipe（与 dsh-welcome 一致）：

    target = self.context.get_config()["provider_settings"].get("default_personality")
    for p in await self.context.get_db().get_personas() or []:
        if getattr(p, "persona_id", None) == target:
            system_prompt = getattr(p, "system_prompt", None)

取不到 system_prompt 时整个插件降级为空转（照样放行），绝不因自己读配置
失败而挡掉正常回复。标题表带缓存（DSH_LEAKGUARD_TTL 秒），命令可手动刷新。
"""

import os
import re
import time

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


ENABLED = _flag("DSH_LEAKGUARD", "1")
GROUPS = _set("DSH_LEAKGUARD_GROUPS", "100000001")
OWNERS = _set("DSH_LEAKGUARD_OWNER", "2774000001")
MODE = os.environ.get("DSH_LEAKGUARD_MODE", "strip").strip().lower()
RULES_SHADOW = _flag("DSH_LEAKGUARD_RULE_SHADOW", "1")
TTL = _int("DSH_LEAKGUARD_TTL", 600)
MIN_TITLE = max(3, _int("DSH_LEAKGUARD_MIN_TITLE", 4))
MIN_RULE = max(8, _int("DSH_LEAKGUARD_MIN_RULE", 12))

# 强信号标题：系统味最足、群里绝不会被真人自然说出来。
_STRONG_DEFAULT = {
    "同轮只办一件事", "最高原则", "群聊铁律", "别跟着嘲讽群主",
    "别自己写标记", "你在这个群里是什么身份", "你还有这些",
}
# 撞日常口语、绝不当泄露的标题：真人群里很可能自然出现。
_COMMON_DEFAULT = {
    "你在哪", "语言", "说话方式", "不认账", "时政不聊", "安全底线",
    "工具规则", "人格与梗", "表情包贴纸", "AI味黑名单", "AI 味黑名单",
}


def _norm(s: str) -> str:
    """归一化：去掉所有空白，用于标题比对。"""
    return re.sub(r"\s+", "", s or "")


def _remove_norm(orig: str, needle: str) -> tuple[str, int]:
    """在 orig 里删掉一个 needle（归一化匹配：允许中间有空白）。返回 (新文本, 删了几次)。"""
    if not needle or not orig:
        return orig, 0
    n_orig = _norm(orig)
    n_need = _norm(needle)
    idx = n_orig.find(n_need)
    if idx < 0:
        return orig, 0
    nsl = [i for i, ch in enumerate(orig) if not ch.isspace()]
    if idx + len(n_need) > len(nsl):
        return orig, 0
    start_o = nsl[idx]
    end_o = nsl[idx + len(n_need) - 1] + 1          # 原文里最后那个非空白的下一位
    return orig[:start_o] + orig[end_o:], 1


def build_markers(system_prompt: str) -> dict:
    """从人格 system_prompt 抽出标题，按 strong / common / weak 分档。

    返回结构：
        {"strong": [(标题, (锚点1, 锚点2...)), ...],   # 命中即按当前模式处理
         "weak":   [标题, ...],                        # 命中只记日志
         "rules":  [指令子弹句, ...],                  # 永远影子层
         "strong_titles": [...], "weak_titles": [...], "system_prompt": 原文}
    """
    strong_set = (set(_STRONG_DEFAULT)
                  | {t.strip() for t in _set("DSH_LEAKGUARD_STRONG") if t.strip()})
    common_set = (set(_COMMON_DEFAULT)
                  | {t.strip() for t in _set("DSH_LEAKGUARD_COMMON") if t.strip()})
    titles = re.findall(r"【([^】]+)】", system_prompt or "")

    strong, weak, strong_t, weak_t = [], [], [], []
    seen = set()
    for raw in titles:
        title = raw.strip()
        if len(title) < MIN_TITLE or title in seen:
            continue
        seen.add(title)
        key = _norm(title)
        # 带冒号/括号后缀的标题，取“主词”来做分档与匹配（泄露常只带主词）
        base = re.split(r"[(:：（）(),，,]", title, 1)[0].strip()
        base_key = _norm(base)
        if (title in common_set or key in common_set
                or base in common_set or base_key in common_set):
            continue
        if (title in strong_set or key in strong_set
                or base in strong_set or base_key in strong_set):
            strong_t.append(title)
            anchors = {title}
            if base and len(base) >= MIN_TITLE and base != title:
                anchors.add(base)
            strong.append((title, sorted(anchors, key=len, reverse=True)))
        else:
            weak_t.append(title)
            weak.append(title)

    # 指令句影子层：`- 开头` 的子弹，逐字抄进正文就记日志
    rules = [x.strip() for x in re.findall(r"^-\s+(.+)$", system_prompt or "", re.M)]
    rules = [x for x in rules if len(_norm(x)) >= MIN_RULE]

    return {
        "strong": strong, "weak": weak, "rules": rules,
        "strong_titles": strong_t, "weak_titles": weak_t,
        "system_prompt": system_prompt or "",
    }


_stat = {"seen": 0, "strip": 0, "block": 0, "shadow_strong": 0, "shadow_rule": 0,
         "fail_fetch": 0}
_last: list[str] = []
_state = {"markers": None, "fetched_at": 0.0, "prompt_len": 0}


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._mode = MODE if MODE in ("strip", "block", "shadow") else "strip"
        logger.info(
            "[leakguard] 已加载：%s 群=%s｜模式=%s｜标题 strong=%d common=%d｜指令句影子=%s",
            "开" if ENABLED else "关",
            "、".join(sorted(GROUPS)) or "无",
            self._mode, len(_STRONG_DEFAULT), len(_COMMON_DEFAULT),
            "开" if RULES_SHADOW else "关",
        )

    # ------------------------------------------------------ 拉人格 system_prompt
    async def _fetch_prompt(self) -> str | None:
        try:
            target = self.context.get_config()["provider_settings"].get("default_personality")
            if not target:
                return None
            for p in await self.context.get_db().get_personas() or []:
                if getattr(p, "persona_id", None) == target:
                    return getattr(p, "system_prompt", None)
            return None
        except BaseException as exc:
            _stat["fail_fetch"] += 1
            logger.debug("[leakguard] 拉人格失败: %r", exc)
            return None

    async def _ensure_markers(self) -> bool:
        """确保标题表新鲜。成功返回 True；拉不到且无旧表时返回 False。"""
        if _state["markers"] is not None and time.time() - _state["fetched_at"] < TTL:
            return True
        prompt = await self._fetch_prompt()
        if not prompt:
            _stat["fail_fetch"] += 1
            return _state["markers"] is not None
        _state["markers"] = build_markers(prompt)
        _state["fetched_at"] = time.time()
        _state["prompt_len"] = len(prompt)
        logger.info(
            "[leakguard] 装载标题表：strong %d / weak %d / 指令句 %d，system_prompt %d 字",
            len(_state["markers"]["strong_titles"]),
            len(_state["markers"]["weak_titles"]),
            len(_state["markers"]["rules"]),
            len(prompt),
        )
        return True

    # ------------------------------------------------------ 出口闸门
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
            if not (result.get_plain_text() or "").strip():
                return
            if not await self._ensure_markers():
                return
            text = result.get_plain_text() or ""
            markers = _state["markers"]
            _stat["seen"] += 1

            # ---- strong 档：命中就按模式处理
            hit: list[str] = []
            for title, anchors in markers["strong"]:
                for a in anchors:
                    if _norm(text).find(_norm(a)) >= 0:
                        hit.append(title)
                        break
            if hit:
                self._handle_strong(event, text, hit)
                return

            # ---- weak 档：只记日志
            for wt in markers["weak"]:
                if _norm(text).find(_norm(wt)) >= 0:
                    _stat["shadow_strong"] += 1
                    self._note("weak 标题「%s」｜原话头：%s" % (wt, text[:40]))
                    logger.info("[leakguard] 影子：正文出现 weak 标题「%s」（不拦）：%s",
                                wt, text[:36])
                    break

            # ---- 指令句影子层（永远只记日志）
            if RULES_SHADOW:
                for rule in markers["rules"]:
                    if _norm(text).find(_norm(rule)) >= 0:
                        _stat["shadow_rule"] += 1
                        self._note("规则句「%s…」｜原话头：%s" % (rule[:18], text[:40]))
                        logger.info("[leakguard] 影子：正文逐字抄了指令句「%s…」：%s",
                                    rule[:18], text[:36])
                        break
        except BaseException as exc:
            logger.warning("[leakguard] 闸门异常，放行: %r", exc)

    # ------------------------------------------------------ strong 命中处理
    def _handle_strong(self, event, text: str, hit: list[str]) -> None:
        mode = self._mode
        strips: list[str] = []
        for title in hit:
            for (t2, anchors) in _state["markers"]["strong"]:
                if t2 == title:
                    strips.extend(anchors)
        strips = sorted(set(strips), key=len, reverse=True)
        joined = "、".join(hit)

        if mode == "shadow":
            _stat["shadow_strong"] += 1
            self._note("strong 标题「%s」｜原话头：%s" % (joined, text[:40]))
            logger.info("[leakguard] 影子：本要拦 strong 标题「%s」：%s", joined, text[:40])
            return

        if mode == "block":
            event.clear_result()
            _stat["block"] += 1
            self._note("整条拦｜strong「%s」｜%s" % (joined, text[:40]))
            logger.info("[leakguard] 整条拦下（strong 标题「%s」）", joined)
            event.stop_event()
            return

        # strip：逐段剥 Plain，剥空了才整条拦
        chain = event.get_result().chain
        removed_any = False
        for comp in chain:
            if not isinstance(comp, Plain):
                continue
            orig = comp.text or ""
            cur = orig
            for s in strips:
                cur, n = _remove_norm(cur, s)
                removed_any = removed_any or n > 0
            if cur != orig:
                comp.text = cur
        total = "".join((c.text or "") for c in chain if isinstance(c, Plain))
        if not total.strip():
            event.clear_result()
            _stat["block"] += 1
            self._note("剥空整条拦｜strong「%s」｜%s" % (joined, text[:40]))
            logger.info("[leakguard] 剥到正文为空，整条拦下（strong「%s」）", joined)
            event.stop_event()
            return
        if removed_any:
            _stat["strip"] += 1
            self._note("剥标题「%s」：%s → %s" % (joined, text[:26], total[:26]))
            logger.info("[leakguard] 剥掉 strong 标题「%s」：%s", joined, total[:36])

    def _note(self, brief: str) -> None:
        _last.append(time.strftime("%H:%M:%S ") + brief)
        del _last[:-8]

    # ------------------------------------------------------ 命令
    @filter.command("泄露guard状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        if not await self._ensure_markers():
            yield event.plain_result("[泄露guard] 人格 system_prompt 读不到，当前标题表为空，未拦截任何内容。")
            return
        m = _state["markers"]
        age = int(time.time() - _state["fetched_at"])
        s = _stat
        yield event.plain_result(
            "[泄露guard] 开=%s 群=%s 模式=%s\n"
            "标题 strong %d / weak %d / common %d（缓存 %d 秒）\n"
            "看过 %d 段｜剥标题 %d｜整条拦 %d｜shadow strong %d 规则句 %d\n"
            "最近：%s"
            % ("开" if ENABLED else "关",
               "、".join(sorted(GROUPS)) or "无", self._mode,
               len(m["strong_titles"]), len(m["weak_titles"]), len(_COMMON_DEFAULT), age,
               s["seen"], s["strip"], s["block"], s["shadow_strong"], s["shadow_rule"],
               "｜".join(_last[-5:]) or "还没有"))

    @filter.command("泄露guard刷新")
    async def cmd_refresh(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        _state["markers"] = None
        _state["fetched_at"] = 0.0
        ok = await self._ensure_markers()
        if not ok:
            yield event.plain_result("[泄露guard] 刷新失败（读不到人格），仍用旧表，详见日志。")
            return
        m = _state["markers"]
        yield event.plain_result(
            "[泄露guard] 已刷新：strong %d / weak %d / 指令句 %d"
            % (len(m["strong_titles"]), len(m["weak_titles"]), len(m["rules"])))

    @filter.command("泄露guard模式")
    async def cmd_mode(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        args = (event.message_str or "").strip().split()
        if len(args) < 2 or args[1] not in ("strip", "block", "shadow"):
            yield event.plain_result("[泄露guard] 用法：/泄露guard模式 strip|block|shadow")
            return
        self._mode = args[1]
        yield event.plain_result("[泄露guard] 已切换模式：" + self._mode)
