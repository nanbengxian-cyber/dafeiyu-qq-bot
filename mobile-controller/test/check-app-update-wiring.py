#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态检查：「公告 + 检查更新 + 下载安装」的**接线**是否完整。

为什么需要它 —— 这个项目已被同一类坑咬过多次：
    「逻辑单测全绿，但生产代码根本没调用」。
    服务器加了 /app/update、/app/apk；App 里 AppUpdate（解析/判定/sha256）
    有纯逻辑单测。但「连接成功后会不会真的去查」「查到了会不会弹窗」
    「点下载会不会真的下载/校验/调安装器」全是 View/Activity 层的事，
    单测永远照不到。这条检查就是静态扫接线。

四个环节缺一不可（缺哪个用户看到的现象）：
   ① 连接成功触发检查        —— 缺了：用户永远看不到公告和新版提示（=没做）
   ② 弹窗里真的有「下载并安装」 —— 缺了：有更新提示但点了没反应
   ③ 下载后真的校验 sha256     —— 缺了：传坏的 APK 也能装（装个坏包）
   ④ manifest 真的注册了 provider/权限 —— 缺了：安装 Intent 直接抛异常，
                                            系统安装器权限申请被拒
   ⑤ 版本号真的升了           —— 缺了：新包安装后永远「已是最新」，
                                       这个功能自己永远不会再触发

退出码：0 = 接线完整，1 = 有问题。
"""

import os
import re
import sys

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(BASE, "app", "src", "com", "dafeiyu", "controller")


def read(name):
    path = os.path.join(SRC, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def read_manifest():
    path = os.path.join(BASE, "app", "AndroidManifest.xml")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def strip_comments(text):
    """剥 // 和 /* */ 注释（逐字符扫，见 check-weblogin-wiring.py 的说明）。"""
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"' or c == "'":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if text[i] == '\\':
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                out.append(text[i])
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
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

    ma = read("MainActivity.java")
    uf = read("UpdateFlow.java")
    man = read_manifest()

    def ck(name, cond, extra=""):
        nonlocal bad, checked
        checked += 1
        mark = "PASS" if cond else "FAIL"
        print("  %s %s%s" % (mark, name, ("  <- " + extra) if (not cond and extra) else ""))
        if not cond:
            bad += 1

    print("① 连接成功触发检查")
    ck("★ onConnectionChanged 里调了 UpdateFlow.maybeCheck",
       re.search(r"UpdateFlow\.maybeCheck\(", strip_comments(ma)) is not None,
       "连上后根本没查 —— 公告/新版永远不出现，和没做一样")

    print()
    print("② UpdateFlow 内部：查 → 弹 → 下载")
    uf_code = strip_comments(uf)
    ck("★ 调了 /app/update（client.appUpdate()）",
       re.search(r"client\.appUpdate\(", uf_code) is not None,
       "没查服务器 —— 弹窗内容从哪来？")
    ck("★ 弹窗按 hasUpdate/hasAnnouncement 决定（只有公告也弹）",
       re.search(r"hasUpdate\(|hasAnnouncement\(", uf_code) is not None)
    ck("★ 弹窗有「下载并安装」按钮（setPositiveButton）",
       "setPositiveButton" in uf_code,
       "提示了却不能装 —— 用户点了没反应")
    ck("★ 下载走后台线程（别在主线程拉几 MB 文件 → ANR）",
       re.search(r"POOL\.execute", uf_code) is not None or
       re.search(r"new Thread\(|Executors\.", uf_code) is not None)

    print()
    print("③ 下载后必须校验 sha256")
    ck("★ 调了 downloadApk（ManagerClient 拉 /app/apk 到本地文件）",
       re.search(r"\.downloadApk\(", uf_code) is not None,
       "没下载 —— 「下载并安装」按钮是死的")
    ck("★ 校验 sha256（sha256Matches —— 传坏的包必须拦）",
       re.search(r"\.sha256Matches\(", uf_code) is not None,
       "不校验就装 —— 半截 APK 装上才知道是坏的")
    ck("★ 校验失败会删掉半截文件（apk.delete）",
       re.search(r"apk\.delete\(\)", uf_code) is not None,
       "坏的 APK 留在私有目录 —— 下次还会装它")

    print()
    print("④ 安装走系统安装器（content:// + 授权）")
    ck("★ 剧情里真的调了 startActivity(ACTION_VIEW)",
       re.search(r"Intent\.ACTION_VIEW", uf_code) is not None,
       "下载完不装 —— 用户永远停在「下载并安装」")
    ck("★ 用了 content://（Android 7+ 禁 file:// 进 Intent）",
       re.search(r"content://", uf_code) is not None,
       "用 file:// 会抛 FileUriExposedException")
    ck("★ FLAG_GRANT_READ_URI_PERMISSION（不给系统安装器临时读权 → 403）",
       re.search(r"FLAG_GRANT_READ_URI_PERMISSION", uf_code) is not None)

    print()
    print("⑤ manifest 注册 provider / 权限 / 版本号")
    ck("★ 声明 REQUEST_INSTALL_PACKAGES（Android 8+ 装应用必须）",
       re.search(r'REQUEST_INSTALL_PACKAGES', man) is not None,
       "没这权限 Android 8+ 直接拒「安装未知应用」流程")
    ck("★ 注册了 ApkProvider（content://com.dafeiyu.controller.apk）",
       re.search(r'android:name="\.ApkProvider"', man) is not None and
       re.search(r'android:authorities="com\.dafeiyu\.controller\.apk"', man) is not None,
       "provider 没注册 —— content:// 无主，安装 Intent 直接崩")
    ck("★ provider exported=false（只给本 App 显式授权过的读）",
       re.search(r'android:exported="false"[\s\S]{0,200}?ApkProvider'
                 r'|ApkProvider[\s\S]{0,200}?android:exported="false"', man) is not None or
       re.search(r'android:exported="false"[\s\S]{0,200}?grantUriPermissions', man) is not None,
       "exported 要 false：别把文件目录开给别的 App")
    ck("★ grantUriPermissions=true（临时授权才能传出去）",
       re.search(r'android:grantUriPermissions="true"', man) is not None)
    # versionCode 升到 2 才是「新版本」—— 不然装完还是「已是最新」。
    mc = re.search(r'android:versionCode="(\d+)"', man)
    mv = re.search(r'android:versionName="([\d.]+)"', man)
    ck("★ versionCode 升到 2（服务器 latest_code=2 才有能比的新版）",
       mc is not None and int(mc.group(1)) >= 2, mc and mc.group(0))
    ck("★ versionName 是 1.2.0",
       mv is not None and mv.group(1) == "1.2.0", mv and mv.group(0))

    print()
    if bad:
        print("共 %d 处接线问题。注意：纯逻辑单测（AppUpdateTest）发现不了这些 ——"
              " 逻辑对了但没接上，用户看到的现象和没修一模一样。" % bad)
        return 1
    print("公告/更新接线检查通过：%d 项，从「连接成功」到「系统安装器」全程接上。"
          % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())