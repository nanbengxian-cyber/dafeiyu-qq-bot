# -*- coding: utf-8 -*-
"""识图 API 探测的测试。

用一个本地假服务器模拟各种 API 的回应，覆盖每一种「防呆」分支。
重点是证明它能区分：
  * 真能看图
  * 假装能看（接受请求但忽略图片、瞎猜）
  * 直接拒绝图片（400）
  * 说看不到
  * Key 不对 / 模型名不对 / 地址不对
"""
import base64, importlib.util, json, os, shutil, struct, sys, tempfile, threading, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec = importlib.util.spec_from_file_location("mgr", "server/dafeiyu-manager.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

fails = []
def ck(n, c, e=""):
    print("  %s %s%s" % ("PASS" if c else "FAIL", n, ("  <- " + str(e)) if not c and e != "" else ""))
    if not c: fails.append(n)

# ---- 假 API 服务器 ----
MODE = {"v": "good"}
SEEN = {}
CALLS = {"n": 0}

COLOR_NAMES = {"红": (255,0,0), "绿": (0,200,0), "蓝": (0,0,255), "黄": (255,255,0)}

def decode_color(b64):
    """从 data URL 里把颜色解出来（模拟「真的看了图」）。"""
    raw = base64.b64decode(b64.split(",", 1)[1])
    i, idat = 8, b""
    while i < len(raw):
        ln = struct.unpack(">I", raw[i:i+4])[0]
        tag = raw[i+4:i+8]
        if tag == b"IDAT": idat += raw[i+8:i+8+ln]
        i += 12 + ln
    px = zlib.decompress(idat)[1:4]
    for name, rgb in COLOR_NAMES.items():
        if tuple(px) == rgb: return name
    return "?"

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.endswith("/models"):
            if MODE["v"] == "badaddr": self._send(404, {"error": "no such endpoint"})
            elif MODE["v"] == "badkey": self._send(401, {"error": "invalid api key"})
            else: self._send(200, {"data": [{"id": "vision-model"}, {"id": "text-model"}]})
        else: self._send(404, {})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        SEEN["body"] = body
        CALLS["n"] += 1
        v = MODE["v"]
        if v == "badaddr": return self._send(404, {"error": {"message": "no such endpoint"}})
        if v == "badkey": return self._send(401, {"error": {"message": "invalid api key"}})
        if v == "badmodel": return self._send(404, {"error": {"message": "model not found"}})
        if v == "nomodel_field":
            return self._send(400, {"error": {"message": "unknown model"}})
        if v == "rejectimg":
            return self._send(400, {"error": {"message": "this model does not support image input"}})
        # 取出图片
        img = None
        for c in body.get("messages", [{}])[0].get("content", []):
            if isinstance(c, dict) and c.get("type") == "image_url":
                img = c["image_url"]["url"]
        if img is None:
            return self._send(200, {"choices": [{"message": {"content": "我不知道"}}]})
        if v == "good":
            return self._send(200, {"choices": [{"message": {"content": decode_color(img)}}]})
        if v == "synonym":
            # 真实情况：deepseek-flash 看到黄色 (255,255,0) 答「金色」。
            # 它明明看见了，只是用词不同 —— 不能因此判它「没看图」。
            c = decode_color(img)
            return self._send(200, {"choices": [{"message": {"content": {"黄": "金色", "红": "大红"}.get(c, c)}}]})
        if v == "refuse":
            return self._send(200, {"choices": [{"message": {"content": "我无法查看图片。"}}]})
        if v == "blind":
            # 假装能看：完全忽略图片，瞎猜一个固定颜色
            return self._send(200, {"choices": [{"message": {"content": "紫色"}}]})
        if v == "empty":
            return self._send(200, {"choices": [{"message": {"content": ""}}]})
        if v == "empty_always":
            return self._send(200, {"choices": [{"message": {"content": ""}}]})
        if v == "flaky":
            # 真实情况：推理模型偶尔把 token 全花在思考上，正文为空
            if CALLS["n"] < 3:
                return self._send(200, {"choices": [{"message": {"content": ""}}]})
            return self._send(200, {"choices": [{"message": {"content": decode_color(img)}}]})
        return self._send(500, {})

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d/v1" % port

tmp = tempfile.mkdtemp(); m.INSTANCES_DIR = tmp
d = m.instance_dir("t"); os.makedirs(os.path.join(d, "astrbot", "data"), exist_ok=True)
m.save_meta("t", {"name":"t","webui_port":16000,"onebot_port":16001,"panel_port":16002,
                  "mac":"02:00:00:00:00:01","created_at":0,"status":"running"})

def probe(mode):
    MODE["v"] = mode
    return m.probe_vision("t", BASE, "sk-test", "vision-model")

print("== 1) 真能看图：必须判为 vision_capable=True ==")
r = probe("good")
ck("reachable", r["reachable"] is True, r)
ck("auth_ok", r["auth_ok"] is True, r)
ck("★ vision_capable=True", r["vision_capable"] is True, r)
ck("说了测的颜色", bool(r["tested_color"]), r)
ck("★ 答对了（答案是测的颜色）", r["tested_color"] in r["message"], r["message"])

print("== 2) ★最阴险的：接受请求但忽略图片、瞎猜 ==")
r = probe("blind")
ck("vision_capable=False（没被骗）", r["vision_capable"] is False, r)
ck("提示说明它在瞎猜", "没有真的看图" in r["message"] or "瞎猜" in r["message"], r["message"])
ck("auth_ok 仍是 True（Key 没错）", r["auth_ok"] is True, r)

print("== 3) 明确说看不到 ==")
r = probe("refuse")
ck("vision_capable=False", r["vision_capable"] is False, r)
ck("提示提到看不到", "看不到" in r["message"], r["message"])

print("== 4) 直接拒绝图片（400）==")
r = probe("rejectimg")
ck("vision_capable=False", r["vision_capable"] is False, r)
ck("提示说不接受图片", "不接受图片" in r["message"], r["message"])
ck("auth_ok=True", r["auth_ok"] is True, r)

print("== 5) 空回复 ==")
r = probe("empty")
ck("vision_capable=None（不敢下结论）", r["vision_capable"] is None, r)

print("== 6) Key 不对 ==")
r = probe("badkey")
# reachable 的定义是「网络层能不能连上」。收到 401 说明**连上了**，
# 只是 Key 不对 —— 所以这里必须是 True。断言成 False 是测试写错了。
ck("reachable=True（连上了，只是 Key 错）", r["reachable"] is True, r)
ck("vision_capable 仍是 None", r["vision_capable"] is None, r)
ck("提示提到 Key", "Key" in r["message"], r["message"])

print("== 7) 模型名不对 ==")
r = probe("badmodel")
ck("vision_capable=None", r["vision_capable"] is None, r)
ck("提示提到模型名", "模型名" in r["message"], r["message"])

print("== 8) 地址不对（/models 和聊天接口都 404）==")
r = probe("badaddr")
# 404 是服务器**回应了** —— 网络层连上了，只是那个路径不存在。
# 所以 reachable=True 才对，和已有的 probe_api 行为一致（已实测比对）。
# 关键是不能误判成「能识图」，那才是防呆要防的。
ck("reachable=True（服务器回应了 404，网络是通的）", r["reachable"] is True, r)
ck("★ 不能误判成能识图", r["vision_capable"] is not True, r)
ck("提示说地址不完整", "地址" in r["message"], r["message"])
ck("有给人看的说明", bool(r["message"]), r)

print("== 9) 没填模型名 / Key / 地址 ==")
MODE["v"] = "good"
ck("没填模型名时不下结论",
   m.probe_vision("t", BASE, "sk-test", "")["vision_capable"] is None)
ck("没填 Key 时明确说",
   "Key" in m.probe_vision("t", BASE, "", "vision-model")["message"])
ck("没填地址时明确说",
   "地址" in m.probe_vision("t", "", "k", "v")["message"])

print("== 10) ★请求里真的带了图片（否则测试毫无意义）==")
probe("good")
content = SEEN["body"]["messages"][0]["content"]
ck("content 是数组", isinstance(content, list), type(content))
imgs = [c for c in content if c.get("type") == "image_url"]
ck("有 image_url 段", len(imgs) == 1, content)
ck("是 data:image/png base64", imgs[0]["image_url"]["url"].startswith("data:image/png;base64,"))
# max_tokens 必须够大。推理模型会先花 token 写 reasoning_content，
# 给少了就 content 为空、finish_reason=length —— 表现是「明明能识图
# 的模型被判成测不出来」。实测 300→空6/8、600→空2/8、1000→空0/8。
ck("★ max_tokens 给足（否则推理模型会返回空正文）",
   SEEN["body"].get("max_tokens", 0) >= 800, SEEN["body"].get("max_tokens"))

print("== 10.5) ★ 推理模型偶尔返回空正文：必须重试，不能直接判失败 ==")
# 模拟：前两次空、第三次才答对（真实 deepseek-flash 就是这样）
CALLS["n"] = 0
MODE["v"] = "flaky"
r = probe("flaky")
ck("★ 重试后判为能识图", r["vision_capable"] is True, r)
ck("★ 确实重试了（发了多次请求）", CALLS["n"] >= 2, CALLS["n"])
MODE["v"] = "empty_always"
CALLS["n"] = 0
r = probe("empty_always")
ck("一直空才判 None（不误判成能看图）", r["vision_capable"] is None, r)
ck("重试次数有上限（不会无限重试）", CALLS["n"] <= 4, CALLS["n"])

print("== 10.6) ★ 用同义词回答也算「看见了」==")
# 这是真实踩到的假阴性：测黄色，模型答「金色」，被判成没看图。
# 假阴性和假阳性一样有害 —— 用户会去换一个本来没问题的 API。
MODE["v"] = "synonym"
oks = []
for _ in range(30):
    oks.append(probe("synonym")["vision_capable"])
ck("★ 同义词回答判为能识图（30 次全对）", all(o is True for o in oks),
   [o for o in oks if o is not True])

print("== 11) 每次测的颜色是随机的（防背答案）==")
cols = set()
for _ in range(40):
    cols.add(probe("good")["tested_color"])
ck("出现过多种颜色", len(cols) >= 2, cols)
ck("都在预期集合里", cols <= set(COLOR_NAMES.keys()), cols)

print("== 12) 保存的配置能当默认值用 ==")
saved = {"api_base": BASE, "api_key": "sk-test", "api_model": "vision-model"}
MODE["v"] = "good"
r = m.probe_vision("t", "", "", "", saved=saved)
ck("用 saved 里的配置也能测", r["vision_capable"] is True, r)

srv.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print()
print("FAILURES: %d %s" % (len(fails), fails if fails else ""))
sys.exit(1 if fails else 0)
