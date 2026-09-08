# -*- coding: utf-8 -*-
"""dsh-aiflavour 纯逻辑：AI 味动态拦截 —— 静态强/弱词表 + 词根热度学习 + 会话刹车。

不 import astrbot，可单测。

三层设计（为什么比固定词表更能拦"解释频率高"这类问题）：

  STRONG(静态，默认剥)：真人绝不会在群里自然说的 AI 典型腔整词，
      包括"解释一下""总结一下"这类高频说教腔。命中就剥/拦。
  WEAK(静态，只记日志)：跟日常口语撞车的软痕迹，绝不剥。
  DYNAMIC(动态学习，核心)：一组"AI 腔词根"（解释/总结/说明/意味着/
      简单来说/综上所述……）。正文命中词根就进热度表，按群累计：
        · 同一词根同群累计 >= LEARN_MIN 次（默认 3）→ 升级进剥除名单，
          之后命中直接剥 —— 机器人在对话里反复"解释、解释"就会自学着被拦；
        · 剥除名单里某词根连续 DEACTIVATE_DAYS（默认 7）天没再命中 →
          自动降级回观察，防误伤累积、防永久拉黑。
  会话刹车：同一群 SHORT_WINDOW（默认 180s）内，剥除名单词根再次命中
      → 整条 block（说明模型在同轮里还在说教，治本）。

数据全部可配（env），状态持久化 JSON（重启不丢）。
"""

import os
import re
import time


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


def _norm(s: str) -> str:
    """归一化：去掉所有空白，用于短语比对。"""
    return re.sub(r"\s+", "", s or "")


# 静态强信号：真人绝不会在群里自然说出来的 AI 典型腔整词（含高频"解释/总结"腔）。
STRONG_BASE: set[str] = {
    # 解释/总结 高频说教腔（用户点名的重灾区）
    "解释一下", "我解释一下", "我来解释", "简单解释", "解释解释",
    "总结一下", "总结来说", "总结起来", "总的来说",
    "简单来说", "简单点说", "说白了", "讲白了", "换句话说", "换而言之",
    "可以这样理解", "可以理解为", "让我解释", "具体来说", "详细说下",
    # 传统 AI 腔
    "综上所述", "总而言之", "作为一个语言模型", "作为AI", "作为 AI",
    "希望这个回答对你有帮助", "有什么可以帮您", "随时找我",
    "这是一个好问题", "这是一个值得思考的问题", "我理解你的感受",
    "值得注意的是", "值得一提", "由此可见", "不难发现", "显而易见",
    "需要指出的是", "需要强调的是", "值得强调的是",
}

# 静态影子：跟日常口语撞车，只记日志、绝不剥（防误伤）。
WEAK_BASE: set[str] = {
    "首先", "其次", "最后", "让我们", "本质上", "说到底",
    "不仅仅是", "不是", "专家认为", "研究表明",
    "前所未有", "前景光明", "标志着", "其实", "就是说", "就是这样",
}

# AI 腔词根：动态学习的提取源。命中的正文片段进热度表。
# 词根要短而专：真人聊天几乎不会连续出现这些词，机器人说教爱用。
ROOT_BASE: set[str] = {
    "解释", "总结", "说明", "意味着", "简单来说", "换句话说", "换而言之",
    "综上所述", "总而言之", "由此可见", "不难发现", "显而易见",
    "值得一提", "值得注意", "需要指出", "需要说明", "强调一下",
    "所以说", "简单说", "就是说", "也就是", "其实", "本质上",
}

# 会话内重复"刹车"的窗口：同一群在窗口内再次命中剥除名单词 → 整条拦。
SHORT_WINDOW = max(30, _int("DSH_AIFLAVOUR_SHORT_WINDOW", 180))
# 动态升级阈值：同一词根同群累计命中次数。
LEARN_MIN = max(2, _int("DSH_AIFLAVOUR_LEARN_MIN", 3))
# 自动降级：连续多少天没再命中就退出剥除名单。
DEACTIVATE_DAYS = max(1, _int("DSH_AIFLAVOUR_DEACTIVATE_DAYS", 7))


class DynState:
    """动态热度表：词根 -> {count, last_ts}。持久化 JSON。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.data: dict[str, dict] = {}

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = __import__("json").load(f)
            if isinstance(raw, dict):
                self.data = raw
        except BaseException:
            self.data = {}

    def save(self) -> None:
        try:
            import json
            import os as _os
            _os.makedirs(_os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
        except BaseException:
            pass  # 写失败不致命，内存态继续工作

    def observe(self, root: str, ts: float, min_count: int,
                deactivate_days: int) -> str:
        """命中词根：累计计数。返回新状态（''=观察中 / 'upgraded'=刚升级 / 'hot'=已升级再命中）。"""
        now = ts
        entry = self.data.get(root)
        if entry is None:
            entry = {"count": 0, "last_ts": 0.0, "active": False}
            self.data[root] = entry
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_ts"] = now
        if entry["active"]:
            # 已在剥除名单：过期降级统一走 deactivate_stale（按 last_ts 判断）
            return "hot"
        if entry["count"] >= min_count:
            entry["active"] = True
            return "upgraded"
        return ""

    def deactivate_stale(self, now: float, deactivate_days: int) -> list[str]:
        """清理超过 N 天没命中的剥除名单词（含计数归零）。返回被降级词根。"""
        stale: list[str] = []
        for root, entry in list(self.data.items()):
            if entry.get("active") and now - float(entry.get("last_ts", 0.0)) > deactivate_days * 86400.0:
                entry["active"] = False
                entry["count"] = 0
                stale.append(root)
        if stale:
            self.save()
        return stale

    def active_roots(self) -> set[str]:
        return {r for r, e in self.data.items() if e.get("active")}


class Matcher:
    """组合静态+动态词表的检测器。"""

    def __init__(self, strong: set[str], weak: set[str], roots: set[str],
                 dyn: DynState, min_count: int = 3, deactivate_days: int = 7,
                 short_window: float = 180.0) -> None:
        self.strong = strong
        self.weak = weak
        self.roots = roots
        self.dyn = dyn
        self.min_count = min_count
        self.deactivate_days = deactivate_days
        self.short_window = short_window
        # 每群最近一次"升级词命中被整条拦"的时间，用于会话刹车
        self._last_block: dict[str, float] = {}
        # 静态词条：按长度降序避免剥"总结一下"时先剥到"总结"
        self._strong_pat = sorted((re.escape(p) for p in strong), key=len, reverse=True)
        self._weak_list = sorted(weak, key=len, reverse=True)

    # ---- 供 main 调用的高层接口 ----

    def inspect(self, text: str, gid: str, ts: float) -> dict:
        """检查一条回复正文。返回命中详情：
        {
          'strong_hits': [...],      # 静态强信号命中的整词
          'shadow_hits': [...],      # 静态影子命中的整词
          'dyn_upgraded': str|None,  # 本轮新升级的词根（触发剥除）
          'dyn_hits': [...],         # 命中剥除名单的词根（含刚升级的）
          'brake': bool,             # 会话刹车：窗口内重复命中升级词 → 建议整条拦
          'active': [...],           # 当前全部剥除名单词根
        }
        """
        res = {
            "strong_hits": [],
            "shadow_hits": [],
            "dyn_upgraded": None,
            "dyn_hits": [],
            "brake": False,
            "active": sorted(self.dyn.active_roots()),
        }
        if not text:
            return res
        norm = _norm(text)
        # 静态强信号
        for pat in self._strong_pat:
            if _norm(pat) in norm:
                res["strong_hits"].append(pat)
        # 静态影子
        for w in self._weak_list:
            if _norm(w) in norm:
                res["shadow_hits"].append(w)
        # 动态词根：命中 → 热度累计 → 升级/降级
        for root in self.roots:
            if _norm(root) in norm:
                st = self.dyn.observe(root, ts, self.min_count, self.deactivate_days)
                if st == "upgraded":
                    res["dyn_upgraded"] = root
                if st in ("upgraded", "hot"):
                    res["dyn_hits"].append(root)
        if res["dyn_upgraded"]:
            self.dyn.save()
        self.dyn.deactivate_stale(ts, self.deactivate_days)
        # 会话刹车：剥除名单词根命中，且同群短窗口内刚拦过 → 整条拦
        if res["dyn_hits"]:
            last = self._last_block.get(gid, 0.0)
            if ts - last <= self.short_window:
                res["brake"] = True
            else:
                self._last_block[gid] = ts
        res["active"] = sorted(self.dyn.active_roots())
        return res

    # ---- 给 main 的剥除工具 ----

    def strip_strong(self, text: str) -> tuple[str, list[str]]:
        """剥掉正文里所有静态强信号整词 + 当前剥除名单词根。返回 (新文本, 命中原词列表)。"""
        hits: list[str] = []
        cur = text or ""
        # 词根按长度降序，先剥长词
        pats = list(self._strong_pat)
        for root in sorted(self.dyn.active_roots(), key=len, reverse=True):
            pats.append(re.escape(root))
        pats = sorted(set(pats), key=len, reverse=True)
        for pat in pats:
            new = re.sub(r"\s*" + pat + r"\s*[,，。；!！?？]?", "", cur)
            if new != cur:
                m = re.search(pat, cur)
                hits.append(m.group(0) if m else pat)
                cur = new
        return cur, hits
