# -*- coding: utf-8 -*-
"""换 Key 后，重启把配置覆盖回旧值时，apply_config 必须自愈或明确报错。

背景（用户实测的坑）：AstrBot 重启时会用**内存里的旧配置**把 cmd_config.json
重写一遍。provider 这个**条目**会被保住，但里面的 Key/地址/模型名可能被旧值
覆盖回去 —— 表现正是「换了新 Key 却怎么都改不动」：保存提示成功，机器人还用
旧 Key。原来的回读校验只看「provider[0] 是不是 dafeiyu-main」，完全看不出这种
「壳还在、值回退」的失败，所以会误报成功。

这个测试用一个「第一次重启会把 Key 盖回旧值」的假 compose 复现该竞态，
验证 apply_config 能自愈（就绪后再写一次），复现不了自愈时必须明确报错、
绝不静默成功。
"""
import importlib.util, os, shutil, sqlite3, sys, tempfile

spec = importlib.util.spec_from_file_location("mgr", "server/dafeiyu-manager.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

fails = []
def ck(n, c, e=""):
    print("  %s %s%s" % ("PASS" if c else "FAIL", n, ("  <- " + str(e)) if not c and e != "" else ""))
    if not c: fails.append(n)

tmp = tempfile.mkdtemp(); m.INSTANCES_DIR = tmp
m.wait_astrbot_ready = lambda *a, **k: True
m.astrbot_started_marker = lambda *a, **k: True
m._napcat_hot_reload = lambda *a, **k: False

def fresh(n):
    d = m.instance_dir(n)
    for sub in ("napcat/persist/napcatcfg/config", "astrbot/data"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    m.save_meta(n, {"name": n, "webui_port": 1, "onebot_port": 2, "panel_port": 3,
                    "mac": "x", "created_at": 0, "status": "running"})
    m.write_json_bom(m.astrbot_cfg_path(n),
        {"platform": [], "provider": [], "provider_sources": [],
         "platform_settings": {}, "provider_settings": {}})

def main_src(n):
    for s in (m.read_json_maybe_bom(m.astrbot_cfg_path(n)).get("provider_sources") or []):
        if s.get("id") == "dafeiyu-main_source": return s
    return {}

def main_prov(n):
    for p in (m.read_json_maybe_bom(m.astrbot_cfg_path(n)).get("provider") or []):
        if p.get("id") == "dafeiyu-main": return p
    return {}


# ── 场景 1：第一次重启把 Key 盖回旧值，第二次不再盖 → 应自愈成 NEW ──
print("== 1) 重启覆盖竞态：换 Key 应自愈 ==")
fresh("a")
m.apply_config("a", "", "", "https://api.a.com/v1", "sk-OLD", "model-1", "")
ck("先配好旧 Key", main_src("a").get("key") == ["sk-OLD"])

restart_count = {"n": 0}
def compose_overwrite_first(name, *args, **kw):
    # 只处理 astrbot restart；第一次重启模拟「用旧内存重写」把 Key 盖回 sk-OLD。
    if "restart" in args:
        restart_count["n"] += 1
        if restart_count["n"] == 1:
            cfg = m.read_json_maybe_bom(m.astrbot_cfg_path(name))
            for s in cfg.get("provider_sources") or []:
                if s.get("id") == "dafeiyu-main_source":
                    s["key"] = ["sk-OLD"]        # 覆盖回旧 Key
            m.write_json_bom(m.astrbot_cfg_path(name), cfg)
    return None
m.compose = compose_overwrite_first

m.apply_config("a", "", "", "https://api.a.com/v1", "sk-NEW", "model-1", "")
ck("★ 触发了不止一次重启（说明检测到覆盖并自愈）", restart_count["n"] >= 2, restart_count["n"])
ck("★ 最终 Key 是 NEW（自愈成功，没被静默盖回）",
   main_src("a").get("key") == ["sk-NEW"], main_src("a").get("key"))


# ── 场景 2：每次重启都盖回旧值（自愈也救不回）→ 必须明确报错，不许静默成功 ──
print("== 2) 每次重启都覆盖：必须明确报错，不静默成功 ==")
fresh("b")
m.apply_config("b", "", "", "https://api.b.com/v1", "sk-OLD", "model-1", "")

def compose_overwrite_always(name, *args, **kw):
    if "restart" in args:
        cfg = m.read_json_maybe_bom(m.astrbot_cfg_path(name))
        for s in cfg.get("provider_sources") or []:
            if s.get("id") == "dafeiyu-main_source":
                s["key"] = ["sk-OLD"]
        m.write_json_bom(m.astrbot_cfg_path(name), cfg)
    return None
m.compose = compose_overwrite_always

try:
    m.apply_config("b", "", "", "https://api.b.com/v1", "sk-NEW", "model-1", "")
    ck("★ 救不回时必须报错（不能静默说成功）", False, "竟然没报错")
except m.ManagerError as e:
    ck("★ 救不回时明确报错", "没保住" in str(e) or "覆盖" in str(e), str(e))
    ck("报错后文件里不是被当成 NEW 生效（仍是旧值）",
       main_src("b").get("key") == ["sk-OLD"], main_src("b").get("key"))


# ── 场景 3：正常情况（重启不覆盖）→ 只重启一次，不触发额外重写 ──
print("== 3) 正常无覆盖：只重启一次 ==")
fresh("c")
m.apply_config("c", "", "", "https://api.c.com/v1", "sk-OLD", "model-1", "")
normal_count = {"n": 0}
def compose_noop(name, *args, **kw):
    if "restart" in args:
        normal_count["n"] += 1
    return None
m.compose = compose_noop
m.apply_config("c", "", "", "https://api.c.com/v1", "sk-NEW", "model-2", "")
ck("正常改 Key 成功", main_src("c").get("key") == ["sk-NEW"])
ck("模型名也改了", main_prov("c").get("model") == "model-2")
ck("★ 正常情况只重启一次（没有多余的自愈重写）", normal_count["n"] == 1, normal_count["n"])

shutil.rmtree(tmp, ignore_errors=True)
print()
print("FAILURES: %d %s" % (len(fails), fails if fails else ""))
sys.exit(1 if fails else 0)
