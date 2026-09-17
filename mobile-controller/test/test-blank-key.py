# -*- coding: utf-8 -*-
"""「Key 留空=不改」是否真的成立。

背景：App 的 Key 输入框一直写着「留空=不改」（因为 Key 出于安全不回显，
用户想只改模型名时 Key 框必然是空的）。但服务器原来要求三样填全，
导致用户**只改人格或只改模型名都做不到**，必须回官网重新复制 Key。
这是把「不回显」的代价转嫁给了用户。
"""
import importlib.util, os, shutil, sqlite3, sys, tempfile

spec = importlib.util.spec_from_file_location("mgr", "server/dafeiyu-manager.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

fails=[]
def ck(n,c,e=""):
    print("  %s %s%s" % ("PASS" if c else "FAIL", n, ("  <- "+str(e)) if not c and e!="" else ""))
    if not c: fails.append(n)

tmp=tempfile.mkdtemp(); m.INSTANCES_DIR=tmp
m.compose=lambda *a,**k: None; m.wait_astrbot_ready=lambda *a,**k: True
m.astrbot_started_marker=lambda *a,**k: True; m._napcat_hot_reload=lambda *a,**k: False

def fresh(n):
    d=m.instance_dir(n)
    for sub in ("napcat/persist/napcatcfg/config","astrbot/data"):
        os.makedirs(os.path.join(d,sub),exist_ok=True)
    m.save_meta(n,{"name":n,"webui_port":1,"onebot_port":2,"panel_port":3,
                   "mac":"x","created_at":0,"status":"running"})
    m.write_json_bom(m.astrbot_cfg_path(n),
        {"platform":[],"provider":[],"provider_sources":[],
         "platform_settings":{},"provider_settings":{}})
    c=sqlite3.connect(m.persona_db_path(n))
    c.execute("""CREATE TABLE personas (created_at TEXT, updated_at TEXT,
      id INTEGER PRIMARY KEY AUTOINCREMENT, persona_id VARCHAR(255) UNIQUE NOT NULL,
      system_prompt TEXT NOT NULL, begin_dialogs JSON, tools JSON, skills JSON,
      custom_error_message TEXT, folder_id VARCHAR(36), sort_order INTEGER)""")
    c.commit(); c.close()

def prov(n, pid):
    for p in (m.read_json_maybe_bom(m.astrbot_cfg_path(n)).get("provider") or []):
        if p.get("id")==pid: return p
    return None
def src(n, sid):
    for s in (m.read_json_maybe_bom(m.astrbot_cfg_path(n)).get("provider_sources") or []):
        if s.get("id")==sid: return s
    return None

print("== 1) 先正常配一次 ==")
fresh("a")
m.apply_config("a","","","https://api.a.com/v1","sk-ORIGINAL","model-1","人格1")
ck("配好了", src("a","dafeiyu-main_source")["key"]==["sk-ORIGINAL"])

print("== 2) ★ 只改模型名，Key 留空（这是最常见的操作）==")
try:
    m.apply_config("a","","","https://api.a.com/v1","","model-2","人格1")
    ck("★ 只改模型名能成功", True)
    ck("★ 模型名改了", prov("a","dafeiyu-main")["model"]=="model-2", prov("a","dafeiyu-main"))
    ck("★ Key 沿用旧的（没被清空）",
       src("a","dafeiyu-main_source")["key"]==["sk-ORIGINAL"],
       src("a","dafeiyu-main_source")["key"])
except m.ManagerError as e:
    ck("★ 只改模型名能成功", False, e)

print("== 3) ★ 只改人格，API 三样全空 ==")
try:
    m.apply_config("a","","","","","","人格2")
    ck("★ 只改人格能成功", True)
    ck("★ API 没被动过", prov("a","dafeiyu-main")["model"]=="model-2")
except m.ManagerError as e:
    ck("★ 只改人格能成功", False, e)

print("== 4) 只改群号，API 全空 ==")
try:
    m.apply_config("a","123456","","","","","")
    ck("只改群号能成功", True)
    ck("API 仍没被动", src("a","dafeiyu-main_source")["key"]==["sk-ORIGINAL"])
except m.ManagerError as e:
    ck("只改群号能成功", False, e)

print("== 5) 换 Key（填了新的就用新的）==")
m.apply_config("a","","","","sk-NEW","","")
ck("Key 换成新的", src("a","dafeiyu-main_source")["key"]==["sk-NEW"],
   src("a","dafeiyu-main_source")["key"])
ck("模型名没被动", prov("a","dafeiyu-main")["model"]=="model-2")

print("== 6) 第一次配（实例上什么都没存）→ 必须报「要填全」==")
fresh("b")
try:
    m.apply_config("b","","","https://api.b.com/v1","","model-x","")
    ck("第一次配只给地址要报错", False, "竟然通过了")
except m.ManagerError as e:
    ck("★ 第一次配只给地址 → 报「要填全」", "缺一不可" in str(e), str(e))
try:
    m.apply_config("b","","","","","","人格")
    ck("只给人格、没配过 API → 不该报 API 的错", True)
except m.ManagerError as e:
    ck("只给人格、没配过 API → 不该报 API 的错", "API" not in str(e), str(e))

print("== 7) 识图 API 的 Key 同样可以留空 ==")
m.apply_config("a","","","","","","","", "https://v.a.com/v1","sk-V1","glm-4v")
ck("识图配好了", src("a","dafeiyu-vision_source")["key"]==["sk-V1"])
try:
    m.apply_config("a","","","","","","","", "https://v.a.com/v1","","glm-4v-2")
    ck("★ 识图只换模型名、Key 留空 → 成功", True)
    ck("★ 识图 Key 沿用旧的",
       src("a","dafeiyu-vision_source")["key"]==["sk-V1"],
       src("a","dafeiyu-vision_source")["key"])
    ck("识图模型名换了", prov("a","dafeiyu-vision")["model"]=="glm-4v-2")
except m.ManagerError as e:
    ck("★ 识图只换模型名、Key 留空 → 成功", False, e)

print("== 8) 识图换一家：地址变了但 Key 没填、实例上也没存过 → 报清楚 ==")
fresh("c")
try:
    m.apply_config("c","","","","","","","", "https://v.c.com/v1","","glm-4v")
    ck("没存过 Key 时报错", False, "竟然通过了")
except m.ManagerError as e:
    ck("★ 报错且指出缺 Key", "Key" in str(e), str(e))

print("== 9) 地址和模型名**不能**沿用（换了家就会配出坏组合）==")
fresh("d")
m.apply_config("d","","","https://api.d.com/v1","sk-D","model-d","","",
               "https://v.d.com/v1","sk-VD","glm-4v")
try:
    # 只填模型名：地址和 Key 都没有 —— 地址不能沿用旧的，
    # 否则会配出「新模型名 + 旧地址」
    m.apply_config("d","","","","","","","", "", "","glm-4v-9b")
    ck("只填识图模型名要报错（地址不能沿用）", False, "竟然通过了")
except m.ManagerError as e:
    ck("★ 只填识图模型名 → 报缺地址", "地址" in str(e), str(e))

shutil.rmtree(tmp,ignore_errors=True)
print()
print("FAILURES: %d %s" % (len(fails), fails if fails else ""))
sys.exit(1 if fails else 0)
