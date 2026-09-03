#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把控制台路由打进 qrweb_auth.py —— 四处精确插入，命中次数必须恰好 1。

为什么用补丁脚本而不是直接给一份新文件：qrweb_auth.py 是「掉线时看二维码」这条
最要紧链路上的东西，我不想凭记忆重写它的鉴权、退避锁定和 _drain 防泄漏逻辑。
补丁只加东西不改原逻辑，每处都断言唯一命中，改不动就整个中止 ——
宁可什么都没发生，也不要产出一个半成品的鉴权服务。

用法：python3 patch_qrweb.py <qrweb_auth.py 路径>
      已经打过补丁会直接退出（幂等），不会打第二遍。
"""

import re
import sys

MARK = "# ---- 控制台后端（可选挂载）----"

IMPORT_ANCHOR = """SCRYPT_MAXMEM = 64 * 1024 * 1024
"""

IMPORT_BLOCK = """SCRYPT_MAXMEM = 64 * 1024 * 1024

# 控制台请求体上限。配置项都是几十字节的小 JSON，16 KB 绰绰有余；
# 定上限是为了防有人拿超大 body 把这台 1.9G 内存的机器顶爆。
CONSOLE_MAX_BODY = 16 * 1024

# ---- 控制台后端（可选挂载）----
# 故意 try/except：控制台是**附加**功能，它导入失败绝不能连带扫码页一起挂掉 ——
# QQ 掉线时能打开这一页扫码是这台服务器最要紧的事，控制台次要得多。
# 失败原因存下来，请求 /api/console/* 时原样告诉手机，别让人对着 503 猜。
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import console_api

    CONSOLE_ERR = ""
except Exception as _exc:  # noqa: BLE001
    console_api = None
    CONSOLE_ERR = "%s: %s" % (type(_exc).__name__, _exc)
"""

APK_ANCHOR = """    "/proxy.json": ("proxy.json", "application/json; charset=utf-8"),
}
"""

APK_BLOCK = """    "/proxy.json": ("proxy.json", "application/json; charset=utf-8"),
    # 控制台 APK 的下载口。手机浏览器登录过这一页就能直接装，
    # 不用把包塞进手机文件系统（sshfs 挂载经常是断的），也不用第三方网盘。
    "/console.apk": ("dafeiyu-console.apk",
                     "application/vnd.android.package-archive"),
}
"""

GET_ANCHOR = """        if not self._authed():
            return self._redirect("/login")
        entry = ALLOWED.get(path)
"""

GET_BLOCK = """        if path.startswith("/api/console/"):
            return self._console("GET", path, urlparse(self.path).query)
        if not self._authed():
            return self._redirect("/login")
        entry = ALLOWED.get(path)
"""

SEND_ANCHOR = """        return self._send(200, body, ctype)
"""

SEND_BLOCK = """        extra = None
        if path == "/console.apk":
            # 不给 Content-Disposition，安卓浏览器会把它当页面渲染出一屏乱码。
            extra = [("Content-Disposition",
                      'attachment; filename="dafeiyu-console.apk"')]
        return self._send(200, body, ctype, extra)
"""

POST_ANCHOR = """    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/login":
            self._drain()
            return self._send(404, b"not found", "text/plain; charset=utf-8")
"""

POST_BLOCK = """    def do_POST(self) -> None:  # noqa: N802
        _path = urlparse(self.path).path
        if _path.startswith("/api/console/"):
            return self._console("POST", _path, urlparse(self.path).query)
        if _path != "/login":
            self._drain()
            return self._send(404, b"not found", "text/plain; charset=utf-8")
"""

METHOD_ANCHOR = """    def _drain(self) -> None:
"""

METHOD_BLOCK = '''    # ---- 控制台 ----

    def _console(self, method: str, path: str, raw_query: str) -> None:
        """/api/console/* 的入口。

        和页面路由刻意不同的三点，都是为了让 APK 好写：
          ① 未登录回 **401 JSON**，不是 303 跳登录页 —— APK 跟着跳会拿到一页
             HTML 还以为成功了；
          ② 出错也回 JSON（{"error": ...}），手机上能直接把原因显示出来；
          ③ 任何异常都兜住转成 500 JSON，绝不让连接裸崩 —— 那样 APK 侧只看得到
             「连接被重置」，等于没有任何线索。
        """
        if console_api is None:
            return self._json(503, {"error": "控制台后端没装载：%s" % CONSOLE_ERR})
        if not self._authed():
            return self._json(401, {"error": "未登录"})

        body = None
        if method == "POST":
            try:
                n = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                n = 0
            if n > CONSOLE_MAX_BODY:
                self._drain()
                return self._json(413, {"error": "请求体太大"})
            raw = self.rfile.read(n) if n > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, ValueError):
                return self._json(400, {"error": "请求体不是合法 JSON"})

        query = {k: v[0] for k, v in parse_qs(raw_query).items()}
        try:
            code, data = console_api.handle(method, path, query, body)
        except console_api.ApiError as exc:
            return self._json(exc.code, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001
            # 日志只记类型和消息，不记请求体 —— 和 log_error 同一个理由：
            # 请求体里可能带着刚提交的配置，将来万一带了别的东西就泄漏了。
            self.log_message("控制台异常 %s: %s", type(exc).__name__, exc)
            return self._json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
        return self._json(code, data)

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._send(code, body, "application/json; charset=utf-8")

    def _drain(self) -> None:
'''

PATCHES = [
    ("导入控制台后端", IMPORT_ANCHOR, IMPORT_BLOCK),
    ("APK 下载白名单", APK_ANCHOR, APK_BLOCK),
    ("do_GET 路由", GET_ANCHOR, GET_BLOCK),
    ("APK 下载响应头", SEND_ANCHOR, SEND_BLOCK),
    ("do_POST 路由", POST_ANCHOR, POST_BLOCK),
    ("_console 方法", METHOD_ANCHOR, METHOD_BLOCK),
]


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: patch_qrweb.py <qrweb_auth.py>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    src = open(path, encoding="utf-8").read()

    if MARK in src:
        print("已经打过补丁，什么都不做")
        return 0

    for name, anchor, block in PATCHES:
        hits = src.count(anchor)
        if hits != 1:
            print("*** %s 的锚点命中 %d 次（要求恰好 1），中止 ***" % (name, hits),
                  file=sys.stderr)
            return 1
        src = src.replace(anchor, block, 1)
        print("  ✓ %s" % name)

    # 补丁只许加行不许删行：行数必须严格变多，且原有内容全在
    open(path, "w", encoding="utf-8").write(src)
    print("已写入 %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
