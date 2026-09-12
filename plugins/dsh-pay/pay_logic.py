# -*- coding: utf-8 -*-
"""dsh-pay 纯逻辑层：不 import astrbot，可独立单测。

把「认出赞助意向 / 认出支付方式 / 认出群主确认短语 / 状态机流转 / 冷却判定」
这些判定逻辑抽出来，main.py 只做事件接线。这样：
  - 判定逻辑可以离线单测（test_pay.py 直接跑，不需要 astrbot 环境）
  - 影子模式与 live 共用同一套判定，保证「影子观察到的＝live 会发的」
"""

import re
from collections import deque

ST_IDLE = 0
ST_ASKED = 1
ST_QR = 2
_ST_NAME = {ST_IDLE: "空闲", ST_ASKED: "待选支付方式", ST_QR: "待群主确认到账"}


def norm(s: str) -> str:
    """归一：去空白、统一小写。供意图结构匹配用。"""
    return "".join(s.split()).lower() if s else ""


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #

# 赞助入口必须同时包含「付款人意愿 + 收款对象是机器人」。旧版是宽松子串表，
# 导致 v4.1、BV1 视频号、白嫖 token、替别人找资助等普通讨论全部误触发。
# 这里宁可要求对方多说清楚一句，也不能在谈钱时突然跳出收款码。
_EXPLICIT_INTENT_RES = [
    re.compile(r"我(?:想|要|愿意|可以|打算|准备|来)?(?:给)?(?:你|大肥鱼)(?:赞助|打赏|投喂|资助|转账|付钱|付款)(?:一下|点|一笔)?"),
    re.compile(r"我(?:想|要|愿意|可以|打算|准备|来)?(?:赞助|打赏|投喂|资助)(?:一下|点)?(?:你|大肥鱼)"),
    re.compile(r"(?:赞助|打赏|投喂|资助)(?:一下|点)?(?:你|大肥鱼)"),
    re.compile(r"给(?:你|大肥鱼)(?:赞助|打赏|投喂|资助)(?:一下|点)?"),
    re.compile(r"(?:给|送|转给)(?:你|大肥鱼)(?:点|一些|一笔)?(?:钱|红包|辛苦费)"),
    re.compile(r"(?:我)?请(?:你|大肥鱼)(?:吃饭|吃顿饭|喝奶茶|喝杯奶茶|喝咖啡|喝水)"),
]
# V你50 是给机器人转钱；V我50 是向机器人要钱，方向相反，绝不能触发。
_V_TO_BOT_RE = re.compile(r"(?:^|[^a-z0-9])v(?:你|大肥鱼)\d+(?:元)?(?:$|[^a-z0-9])", re.I)

def detect_intent(text: str, extra_words=None) -> str:
    """返回明确的「给机器人赞助」意向；讨论赞助/钱/版本号一律不算。"""
    n = norm(text)
    if not n:
        return ""
    m = _V_TO_BOT_RE.search(n)
    if m:
        return m.group(0).strip()
    for pattern in _EXPLICIT_INTENT_RES:
        m = pattern.search(n)
        if m:
            return m.group(0)
    # 自定义词属于运维方主动配置，仍按精确归一化后的整句匹配，避免重新引入子串误触发。
    for word in extra_words or ():
        w = norm(word)
        if w and n == w:
            return word
    return ""


def detect_method(text: str) -> str:
    """只接受简短、明确的支付方式选择，普通微信/支付宝讨论不推进状态机。"""
    n = norm(text)
    # 允许「微信」「用微信」「我用支付宝」「支付宝吧/支付」，拒绝长句中的偶然提及。
    if re.fullmatch(r"(?:我)?(?:就)?用?微信(?:吧|支付|付款|转账)?[呀啊呢哦嘛]?[。！!]?", n):
        return "wechat"
    if re.fullmatch(r"(?:我)?(?:就)?用?支付宝(?:吧|支付|付款|转账)?[呀啊呢哦嘛]?[。！!]?", n):
        return "alipay"
    return ""


_OWNER_CONFIRM_RE = re.compile(
    r"(?:钱|赞助|打赏|转账)?(?:已经|已)?(?:到账(?:了)?|收到(?:了)?|收到了)(?:钱|赞助|打赏|转账)?"
    r"|(?:钱|赞助|打赏|转账)?(?:收款成功|确认收到|确认到账)"
)
_OWNER_CONFIRM_PREFIX_RE = re.compile(r"(?:感谢)?(?:已经|已)(?:到账(?:了)?|收到(?:了)?)")
_OWNER_CONFIRM_NEG_RE = re.compile(r"(?:没|没有|未|还没|尚未|失败|不到账|没到|未到|没收|未收|不是)")
_OWNER_CONFIRM_META_RE = re.compile(r"(?:吗|么|嘛|？|\?|如果|假如|怎么|如何|逻辑|代码|测试|讨论|判断|检测)")


def is_owner_confirm(text: str) -> bool:
    """只接受明确肯定且短小的到账确认，否定/疑问/元讨论一律不算。"""
    n = norm(text).strip("，,。.!！")
    if not n or _OWNER_CONFIRM_NEG_RE.search(n) or _OWNER_CONFIRM_META_RE.search(n):
        return False
    return bool(_OWNER_CONFIRM_RE.fullmatch(n) or _OWNER_CONFIRM_PREFIX_RE.fullmatch(n))


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #

class PayMachine:
    """单个群一份。所有写入都带时间戳与冷却判定，供影子模式照样走。"""

    def __init__(self, ask_cooldown=600, global_cooldown=300,
                 pending_ttl=1800, thank_cooldown=900, now=None):
        self.ask_cooldown = ask_cooldown
        self.global_cooldown = global_cooldown
        self.pending_ttl = pending_ttl
        self.thank_cooldown = thank_cooldown
        self._now = now if now is not None else __import__("time").time
        self.st = ST_IDLE
        self.payer_uid = ""
        self.payer_name = ""
        self.method = None
        self.ts = 0.0
        # 冷却记账
        self.last_ask = 0.0
        self.last_user_ask = {}
        self.last_thank = {}

    def expire(self) -> None:
        """待确认/待选过期则回空闲。"""
        if self.st in (ST_ASKED, ST_QR) and self._now() - self.ts > self.pending_ttl:
            self.st = ST_IDLE
            self.payer_uid = ""
            self.payer_name = ""
            self.method = None

    def trigger_intent(self, uid: str, uname: str) -> str:
        """有人在空闲态表达了赞助意向。
        返回 'ask'(可以反问支付方式) / 'cooldown'(冷却内不反问)。
        """
        if self.st != ST_IDLE:
            return "busy"
        now = self._now()
        if self.global_cooldown > 0 and now - self.last_ask < self.global_cooldown:
            return "cooldown"
        if now - self.last_user_ask.get(uid, 0) < self.ask_cooldown:
            return "cooldown"
        self.last_ask = now
        self.last_user_ask[uid] = now
        self.st = ST_ASKED
        self.payer_uid = uid
        self.payer_name = uname
        self.method = None
        self.ts = now
        return "ask"

    def choose_method(self, uid: str, method: str) -> str:
        """待选态下触发人本人选了支付方式。
        返回 'qr'(发码) / 'busy'(不是待选者或未识别到方式)。
        """
        if self.st != ST_ASKED or uid != self.payer_uid:
            return "busy"
        if method not in ("wechat", "alipay"):
            return "busy"
        self.method = method
        self.st = ST_QR
        self.ts = self._now()
        return "qr"

    def owner_confirm(self):
        """群主确认到账。
        返回 (code, payer)：
          code='thank' 可以感谢，payer=(name, uid, method) 是本次确认的赞助人；
          code='cooldown' 该人被感谢冷却内；
          code='idle' 本群无待确认。
        绑定到付款发起人，避免群主确认给冷却内的同一人重复感谢。
        """
        if self.st != ST_QR:
            return "idle", None
        now = self._now()
        if now - self.last_thank.get(self.payer_uid, 0) < self.thank_cooldown:
            return "cooldown", None
        self.last_thank[self.payer_uid] = now
        payer = (self.payer_name, self.payer_uid, self.method)
        self.st = ST_IDLE
        self.payer_uid = ""
        self.payer_name = ""
        self.method = None
        return "thank", payer