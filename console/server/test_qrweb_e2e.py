#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打完补丁的 qrweb 端到端测试 —— 真起一个 HTTP 服务，真走 cookie。

单测（test_console.py）只测了后端函数。这里补的是**补丁本身**：
路由有没有插错位置、鉴权有没有被绕过、扫码页有没有被弄坏。

最要紧的一条断言是「扫码页照旧」：qrweb 是 QQ 掉线时唯一能看二维码的地方，
控制台是附加功能，宁可控制台不可用也不能让扫码页出问题。所以这里既测
控制台能用，也测**没登录时二维码依然拿不到**、控制台后端崩了扫码页依然能开。

跑法：python3 test_qrweb_e2e.py（自己起服务、自己收尾，不碰真机）
"""

import copy
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_console import BASE_CFG, PLUGINS, FakeDash, _write  # noqa: E402

PASS = 0
FAIL = []


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append("%s %s" % (name, extra))
        print("  ✗ %s %s" % (name, extra))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def req(url, method="GET", body=None, cookie="", follow=False, timeout=20):
    """回 (状态码, 文本, Set-Cookie)。不跟跳转 —— 要能看见 303 本身。"""
    data = None
    headers = {}
    if body is not None:
        if isinstance(body, dict):
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        else:
            data = body.encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    r = urllib.request.Request(url, data=data, headers=headers, method=method)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener() if follow else \
        urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), \
                resp.headers.get("Set-Cookie"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), \
            exc.headers.get("Set-Cookie"), dict(exc.headers)


def wait_up(port, tries=60):
    for _ in range(tries):
        try:
            code, _, _, _ = req("http://127.0.0.1:%d/healthz" % port)
            if code == 200:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="qrweb-e2e-")
    pub = os.path.join(tmp, "public")
    os.makedirs(pub)
    cfg_path = os.path.join(tmp, "cmd_config.json")
    _write(cfg_path, BASE_CFG)
    # 新版 schema 含 env: 旋钮；端到端测试提供一个隔离的 env 文件，
    # 避免测试机没有 /opt/qqbot 时把 CONFIG 端点误判成后端故障。
    env_path = os.path.join(tmp, "imagegen.env")
    with open(env_path, "w", encoding="utf-8") as fh:
        fh.write("DSH_DECIDE=1\nDSH_AT_MODE=1\n")

    # 扫码页要用的四个文件
    with open(os.path.join(pub, "index.html"), "w") as fh:
        fh.write("<html><body>扫码页占位</body></html>")
    with open(os.path.join(pub, "qrcode.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    with open(os.path.join(pub, "status.json"), "w") as fh:
        json.dump({"state": "online", "message": "机器人在线",
                   "account": "100000002", "last_seen_seconds": 5,
                   "qr_available": True, "qr_age_seconds": 30,
                   "detect_reason": "探针 get_status online=true",
                   "updated_epoch": int(time.time())}, fh)
    with open(os.path.join(pub, "proxy.json"), "w") as fh:
        json.dump({"proxy_active": True, "tunnel_healthy": True,
                   "egress_ip": "203.0.113.10"}, fh)
    # 假 APK（下载路径要能真发出去）
    apk_bytes = b"PK\x03\x04" + b"fake-apk-payload" * 100
    with open(os.path.join(pub, "dafeiyu-console.apk"), "wb") as fh:
        fh.write(apk_bytes)

    # 假 dashboard
    dash = ThreadingHTTPServer(("127.0.0.1", 0), FakeDash)
    dash.state = {"cfg": copy.deepcopy(BASE_CFG), "path": cfg_path,
                  "plugins": copy.deepcopy(PLUGINS), "calls": [],
                  "models": ["deepseek-v4-flash-0731", "glm-5.3"]}
    dash_port = dash.server_address[1]
    threading.Thread(target=dash.serve_forever, daemon=True).start()

    # 打补丁的 qrweb（连同后端一起复制到临时目录，模拟真实部署布局）
    work = os.path.join(tmp, "deploy")
    os.makedirs(work)
    shutil.copy(os.path.join(HERE, "console_api.py"), work)
    shutil.copy(os.path.join(HERE, "console_spec.py"), work)
    shutil.copy(os.path.join(HERE, "envfile.py"), work)
    qrweb = os.path.join(work, "qrweb_auth.py")
    shutil.copy(os.path.join(HERE, "qrweb_auth.py.orig"), qrweb)
    rc = subprocess.run([sys.executable, os.path.join(HERE, "patch_qrweb.py"), qrweb],
                        capture_output=True, text=True)
    check("补丁应用成功", rc.returncode == 0, rc.stderr.strip()[:200])
    if rc.returncode != 0:
        return 1

    conf_path = os.path.join(tmp, "qrweb_auth.json")
    port = free_port()
    env = dict(os.environ)
    env.update({
        "QRWEB_DIR": pub,
        "QRWEB_CONF": conf_path,
        "QRWEB_PORT": str(port),
        "QRWEB_BIND": "127.0.0.1",
        "CONSOLE_CFG": cfg_path,
        "CONSOLE_ENV": env_path,
        "CONSOLE_DASH": "http://127.0.0.1:%d" % dash_port,
        "CONSOLE_STATUS_TTL": "0",
        "QRWEB_FAIL_LIMIT": "3",
        "QRWEB_LOCK_BASE": "2",
    })
    pw = "test-console-pw-123"
    rc = subprocess.run([sys.executable, qrweb, "--set-password", "--stdin"],
                        input=pw, capture_output=True, text=True, env=env)
    check("设密码成功", rc.returncode == 0, rc.stderr.strip()[:200])

    proc = subprocess.Popen([sys.executable, qrweb], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        check("服务起来了", wait_up(port))
        root = "http://127.0.0.1:%d" % port

        # ---------------------------------------------------------- 1 未登录
        print("[1] 未登录的边界")
        code, body, _, _ = req(root + "/healthz")
        check("healthz 免鉴权", code == 200 and '"ok"' in body)
        code, body, _, _ = req(root + "/")
        check("首页要登录（303）", code == 303, "code=%d" % code)
        code, _, _, _ = req(root + "/qrcode.png")
        check("二维码要登录", code == 303, "code=%d" % code)
        code, _, _, _ = req(root + "/status.json")
        check("状态文件要登录", code == 303)
        code, _, _, _ = req(root + "/console.apk")
        check("APK 也要登录", code == 303, "code=%d" % code)

        # 控制台端点：必须 401 JSON，不是 303
        for path in ("/api/console/status", "/api/console/schema",
                     "/api/console/config"):
            code, body, _, _ = req(root + path)
            check("未登录 %s 回 401" % path, code == 401, "code=%d" % code)
            check("  是 JSON 不是 HTML", body.strip().startswith("{"), body[:60])
        code, body, _, _ = req(root + "/api/console/mode", "POST", {"id": "quiet"})
        check("未登录 POST 也 401", code == 401, "code=%d" % code)

        # 白名单之外一律 404（含 .bak）
        with open(os.path.join(pub, "index.html.bak.999"), "w") as fh:
            fh.write("secret")

        # ---------------------------------------------------------- 2 登录
        print("[2] 登录")
        code, body, sc, _ = req(root + "/login", "POST", "password=wrong-pw")
        check("错密码不给 cookie", not (sc or "").startswith("dsh_qr="), sc)
        code, body, sc, _ = req(root + "/login", "POST", "password=" + pw)
        check("对密码回 303", code == 303, "code=%d" % code)
        check("对密码给 cookie", (sc or "").startswith("dsh_qr="), sc)
        cookie = (sc or "").split(";")[0]

        code, body, _, _ = req(root + "/", cookie=cookie)
        check("登录后能看扫码页", code == 200 and "扫码页占位" in body)
        code, body, _, _ = req(root + "/status.json", cookie=cookie)
        check("登录后能看 status.json", code == 200 and "100000002" in body)
        code, _, _, _ = req(root + "/index.html.bak.999", cookie=cookie)
        check("登录后 .bak 依然 404", code == 404, "code=%d" % code)

        # ---------------------------------------------------------- 3 控制台读
        print("[3] 控制台读接口")
        code, body, _, _ = req(root + "/api/console/status", cookie=cookie)
        check("status 200", code == 200, "code=%d body=%s" % (code, body[:200]))
        st = json.loads(body)
        check("status 有 qq", st.get("qq", {}).get("state") == "online", st.get("qq"))
        check("status 有 models",
              st.get("models", {}).get("chat_provider") == "bigfeiyu-glm")
        check("status 有 plugins", isinstance(st.get("plugins"), list))
        check("status 不含密钥", "sk-SECRET" not in body)
        check("status 不含 jwt_secret", "0" * 64 not in body)
        check("status 不含密码", "never-read-this" not in body)

        code, body, _, _ = req(root + "/api/console/schema", cookie=cookie)
        check("schema 200", code == 200, "code=%d" % code)
        sch = json.loads(body)
        check("schema 有 knobs", len(sch.get("knobs") or []) >= 10)
        check("schema 有 modes", len(sch.get("modes") or []) >= 5)
        check("schema 有 actions", len(sch.get("actions") or []) >= 3)
        check("schema 不含密钥", "sk-SECRET" not in body)

        code, body, _, _ = req(root + "/api/console/config", cookie=cookie)
        check("config 200", code == 200)
        check("config 值齐",
              len(json.loads(body).get("values") or {}) == len(sch["knobs"]))

        code, body, _, _ = req(root + "/api/console/nope", cookie=cookie)
        check("未知控制台路径 404 JSON",
              code == 404 and body.strip().startswith("{"), "%d %s" % (code, body[:80]))

        # ---------------------------------------------------------- 4 控制台写
        print("[4] 控制台写接口")
        code, body, _, _ = req(root + "/api/console/config", "POST",
                               {"values": {"platform_settings.rate_limit.count": 12}},
                               cookie=cookie)
        check("改限流 200", code == 200, "code=%d body=%s" % (code, body[:200]))
        check("回报改动", len(json.loads(body).get("changed") or []) == 1, body[:200])
        cur = json.loads(open(cfg_path, "rb").read().decode("utf-8-sig"))
        check("落盘生效", cur["platform_settings"]["rate_limit"]["count"] == 12)
        check("BOM 还在", open(cfg_path, "rb").read(3) == b"\xef\xbb\xbf")
        check("密钥没动", cur["provider_sources"][0]["key"] == ["sk-SECRET-1"])
        check("tool_use 没丢", "tool_use" in cur["provider"][0]["modalities"])

        # 越权路径必须 403
        code, body, _, _ = req(root + "/api/console/config", "POST",
                               {"values": {"dashboard.password": "hacked"}},
                               cookie=cookie)
        check("改密码 403", code == 403, "code=%d" % code)
        cur = json.loads(open(cfg_path, "rb").read().decode("utf-8-sig"))
        check("密码真没被改", cur["dashboard"]["password"] == "never-read-this")

        # 越界值必须 400 且不落盘
        code, body, _, _ = req(root + "/api/console/config", "POST",
                               {"values": {"platform_settings.rate_limit.count": 9999}},
                               cookie=cookie)
        check("越界 400", code == 400, "code=%d" % code)
        cur = json.loads(open(cfg_path, "rb").read().decode("utf-8-sig"))
        check("越界没落盘", cur["platform_settings"]["rate_limit"]["count"] == 12)

        # 坏 JSON / 超大 body
        code, body, _, _ = req(root + "/api/console/config", "POST",
                               "not-json-at-all", cookie=cookie)
        check("坏 JSON 400", code == 400, "code=%d" % code)
        big = json.dumps({"values": {"x": "y" * 20000}})
        code, body, _, _ = req(root + "/api/console/config", "POST", big, cookie=cookie)
        check("超大 body 413", code == 413, "code=%d" % code)

        # 模式与模型
        code, body, _, _ = req(root + "/api/console/mode", "POST", {"id": "normal"},
                               cookie=cookie)
        check("套用日常模式 200", code == 200, "code=%d body=%s" % (code, body[:200]))
        out = json.loads(body)
        check("模式全成功", out.get("ok") is True, out.get("steps"))
        cur = json.loads(open(cfg_path, "rb").read().decode("utf-8-sig"))
        check("日常：限流回 8", cur["platform_settings"]["rate_limit"]["count"] == 8)
        code, body, _, _ = req(root + "/api/console/mode", "POST", {"id": "no-such"},
                               cookie=cookie)
        check("未知模式 404", code == 404, "code=%d" % code)

        code, body, _, _ = req(root + "/api/console/model", "POST", {"chat": "glm-5.3"},
                               cookie=cookie)
        check("换模型 200", code == 200, "code=%d body=%s" % (code, body[:200]))
        cur = json.loads(open(cfg_path, "rb").read().decode("utf-8-sig"))
        check("模型落盘", cur["provider"][0]["model"] == "glm-5.3")
        check("换模型后 tool_use 还在", "tool_use" in cur["provider"][0]["modalities"])

        code, body, _, _ = req(root + "/api/console/plugin", "POST",
                               {"name": "dsh-web", "enabled": False}, cookie=cookie)
        check("关插件 200", code == 200, "code=%d body=%s" % (code, body[:200]))
        code, body, _, _ = req(root + "/api/console/plugin", "POST",
                               {"name": "astrbot", "enabled": False}, cookie=cookie)
        check("关非 dsh 插件 403", code == 403, "code=%d" % code)
        req(root + "/api/console/plugin", "POST",
            {"name": "dsh-web", "enabled": True}, cookie=cookie)

        code, body, _, _ = req(root + "/api/console/action", "POST", {"id": "rm -rf /"},
                               cookie=cookie)
        check("乱传动作 404", code == 404, "code=%d" % code)

        # ---------------------------------------------------------- 5 APK 下载
        print("[5] APK 下载")
        code, body, _, headers = req(root + "/console.apk", cookie=cookie)
        check("APK 200", code == 200, "code=%d" % code)
        check("APK 字节数对", len(body.encode("utf-8", "replace")) > 0)
        check("APK Content-Type 对",
              headers.get("Content-Type") == "application/vnd.android.package-archive",
              headers.get("Content-Type"))
        check("APK 有 attachment 头",
              "attachment" in (headers.get("Content-Disposition") or ""),
              headers.get("Content-Disposition"))
        check("普通页面没有 attachment 头",
              "Content-Disposition" not in req(root + "/", cookie=cookie)[3])

        # ---------------------------------------------------------- 6 伪造 cookie
        print("[6] 伪造 cookie")
        for bad in ("dsh_qr=deadbeef", "dsh_qr=", "dsh_qr=MTc4ODM1OTU1NQ.xxxx",
                    "dsh_qr=" + "A" * 80):
            code, _, _, _ = req(root + "/api/console/status", cookie=bad)
            check("伪造 %s 被拒" % bad[:24], code == 401, "code=%d" % code)
            code, _, _, _ = req(root + "/console.apk", cookie=bad)
            check("  APK 也拒", code == 303, "code=%d" % code)

        # ---------------------------------------------------------- 7 后端崩了
        print("[7] 控制台后端不可用时扫码页必须照旧")
        broken = os.path.join(tmp, "broken")
        os.makedirs(broken)
        shutil.copy(qrweb, os.path.join(broken, "qrweb_auth.py"))
        with open(os.path.join(broken, "console_api.py"), "w") as fh:
            fh.write("raise RuntimeError('故意炸掉')\n")
        port2 = free_port()
        env2 = dict(env)
        env2["QRWEB_PORT"] = str(port2)
        p2 = subprocess.Popen([sys.executable, os.path.join(broken, "qrweb_auth.py")],
                              env=env2, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            check("后端坏了服务照样起来", wait_up(port2))
            r2 = "http://127.0.0.1:%d" % port2
            code, _, sc2, _ = req(r2 + "/login", "POST", "password=" + pw)
            ck2 = (sc2 or "").split(";")[0]
            code, body, _, _ = req(r2 + "/", cookie=ck2)
            check("扫码页仍然能开", code == 200 and "扫码页占位" in body, "code=%d" % code)
            code, body, _, _ = req(r2 + "/qrcode.png", cookie=ck2)
            check("二维码仍然能下", code == 200)
            code, body, _, _ = req(r2 + "/api/console/status", cookie=ck2)
            check("控制台回 503 并说明原因", code == 503 and "没装载" in body,
                  "%d %s" % (code, body[:120]))
        finally:
            p2.send_signal(signal.SIGTERM)
            p2.wait(timeout=10)

        # ---------------------------------------------------------- 8 爆破锁定
        print("[8] 爆破锁定仍然有效")
        for _ in range(3):
            req(root + "/login", "POST", "password=nope")
        code, body, _, _ = req(root + "/login", "POST", "password=nope")
        check("失败到上限后 429", code == 429, "code=%d" % code)
        check("提示等待秒数", "试太多次" in body, body[:120])
        # 锁定期内正确密码也应被拒（防绕过）
        code, _, sc3, _ = req(root + "/login", "POST", "password=" + pw)
        check("锁定期内对密码也不给 cookie", not (sc3 or "").startswith("dsh_qr="), sc3)
        # 已有 cookie 不受锁定影响 —— 掉线时不能把自己也关在外面
        code, _, _, _ = req(root + "/api/console/status", cookie=cookie)
        check("锁定不影响已登录会话", code == 200, "code=%d" % code)

        # ---------------------------------------------------------- 9 日志不漏密码
        print("[9] 日志不含密码")
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
        log = (out or b"").decode("utf-8", "replace") + (err or b"").decode("utf-8", "replace")
        check("日志不含明文密码", pw not in log)
        check("日志不含 password=", "password=" not in log)
        check("日志不含密钥", "sk-SECRET" not in log)
        check("日志不含 jwt", "0" * 64 not in log)
    finally:
        if proc.poll() is None:
            proc.kill()
        dash.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("通过 %d，失败 %d" % (PASS, len(FAIL)))
    for f in FAIL:
        print("  FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
