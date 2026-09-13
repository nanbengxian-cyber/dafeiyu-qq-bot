# -*- coding: utf-8 -*-
"""dsh-clarify -- 对突然、含糊、无法落地的新话题先问清楚。

模型只做语境分类，行为由代码决定：
- connected / new_clear：正常回复；
- unclear 且被点名：注入一次自然追问意图；
- unclear 且没人点名：停止这次随机插话，不抢着脑补。

同一发送者的同一模糊表达在冷却期内只处理一次。分类失败时，被点名只注入
“不要脑补”的保守提醒；未被点名则不额外拦截，避免接口故障让机器人变哑。
"""

import asyncio
import json
import os
import re
import sqlite3
import time
from collections import deque

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_CLARIFY")
SHADOW = _flag("DSH_CLARIFY_SHADOW", "0")
GROUPS = _set("DSH_CLARIFY_GROUPS", "100000001")
OWNERS = _set("DSH_CLARIFY_OWNER", "2774000001")
DB = os.environ.get("DSH_MEM_DB", "/AstrBot/data/dsh_memory.db")
PROVIDER = os.environ.get("DSH_CLARIFY_PROVIDER", "").strip()
TIMEOUT = max(2.0, float(os.environ.get("DSH_CLARIFY_TIMEOUT", "12")))
LOOKBACK = max(2, int(os.environ.get("DSH_CLARIFY_LOOKBACK", "6")))
SPAN = max(30.0, float(os.environ.get("DSH_CLARIFY_SPAN", "300")))
COOLDOWN = max(30.0, float(os.environ.get("DSH_CLARIFY_COOLDOWN", "300")))
MIN_CONTEXT = max(1, int(os.environ.get("DSH_CLARIFY_MIN_CONTEXT", "1")))

SYS = "你是只输出 JSON 的群聊语境分类器。只判断事实，不续写对话，不解释。"
PROMPT = """判断 QQ 群聊最后一条消息与前文的关系，并提炼正在谈的具体事情。

{transcript}

只输出一行 JSON：
{{"state":"connected","missing":"","question":"","topic":"具体事情","revisit":false}}

state 只能是：
- connected：最后一句能由紧邻前文自然解释，指代对象也能确定。
- new_clear：换了新话题，但最后一句自身完整明确，不依赖前文也知道在说什么。
- unclear：与前文接不上，并且缺主语、对象、事件、指代来源或必要条件；若强行回答只能靠猜。

严格边界：
1. 突然换话题本身不是 unclear；“我今天买了台电脑”属于 new_clear。
2. 玩梗、感叹、短句也不自动算 unclear；只有确实缺关键信息才算。
3. “他又来了”“那个是不是寄了”“还是不行”“这下真完了”在前文没有明确所指时属于 unclear。
4. missing 用不超过 8 个字说明缺什么；question 只在 unclear 时给一句自然、短的中文追问（如“谁又来了？”“啥不行？”），禁止客服腔，禁止“请提供更多上下文”。
5. topic 用不超过 12 个字概括当前具体事情（如“要求语音叫名字”），不要只写“语音”“聊天”这种大类；不确定就留空。
6. revisit：最后一句是否在继续追问/要求机器人已经回答过的同一件事。补充关键新信息、换了具体问题、只是承接前文都填 false；无新增信息地再问、催答、要求再说一次才填 true。
7. 最后一句如果是别人特意 @ 机器人（点名）才说的，它的指代对象就是机器人本人。被点名时，短句、玩梗、感叹、寒暄（如「想你了」「你的也是」「顶你」「又来啦」）直接判 connected，不要因缺主语/对象判 unclear。"""

_FALLBACK_BLOCK = """<clarification>
这句话的信息不够明确。不要自行补主语、对象、事件或设定；能确定就正常接，不能确定就用一句很短、口语化的问题问清楚。不要提到规则或“上下文”。
</clarification>"""


def render(question: str, missing: str = "") -> str:
    q = sanitize_question(question)
    lines = ["<clarification>", "这句话与刚才的话接不上，而且缺少关键信息，不能靠猜。"]
    if missing:
        lines.append("缺的是：%s。" % missing[:8])
    if q:
        lines.append("这次只自然地追问一句，意思按“%s”；可以贴合你平时语气，但不要额外猜答案。" % q)
    else:
        lines.append("这次只用一句很短、口语化的问题问清楚，不要回答不存在的信息。")
    lines.append("不要提到分类、规则或上下文，也不要用客服腔。")
    lines.append("</clarification>")
    return "\n".join(lines)


def sanitize_question(text: str) -> str:
    q = re.sub(r"\s+", "", text or "").strip("\"'“”‘’")
    q = re.sub(r"^(请问|麻烦你?|请)(提供|补充|说明|告知)?", "", q)
    if not q or "上下文" in q or len(q) > 18:
        return ""
    if q[-1] not in "？?":
        q += "？"
    return q


def parse_result(raw: str) -> dict | None:
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    body = s[i:j + 1] if i >= 0 and j > i else s
    try:
        obj = json.loads(body)
    except Exception:
        obj = {}
        for key in ("state", "missing", "question", "topic"):
            m = re.search(r'"%s"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"' % key, body)
            if m:
                try:
                    obj[key] = json.loads('"' + m.group(1) + '"')
                except Exception:
                    obj[key] = m.group(1)
        m = re.search(r'"revisit"\s*:\s*(true|false)', body, re.I)
        if m:
            obj["revisit"] = m.group(1).lower() == "true"
    state = str(obj.get("state", "")).strip().lower()
    if state not in {"connected", "new_clear", "unclear"}:
        return None
    topic = re.sub(r"[\s<>\x00-\x1f]+", "", str(obj.get("topic", "")))[:12]
    revisit = obj.get("revisit", False)
    if not isinstance(revisit, bool):
        revisit = str(revisit).strip().lower() == "true"
    return {
        "state": state,
        "missing": str(obj.get("missing", "")).strip()[:8],
        "question": sanitize_question(str(obj.get("question", ""))),
        "topic": topic,
        "revisit": revisit,
    }


def _recent(gid: str, current: str, addressed: bool = False) -> tuple[str, int]:
    rows = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=2.0)
        try:
            rows = con.execute(
                "SELECT name, user_id, text, ts FROM buffer WHERE group_id=? "
                "ORDER BY ts DESC LIMIT ?", (gid, LOOKBACK + 2)
            ).fetchall()
        finally:
            con.close()
    except Exception:
        rows = []

    if not rows:
        if addressed:
            return "（刚刚这条 @ 你 的）%s" % current[:80], 0
        return "（刚刚这条）%s" % current[:80], 0
    newest = max(float(r[3] or 0) for r in rows)
    # 查询结果是新到旧；collect 钩子通常已把当前消息写进 buffer。
    # 必须先从新到旧跳过第一条同文，再反转，否则会误删更早的同一句。
    prior = []
    skipped_current = False
    for row in rows:
        name, uid, text, ts = row
        t = (text or "").strip()
        if not t or newest - float(ts or 0) > SPAN:
            continue
        if not skipped_current and t == current:
            skipped_current = True
            continue
        prior.append((name, uid, t, ts))
    kept = [
        "%s：%s" % ((name or uid or "群友").strip(), text[:80])
        for name, uid, text, _ts in reversed(prior[-LOOKBACK:])
    ]
    if addressed:
        kept.append("（这条是 @ 机器人 才说的）%s" % current[:80])
    else:
        kept.append("（刚刚这条）%s" % current[:80])
    return "\n".join(kept), len(kept) - 1


_recent_handled: dict[str, deque[tuple[str, float]]] = {}
_stat = {"seen": 0, "asked": 0, "silent": 0, "clear": 0, "fail": 0,
         "thin": 0, "cooldown": 0, "shadow_ask": 0, "shadow_silent": 0}
_last: deque[str] = deque(maxlen=8)


def _signature(uid: str, text: str) -> str:
    core = re.sub(r"[\s，。！？、,.!?~…：:；;\"'“”‘’（）()\[\]【】]", "", text or "")
    return "%s:%s" % (uid, core[:40])


def _seen_recent(gid: str, signature: str, now: float) -> bool:
    q = _recent_handled.setdefault(gid, deque(maxlen=30))
    while q and now - q[0][1] > COOLDOWN:
        q.popleft()
    return any(sig == signature for sig, _ in q)


def _mark(gid: str, signature: str, now: float) -> None:
    _recent_handled.setdefault(gid, deque(maxlen=30)).append((signature, now))


def decide_action(state: str | None, addressed: bool, duplicate: bool = False) -> str:
    """把分类结果变成固定行为，避免让分类模型直接决定说什么。"""
    if state in {"connected", "new_clear"}:
        return "pass"
    if duplicate:
        return "pass" if addressed else "silent"
    # 无结果也按含糊处理：被点名则保守追问，未点名则不要抢话。
    return "ask" if addressed else "silent"


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[clarify] 已加载：%s%s 群=%s 回看%d条/%.0fs 超时%.0fs 冷却%.0fs",
            "开" if ENABLED else "关", "（影子）" if SHADOW else "",
            "、".join(sorted(GROUPS)) or "无", LOOKBACK, SPAN, TIMEOUT, COOLDOWN,
        )

    async def _classify(self, umo: str, transcript: str) -> dict | None:
        pid = PROVIDER
        if not pid:
            try:
                pid = await self.context.get_current_chat_provider_id(umo)
            except Exception:
                return None
        if not pid:
            return None
        resp = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=pid,
                prompt=PROMPT.format(transcript=transcript),
                system_prompt=SYS,
                temperature=0,
            ),
            timeout=TIMEOUT,
        )
        raw = (getattr(resp, "completion_text", "") or "").strip()
        if not raw:
            raw = (getattr(resp, "reasoning_content", "") or "").strip()
        return parse_result(raw)

    @filter.on_llm_request(priority=1900)
    async def clarify(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED:
            return
        try:
            from astrbot.core.platform.message_type import MessageType
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            if event.get_extra("dsh_initiate") or event.get_extra("dsh_proactive"):
                return
            gid = str(event.get_group_id() or "")
            if GROUPS and gid not in GROUPS:
                return
            text = (event.message_str or "").strip()
            if not text or text.startswith(("/", "／", "!", "！")):
                return
            _stat["seen"] += 1
            addressed = bool(getattr(event, "is_at_or_wake_command", False))
            uid = str(event.get_sender_id() or "")
            transcript, context_count = _recent(gid, text, addressed)
            if context_count < MIN_CONTEXT:
                _stat["thin"] += 1
                # 没有前文就无法判断是否重提；明确清掉联动元数据，避免下游凭空疲劳。
                event.set_extra("dsh_topic_revisit", False)
                action = decide_action(None, addressed)
                if action == "ask":
                    req.extra_user_content_parts.append(TextPart(text=_FALLBACK_BLOCK))
                elif action == "silent" and not SHADOW:
                    event.stop_event()
                return
            try:
                result = await self._classify(event.unified_msg_origin, transcript)
            except Exception as exc:
                _stat["fail"] += 1
                logger.warning("[clarify] 分类失败，保守回退: %s", exc)
                result = None
            if result is None:
                _stat["fail"] += 1
                event.set_extra("dsh_topic_revisit", False)
                action = decide_action(None, addressed)
                if action == "ask":
                    req.extra_user_content_parts.append(TextPart(text=_FALLBACK_BLOCK))
                elif action == "silent" and not SHADOW:
                    event.stop_event()
                return
            # 给 dsh-fatigue 复用同一次语境分类；没有该插件时 extra 只是无害元数据。
            if result.get("topic"):
                event.set_extra("dsh_topic_key", result["topic"])
            event.set_extra("dsh_topic_revisit", bool(result.get("revisit")))
            action = decide_action(result["state"], addressed)
            if action == "pass":
                _stat["clear"] += 1
                return

            now = time.time()
            sig = _signature(uid, text)
            if _seen_recent(gid, sig, now):
                _stat["cooldown"] += 1
                logger.info("[clarify] 同一模糊话题冷却中，不再追问 uid=%s text=%s", uid, text[:30])
                if not addressed and not SHADOW:
                    event.stop_event()
                return
            _mark(gid, sig, now)
            detail = "%s missing=%s q=%s" % ("被点名" if addressed else "未点名", result["missing"], result["question"])
            _last.append(time.strftime("%H:%M:%S ") + detail)
            if addressed:
                if SHADOW:
                    _stat["shadow_ask"] += 1
                    logger.info("[clarify] 影子：本来会追问 %s", detail)
                else:
                    req.extra_user_content_parts.append(TextPart(text=render(result["question"], result["missing"])))
                    _stat["asked"] += 1
                    logger.info("[clarify] 注入自然追问 %s", detail)
            else:
                if SHADOW:
                    _stat["shadow_silent"] += 1
                    logger.info("[clarify] 影子：本来会因含糊而不插话 %s", detail)
                else:
                    _stat["silent"] += 1
                    logger.info("[clarify] 含糊且没人点名，不抢话 %s", detail)
                    event.stop_event()
        except Exception as exc:
            _stat["fail"] += 1
            logger.warning("[clarify] 处理失败，保持原流程: %s", exc)

    @filter.command("疑问状态")
    async def status(self, event: AstrMessageEvent):
        if str(event.get_sender_id() or "") not in OWNERS:
            return
        s = _stat
        lines = [
            "语境疑问：%s%s｜群：%s" % (
                "开" if ENABLED else "关", "（影子）" if SHADOW else "",
                "、".join(sorted(GROUPS)) or "无"),
            "看过 %d 轮｜正常 %d｜追问 %d｜含糊沉默 %d｜上下文不足 %d" % (
                s["seen"], s["clear"], s["asked"], s["silent"], s["thin"]),
            "失败 %d｜重复冷却 %d｜影子追问 %d｜影子沉默 %d" % (
                s["fail"], s["cooldown"], s["shadow_ask"], s["shadow_silent"]),
        ]
        if _last:
            lines.append("最近：" + "｜".join(list(_last)[-3:]))
        yield event.plain_result("\n".join(lines))
