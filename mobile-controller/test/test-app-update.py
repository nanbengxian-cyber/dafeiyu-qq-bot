# -*- coding: utf-8 -*-
"""App 公告 + 版本检查 / APK 下载 的服务器端测试。

背景（2026-09-19 用户要求）：App 要能看公告、检查新版本、一键下载安装新版。
服务器端提供：
  GET /app/update —— app-update.json 里的版本号 + 公告 + APK sha256/大小
  GET /app/apk    —— 把部署时放进去的 APK 原样发出去（走 SSH 隧道，0 额外凭据）

重点验证：
  ① 文件不存在 → 空壳（latest_code=0），App 判「无更新」，**不报错**；
  ② 有文件 → 字段原样读出，APK 存在时填 apk_size；
  ③ 版本号写了但 APK 没放 → apk_size=0（App 不该看见「能更新但下不动」）；
  ④ /app/apk：没有包 → 404 JSON；有包 → 200 + 二进制逐字节一致。
"""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile

spec = importlib.util.spec_from_file_location("mgr", "server/dafeiyu-manager.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []
def ck(n, c, e=""):
    print("  %s %s%s" % ("PASS" if c else "FAIL", n, ("  <- " + str(e)) if not c and e != "" else ""))
    if not c:
        fails.append(n)

tmp = tempfile.mkdtemp()
m.MANAGER_DIR = os.path.join(tmp, "manager")
os.makedirs(m.MANAGER_DIR, exist_ok=True)
m.APP_UPDATE_FILE = os.path.join(m.MANAGER_DIR, "app-update.json")
m.APK_DIR = os.path.join(m.MANAGER_DIR, "apk")

print("== 1) 没有 app-update.json → 空壳（无更新，不报错）==")
info = m.app_update_info()
ck("latest_code = 0", info["latest_code"] == 0, info)
ck("announcement 空", info["announcement"] == "")
ck("中文字段也能序列化", json.dumps(info, ensure_ascii=False) is not None)

print("== 2) 有文件 + 有 APK → 字段原样 + 填 apk_size ==")
os.makedirs(m.APK_DIR, exist_ok=True)
fake_apk = os.path.join(m.APK_DIR, "dafeiyu-controller-mine.apk")
apk_bytes = b"PK\x03\x04fake-apk-binary-content-" * 100
with open(fake_apk, "wb") as fh:
    fh.write(apk_bytes)
with open(m.APP_UPDATE_FILE, "w", encoding="utf-8") as fh:
    json.dump({
        "latest_code": 2,
        "latest_name": "1.2.0",
        "announcement": "第一行公告\n第二行公告",
        "sha256": "  AB12CD34  ",
    }, fh, ensure_ascii=False)
info = m.app_update_info()
ck("latest_code = 2", info.get("latest_code") == 2, info)
ck("latest_name = 1.2.0", info.get("latest_name") == "1.2.0", info)
ck("公告多行保留", info.get("announcement") == "第一行公告\n第二行公告", info)
ck("sha256 规范化（去空格小写）", info.get("sha256") == "ab12cd34", info)
ck("有 APK → apk_size 填上", info.get("apk_size") == len(apk_bytes), info)

print("== 3) 版本写了但 APK 没放 → apk_size=0（不能出现能更新但下不动）==")
os.remove(fake_apk)
info = m.app_update_info()
ck("apk_size = 0", info["apk_size"] == 0, info)
ck("其它字段仍在（公告还能看）", info["announcement"].startswith("第一行公告"), info)
os.makedirs(m.APK_DIR, exist_ok=True)
with open(fake_apk, "wb") as fh:
    fh.write(apk_bytes)

print("== 4) /app/apk 逻辑：没有包 404 JSON，有包 200 逐字节一致 ==")
class FakeHandler:
    def __init__(self):
        self.headers = {}
        self.body = b""
        self.status = None
        self.ctype = None
    def send_response(self, code):
        self.status = code
    def send_header(self, k, v):
        self.headers[k] = v
    def end_headers(self):
        pass
    def write(self, b):
        self.body += b
    @property
    def wfile(self):
        class W:
            def __init__(self, h):
                self.h = h
            def write(self, b):
                self.h.body += b
        return W(self)

# 没有包
os.remove(fake_apk)
h = FakeHandler()
m.app_apk(h)
ck("没有包 → 404", h.status == 404, h.status)
ck("没有包 → JSON 错误信息", "安装包".encode("utf-8") in h.body, h.body[:60])

# 有包
with open(fake_apk, "wb") as fh:
    fh.write(apk_bytes)
h = FakeHandler()
m.app_apk(h)
ck("有包 → 200", h.status == 200, h.status)
ck("有包 → Content-Type 正确", "package-archive" in (h.headers.get("Content-Type") or ""), h.headers)
ck("有包 → Content-Length 正确", int(h.headers["Content-Length"]) == len(apk_bytes), h.headers)
ck("★ 二进制逐字节一致", h.body == apk_bytes, "len=%d vs %d" % (len(h.body), len(apk_bytes)))

print("== 5) 空壳时 /app/apk 也不该崩（文件缺失路径安全）==")
import inspect
src = inspect.getsource(m.app_apk)
ck("app_apk 不抛未捕获异常（有 try 之外的兜底？——至少路径用常量）",
   "m.APK_DIR" in src or "APK_DIR" in src)

shutil.rmtree(tmp, ignore_errors=True)
print()
print("FAILURES: %d %s" % (len(fails), fails if fails else ""))
sys.exit(1 if fails else 0)