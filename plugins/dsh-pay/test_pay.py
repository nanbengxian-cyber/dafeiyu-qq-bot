# -*- coding: utf-8 -*-
"""dsh-pay 意图和状态机回归测试（不依赖 AstrBot）。"""

from pay_logic import PayMachine, detect_intent, detect_method, is_owner_confirm

fails = []


def check(name, got, want):
    if got == want:
        print("  PASS", name)
    else:
        print("  FAIL %s: got=%r want=%r" % (name, got, want))
        fails.append(name)


print("A. 明确赞助意向")
for text in (
    "我要赞助你", "我想给你打赏一下", "我赞助一下大肥鱼", "打赏你一点",
    "给大肥鱼赞助一下", "送你点钱", "转给你一笔钱", "请你喝奶茶", "V你50",
):
    check("命中 %r" % text, bool(detect_intent(text)), True)

print("B. 普通讨论与历史误触发样本")
for text in (
    "为什么酒馆调用不了v4.1？", "b站 BV1hZYu6cETF", "BV1QP411f76k",
    "注册新号白嫖的10t", "你自己找大肥鱼付款去吧", "他跳赞助的机制好奇怪",
    "这个赞助的话我那个群不能搞", "有无群友赞助一下？", "有没有资助我一块钱",
    "给你点进去注册应该会给钱吧", "明天我结婚，你v我50份子钱", "v我50",
    "支持你搞这个项目", "支付宝到账了吗", "收款码", "赞助", "打赏",
):
    check("放过 %r" % text, detect_intent(text), "")

print("C. 自定义触发词仅整句匹配")
check("自定义整句", detect_intent("鱼粮", ["鱼粮"]), "鱼粮")
check("自定义不做子串", detect_intent("今天买了鱼粮", ["鱼粮"]), "")

print("D. 支付方式必须简短明确")
for text, want in (
    ("微信", "wechat"), ("用微信", "wechat"), ("我用支付宝吧", "alipay"),
    ("支付宝付款", "alipay"), ("我基本都在微信上操作", ""),
    ("支付宝为什么打不开", ""), ("有人发了微信二维码", ""),
):
    check("方式 %r" % text, detect_method(text), want)

print("E. 确认与状态机")
for text in ("钱到账了", "已收到赞助", "转账已到账", "确认收到"):
    check("群主确认 %r" % text, is_owner_confirm(text), True)
for text in (
    "还没到账", "未到账", "到账了吗", "到账失败", "如果到账了",
    "讨论到账逻辑", "怎么判断到账", "不是已收到", "他已到账了吧",
    "确认", "已确认",
):
    check("不确认 %r" % text, is_owner_confirm(text), False)
clock = [1000.0]
m = PayMachine(now=lambda: clock[0], global_cooldown=0)
check("进入待选", m.trigger_intent("1", "阿鱼"), "ask")
check("别人不能选", m.choose_method("2", "wechat"), "busy")
check("本人选微信", m.choose_method("1", "wechat"), "qr")
code, payer = m.owner_confirm()
check("确认感谢", code, "thank")
check("对账对象", payer, ("阿鱼", "1", "wechat"))

if fails:
    raise SystemExit("FAILED %d: %s" % (len(fails), fails))
print("ALL PASS")
