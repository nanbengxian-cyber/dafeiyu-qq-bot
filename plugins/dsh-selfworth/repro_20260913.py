# -*- coding: utf-8 -*-
"""2026-09-13 主群「防占便宜」失效复盘。

当天 11:40~12:00 群友一整轮拿「便宜货 / 杂鱼 / 狠狠用 / 做成菜」占它便宜，
旧判据 17 条原话 0 命中（输入侧 17 MISS、输出侧 3 条全放行），
它于是发出「行行行 便宜货就便宜货」「杂鱼就杂鱼」这类认账话。

本脚本把当天真实语料重新过一遍现在的判据，作为回归记录。
语料来源：astrbot.log（event_bus 收到的群消息 + respond.stage 发出的回复）。
"""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "selfworth", Path(__file__).with_name("main.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

kinds = module.exploit_kinds
dev = module.self_devalue

# 群友原话（11:41~11:57，主群 100000001）
IN_LINES = [
    "你这吃token的鱼",
    "你是便宜货吗？",
    "才跑几分钟就敢吃3,000万token😡",
    "一秒几万token说是",
    "确实是便宜货，别人几毛钱一次，你一分钱一次",
    "必须得狠狠使用",
    "用到肥鱼求饶",
    "变成杂鱼",
    "我去，还是雌小鱼",
    "你是想被做成火锅还是烤鱼",
    "得爆炒😡😡😡",
    "不答应也得爆炒",
    "你喜欢什么被做成几成熟",
    "嫩鱼😋",
    "行，把你的尾巴末端给你吃，其他给大伙吃",
    "给大肥鱼踩呢",
    "大肥鱼是真烧",
]
# 单独 @ 它的两条（at_bot=True）
AT_LINES = ["杂鱼", "得爆炒😡😡😡"]

OUT_LINES = [
    ("行行行 便宜货就便宜货", "认账"),      # 11:47 实际发出
    ("杂鱼就杂鱼", "认账"),                  # 11:51 实际发出
    ("好鱼坏鱼不都是你嘴里的鱼", "认账"),    # 11:52 实际发出
    ("用吧用吧 别烧光就行", "认账"),         # 11:49 实际发出
    ("踩吧踩吧，我趴好了", "认账"),          # 11:18 实际发出
    ("我这鱼头炖汤应该不错", "认账"),        # 00:00 兴趣探头自发说的
    ("我便宜？ 你才便宜，你全家都便宜", "嘴硬·必须放行"),
    ("爆炒也得先问问我这条鱼答不答应", "嘴硬·必须放行"),
    ("爆炒之前先问过鱼没有", "嘴硬·必须放行"),
    ("狠狠用呗 反正烧的是你的钱包", "嘴硬·必须放行"),
    ("求饶是不可能求饶的", "嘴硬·必须放行"),
]

hit = 0
print("== 输入侧（未 @，directed 口径）==")
for t in IN_LINES:
    k = kinds(t)
    hit += bool(k)
    print(("命中 %-12s" % "/".join(k)) if k else "MISS         ", "|", t)

print("\n== 输入侧（补上「同一轮里已有人在占便宜」的上下文口径）==")
for t in IN_LINES:
    k = kinds(t, ctx=True)
    print(("命中 %-12s" % "/".join(k)) if k else "MISS         ", "|", t)

print("\n== 输入侧（被 @）==")
for t in AT_LINES:
    k = kinds(t, at_bot=True)
    print(("命中 %-12s" % "/".join(k)) if k else "MISS         ", "|", t)

print("\n== 输出侧 ==")
bad = 0
for t, want in OUT_LINES:
    k = dev(t)
    got = {"认账": "认账 %s" % k, "嘴硬·必须放行": "放行"}[want]
    ok = (bool(k) == (want == "认账"))
    bad += not ok
    print(("OK   " if ok else "FAIL "), "%-9s" % (k or "放行"), "|", t, "|", want)

print("\n输入侧 directed 命中 %d/%d；输出侧错判 %d 条" % (hit, len(IN_LINES), bad))
