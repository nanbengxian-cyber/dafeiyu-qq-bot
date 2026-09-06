#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制台后端单测 —— 不碰真服务器，用假 dashboard + 假配置跑。

为什么要单测：这套东西唯一的写入通道是「GET 全量配置 → 改几个字段 → POST 回去」，
写错一个路径就可能把 45 个顶层字段里的某个搞没了。所以每条断言都盯着两件事：
  ① 该改的改了；② **不该动的一个字节都没动**（尤其 provider_sources[].key、
     provider[].modalities 里的 tool_use、platform[]）。

跑法：python3 test_console.py
失败即非零退出，构建脚本靠这个 gate。
"""

import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---------------------------------------------------------------- 假配置
# 形状照真机来：UTF-8 BOM、key 在 provider_sources、model 在 provider、
# modalities 带 tool_use。

BASE_CFG = {
    "dashboard": {
        "username": "astrbot",
        "password": "never-read-this",
        "jwt_secret": "0" * 64,
    },
    "provider_settings": {
        "default_provider_id": "bigfeiyu-glm",
        "default_image_caption_provider_id": "vision-opus5",
        "default_personality": "qun-by-and-ai",
        "max_context_length": 40,
        "dequeue_context_length": 1,
        "identifier": True,
        "web_search": False,
        "show_tool_use_status": False,
        "show_tool_call_result": False,
        "max_agent_step": 30,
    },
    "provider_ltm_settings": {
        "active_reply": {"enable": True, "method": "possibility_reply",
                         "possibility_reply": 0.25, "whitelist": []},
    },
    "platform_settings": {
        "rate_limit": {"time": 60, "count": 8, "strategy": "discard"},
        "reply_with_mention": False,
        "reply_with_quote": False,
        "ignore_at_all": False,
        "segmented_reply": {"enable": False, "interval": "1.5,3.5"},
    },
    "provider_sources": [
        {"id": "bigfeiyu-glm_source", "api_base": "https://x/v1", "key": ["sk-SECRET-1"]},
        {"id": "zhipu-vision_source", "api_base": "https://y/v4", "key": ["sk-SECRET-2"]},
    ],
    "provider": [
        {"id": "bigfeiyu-glm", "model": "deepseek-v4-flash-0731",
         "modalities": ["text", "tool_use"], "enable": True},
        {"id": "zhipu-vision", "model": "glm-4.6v-flash",
         "modalities": ["text", "image"], "enable": True},
        {"id": "vision-opus5", "model": "claude-opus-5",
         "modalities": ["text", "image"], "enable": True},
    ],
    "platform": [{"id": "aiocqhttp", "enable": True, "type": "aiocqhttp"}],
}

PLUGINS = [
    {"name": "dsh-imagegen", "activated": True, "version": "1.0.0"},
    {"name": "dsh-voice", "activated": True, "version": "1.0.0"},
    {"name": "dsh-video", "activated": True, "version": "1.0.0"},
    {"name": "dsh-web", "activated": True, "version": "1.0.0"},
    {"name": "dsh-mention", "activated": True, "version": "1.0.0"},
    {"name": "builtin_commands", "activated": True, "version": "1.0.0"},
]


class FakeDash(BaseHTTPRequestHandler):
    """假 AstrBot dashboard。行为照真机实测的来：
    · /api/config/get 回 {"data":{"config": 完整配置}}
    · /api/config/astrbot/update 整份替换后落盘（真机就是这样）
    · /api/config/provider/update 只换 provider[] 里那一条
    · 无 Bearer 一律 401（验证我们真的在签 token）
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        return (self.headers.get("Authorization") or "").startswith("Bearer eyJ")

    def do_GET(self):
        if not self._auth_ok():
            return self._send(401, {"status": "error", "message": "no token"})
        st = self.server.state
        st["calls"].append(("GET", self.path))
        if self.path == "/api/plugin/get":
            return self._send(200, {"status": "ok", "data": st["plugins"]})
        if self.path.startswith("/api/config/provider/model_list"):
            return self._send(200, {"status": "ok", "data": {"models": st["models"]}})
        if self.path == "/api/config/get":
            return self._send(200, {"status": "ok",
                                    "data": {"config": copy.deepcopy(st["cfg"])}})
        return self._send(404, {"status": "error", "message": "nope"})

    def do_POST(self):
        if not self._auth_ok():
            return self._send(401, {"status": "error", "message": "no token"})
        n = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(n) or b"{}")
        st = self.server.state
        st["calls"].append(("POST", self.path))
        if st.get("fail_next"):
            st["fail_next"] = False
            return self._send(200, {"status": "error", "message": "假的失败"})
        if self.path == "/api/config/astrbot/update":
            st["cfg"] = copy.deepcopy(body["config"])
            _write(st["path"], st["cfg"])
            return self._send(200, {"status": "ok", "message": "保存成功~"})
        if self.path == "/api/config/provider/update":
            pid, new = body["id"], body["config"]
            for i, p in enumerate(st["cfg"]["provider"]):
                if p.get("id") == pid:
                    st["cfg"]["provider"][i] = copy.deepcopy(new)
                    break
            else:
                return self._send(200, {"status": "error", "message": "no provider"})
            _write(st["path"], st["cfg"])
            return self._send(200, {"status": "ok", "message": "更新成功，已经实时生效~"})
        if self.path in ("/api/plugin/on", "/api/plugin/off"):
            want = self.path.endswith("/on")
            for p in st["plugins"]:
                if p["name"] == body.get("name"):
                    p["activated"] = want
                    return self._send(200, {"status": "ok", "message": "ok"})
            return self._send(200, {"status": "error", "message": "no plugin"})
        return self._send(404, {"status": "error", "message": "nope"})


def _write(path, cfg):
    """带 BOM 写回，和真机一致。"""
    with open(path, "wb") as fh:
        fh.write(b"\xef\xbb\xbf" + json.dumps(cfg, ensure_ascii=False, indent=2).encode())


PASS = 0
FAIL = []


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append("%s %s" % (name, extra))
        print("  ✗ %s %s" % (name, extra))


def main():
    tmp = tempfile.mkdtemp(prefix="console-test-")
    cfg_path = os.path.join(tmp, "cmd_config.json")
    _write(cfg_path, BASE_CFG)
    pub = os.path.join(tmp, "public")
    os.makedirs(pub)
    with open(os.path.join(pub, "status.json"), "w") as fh:
        json.dump({"state": "online", "message": "机器人在线", "account": "100000002",
                   "detect_reason": "探针 get_status online=true",
                   "last_seen_seconds": 12, "qr_available": True,
                   "qr_age_seconds": 100, "updated_epoch": int(time.time())}, fh)
    with open(os.path.join(pub, "proxy.json"), "w") as fh:
        json.dump({"proxy_active": True, "tunnel_healthy": True,
                   "egress_ip": "203.0.113.10"}, fh)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeDash)
    srv.state = {"cfg": copy.deepcopy(BASE_CFG), "path": cfg_path,
                 "plugins": copy.deepcopy(PLUGINS), "calls": [],
                 "models": ["deepseek-v4-flash-0731", "glm-5.3", "glm-5.3-flash"]}
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    os.environ["CONSOLE_CFG"] = cfg_path
    os.environ["CONSOLE_DASH"] = "http://127.0.0.1:%d" % port
    os.environ["QRWEB_DIR"] = pub
    os.environ["CONSOLE_STATUS_TTL"] = "0"

    import console_api as api

    # ---------------------------------------------------------- 1 读
    print("[1] 读取")
    cfg, md5 = api.read_cfg()
    check("BOM 能读", cfg["provider_settings"]["max_context_length"] == 40)
    check("md5 稳定", md5 == api.cfg_fingerprint())
    check("dig 深路径", api.dig(cfg, "platform_settings.rate_limit.count") == 8)
    check("dig 缺失回 default", api.dig(cfg, "a.b.c", "X") == "X")

    schema = api.build_schema()
    check("schema 有 knobs", len(schema["knobs"]) >= 10)
    check("schema 有 modes", len(schema["modes"]) >= 5)
    check("schema 模型列表", "glm-5.3" in schema["chat_models"])
    check("schema 识图渠道 2 个",
          {v["id"] for v in schema["vision_providers"]} == {"zhipu-vision", "vision-opus5"},
          schema["vision_providers"])
    conf = api.build_config()
    check("config 值齐", len(conf["values"]) == len(schema["knobs"]))
    check("config 读到概率", conf["values"]["provider_ltm_settings.active_reply.possibility_reply"] == 0.25)

    # ---------------------------------------------------------- 2 校验
    print("[2] 入参校验")
    for bad, why in [
        ({"provider_ltm_settings.active_reply.possibility_reply": 1.5}, "概率>1"),
        ({"provider_ltm_settings.active_reply.possibility_reply": -0.1}, "概率<0"),
        ({"platform_settings.rate_limit.count": 0}, "限流 0"),
        ({"platform_settings.rate_limit.count": 999}, "限流超上限"),
        ({"provider_settings.max_context_length": 1}, "轮数太小"),
        ({"provider_settings.identifier": "yes"}, "bool 收到字符串"),
        ({"provider_settings.max_agent_step": True}, "int 收到 bool"),
        ({"dashboard.password": "x"}, "改密码"),
        ({"admins_id": ["1"]}, "改管理员"),
        ({"platform_settings.reply_with_mention": True}, "改全局@开关"),
        ({"provider_sources.0.key": "x"}, "改密钥"),
    ]:
        try:
            api.apply_knobs(bad)
            check("拒绝 " + why, False, "居然通过了")
        except api.ApiError as exc:
            check("拒绝 " + why, True)
            if why in ("改密码", "改管理员", "改全局@开关", "改密钥"):
                check("  " + why + " 报 403", exc.code == 403, "code=%d" % exc.code)

    before = api.cfg_fingerprint()
    check("非法入参没落盘", before == md5)

    # ---------------------------------------------------------- 3 写
    print("[3] 写配置")
    changed = api.apply_knobs({
        "provider_ltm_settings.active_reply.possibility_reply": 0.5,
        "platform_settings.rate_limit.count": 16,
    })
    check("回报 2 条改动", len(changed) == 2, changed)
    after, _ = api.read_cfg()
    check("概率写进去", after["provider_ltm_settings"]["active_reply"]["possibility_reply"] == 0.5)
    check("限流写进去", after["platform_settings"]["rate_limit"]["count"] == 16)
    check("BOM 还在", open(cfg_path, "rb").read(3) == b"\xef\xbb\xbf")
    check("密钥没动", after["provider_sources"][0]["key"] == ["sk-SECRET-1"])
    check("tool_use 没丢", "tool_use" in after["provider"][0]["modalities"])
    check("platform 没动", after["platform"] == BASE_CFG["platform"])
    check("顶层字段数没变", len(after) == len(BASE_CFG), (len(after), len(BASE_CFG)))
    check("密码没动", after["dashboard"]["password"] == "never-read-this")

    check("同值不写", api.apply_knobs({"platform_settings.rate_limit.count": 16}) == [])
    check("空 dict 不写", api.apply_knobs({}) == [])

    # ---------------------------------------------------------- 4 模型
    print("[4] 模型")
    res = api.set_chat_model("glm-5.3")
    check("换模型成功", res["changed"] and res["model"] == "glm-5.3", res)
    after, _ = api.read_cfg()
    check("落盘是 glm-5.3", after["provider"][0]["model"] == "glm-5.3")
    check("换模型后 tool_use 还在", "tool_use" in after["provider"][0]["modalities"])
    check("换模型没碰其他 provider", after["provider"][1]["model"] == "glm-4.6v-flash")
    check("同模型不重复写", api.set_chat_model("glm-5.3")["changed"] is False)
    try:
        api.set_chat_model("")
        check("空模型名被拒", False)
    except api.ApiError:
        check("空模型名被拒", True)

    # tool_use 被吃掉时必须炸 —— 这是空头承诺的唯一根因，绝不能静默
    srv.state["cfg"]["provider"][0]["modalities"] = ["text"]
    _write(cfg_path, srv.state["cfg"])
    try:
        api.set_chat_model("deepseek-v4-flash-0731")
        check("tool_use 丢失必须报错", False, "居然静默通过")
    except api.ApiError as exc:
        check("tool_use 丢失必须报错", "tool_use" in exc.message, exc.message)
    srv.state["cfg"]["provider"][0]["modalities"] = ["text", "tool_use"]
    _write(cfg_path, srv.state["cfg"])

    res = api.set_vision_provider("zhipu-vision")
    check("换识图成功", res["changed"], res)
    after, _ = api.read_cfg()
    check("识图落盘", after["provider_settings"]["default_image_caption_provider_id"] == "zhipu-vision")
    try:
        api.set_vision_provider("bigfeiyu-glm")   # 它没有 image 能力
        check("非识图渠道被拒", False)
    except api.ApiError:
        check("非识图渠道被拒", True)

    # ---------------------------------------------------------- 5 插件
    print("[5] 插件")
    check("关插件", api.set_plugin("dsh-web", False)["enabled"] is False)
    check("插件状态已变", [p for p in srv.state["plugins"] if p["name"] == "dsh-web"][0]["activated"] is False)
    check("开插件", api.set_plugin("dsh-web", True)["enabled"] is True)
    for bad in ("astrbot", "builtin_commands", "../etc/passwd", ""):
        try:
            api.set_plugin(bad, False)
            check("拒绝非 dsh 插件 %r" % bad, False)
        except api.ApiError:
            check("拒绝非 dsh 插件 %r" % bad, True)
    try:
        api.set_plugin("dsh-nonexistent", False)
        check("拒绝不存在的插件", False)
    except api.ApiError as exc:
        check("拒绝不存在的插件", exc.code == 404, exc.code)

    # ---------------------------------------------------------- 6 模式
    print("[6] 一键模式")
    out = api.apply_mode("normal")
    check("日常模式全成功", out["ok"], out["steps"])
    after, _ = api.read_cfg()
    check("日常：概率回 0.25", after["provider_ltm_settings"]["active_reply"]["possibility_reply"] == 0.25)
    check("日常：限流回 8", after["platform_settings"]["rate_limit"]["count"] == 8)
    check("日常：模型回 flash", after["provider"][0]["model"] == "deepseek-v4-flash-0731")
    check("日常：识图回 opus", after["provider_settings"]["default_image_caption_provider_id"] == "vision-opus5")
    check("日常模式能被反推", api.current_mode(after) == "normal", api.current_mode(after))

    out = api.apply_mode("quiet")
    check("安静模式成功", out["ok"], out["steps"])
    after, _ = api.read_cfg()
    check("安静：主动插话关了", after["provider_ltm_settings"]["active_reply"]["enable"] is False)
    check("安静模式能被反推", api.current_mode(after) == "quiet", api.current_mode(after))

    out = api.apply_mode("mute")
    check("闭嘴模式成功", out["ok"], out["steps"])
    check("闭嘴：出图关了",
          [p for p in srv.state["plugins"] if p["name"] == "dsh-imagegen"][0]["activated"] is False)
    check("闭嘴：4 个插件都关了",
          all(not p["activated"] for p in srv.state["plugins"]
              if p["name"] in ("dsh-imagegen", "dsh-voice", "dsh-video", "dsh-web")))

    out = api.apply_mode("media_on")
    check("全媒体模式成功", out["ok"], out["steps"])
    check("全媒体：4 个插件都开了",
          all(p["activated"] for p in srv.state["plugins"]
              if p["name"] in ("dsh-imagegen", "dsh-voice", "dsh-video", "dsh-web")))

    for mid in ("cheap", "strong", "lively"):
        out = api.apply_mode(mid)
        check("模式 %s 成功" % mid, out["ok"], out["steps"])
        after, _ = api.read_cfg()
        check("模式 %s 后 tool_use 在" % mid, "tool_use" in after["provider"][0]["modalities"])
        check("模式 %s 后密钥在" % mid, after["provider_sources"][0]["key"] == ["sk-SECRET-1"])
    try:
        api.apply_mode("no-such-mode")
        check("未知模式被拒", False)
    except api.ApiError as exc:
        check("未知模式被拒", exc.code == 404)

    # 某一段失败要单独回报，不能整体谎报成功
    api.apply_mode("normal")
    srv.state["fail_next"] = True
    out = api.apply_mode("lively")
    check("部分失败时 ok=False", out["ok"] is False, out)
    check("失败步骤带 error", any(not s.get("ok") and s.get("error") for s in out["steps"]), out["steps"])

    # ---------------------------------------------------------- 7 状态
    print("[7] 状态")
    st = api.build_status(force=True)
    check("状态有 QQ", st["qq"]["state"] == "online")
    check("看门狗不算过期", st["qq"]["watchdog_stale"] is False)
    check("状态有模型", st["models"]["chat_provider"] == "bigfeiyu-glm")
    check("状态有 tool_use 标志", st["runtime"]["tool_use_ok"] is True)
    check("状态有插件表", isinstance(st["plugins"], list) and st["plugins"])
    check("插件有中文名", any(p["label"] == "出图" for p in st["plugins"]))
    check("插件表过滤内置", all(p["name"].startswith("dsh-") for p in st["plugins"]))
    check("有 host 信息", st["host"].get("mem_total"))
    check("状态可 JSON 序列化", bool(json.dumps(st, ensure_ascii=False)))
    check("状态不含密钥", "sk-SECRET" not in json.dumps(st, ensure_ascii=False))
    check("状态不含 jwt", "0" * 64 not in json.dumps(st, ensure_ascii=False))
    check("状态不含密码", "never-read-this" not in json.dumps(st, ensure_ascii=False))

    # 看门狗停摆要能识别出来
    with open(os.path.join(pub, "status.json"), "w") as fh:
        json.dump({"state": "online", "updated_epoch": int(time.time()) - 600}, fh)
    st = api.build_status(force=True)
    check("看门狗停摆被标记", st["qq"]["watchdog_stale"] is True)

    # public 文件缺失不能让整页崩
    os.remove(os.path.join(pub, "status.json"))
    st = api.build_status(force=True)
    check("status.json 缺失也能出状态", st["qq"]["state"] is None and "containers" in st)

    # ---------------------------------------------------------- 8 路由
    print("[8] 路由")
    code, _ = api.handle("GET", "/api/console/schema", {}, None)
    check("GET schema 200", code == 200)
    code, _ = api.handle("GET", "/api/console/nope", {}, None)
    check("未知路径 404", code == 404)
    code, _ = api.handle("PUT", "/api/console/config", {}, None)
    check("PUT 405", code == 405)
    try:
        api.handle("POST", "/api/console/config", {}, {"values": "notdict"})
        check("values 非 dict 被拒", False)
    except api.ApiError:
        check("values 非 dict 被拒", True)
    try:
        api.handle("POST", "/api/console/action", {}, {"id": "rm -rf /"})
        check("乱传动作被拒", False)
    except api.ApiError as exc:
        check("乱传动作被拒", exc.code == 404)
    for aid in ("clear_context", "", None, "restart_host"):
        try:
            api.run_action(aid)
            check("动作 %r 被拒" % aid, False)
        except api.ApiError as exc:
            check("动作 %r 被拒" % aid, exc.code == 404)

    # ---------------------------------------------------------- 9 并发
    print("[9] 并发保护")
    # 模拟「读到配置后，别人改了文件」：apply_knobs 必须 409 而不是覆盖
    orig_get = api.dash_call

    def racing(path, body=None, timeout=60):
        out = orig_get(path, body, timeout)
        if path == "/api/config/get":
            side = copy.deepcopy(srv.state["cfg"])
            side["provider_settings"]["identifier"] = not side["provider_settings"]["identifier"]
            _write(cfg_path, side)      # 有人在这瞬间改了文件
            srv.state["cfg"] = side
        return out

    api.dash_call = racing
    try:
        api.apply_knobs({"platform_settings.rate_limit.time": 90})
        check("并发写被拦", False, "居然覆盖了")
    except api.ApiError as exc:
        check("并发写被拦", exc.code == 409, "code=%d" % exc.code)
    finally:
        api.dash_call = orig_get

    srv.shutdown()
    print()
    print("通过 %d，失败 %d" % (PASS, len(FAIL)))
    for f in FAIL:
        print("  FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
