# -*- coding: utf-8 -*-
"""ensure_pairing / pairing_state 的纯逻辑测试（临时目录，不碰真服务器）。"""
import importlib.util, json, os, shutil, sys, tempfile

spec = importlib.util.spec_from_file_location("mgr", "server/dafeiyu-manager.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []
def ck(name, cond, extra=""):
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  <- " + str(extra)) if not cond and extra else ""))
    if not cond: fails.append(name)

tmp = tempfile.mkdtemp()
m.INSTANCES_DIR = tmp
name = "t1"
d = m.instance_dir(name)
for sub in ("napcat/config", "napcat/persist/qqconfig/.config",
            "napcat/persist/napcatcfg/config", "astrbot/data"):
    os.makedirs(os.path.join(d, sub), exist_ok=True)
m.save_meta(name, {"name": name, "webui_port": 16000, "onebot_port": 16001,
                   "panel_port": 16002, "mac": "02:00:00:00:00:01",
                   "created_at": 0, "status": "created"})

print("== 1) 首次 ensure_pairing（只有 napcat 目录，无 astrbot 配置）==")
r = m.ensure_pairing(name)
tok = r["token"]
ck("生成了 32 位 hex token", len(tok) == 32 and all(c in "0123456789abcdef" for c in tok), tok)
ck("写了 onebot11.json", "onebot11.json" in r["napcat"], r["napcat"])
ck("astrbot 侧还没写（文件不存在）", r["astrbot"] is False)

p = os.path.join(m.napcat_cfg_dir(name), "onebot11.json")
cfg = json.load(open(p, encoding="utf-8"))
wc = cfg["network"]["websocketClients"]
ck("websocketClients 有 1 条", len(wc) == 1, wc)
ck("url 指向 ws://astrbot:6199/ws", wc[0]["url"] == "ws://astrbot:6199/ws", wc[0]["url"])
ck("token 与 meta 一致", wc[0]["token"] == tok)
ck("enable=True", wc[0]["enable"] is True)
ck("保留了 timeout 等必需键", "timeout" in cfg and "musicSignUrl" in cfg, list(cfg.keys()))
ck("无 BOM（NapCat 要无 BOM）", open(p, "rb").read(3) != b"\xef\xbb\xbf")

print("== 2) 幂等：再调一次 token 不变、内容不变 ==")
before = open(p, "rb").read()
r2 = m.ensure_pairing(name)
ck("token 不变", r2["token"] == tok)
ck("文件内容不变", open(p, "rb").read() == before)

print("== 3) astrbot 侧：有 cmd_config.json 时应写 platform ==")
acfg = os.path.join(m.astrbot_cfg_path(name))
with open(acfg, "w", encoding="utf-8-sig") as fh:
    json.dump({"platform": [], "provider": [{"id": "dafeiyu-main"}]}, fh)
r3 = m.ensure_pairing(name)
ck("astrbot 侧写了", r3["astrbot"] is True)
a = json.load(open(acfg, encoding="utf-8-sig"))
pl = a["platform"]
ck("platform 有 1 条 aiocqhttp", len(pl) == 1 and pl[0]["type"] == "aiocqhttp", pl)
ck("端口 6199", pl[0]["ws_reverse_port"] == 6199)
ck("token 两端一致", pl[0]["ws_reverse_token"] == tok)
ck("保留了原 provider", a.get("provider") and a["provider"][0]["id"] == "dafeiyu-main")
ck("AstrBot 配置带 BOM", open(acfg, "rb").read(3) == b"\xef\xbb\xbf")

print("== 4) pairing_state 应报 paired=True ==")
st = m.pairing_state(name)
ck("paired", st["paired"] is True, st)
ck("napcat_configured", st["napcat_configured"] is True)
ck("astrbot_configured", st["astrbot_configured"] is True)
ck("tokens_match", st["tokens_match"] is True)

print("== 5) 变异测试：把 napcat token 改坏 → 必须报未配对 ==")
c = json.load(open(p, encoding="utf-8"))
c["network"]["websocketClients"][0]["token"] = "0" * 32
json.dump(c, open(p, "w", encoding="utf-8"))
st = m.pairing_state(name)
ck("token 不一致时 paired=False", st["paired"] is False, st)
ck("tokens_match=False", st["tokens_match"] is False)

print("== 6) 变异测试：删掉 astrbot platform → 必须报未配对 ==")
m.ensure_pairing(name)                      # 修回来
a = json.load(open(acfg, encoding="utf-8-sig")); a["platform"] = []
with open(acfg, "w", encoding="utf-8-sig") as fh: json.dump(a, fh)
st = m.pairing_state(name)
ck("astrbot 侧缺失时 paired=False", st["paired"] is False, st)
ck("astrbot_configured=False", st["astrbot_configured"] is False)

print("== 7) 已登录实例：onebot11_<uin>.json 也要被更新 ==")
m.ensure_pairing(name)
uin_f = os.path.join(m.napcat_cfg_dir(name), "onebot11_3685485028.json")
json.dump({"network": {"websocketClients": []}}, open(uin_f, "w", encoding="utf-8"))
m.ensure_pairing(name)
u = json.load(open(uin_f, encoding="utf-8"))
ck("带 uin 的文件也写上了", len(u["network"]["websocketClients"]) == 1, u["network"])
ck("url 正确", u["network"]["websocketClients"][0]["url"] == "ws://astrbot:6199/ws")

print("== 8) compose 必须显式挂 /app/.config/QQ ==")
compose = m.render_compose(name, m.load_meta(name))
ck("有 /app/.config/QQ 挂载", "/app/.config/QQ" in compose)
ck("挂载数 >= 4", compose.count("- ./napcat/") >= 3, compose.count("- ./napcat/"))

shutil.rmtree(tmp, ignore_errors=True)
print()
print("FAILURES: %d %s" % (len(fails), fails if fails else ""))
sys.exit(1 if fails else 0)
