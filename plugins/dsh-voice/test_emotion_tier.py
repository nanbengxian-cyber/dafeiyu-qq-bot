"""分情绪门槛 + 小时配额回归（patch:emotion-tier）。

背景：dsh-emotion 的强度生成只有 angry 能到 3，其余情绪上限是 2。
原先语音侧只有一个全局 EMOTION_THRESHOLD=3，等于「只有发火才配出声」——
三天生产日志里情绪主动语音只成功 3 次且全是 angry。本测试锁住修好后的行为：

  1. angry≥3 仍然只在真发火时触发（不放宽最敏感的那种）；
  2. 上限为 2 的情绪（happy/curious 等）在 intensity=2 时能触发；
  3. 单情绪门槛可以被覆盖，未列出的情绪回落到全局门槛；
  4. 小时配额生效，且是滑动窗口不是硬重置。

不合成音频、不联网：只测 should_emotion_voice 这一个纯函数。
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
want = {
    "_strip_all_leaks", "_extract_arg", "_clean_leaked_call", "_clean_for_tts",
    "_read_emotion", "should_emotion_voice",
}
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
              max_per_hour: int = 3, rate: float = 1.0) -> None:
        state_path.write_text(json.dumps({"g": {
            "emotion": emotion, "intensity": intensity,
            "expires_at": expires, "source": "message",
        }}), encoding="utf-8")
        ns["EMOTION_STATE_PATH"] = str(state_path)
        ns["EMOTION_AUTO"] = True
        ns["EMOTION_GROUPS"] = set()
        ns["EMOTION_NAMES"] = {
            "angry", "happy", "curious", "awkward", "excited",
            "sad", "surprised", "worried", "proud",
        }
        ns["EMOTION_THRESHOLD"] = 3
        ns["EMOTION_THRESHOLD_BY_NAME"] = threshold_by_name if threshold_by_name is not None else {
            "angry": 3, "happy": 2, "curious": 2, "awkward": 2,
            "excited": 2, "sad": 2, "surprised": 2, "worried": 2, "proud": 2,
        }
        ns["EMOTION_MAX_PER_HOUR"] = max_per_hour
        ns["EMOTION_COOLDOWN"] = 1800
        ns["EMOTION_RATE"] = rate
        ns["_emotion_last"] = {}
        ns["_emotion_hits"] = {}

    should = ns["should_emotion_voice"]

    print("① angry 仍然要 3：真发火才出声")
    reset("angry", 3)
    ok, why = should("g", "s", "先别笑，让他说完", now=now, roll=0.0)
    check(ok, "angry(3) 触发：%s" % why)
    reset("angry", 2)
    ok, why = should("g", "s", "先别笑，让他说完", now=now, roll=0.0)
    check(not ok and why == "强度不足", "angry(2) 被拦：%s" % why)

    print("② 上限为 2 的情绪现在能触发（这是修复的核心）")
    for emo in ("happy", "curious", "awkward", "excited", "surprised", "worried", "sad", "proud"):
        reset(emo, 2)
        ok, why = should("g", "s", "这话得念出来才够味", now=now, roll=0.0)
        check(ok, "%s(2) 触发：%s" % (emo, why))

    print("③ 未列出的情绪回落到全局门槛")
    reset("happy", 2, threshold_by_name={"angry": 3})
    ok, why = should("g", "s", "这话得念出来才够味", now=now, roll=0.0)
    check(not ok and why == "强度不足", "happy 回落到全局 3 被拦：%s" % why)
    reset("happy", 3, threshold_by_name={"angry": 3})
    ok, why = should("g", "s", "这话得念出来才够味", now=now, roll=0.0)
    check(ok, "happy(3) 过全局门槛：%s" % why)

    print("④ 概率闸仍然有效（门槛放宽不等于必发）")
    reset("happy", 2, rate=0.5)
    ok, why = should("g", "s", "这话得念出来才够味", now=now, roll=0.9)
    check(not ok and why == "概率未中", "概率未中：%s" % why)

    print("⑤ 小时配额：前 3 次过，第 4 次拦")
    # 时间步长的算术很关键：配额窗口是 3600s，要凑满 3 次就必须让
    # 3 次都落在同一小时里 —— 间隔必须 < 3600/3 = 1200s。
    # 第一版用 2000s 间隔（2000×3=6000s > 3600s），旧记录每次都滑出窗口，
    # 于是永远凑不满、第 4 次照样通过，看起来像"配额失效"，其实是测试算错。
    # 同时步长必须 > EMOTION_COOLDOWN(1800s)？不需要 —— 冷却在这里是 1800s，
    # 所以取 1000s 会撞冷却。故把冷却调到 600s，步长 900s：
    # 3×900=2700s < 3600s（同窗口），900 > 600（不撞冷却）。
    reset("happy", 2, max_per_hour=3)
    ns["EMOTION_COOLDOWN"] = 600
    results = []
    for i in range(4):
        t = now + i * 900
        state_path.write_text(json.dumps({"g": {
            "emotion": "happy", "intensity": 2,
            "expires_at": t + 600, "source": "message",
        }}), encoding="utf-8")
        ok, why = should("g", "s", "这话得念出来才够味", now=t, roll=0.0)
        results.append((t, ok, why))
        if ok:
            ns["_emotion_last"]["s"] = t
            ns["_emotion_hits"].setdefault("s", []).append(t)
    check([r[1] for r in results] == [True, True, True, False],
          "配额命中序列：%s" % [r[1] for r in results])
    check(results[-1][2] == "小时配额满", "第 4 次理由：%s" % results[-1][2])

    print("⑥ 配额是滑动窗口：一小时后再来又能发")
    t = now + 4 * 900 + 3601
    state_path.write_text(json.dumps({"g": {
        "emotion": "happy", "intensity": 2,
        "expires_at": t + 600, "source": "message",
    }}), encoding="utf-8")
    ns["_emotion_last"]["s"] = now + 3 * 900  # 最后一次成功在很久以前
    ok, why = should("g", "s", "这话得念出来才够味", now=t, roll=0.0)
    check(ok, "窗口滑出后可再触发：%s" % why)

    print("⑦ 未启用群 / 关闭总闸 仍然优先拦")
    reset("happy", 2)
    ns["EMOTION_GROUPS"] = {"999"}
    ok, why = should("g", "s", "这话得念出来才够味", now=now, roll=0.0)
    check(not ok and why == "群未启用", "群未启用：%s" % why)
    reset("happy", 2)
    ns["EMOTION_AUTO"] = False
    ok, why = should("g", "s", "这话得念出来才够味", now=now, roll=0.0)
    check(not ok and why == "关闭", "总闸关闭：%s" % why)

    print("⑧ 过期情绪视为平静（不因门槛降低而吃到陈旧状态）")
    reset("happy", 3, expires=now - 1)
    ok, why = should("g", "s", "这话得念出来才够味", now=now, roll=0.0)
    check(not ok and why == "情绪不匹配", "过期不触发：%s" % why)

print()
if FAILED:
    print("FAILED %d 项：" % len(FAILED))
    for item in FAILED:
        print("  -", item)
    raise SystemExit(1)
print("test_emotion_tier: 全部通过")
