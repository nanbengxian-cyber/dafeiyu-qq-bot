# -*- coding: utf-8 -*-
"""
dsh-factguard —— 记忆看门狗（2026-09-07 v1.0.0）。

问题：机器人被群友问「你是男是女」「你是不是 X」「你答应过 Y」时会左右摇摆、
乱承认、甚至顺着别人编造关于自己的事。群里说的不一定是真的，但机器人分不清
「别人的断言」和「自己的事实」。

方案：一张「自身事实表」（self-facts.json）钉死确定的信息（性别、生日等），
外加一条硬规则注入：凡是群友询问/断言它自身属性或过去行为时——

  1. 属性（性别/生日）只按事实表答，不摇摆不改口；
  2. 断言它做过/说过/答应过什么 → 先对照上下文里已有的记忆和聊天记录找依据，
     有依据才承认，没依据直接否认纠正（「没这回事」「你记错了吧」），
     绝不顺着承认、绝不编造；
  3. 事实表里没有的属性，不编造，可以「不告诉你」「你猜」。

注入机制同 dsh-slang：on_llm_request 里命中粗筛 -> 往 extra_user_content_parts
塞一个 <self_facts> 块（TextPart）。粗筛宁可多命中（每轮多 <200 token）也不可漏。

事实表可被群主命令改（/看门狗设置 字段 值），存 self-facts.json（本目录）。
零外部依赖；不主动回复，只在别人问它/说它时提供依据。

命令（群主，QQ 号由 DSH_FACTGUARD_OWNER 配置，仓库内不写真实号）：
  /看门狗状态        显示事实表 + 触发/注入统计
  /看门狗设置 <字段> <值>   改事实表字段（gender / birthday / note），立即生效
"""

import asyncio
import json
import os
import re

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType


# ---------------------------------------------------------------- 配置
def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


ENABLED = _flag("DSH_FACTGUARD")
GROUPS = _set("DSH_FACTGUARD_GROUPS")  # 真实群号由服务器 env 配置，仓库内不留
OWNERS = _set("DSH_FACTGUARD_OWNER")   # 真实群主 QQ 由服务器 env 配置，仓库内不留
_FACTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "self-facts.json")

# 默认事实表（self-facts.json 存在则以其为准；被删了也能兜底）。
# 生日 = DeepSeek 首个模型（DeepSeek Coder）发布日 2023-11-02，由群主指定。
_DEFAULT_FACTS = {
    "gender": "女",
    "birthday": "2023-11-02",
    "birthday_note": "DeepSeek 首个模型（DeepSeek Coder）发布日",
    "note": "被问年龄：报生日（2023-11-02）让人自己算，不编具体数字",
}

# 触发粗筛：针对机器人自身的询问或断言（宁可多命中，不可漏）。
# 「小鲸鱼/大肥鱼」昵称出现即算针对机器人；「你+属性/行为词」命中即算。
_TRIGGER_RE = re.compile(
    r"你(?:是|不是|是不是|答应过?|说过|做过|承认|记得|以前|上次|居然|竟然|"
    r"多大了|几岁|生日|叫|名字|是谁|性别|年龄|男|女)"
    r"|小鲸鱼|大肥鱼|性别|男的|女的"
)

_stat = {"triggered": 0, "injected": 0}
_facts: dict = {}
_facts_loaded = False
_facts_lock = asyncio.Lock()


def load_facts() -> dict:
    """读 self-facts.json；没有/损坏则用默认。"""
    try:
        with open(_FACTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("gender"):
            return data
    except Exception:
        pass
    return dict(_DEFAULT_FACTS)


def ensure_facts() -> None:
    global _facts, _facts_loaded
    if not _facts_loaded:
        _facts = load_facts()
        _facts_loaded = True


def save_facts() -> None:
    """事实表写回 self-facts.json（权限 0600）。"""
    tmp = "%s.%d.tmp" % (_FACTS_PATH, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_facts, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _FACTS_PATH)


def build_inject() -> str:
    """事实表 + 铁律，渲染成注入块。"""
    gender = _facts.get("gender") or _DEFAULT_FACTS["gender"]
    birthday = _facts.get("birthday") or _DEFAULT_FACTS["birthday"]
    note = _facts.get("note") or ""
    lines = [
        "<self_facts>",
        "【机器人自身设定（记忆看门狗，最高优先遵守）】",
        "性别：%s" % gender,
        "生日：%s" % birthday,
        "规则：",
        "1. 被问自己的属性（性别/生日/年龄）只按上面答，不摇摆、不改口；",
    ]
    if note:
        lines.append("   %s" % note)
    lines += [
        "2. 别人断言你的事（你是…、你答应过…、你说过…、你做过…、你记得…），"
        "先对照你上下文里已有的记忆和聊天记录找依据：有依据才承认；",
        "   没依据必须直接否认纠正（如「没这回事」「你记错了吧」「我没说过」），"
        "绝不顺着承认、绝不编造。",
        "3. 设定里没有的属性（身份来历、住址等）不编造，可以说「这个不告诉你」「你猜」。",
        "</self_facts>",
    ]
    return "\n".join(lines)


class Main(star.Star):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ensure_facts()
        if ENABLED:
            logger.info(
                "[factguard] 已加载：开 事实表=%s（性别=%s 生日=%s）",
                _FACTS_PATH, _facts.get("gender"), _facts.get("birthday"))

    def _owner(self, event: AstrMessageEvent) -> bool:
        uid = str(event.get_sender_id() or "")
        return bool(OWNERS) and uid in OWNERS

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
        except Exception:
            return
        gid = str(event.get_group_id() or "")
        if not gid or (GROUPS and gid not in GROUPS):
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        if not _TRIGGER_RE.search(text):
            return
        _stat["triggered"] += 1
        ensure_facts()
        block = build_inject()
        try:
            req.extra_user_content_parts.append(TextPart(text=block))
            _stat["injected"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[factguard] 注入失败: %s", exc)

    @filter.command("看门狗状态")
    async def status(self, event: AstrMessageEvent):
        if not self._owner(event):
            return
        ensure_facts()
        lines = [
            "记忆看门狗：%s" % ("开" if ENABLED else "关"),
            "事实表：",
            "  性别=%s" % (_facts.get("gender") or "？"),
            "  生日=%s" % (_facts.get("birthday") or "？"),
            "  备注=%s" % (_facts.get("note") or "（无）"),
            "统计：触发 %d 次 / 注入 %d 次" % (_stat["triggered"], _stat["injected"]),
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("看门狗设置")
    async def set_field(self, event: AstrMessageEvent):
        if not self._owner(event):
            return
        args = (event.message_str or "").strip().split(None, 1)
        rest = args[1].strip() if len(args) > 1 else ""
        parts = rest.split()
        if len(parts) < 2:
            yield event.plain_result("用法：/看门狗设置 <字段> <值>（如：gender 女）")
            return
        field, value = parts[0].strip().lower(), parts[1].strip()
        if field not in ("gender", "birthday", "note"):
            yield event.plain_result("字段只支持 gender / birthday / note")
            return
        async with _facts_lock:
            ensure_facts()
            _facts[field] = value
            save_facts()
        yield event.plain_result("已更新：%s = %s（立即生效）" % (field, value))
