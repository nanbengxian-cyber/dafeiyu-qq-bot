# -*- coding: utf-8 -*-
"""dsh-pay —— QQ 群「赞助/打赏」收款引导插件。

---------------------------------------------------------------------------
做什么

群里有人表示想赞助/打赏/投喂大肥鱼（支付宝、微信、V 等赞助语气）时，机器人
先反问用支付宝还是微信，对方选定后把对应收款码发出去。**只有群主（大肥鱼
本尊 2774000001）在群里确认到账后**，机器人才感谢对方——这是唯一的感谢
入口，杜绝「自己喊一句就触发感谢」的乱触发刷屏。

---------------------------------------------------------------------------
为什么是「群主在场确认」才能感谢

机器人无法真实校验钱是否到账。如果某人说一句「付了」AI 就感谢，任何人都能
空口刷屏，感谢会被玩坏。所以感谢的唯一条件是：群主在群里发了确认短语（已到
账 / 到账了 / 已收到 / 收到赞助 …）或 /确认赞助 命令，机器人才把这笔待确认
赞助人对账并感谢。没有群主确认，机器人永远不发感谢。

---------------------------------------------------------------------------
状态机（每个群独立）

  IDLE ──有人表达赞助意向──▶ ASKED(payer, ts) ──payer 选支付方式──▶ QR_SENT(payer, method, ts)
    ▲                                                                      │
    └──────────────── 群主确认到账 → 感谢 payer → 回到 IDLE ◀───────────────┘

防刷屏措施：
  · ASK_COOLDOWN    同一人两次触发「问支付方式」的最小间隔（防一个人反复刷问）。
  · GLOBAL_COOLDOWN 同一群任意人触发的最小间隔（防多人连续刷）。
  · PENDING_TTL     待支付/待确认状态有效期，过期自动回 IDLE，
                   群主拖太久才确认不会对错最远那笔。
  · THANK_COOLDOWN 同一人两次被感谢的最小间隔。

判定与状态机全部在 pay_logic.py（纯逻辑，可单测）；本文件只做事件接线。

---------------------------------------------------------------------------
影子模式（DSH_PAY_MODE=shadow，默认）

所有检测都走完整的状态判定，但**一条消息都不发**，只把每一步（谁触发、命
中什么词、状态怎么走）打到日志 + 本机表。用于上线前观察触发频率、看哪些词
误报，再切 live。live 才真正发消息。判定逻辑影子与 live 完全相同，保证
「影子观察到的＝live 会发的」。

---------------------------------------------------------------------------
配置（env，全部可配，走 imagegen.env）：
  DSH_PAY_ENABLE=1
  DSH_PAY_MODE=shadow|live          默认 shadow（先观察再上线）
  DSH_PAY_GROUPS=100000001          作用群
  DSH_PAY_OWNER=2774000001          群主（唯一确认到账的人）
  DSH_PAY_DIR=/AstrBot/data/pay     收款码目录
  DSH_PAY_WECHAT_IMG=wechat.png     微信收款码文件名
  DSH_PAY_ALIPAY_IMG=alipay.png     支付宝收款码文件名
  DSH_PAY_ASK_COOLDOWN=600          同人问支付方式冷却（秒）
  DSH_PAY_GLOBAL_COOLDOWN=300       同群任意触发冷却（秒）
  DSH_PAY_PENDING_TTL=1800          待确认有效期（秒）
  DSH_PAY_THANK_COOLDOWN=900        同人被感谢冷却（秒）
  追加触发词 DSH_PAY_EXTRA_WORDS=…（逗号分隔）

命令（仅群主）：
  /赞助状态    模式、状态机、冷却、最近触发
  /赞助模式 shadow|live   运行时切模式
  /赞助重置    清空所有群的待确认/待支付状态
---------------------------------------------------------------------------
任何异常都只记日志，绝不误伤正常聊天；影子模式下任何消息都不发。
"""

import os
import time
from collections import deque

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.core import logger
from astrbot.core.message.message_event_result import MessageChain

from .pay_logic import PayMachine, norm, detect_intent, detect_method, \
    is_owner_confirm, _ST_NAME

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}

def _set(name: str, default: str = "") -> set[str]:
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}

ENABLED = _flag("DSH_PAY_ENABLE", "1")
GROUPS = _set("DSH_PAY_GROUPS", "100000001")
OWNERS = _set("DSH_PAY_OWNER", "2774000001")
MODE = os.environ.get("DSH_PAY_MODE", "shadow").strip().lower()

PAY_DIR = os.environ.get("DSH_PAY_DIR", "/AstrBot/data/pay")
WECHAT_IMG = os.environ.get("DSH_PAY_WECHAT_IMG", "wechat.png")
ALIPAY_IMG = os.environ.get("DSH_PAY_ALIPAY_IMG", "alipay.png")

ASK_COOLDOWN = max(1, int(os.environ.get("DSH_PAY_ASK_COOLDOWN", "600")))
GLOBAL_COOLDOWN = max(0, int(os.environ.get("DSH_PAY_GLOBAL_COOLDOWN", "300")))
PENDING_TTL = max(30, int(os.environ.get("DSH_PAY_PENDING_TTL", "1800")))
THANK_COOLDOWN = max(1, int(os.environ.get("DSH_PAY_THANK_COOLDOWN", "900")))

EXTRA_WORDS = [w for w in os.environ.get("DSH_PAY_EXTRA_WORDS", "").split(",") if w.strip()]

_METHOD_CN = {"wechat": "微信", "alipay": "支付宝"}

# gid -> PayMachine
_machines: dict[str, PayMachine] = {}
# 统计
_stat = {"detect_intent": 0, "ask": 0, "method": 0, "qr": 0,
         "owner_confirm_note": 0, "thank": 0, "expired": 0,
         "shadow_block": 0}
_events: deque = deque(maxlen=40)


def _gid(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_group_id() or event.unified_msg_origin or "?")
    except BaseException:
        return "?"

def _uid(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id() or "?")
    except BaseException:
        return "?"

def _uname(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_name() or "")
    except BaseException:
        return "?"

def _note(brief: str) -> None:
    _events.append(time.strftime("%H:%M:%S ") + brief)

def _machine(gid: str) -> PayMachine:
    m = _machines.get(gid)
    if m is None:
        m = PayMachine(ask_cooldown=ASK_COOLDOWN, global_cooldown=GLOBAL_COOLDOWN,
                       pending_ttl=PENDING_TTL, thank_cooldown=THANK_COOLDOWN)
        _machines[gid] = m
    return m

def _img_path(method: str):
    name = WECHAT_IMG if method == "wechat" else (ALIPAY_IMG if method == "alipay" else None)
    if not name:
        return None
    p = os.path.join(PAY_DIR, name)
    return p if os.path.isfile(p) else None

def _stat_add(key: str) -> None:
    _stat[key] = _stat.get(key, 0) + 1


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        self._mode = MODE if MODE in ("shadow", "live") else "shadow"
        logger.info(
            "[赞助] 已加载：%s 群=%s 模式=%s｜微信%s 支付宝%s｜同人问%d 全局问%d 待确认%d 感谢冷却%d",
            "开" if ENABLED else "关",
            "、".join(sorted(GROUPS)) or "无",
            self._mode,
            _img_path("wechat") or "缺",
            _img_path("alipay") or "缺",
            ASK_COOLDOWN, GLOBAL_COOLDOWN, PENDING_TTL, THANK_COOLDOWN,
        )

    # ------------------------------------------------------------ #
    # 主钩子：所有群内消息
    # ------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            gid = _gid(event)
            uid = _uid(event)
            if "?" in (gid, uid) or gid not in GROUPS:
                return
            text = event.message_str or ""
            if not norm(text):
                return
            uname = _uname(event)
            m = _machine(gid)
            m.expire()

            # 群主确认到账 —— 唯一感谢入口
            if uid in OWNERS and is_owner_confirm(text):
                await self._owner_confirmed(event, m, gid, text)
                return

            # 群主是收款方，不是付款方：群主本人的消息永不触发「问支付方式」流程。
            # 只上面的确认短语例外（已 return）。避免群主发「赞助/打赏」之类的话
            # 把 bot 带进待选态、自己占住流程后不断误判后续消息。
            if uid in OWNERS:
                return

            # 待选态：等触发人本人选支付方式
            if m.st == 1:  # ST_ASKED
                if uid == m.payer_uid:
                    await self._on_method_select(event, m, gid, text)
                return

            # 空闲态：检测赞助意向
            if m.st == 0:  # ST_IDLE
                kw = detect_intent(text, EXTRA_WORDS)
                if kw:
                    _stat_add("detect_intent")
                    code = m.trigger_intent(uid, uname)
                    await self._on_intent(event, m, gid, uid, uname, text, kw, code)
        except BaseException as exc:
            logger.warning("[赞助] on_message 异常放行: %r", exc)

    # ------------------------------------------------------------ #
    # 群主确认到账 → 感谢
    # ------------------------------------------------------------ #
    async def _owner_confirmed(self, event, m: PayMachine, gid: str, text: str) -> None:
        code, payer = m.owner_confirm()
        if code == "idle":
            _stat_add("owner_confirm_note")
            logger.info("[赞助] 群主确认短语，但本群无待确认 => 不动作 gid=%s", gid)
            return
        if code == "cooldown":
            logger.info("[赞助] 该赞助人感谢冷却内，跳过 gid=%s", gid)
            return
        # code == "thank"
        pname, p_uid, method = payer
        _stat_add("thank")
        _note("群主确认到账 → 感谢 %s(%s) 方式=%s" % (pname, p_uid, method or "-"))
        logger.info("[赞助] 群主确认，感谢 %s(%s) 方式=%s", pname, p_uid, method or "-")
        if self._mode != "live":
            _stat_add("shadow_block")
            logger.info("[赞助][影子] 拦截感谢(本应发)：%s(%s)", pname, p_uid)
            return
        await self._send_thank(event, gid, p_uid, pname)

    async def _send_thank(self, event, gid: str, p_uid: str, pname: str) -> None:
        try:
            chain = [
                At(qq=int(p_uid), name=pname or p_uid),
                Plain(" 太感谢你啦～谢谢你支持，钱到账了，我记心里了！🙏"),
            ]
            await event.send(MessageChain(chain=chain))
            logger.info("[赞助] 已感谢 %s(%s) gid=%s", pname, p_uid, gid)
        except BaseException as exc:
            logger.warning("[赞助] 感谢发送失败: %r", exc)

    # ------------------------------------------------------------ #
    # 意向命中 → 反问支付方式
    # ------------------------------------------------------------ #
    async def _on_intent(self, event, m: PayMachine, gid: str, uid: str, uname: str,
                         text: str, kw: str, code: str) -> None:
        _note("有人要赞助：%s(%s) 命中「%s」%s" % (uname, uid, kw, text[:30]))
        if code != "ask":
            logger.info("[赞助] 意向命中「%s」但%s from %s(%s): %s",
                        kw, "冷却内(不反问)" if code == "cooldown" else "忙(待处理中)",
                        uname, uid, text[:40])
            return
        _stat_add("ask")
        logger.info("[赞助] 意向命中「%s」from %s(%s): %s", kw, uname, uid, text[:40])
        if self._mode != "live":
            _stat_add("shadow_block")
            logger.info("[赞助][影子] 拦截反问支付方式(本应发)")
            return
        await self._ask_method(event)

    async def _ask_method(self, event) -> None:
        try:
            await event.send(MessageChain(chain=[Plain("你也要赞助我呀？太感谢啦～支付宝还是微信呀？")]))
        except BaseException as exc:
            logger.warning("[赞助] 反问支付方式发送失败: %r", exc)

    # ------------------------------------------------------------ #
    # 待选者选方式 → 发码
    # ------------------------------------------------------------ #
    async def _on_method_select(self, event, m: PayMachine, gid: str, text: str) -> None:
        method = detect_method(text)
        if not method:
            logger.info("[赞助] 待选者 %s 未说出支付方式: %s", m.payer_uid, text[:30])
            return
        code = m.choose_method(m.payer_uid, method)
        if code != "qr":
            return
        _stat_add("method")
        mc = _METHOD_CN.get(method, method)
        _note("%s(%s) 选择%s 发码" % (m.payer_name, m.payer_uid, mc))
        logger.info("[赞助] %s(%s) 选定%s → 待群主确认 gid=%s", m.payer_name, m.payer_uid, mc, gid)
        if self._mode != "live":
            _stat_add("shadow_block")
            logger.info("[赞助][影子] 拦截发码(本应发)")
            return
        await self._send_qr(event, gid, method, mc)

    async def _send_qr(self, event, gid: str, method: str, mc: str) -> None:
        path = _img_path(method)
        if not path:
            logger.warning("[赞助] 收款码缺失: %s/%s 不存在", PAY_DIR, WECHAT_IMG if method == "wechat" else ALIPAY_IMG)
            await event.send(MessageChain(chain=[Plain("抱歉，%s收款码暂时没配好，晚点再赞助我～" % mc)]))
            return
        _stat_add("qr")
        try:
            chain = [
                Plain("用%s扫这个收款码就好，备注别留全名～💕" % mc),
                Image.fromFileSystem(path),
            ]
            await event.send(MessageChain(chain=chain))
            logger.info("[赞助] 已发%s收款码 gid=%s", mc, gid)
        except BaseException as exc:
            logger.warning("[赞助] 发码失败: %r", exc)

    # ------------------------------------------------------------ #
    # 命令
    # ------------------------------------------------------------ #
    @filter.command("赞助状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if _uid(event) not in OWNERS:
            return
        lines = [
            "[赞助] 状态",
            "开关 %s｜模式 %s｜群 %s" % ("开" if ENABLED else "关", self._mode, "、".join(sorted(GROUPS)) or "无"),
            "收款码 微信 %s｜支付宝 %s" % (_img_path("wechat") or "缺", _img_path("alipay") or "缺"),
            "冷却 同人问 %ds｜全局问 %ds｜待确认 %ds｜同人感谢 %ds" % (ASK_COOLDOWN, GLOBAL_COOLDOWN, PENDING_TTL, THANK_COOLDOWN),
            "累计 意向%d 问%d 选方式%d 发码%d 群主确认%d 感谢%d 过期%d 影子拦截%d" % (
                _stat["detect_intent"], _stat["ask"], _stat["method"],
                _stat["qr"], _stat["owner_confirm_note"], _stat["thank"],
                _stat["expired"], _stat["shadow_block"]),
        ]
        stlines = []
        for g, m in _machines.items():
            stlines.append("%s=%s(%s)@%s" % (g, _ST_NAME.get(m.st, "?"), m.payer_uid or "-", m.method or "-"))
        lines.append("各群状态：%s" % ("；".join(stlines) if stlines else "全部空闲"))
        lines.append("最近：%s" % ("｜".join(_events[-6:]) or "还没有"))
        yield event.plain_result("\n".join(lines))

    @filter.command("赞助模式")
    async def cmd_mode(self, event: AstrMessageEvent):
        if _uid(event) not in OWNERS:
            return
        args = (event.message_str or "").strip().split()
        if len(args) < 2 or args[1] not in ("shadow", "live"):
            yield event.plain_result("[赞助] 用法：/赞助模式 shadow|live")
            return
        self._mode = args[1]
        yield event.plain_result("[赞助] 已切换模式：" + self._mode)

    @filter.command("赞助重置")
    async def cmd_reset(self, event: AstrMessageEvent):
        if _uid(event) not in OWNERS:
            return
        n = len(_machines)
        _machines.clear()
        yield event.plain_result("[赞助] 已清空 %d 个群的全部待确认/待支付状态" % n)
