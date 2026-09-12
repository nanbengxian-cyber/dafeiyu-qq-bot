"""情绪阈值主动语音纯规则回归：不合成真实音频。"""
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
want = {"_strip_all_leaks", "_extract_arg", "_clean_leaked_call", "_clean_for_tts", "_read_emotion", "should_emotion_voice"}
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        try:
            exec(compile(ast.Module([node], []), str(path), "exec"), ns)
        except (NameError, AttributeError, TypeError):
            pass
    elif isinstance(node, ast.FunctionDef) and node.name in want:
        exec(compile(ast.Module([node], []), str(path), "exec"), ns)

now = 10_000.0
with tempfile.TemporaryDirectory() as tmp:
    state_path = Path(tmp) / "emotion.json"
    ns["EMOTION_STATE_PATH"] = str(state_path)
    ns["EMOTION_AUTO"] = True
    ns["EMOTION_GROUPS"] = set()
    ns["EMOTION_NAMES"] = {"angry", "sad", "excited", "surprised", "worried"}
    ns["EMOTION_THRESHOLD"] = 3
    ns["EMOTION_COOLDOWN"] = 1800
    ns["EMOTION_RATE"] = 0.65
    ns["_emotion_last"] = {}

    def save(emotion, intensity, expires=20_000):
        state_path.write_text(json.dumps({"g": {
            "emotion": emotion, "intensity": intensity, "expires_at": expires
        }}), encoding="utf-8")

    save("excited", 3)
    assert ns["should_emotion_voice"]("g", "s", "太好了，这次真的成了！", now, 0.1)[0]
    save("happy", 3)
    assert not ns["should_emotion_voice"]("g", "s", "今天不错", now, 0.1)[0]
    save("angry", 2)
    assert not ns["should_emotion_voice"]("g", "s", "这也太离谱了", now, 0.1)[0]
    save("angry", 3, expires=9_000)
    assert not ns["should_emotion_voice"]("g", "s", "这也太离谱了", now, 0.1)[0]
    save("worried", 3)
    assert not ns["should_emotion_voice"]("g", "s", "我有点担心", now, 0.9)[0]
    ns["_emotion_last"]["s"] = 9_500
    assert not ns["should_emotion_voice"]("g", "s", "我有点担心", now, 0.1)[0]

print("EMOTION_AUTO_VOICE_TEST_OK")
