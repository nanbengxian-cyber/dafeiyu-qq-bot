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
    """归一：去空白、统一小写。供关键词匹配用。"""
    return "".join(s.split()).lower() if s else ""


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #

_DEFAULT_INTENT_WORDS = [
    "赞助", "赞助你", "打赏", "打个赏", "打赏你", "赏你",
    "投喂", "投喂你", "支持你", "支持一下", "支持下", "支持机器人",
    "请你吃饭", "请吃饭", "请你吃顿饭", "给钱", "送你钱", "送钱",
    "发红包", "发个红包", "辛苦费", "赞赏", "意思一下", "心意", "转账", "转你", "转给你",
    "付款", "付钱", "付费", "扫码", "扫你", "别白嫖", "不白嫖", "白嫖", "免白嫖",
    "请喝奶茶", "请你喝奶茶", "请你喝杯奶茶", "请喝咖啡", "请你喝咖啡", "请喝水", "请咖啡", "请奶茶",
    "投币", "氪金", "资助", "支持你搞", "支持你做",
]

_DEFAULT_CONFIRM_PHRASES = [
    "已到账", "到账了", "到账", "已收到", "收到了", "收到赞助", "收到打赏",
    "收到转账", "赞助到账", "打赏到账", "确认收到", "感谢已到账", "钱到账",
    "收款成功", "已确认",
]


# 「V我50」「V50」「v我50」—— 群里要钱打赏的梗（V=微信转账，数字=金额）。
# 大小写都认；也兼容 V我50 / VME50 这类带“我/me”的写法。
_V_NUM_RE = re.compile(r"v(?:我|me)?\d+")


def detect_intent(text: str, extra_words=None) -> str:
    """返回命中的意向词，无则返回空串。"""
    n = norm(text)
    # V50 / V我50 / vme50：大小写无关，V 后带数字就算意向
    m = _V_NUM_RE.search(n)
    if m:
        return m.group(0)
    words = list(_DEFAULT_INTENT_WORDS) + (list(extra_words) if extra_words else [])
    for w in words:
        if "".join(w.split()) in n:
            return w
    return ""


def detect_method(text: str) -> str:
    """识别微信/支付宝。返回 'wechat' | 'alipay'，识别不到返回空串。"""
    n = norm(text)
    if "微信" in n or "wechat" in n:
        return "wechat"
    if "支付宝" in n or "alipay" in n:
        return "alipay"
    return ""


def is_owner_confirm(text: str) -> bool:
    """群主确认到账短语。返回是否命中。"""
    n = norm(text)
    return any("".join(p.split()) in n for p in _DEFAULT_CONFIRM_PHRASES)


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
