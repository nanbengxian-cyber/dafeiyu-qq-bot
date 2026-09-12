#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重载单个 AstrBot 插件（走 dashboard 的 /api/plugin/reload），不重启容器。

用法：sudo python3 /opt/qqbot/observe/reload_plugin.py dsh-vischain
凭据从 /opt/qqbot/astrbot/data/cmd_config.json 读，绝不打印。
"""
import json
import sys
import urllib.error
import urllib.request

CFG = "/opt/qqbot/astrbot/data/cmd_config.json"


def _post(url, payload, token=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode("utf-8", "replace")
        return r.status, json.loads(body) if body.strip() else {}


def main(name):
    cfg = json.load(open(CFG, encoding="utf-8"))
    dash = cfg.get("dashboard", {})
    port = int(dash.get("port") or 6185)
    base = "http://127.0.0.1:%d" % port
    user, pwd = dash.get("username"), dash.get("password")
    if not user or not pwd:
        print("没有可用的 dashboard 用户名/密码，改走 docker restart")
        return 2
    try:
        st, res = _post(base + "/api/auth/login", {"username": user, "password": pwd})
    except urllib.error.HTTPError as e:
        print("登录失败 HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200]))
        return 2
    token = (res.get("data") or {}).get("token") or res.get("token")
    if not token:
        print("登录返回里没有 token：%s" % str(res)[:200])
        return 2
    try:
        st, res = _post(base + "/api/plugin/reload", {"name": name}, token)
    except urllib.error.HTTPError as e:
        print("重载失败 HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        return 3
    print("HTTP %s %s" % (st, json.dumps(res, ensure_ascii=False)[:300]))
    return 0 if res.get("status") in ("ok", "success", None) else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "dsh-vischain"))
