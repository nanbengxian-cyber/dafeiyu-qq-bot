"""生图触发意图回归：验证明确创作才放行，普通聊天与已有图片操作不放行。"""
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
want = {"has_explicit_creation_intent"}

for node in tree.body:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        try:
            exec(compile(ast.Module([node], []), str(path), "exec"), ns)
        except (NameError, AttributeError, TypeError, ValueError):
            pass
    elif isinstance(node, ast.FunctionDef) and node.name in want:
        exec(compile(ast.Module([node], []), str(path), "exec"), ns)

has_intent = ns["has_explicit_creation_intent"]

positive = [
    "给我画一张戴墨镜的橘猫",
    "生成一个蓝发猫娘",
    "把你的理想型画出来",
    "帮我制作一张鲸鱼壁纸",
    "画懒羊羊",
    "P一张赛博朋克头像",
]

negative = [
    "能不能给大肥鱼搞个塔菲音色",
    "长什么样啊？具体说一下",
    "你去发一张图问大肥鱼就知道了",
    "我要发美图了，你要是敢踢我就等死吧",
    "这张图片挺好看的",
    "看看你的品味怎么样",
    "来个猫娘",
    "弄个表格统计一下",
    "生成一段动画，背景是海边",
    "我想设计一个角色，蓝色长发，穿着白裙子",
    "/画图 一只橘猫",
]

fails = []
for text in positive:
    if not has_intent(text):
        fails.append("应放行：" + text)
for text in negative:
    if has_intent(text):
        fails.append("应拦截：" + text)

if fails:
    print("\n".join("FAIL " + item for item in fails))
    sys.exit(1)
print("TRIGGER_INTENT_TEST_OK: %d positive + %d negative" % (len(positive), len(negative)))
