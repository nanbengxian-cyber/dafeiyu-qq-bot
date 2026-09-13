# -*- coding: utf-8 -*-
"""把真实语料里机器人说过的每一句话过一遍输出侧判据，人工看有没有误伤。

用法：python3 sweep_corpus.py <replies.txt> [more.txt ...]
文件每行：时间戳<TAB>正文，或 时间戳<TAB>昵称/QQ号: 正文
"""

import importlib.util
import re
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "selfworth", Path(__file__).with_name("main.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
dev = module.self_devalue

_PREFIX = re.compile(r"^[^/:\t]{1,24}/\d+:\s*")


def texts(path):
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if "\t" not in line:
            continue
        body = line.split("\t", 1)[1]
        body = _PREFIX.sub("", body).strip()
        if body:
            yield body


total = 0
hits = []
for p in sys.argv[1:]:
    for t in texts(p):
        total += 1
        k = dev(t)
        if k:
            hits.append((k, t))

print("语料 %d 条，输出侧命中 %d 条（需要人工确认有没有嘴硬被误伤）\n" % (total, len(hits)))
for k, t in hits:
    print("%-9s %s" % (k, t[:70]))
