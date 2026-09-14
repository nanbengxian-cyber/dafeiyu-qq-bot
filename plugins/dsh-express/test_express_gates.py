"""dsh-express 纯规则回归：分情绪门槛、小时配额、滑动窗口。

不注入、不联网：只测 _should_express 这一个纯函数。
口径与 dsh-voice 的 test_emotion_tier 一致（同一种闸门形状，独立实现）。
"""
from __future__ import annotations

import ast
import json
import os
import random
import re
import tempfile
import time
from pathlib import Path

path = Path(__file__).with_name("main.py")
src = path.read_text(encoding="utf-8")
tree = ast.parse(src)
ns = {"json": json, "os": os, "random": random, "re": re, "time": time}
want = {"_read_emotion", "_should_express"}
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        try:
            exec(compile(ast.Module([node], []), str(path), "exec"), ns)
        except (NameError, AttributeError, TypeError):
            pass
    elif isinstance(node, ast.FunctionDef) and node.name in want:
        exec(compile(ast.Module([node], []), str(path), "exec"), ns)

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    if not cond:
        FAILED.append(label)
        print("  FAIL  %s" % label)
    else:
        print("  ok    %s" % label)


now = 10_000.0
with tempfile.TemporaryDirectory() as tmp:
    state_path = Path(tmp) / "emotion.json"

    def reset(emotion: str, intensity: int, expires: float = 20_000.0,
              threshold_by_name: dict | None = None,
              max_per_hour: int = 2, rate: float = 1.0,
              cooldown: int = 180, names: set | None = None) -> None:
        state_path.write_text(json.dumps({"g": {
            "emotion": emotion, "intensity": intensity,
            "expires_at": expires, "source": "message",
        }}), encoding="utf-8")
        ns["EMOTION_STATE_PATH"] = str(state_path)
        ns["ENABLED"] = True
        ns["GROUPS"] = set()
        ns["EMOTION_NAMES"] = names if names is not None else {
            "angry", "happy", "curious", "awkward", "excited",
            "sad", "surprised", "worried", "proud",
        }
        ns["THRESHOLD"] = 3
        ns["THRESHOLD_BY_NAME"] = threshold_by_name if threshold_by_name is not None else {
            "angry": 3, "happy": 2, "curious": 2, "awkward": 2,
            "excited": 2, "sad": 2, "surprised": 2, "worried": 2, "proud": 2,
        }
        ns["COOLDOWN"] = cooldown
        ns["MAX_PER_HOUR"] = max_per_hour
        ns["RATE"] = rate
        ns["_last"] = {}
        ns["_hits"] = {}

    should = ns["_should_express"]

    print("① 各情绪按各自门槛触发")
    for emo, inten in (("angry", 3), ("happy", 2), ("curious", 2), ("awkward", 2),
                       ("excited", 2), ("surprised", 2), ("worried", 2),
                       ("sad", 2), ("proud", 2)):
        reset(emo, inten)
        ok, why = should("g", now=now)
        check(ok, "%s(%d) 触发：%s" % (emo, inten, why))

    print("② angry(2) 仍然被拦（真发火才表达）")
    reset("angry", 2)
    ok, why = should("g", now=now)
    check(not ok and why == "强度不足", "angry(2) 被拦：%s" % why)

    print("③ calm / 过期 / 坏文件不触发")
    reset("calm", 0)
    ok, why = should("g", now=now)
    check(not ok and why == "情绪不匹配", "calm：%s" % why)
    reset("happy", 2, expires=now - 1)
    ok, why = should("g", now=now)
    check(not ok and why == "情绪不匹配", "过期：%s" % why)
    state_path.write_text("not json", encoding="utf-8")
    ok, why = should("g", now=now)
    check(not ok and why == "情绪不匹配", "坏文件：%s" % why)

    print("④ 概率闸")
    reset("happy", 2, rate=0.5)
    ok, why = should("g", now=now)
    check(not ok and why == "概率未中", "概率未中：%s" % why)

    print("⑤ 小时配额（上限 2，第三次拦）")
    reset("happy", 2, max_per_hour=2, cooldown=100)
    seq = []
    for i in range(3):
        t = now + i * 150  # 150 > 100 冷却，3×150=300 < 3600 同窗
        state_path.write_text(json.dumps({"g": {
            "emotion": "happy", "intensity": 2,
            "expires_at": t + 600, "source": "message",
        }}), encoding="utf-8")
        ok, why = should("g", now=t)
        seq.append(ok)
        if ok:
            ns["_last"]["g"] = t
            ns["_hits"].setdefault("g", []).append(t)
    check(seq == [True, True, False], "配额序列：%s" % seq)
    # 第三次的拒绝理由
    ok, why = should("g", now=now + 2 * 150)
    check(why == "小时配额满", "第三次理由：%s" % why)

    print("⑥ 滑动窗口滑出后可再触发")
    t = now + 3 * 150 + 3601
    state_path.write_text(json.dumps({"g": {
        "emotion": "happy", "intensity": 2,
        "expires_at": t + 600, "source": "message",
    }}), encoding="utf-8")
    ns["_last"]["g"] = now  # 冷却早已过
    ok, why = should("g", now=t)
    check(ok, "窗口滑出可再触发：%s" % why)

    print("⑦ 总闸/群开关")
    reset("happy", 2)
    ns["ENABLED"] = False
    ok, why = should("g", now=now)
    check(not ok and why == "关闭", "关闭：%s" % why)
    reset("happy", 2)
    ns["GROUPS"] = {"999"}
    ok, why = should("g", now=now)
    check(not ok and why == "群未启用", "群未启用：%s" % why)

    print("⑧ 白名单外情绪不触发")
    reset("happy", 2, names={"angry"})
    ok, why = should("g", now=now)
    check(not ok and why == "情绪不匹配", "白名单外：%s" % why)

print()
if FAILED:
    print("FAILED %d 项：" % len(FAILED))
    for item in FAILED:
        print("  -", item)
    raise SystemExit(1)
print("test_express_gates: 全部通过")
