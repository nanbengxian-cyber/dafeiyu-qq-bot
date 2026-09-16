#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态检查：UI 类里「声明了却从未赋值」的控件字段。

为什么需要它 —— v1.0.0 的真实事故：
    ConsoleView 里 testBtn / deployBtn / statusLine 三个字段只声明没创建，
    view() 里却直接 testBtn.setOnClickListener(...) → NullPointerException
    → App 一启动就闪退，连界面都出不来。

这类 bug 的特点：
  * 编译期不报错（Java 允许字段为 null）；
  * 单测碰不到（View/Activity 刻意不进测试面）；
  * 只有真机上点开才暴露，而「打开就崩」连日志都不好看。

所以用最笨但可靠的办法：扫源码，找「非基本类型的字段 + 从未被赋值 + 却被 .方法() 调用」。
这不是编译器级的严谨分析，但对本项目这种手搓 UI 的写法足够准 —— 且**零依赖**。

退出码：0 = 干净，1 = 发现问题（打印每个字段的文件/行号/使用处）。
"""

import glob
import os
import re
import sys

SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "app", "src", "com", "dafeiyu", "controller")

# 基本类型/字符串不需要「创建」，漏赋值也不会 NPE。
PRIMITIVES = {"int", "long", "boolean", "float", "double", "short", "byte",
              "char", "String", "Integer", "Long", "Boolean", "Float", "Double"}

DECL_RE = re.compile(
    r"^\s*private\s+(?:static\s+)?(?:final\s+)?"
    r"([A-Za-z_][\w.]*(?:<[^;]*?>)?(?:\[\])?)\s+([a-z_]\w*)\s*;\s*$")


def scan(path):
    """返回该文件里「未赋值却被调用」的字段列表。"""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    problems = []
    for idx, line in enumerate(lines):
        m = DECL_RE.match(line)
        if not m:
            continue
        typ, name = m.group(1), m.group(2)
        base = re.sub(r"<.*>|\[\]", "", typ)
        if base in PRIMITIVES:
            continue

        # 赋值：this.name = ... / name = ...（排除 ==、!=、>=、<=）
        assigned = False
        for j, other in enumerate(lines):
            if j == idx:
                continue
            if re.search(r"(?:this\.)?" + re.escape(name) + r"\s*=(?!=)", other):
                assigned = True
                break
        if assigned:
            continue

        # 被当作对象用：name.xxx(
        uses = [j + 1 for j, other in enumerate(lines)
                if re.search(r"(?<![\w.])" + re.escape(name) + r"\.", other)]
        if uses:
            problems.append((name, typ, idx + 1, uses))
    return problems


def main():
    files = sorted(glob.glob(os.path.join(SRC_DIR, "*.java")))
    if not files:
        print("找不到源码目录：%s" % SRC_DIR, file=sys.stderr)
        return 1

    bad = 0
    for path in files:
        for name, typ, decl, uses in scan(path):
            bad += 1
            print("❌ %s：字段 %s (%s) 在第 %d 行声明，从未赋值，却在第 %s 行被调用"
                  " → 运行期必为 null，启动即崩。"
                  % (os.path.basename(path), name, typ, decl, uses[:4]))
    if bad:
        print("\n共 %d 处。修法：在 view()/onCreate() 里创建并赋值这些控件。" % bad,
              file=sys.stderr)
        return 1
    print("控件字段检查通过：%d 个源文件，没有「声明未赋值却被调用」的字段。" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
