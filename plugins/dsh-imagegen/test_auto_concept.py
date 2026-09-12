"""主动概念生图纯规则回归：只摘 AST，不访问真实生图接口。"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

path = Path(__file__).with_name("main.py")
src = path.read_text(encoding="utf-8")
tree = ast.parse(src)
ns = {"os": os, "re": re}
want = {"detect_visual_concept", "_concept_prompt"}
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        try:
            exec(compile(ast.Module([node], []), str(path), "exec"), ns)
        except (NameError, AttributeError, TypeError):
            pass
    elif isinstance(node, ast.FunctionDef) and node.name in want:
        exec(compile(ast.Module([node], []), str(path), "exec"), ns)

detect = ns["detect_visual_concept"]
prompt = ns["_concept_prompt"]

positive = [
    "我想设计一个角色，蓝色长发，穿着白色裙子，身后有鲸鱼尾巴",
    "这个制品整体做成透明材质，里面有蓝色发光的机械结构",
    "画面主体站在海边，背景是金色夕阳，整体是可爱的二次元风格",
    "设想一间房间，白色墙面，蓝色灯光，桌子旁边放着机械鲸鱼",
]
negative = [
    "这个模型很可爱但是接口速度有点慢",
    "我想做一个主动回复机制，情绪达到阈值就触发语音",
    "帮我生成一个部署方案和配置文档",
    "不要生图，我只是描述一下这个角色的背景故事",
    "为什么这个人物穿着蓝色衣服？",
    "来个猫娘",
    "生成一段动画，背景是海边",
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
if "概念草图" not in p or positive[0] not in p:
    fails.append("prompt 未保留描述或缺少确认语义")

if fails:
    print("\n".join("FAIL " + x for x in fails))
    sys.exit(1)
print("AUTO_CONCEPT_TEST_OK: %d positive + %d negative" % (len(positive), len(negative)))
