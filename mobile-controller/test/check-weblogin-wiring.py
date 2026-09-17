#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态检查：网页自动登录的**接线**是否完整。

为什么需要它 —— 这类 bug 刚踩过一次，代价很大：
    修「APK 不显示二维码」时，`WebProxyPath` 的逻辑单测全绿，
    但生产代码根本没调用它（四个 NapCat 接口没剥 data 外壳），
    而当时的单测**自己剥了一层**，把 bug 掩掉了。
    → 教训：纯逻辑测过了，不代表生产代码**真的用了**它。

这次的同类风险：`WebProxyPath.withToken()` 有 10 项单测，
但真正决定「用户会不会看到请输入token」的是三个 android 类里的接线：
    LoginView        —— 取口令，并把它传出去
    MainActivity     —— 把口令塞进 Intent
    WebLoginActivity —— 从 Intent 取出口令，拼进要加载的地址

这三处任意一处漏掉，单测照样全绿，而用户看到的现象和没修一模一样。
View/Activity 刻意不进单测面（见 run-tests.sh 的注释），所以只能用静态扫。

退出码：0 = 接线完整，1 = 有问题。
"""

import os
import re
import sys

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(BASE, "app", "src", "com", "dafeiyu", "controller")

# (文件, 说明, 必须出现的片段)
CHECKS = [
    ("LoginView.java", "取实例的 WebUI 口令（带解锁口令，否则私密机器人拿到空串）",
     [r"webuiToken\s*\(\s*inst\s*,\s*pw\s*\)"]),

    ("LoginView.java", "把口令交给 openWebLogin（取到不用 = 白取）",
     [r"openWebLogin\s*\([^)]*useTok\s*\)"]),

    ("LoginView.java", "取口令走后台线程（别在主线程发网络请求）",
     [r"pool\.execute"]),

    ("MainActivity.java", "把口令塞进 Intent",
     [r"putExtra\s*\(\s*WebLoginActivity\.EXTRA_WEBUI_TOKEN"]),

    ("WebLoginActivity.java", "声明 EXTRA_WEBUI_TOKEN",
     [r'EXTRA_WEBUI_TOKEN\s*=\s*"webui_token"']),

    ("WebLoginActivity.java", "从 Intent 取出口令",
     [r"getStringExtra\s*\(\s*EXTRA_WEBUI_TOKEN\s*\)"]),

    ("WebLoginActivity.java", "★ 拼进要加载的地址（漏了这步 = 用户照样看到请输入token）",
     [r"withToken\s*\("]),

    ("WebLoginActivity.java", "加载地址确实用了 withToken 的返回值",
     [r"loadUrl\s*\(\s*viaProxy\s*\?\s*WebProxyPath\.withToken\s*\("]),
]

# 反向检查：不能把口令写进查询串之外的日志/持久化
FORBIDDEN = [
    ("LoginView.java", r"store\.\w*[Ss]ave\w*\([^)]*[Tt]oken", "口令不能被持久化"),
    ("WebLoginActivity.java", r"android\.util\.Log\.\w+\([^)]*webuiToken", "口令不能进日志"),
]


def read(name):
    path = os.path.join(SRC, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def strip_comments(text):
    """去掉注释再匹配 —— 否则「注释里写了」会被当成「代码里做了」。

    这条很关键：修复说明里必然会出现 withToken(...) 这样的字样，
    不剥注释的话，把真正的调用删掉、只留注释，检查照样通过。

    ★ 必须逐字符扫描，不能用正则去 //：
      代码里有 `"http://127.0.0.1"` 这种字符串字面量，里面的 `//`
      不是注释。用正则一刀切会把**真实代码**也删掉 ——
      结果是「代码明明写对了却报缺失」（假失败），
      更糟的是反向检查（FORBIDDEN）会假通过。
      第一版就是这么写的，被自己抓到了。
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        # 字符串字面量：整段照抄，里面的 // 和 /* 都不算注释
        if c == '"' or c == "'":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if text[i] == '\\':          # 转义：连下一个字符一起抄
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                out.append(text[i])
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        # 块注释
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        # 行注释
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        out.append(c)
        i += 1
    return "".join(out)


def main():
    bad = 0
    checked = 0

    for name, why, patterns in CHECKS:
        src = read(name)
        if src is None:
            print("❌ 找不到源码：%s" % name, file=sys.stderr)
            bad += 1
            continue
        code = strip_comments(src)
        for pat in patterns:
            checked += 1
            if not re.search(pat, code):
                print("❌ %s：%s\n     没找到：%s" % (name, why, pat))
                bad += 1

    for name, pat, why in FORBIDDEN:
        src = read(name)
        if src is None:
            continue
        if re.search(pat, strip_comments(src)):
            print("❌ %s：%s（匹配到 %s）" % (name, why, pat))
            bad += 1

    if bad:
        print("\n共 %d 处接线问题。注意：纯逻辑单测发现不了这些 ——"
              " 逻辑对了但没被调用，用户看到的现象和没修一模一样。"
              % bad, file=sys.stderr)
        return 1

    print("网页自动登录接线检查通过：%d 项，口令从「取」到「用」全程接上。"
          % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
