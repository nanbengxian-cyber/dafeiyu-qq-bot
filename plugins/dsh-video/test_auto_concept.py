# -*- coding: utf-8 -*-
"""主动动态概念短片判据回归；不访问真实出片接口。"""
from __future__ import annotations

import ast
import os
import random
import re
import sys
from pathlib import Path

path = Path(__file__).with_name("main.py")
src = path.read_text(encoding="utf-8")
tree = ast.parse(src)
ns = {"os": os, "random": random, "re": re}
want = {"detect_motion_concept", "_concept_prompt"}
for _ in range(10):
    progressed = False
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            try:
                exec(compile(ast.Module([node], []), str(path), "exec"), ns)
                progressed = True
            except (NameError, AttributeError, TypeError):
                pass
        elif isinstance(node, ast.FunctionDef) and node.name in want and node.name not in ns:
            try:
                exec(compile(ast.Module([node], []), str(path), "exec"), ns)
                progressed = True
            except NameError:
                pass
    if all(name in ns for name in want):
        break
    if not progressed:
        break

detect = ns["detect_motion_concept"]
prompt = ns["_concept_prompt"]

positive = [
    "我想设计一个片段，蓝色机械鲸鱼从海里跃起，水花慢慢落下，镜头跟着它推进",
    "设想一个场景，白衣少女站在金色夕阳下，突然回头挥手，然后向远处跑去",
    "这个镜头主体是一台发光机甲，它先慢慢展开翅膀，再飞向夜空",
    "画面里可爱的机器人在城市夜景中旋转跳舞，随后灯光逐渐熄灭",
]
negative = [
    "我想设计一个角色，蓝色长发，穿着白色裙子，身后有鲸鱼尾巴",
    "我想做一个主动回复机制，情绪达到阈值就触发语音",
    "不要做视频，我只是描述一下这个故事",
    "帮我生成一个赛博朋克城市夜景的视频",
    "帮我看看这个视频里讲了什么 https://b23.tv/abc123",
    "这个头像是白发少女站在海边，背景有夕阳",
    "为什么这个人物突然回头挥手？",
]

fails = []
for text in positive:
    ok, why = detect(text)
    if not ok:
        fails.append("应触发：%s（%s）" % (text, why))
for text in negative:
    ok, why = detect(text)
    if ok:
        fails.append("不应触发：%s（%s）" % (text, why))

p = prompt(positive[0])
if "概念短片" not in p or positive[0] not in p:
    fails.append("prompt 未保留动态描述或缺少确认语义")

if fails:
    print("\n".join("FAIL " + x for x in fails))
    sys.exit(1)
print("AUTO_VIDEO_CONCEPT_TEST_OK: %d positive + %d negative" % (len(positive), len(negative)))
