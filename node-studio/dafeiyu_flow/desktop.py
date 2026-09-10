from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Optional


def _bundle_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root) if frozen_root else Path(__file__).resolve().parent.parent


def _configure_resources() -> None:
    from . import server
    root = _bundle_root()
    server.ROOT = root
    server.STATIC = root / "dafeiyu_flow" / "static"
    server.GRAPHS = root / "graphs"


def _bind_server(host: str, requested: int):
    from .server import Handler, SafeThreadingHTTPServer
    candidates = [requested] if requested == 0 else [requested, 0]
    last_error = None  # type: Optional[OSError]
    for port in candidates:
        try:
            return SafeThreadingHTTPServer((host, port), Handler)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError("无法绑定本地端口")


def _open_browser_when_ready(url: str, server, attempts: int = 50) -> None:
    for _ in range(attempts):
        if getattr(server, "_BaseServer__shutdown_request", False):
            return
        try:
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=0.2):
                webbrowser.open(url, new=1)
                return
        except OSError:
            threading.Event().wait(0.1)
    print("浏览器未自动打开，请手动访问：%s" % url)


def main() -> int:
    parser = argparse.ArgumentParser(description="大肥鱼行为节点 Studio（离线版）")
    parser.add_argument("--port", type=int, default=8765, help="本地端口；占用时自动回退随机端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    _configure_resources()
    host = "127.0.0.1"
    try:
        httpd = _bind_server(host, args.port)
    except OSError as exc:
        print("启动失败：无法使用本地回环端口。")
        print(str(exc))
        return 2
    url = "http://%s:%d" % (host, httpd.server_port)
    if args.port not in (0, httpd.server_port):
        print("端口 %d 已占用，已自动改用 %d。" % (args.port, httpd.server_port))
    print("大肥鱼行为节点 Studio 已启动：%s" % url)
    print("临时离线编辑；刷新或退出会丢失修改。不连接 QQ、AstrBot 或生产环境。")
    browser_thread = None
    if not args.no_browser:
        browser_thread = threading.Thread(target=_open_browser_when_ready, args=(url, httpd), daemon=True)
        browser_thread.start()
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
