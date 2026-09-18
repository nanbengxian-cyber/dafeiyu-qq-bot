# -*- coding: utf-8 -*-
"""静态检查：「虚拟屏」有没有被接上、关屏有没有真的把资源放掉。

背景（2026-09-18 用户要求）：
  短信 / 二维码 / 密码这些验证界面原来常驻内存，用户希望它们
  **点开才造、关掉就放**，平时零占用。于是有了 VirtualScreen：
    build() 现造视图树 → onEnter() 可见后起轮询 → dispose() 放掉一切。
  由 ScreenHost 托管，同一时刻最多一块屏存活。

为什么这条必须单独静态检查（而不是单测）：
  VirtualScreen / ScreenHost / QrScreen / PwScreen 全都 import android.*，
  按 run-tests.sh 的口径它们**不进单测面**（View 类只画界面、点事件）。
  而这个功能有两类「单测永远照不到」的坏法：

  ① **接不上**：LoginView 里改回直接 drawQrCode / 弹个旧的常驻面板，
     屏幕照样能用，只是又变回「常驻占内存」—— 用户看到的现象和没做一样。
     这个项目已经因为「逻辑对了但没接上」踩过三次（网页自动登录、停止按钮、
     消息通道），所以这里也按同样的办法静态扫。

  ② **关屏不放**：dispose 忘了 recycle 位图 / 忘了 shutdownNow 线程池，
     或者密码明文没抹掉就把输入框引用置空。这种情况**完全没有任何外部症状**：
     二维码少一张、线程池多一个，谁都不会发现，直到内存慢慢涨。
     尤其密码：关屏后明文还留在 EditText 里，是纯隐私问题。
     所以「dispose 里到底放了什么」必须逐条盯住，还要盯**顺序**。

  ③ 关屏后不恢复状态轮询：中枢的轮询在开屏时停了（怕和虚拟屏抢），
     关屏回调里必须把它恢复，否则用户关掉屏会觉得「界面卡住了」。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = "app/src/com/dafeiyu/controller"
fails = []


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as fh:
        return fh.read()


def strip_comments(s):
    """去掉 // 和 /* */ 注释（手写扫描，不用正则 —— 正则会把
    "http://..." 里的 // 当注释，既误报又漏报）。"""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            out.append(c); i += 1
            while i < n:
                if s[i] == "\\":
                    out.append(s[i:i + 2]); i += 2; continue
                out.append(s[i])
                if s[i] == '"':
                    i += 1; break
                i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            while i < n and s[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i += 2
            while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c); i += 1
    return "".join(out)


def ck(name, cond, extra=""):
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  <- " + str(extra)) if not cond and extra else ""))
    if not cond:
        fails.append(name)


def method_body(src, sig):
    """切出某个方法体（到下一个方法定义为止）。

    不用 `\\n    }` 收尾：方法里嵌匿名类/lambda 时会有更深的 `}`，
    先出现的那个 `\n    }` 其实是对的（方法体缩进 4），但为了稳，
    用「下一个同缩进的方法定义」作结束更可靠。
    """
    if sig not in src:
        return ""
    tail = src.split(sig, 1)[1]
    m = re.search(r"\n    (?:private|public|protected|static|@Override)", tail)
    return tail[:m.start()] if m else tail


vs = strip_comments(read(SRC + "/VirtualScreen.java"))
sh = strip_comments(read(SRC + "/ScreenHost.java"))
qr = strip_comments(read(SRC + "/QrScreen.java"))
pw = strip_comments(read(SRC + "/PwScreen.java"))
ws = strip_comments(read(SRC + "/WebScreen.java"))
ma = strip_comments(read(SRC + "/MainActivity.java"))
lv = strip_comments(read(SRC + "/LoginView.java"))

print("① VirtualScreen 的生命周期契约完整（少一个就没法「用完即弃」）")
for sig in ("String title()", "View build(Context ctx)", "void onEnter()",
            "boolean onBack()", "void dispose()"):
    ck("契约里有 %s" % sig, sig in vs)

print()
print("② ScreenHost：一次只活一块，且按顺序 现造→挂载→可见→onEnter")
openb = method_body(sh, "public void open(VirtualScreen screen)")
ck("切出了 open() 方法体", len(openb) > 200, len(openb))
ck("★ 开新屏前先关掉旧屏（保证同一时刻只活一块）",
   re.search(r"if \(current != null\)\s*\{\s*close\(\);", openb) is not None)
# ★ 顺序很重要：onEnter 是「启动轮询/发首个请求」的时机。
#   在视图还没可见时调它，会出现「屏还没显示就已经在轮询」；
#   而 build 之后没 addView 就 VISIBLE，用户看到的是一片空白。
i_build = openb.find("screen.build(ctx)")
i_add = openb.find("body.addView(")
i_vis = openb.find("View.VISIBLE")
i_enter = openb.find("screen.onEnter()")
ck("★ 顺序是 build → addView → VISIBLE → onEnter",
   -1 < i_build < i_add < i_vis < i_enter,
   "build=%d addView=%d VISIBLE=%d onEnter=%d" % (i_build, i_add, i_vis, i_enter))
ck("★ open() 把现造的视图挂到 body（body.addView —— 漏了屏就是空白）",
   re.search(r"body\.addView\(content", openb) is not None)

# 挂载链的第二环：open() 只能往 body 里挂，body 本身要在**构造函数**里
# 就挂到覆盖层 overlay 上（overlay.addView(col)）。漏了这句的话 open()
# 一切照跑、onEnter 照发、万事无报错，但整棵屏树不存在于窗口 ——
# 用户看到的和没做一样（这个项目已经因此踩过三次）。
ctor = method_body(sh, "public ScreenHost(Context ctx)")
ck("切出了 ScreenHost 构造函数体", len(ctor) > 150, len(ctor))
ck("★ 覆盖层装着承载 body 的 col（overlay.addView(col)）",
   re.search(r"overlay\.addView\(col\)", ctor) is not None)

closeb = method_body(sh, "public void close()")
ck("切出了 close() 方法体", len(closeb) > 150, len(closeb))
ck("★ 关屏先 dispose()（放资源）", "s.dispose()" in closeb)
ck("★ 再 removeAllViews()（放视图树 → 位图/EditText 可被回收）",
   "body.removeAllViews()" in closeb)
ck("★ 最后隐藏覆盖层（GONE，平时零占用）", "View.GONE" in closeb)
ck("★ current 置空（不留下已关屏的实例）",
   re.search(r"current = null;", closeb) is not None)
ck("★ dispose 抛异常也要继续清视图（否则屏关不掉、资源全泄漏）",
   "catch (Throwable" in closeb)

print()
print("③ 关屏后必须恢复中枢的状态轮询（不然用户觉得「界面卡住了」）")
ck("★ MainActivity 设了 onCloseListener",
   re.search(r"screenHost\.setOnCloseListener\(", ma) is not None)
ck("★ 回调里恢复了登录中枢（loginView.onShow）",
   re.search(r"setOnCloseListener\(new Runnable\(\)\s*\{.*?loginView\.onShow\(\)",
             ma, re.S) is not None)

print()
print("④ 接线：登录界面真的打开了这三块屏（不是死代码）")
ck("★ 二维码登录打开 QrScreen",
   re.search(r"openScreen\(new QrScreen\(", lv) is not None)
ck("★ 密码登录打开 PwScreen",
   re.search(r"openScreen\(new PwScreen\(", lv) is not None)
ck("★ 短信/网页验证打开 WebScreen（三大验证统一成虚拟屏，不是 Activity）",
   re.search(r"openScreen\(new WebScreen\(", lv) is not None)
ck("★ 登录界面的 host 接口暴露了 openScreen/closeScreen",
   re.search(r"void openScreen\(VirtualScreen screen\);", lv) is not None and
   re.search(r"void closeScreen\(\);", lv) is not None)
ck("★ MainActivity 把 openScreen 接到了 ScreenHost",
   re.search(r"public void openScreen\(VirtualScreen screen\)\s*\{\s*screenHost\.open\(screen\)",
             ma) is not None)
ck("★ MainActivity 把 closeScreen 接到了 ScreenHost",
   re.search(r"public void closeScreen\(\)\s*\{\s*screenHost\.close\(\)", ma) is not None)
# 反例：旧的常驻画法必须已经不在登录页里了（否则等于没改）
ck("★ 登录页里不再自己塞二维码位图（应交给 QrScreen）",
   not re.search(r"setImageBitmap\(", lv),
   "LoginView 里还有 setImageBitmap —— 二维码又变回常驻面板了")
# 反例：网页验证不再是独立 Activity（用户 2026-09-19 要求三大验证统一，
# 之前「短信/网页验证」会跳出一整页网页 —— 那就是用户看到的现象）
ck("★ WebLoginActivity 已移除（网页验证不再是「之前的网页」）",
   os.path.exists(SRC + "/WebLoginActivity.java") is False,
   "WebLoginActivity.java 还在 —— 短信/网页验证又会跳出独立网页")

print()
print("⑤ 覆盖层要真的盖在上面，且返回键先给虚拟屏")
ck("★ 外层套了 FrameLayout",
   re.search(r"(?:android\.widget\.)?FrameLayout outer = "
             r"new (?:android\.widget\.)?FrameLayout\(this\)", ma) is not None)
# 顺序：主界面先加、覆盖层后加 —— 后加的在上层。反了的话虚拟屏被压在底下，
# 用户点「二维码登录」看到的还是原界面（现象和没修一样）。
i_root = ma.find("outer.addView(root,")
i_overlay = ma.find("outer.addView(screenHost.view()")
ck("★ 主界面先加、虚拟屏覆盖层后加（后加的在上面）",
   0 <= i_root < i_overlay, "root=%d overlay=%d" % (i_root, i_overlay))
ck("★ setContentView 用的是套好的 outer",
   re.search(r"setContentView\(outer\)", ma) is not None)
ck("★ 返回键先交给虚拟屏消化（WebView 可能要 goBack）",
   re.search(r"screenHost\.onBack\(\)", ma) is not None)

print()
print("⑥ 用完即弃：dispose 里到底放了什么（这里漏了完全没有外部症状）")
qrd = method_body(qr, "public void dispose()")
ck("切出了 QrScreen.dispose 方法体", len(qrd) > 100, len(qrd))
ck("★ 停轮询（polling = false）", re.search(r"polling = false;", qrd) is not None)
ck("★ 关线程池（shutdownNow）", "shutdownNow()" in qrd)
ck("★ 摘掉 ImageView 上的位图", "setImageBitmap(null)" in qrd)
ck("★ recycle 位图（不回收就等 GC，二维码反复开就是反复涨）",
   re.search(r"\.recycle\(\)", qrd) is not None)
ck("★ recycle 前判 isRecycled（重复 recycle 会抛异常）",
   "isRecycled()" in qrd)
ck("★ 字段置空（不吊着已回收的位图）", "currentQr = null;" in qrd)

pwd = method_body(pw, "public void dispose()")
ck("切出了 PwScreen.dispose 方法体", len(pwd) > 80, len(pwd))
ck("★ 关线程池（shutdownNow）", "shutdownNow()" in pwd)
# ★ 顺序：必须先把明文抹掉，再丢引用。
#   反了的话 setText 会打在 null 上（NPE），或者干脆忘了抹 ——
#   密码明文留在 EditText 里，是纯隐私问题，而且没有任何外部症状。
i_clear = pwd.find('qqPassword.setText("")')
i_null = pwd.find("qqPassword = null;")
ck("★ 关屏抹掉密码明文（setText(\"\")）", i_clear >= 0)
ck("★ 抹明文在丢引用之前（反了会 NPE 或忘了抹）",
   0 <= i_clear < i_null, "setText=%d 置空=%d" % (i_clear, i_null))

wsd = method_body(ws, "public void dispose()")
ck("切出了 WebScreen.dispose 方法体", len(wsd) > 80, len(wsd))
ck("★ WebView 必须 destroy（否则渲染线程/native 资源常驻 = 同二维码位图那类泄漏）",
   "webView.destroy()" in wsd)
ck("★ destroy 前先 stopLoading（不然关屏时可能还在加载大页面）",
   "webView.stopLoading()" in wsd)
ck("★ destroy 后字段置空（不吊着已销毁的 WebView → 防二次 destroy 崩）",
   "webView = null;" in wsd)
# ★ WebScreen.onEnter 才加载，build 不 loadUrl —— 否则「屏还没显示就在请求」
wse = method_body(ws, "public void onEnter()")
ck("切出了 WebScreen.onEnter 方法体", len(wse) > 30, len(wse))
ck("★ 加载在 onEnter（视图可见后）而不是 build 里",
   re.search(r"loaded = true;\s*webView\.loadUrl", wse) is not None)

print()
print("FAILS: %d" % len(fails))
for f in fails:
    print("  !! %s" % f)
if fails:
    print()
    print("这条坏了的表现：虚拟屏要么根本没接上（登录页还是原来那套常驻面板，")
    print("用户看到的现象和没做一样），要么关屏时没把位图/线程池/密码明文放掉 ——")
    print("后者没有任何报错，只会让内存慢慢涨、密码留在输入框里。")
sys.exit(1 if fails else 0)