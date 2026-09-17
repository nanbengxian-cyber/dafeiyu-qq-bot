# -*- coding: utf-8 -*-
"""识图 API 的写入与回读校验测试。"""
import importlib.util, json, os, shutil, sys, tempfile

spec = importlib.util.spec_from_file_location("mgr", "server/dafeiyu-manager.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

fails=[]
def ck(n,c,e=""):
    print("  %s %s%s" % ("PASS" if c else "FAIL", n, ("  <- "+str(e)) if not c and e!="" else ""))
    if not c: fails.append(n)

tmp=tempfile.mkdtemp(); m.INSTANCES_DIR=tmp
# 把等待和重启变成空操作（没有 docker）
m.compose = lambda *a, **k: None
# 模拟「AstrBot 已就绪」：这个测试要验的是配置写入逻辑，不是就绪判定
# （就绪判定由已有测试覆盖）。
m.wait_astrbot_ready = lambda *a, **k: True
m.astrbot_started_marker = lambda *a, **k: True
m._napcat_hot_reload = lambda *a, **k: False

n="t"; d=m.instance_dir(n)
for sub in ("napcat/persist/napcatcfg/config","astrbot/data"):
    os.makedirs(os.path.join(d,sub),exist_ok=True)
m.save_meta(n,{"name":n,"webui_port":16000,"onebot_port":16001,"panel_port":16002,
               "mac":"02:00:00:00:00:01","created_at":0,"status":"running"})
m.write_json_bom(m.astrbot_cfg_path(n),
                 {"platform":[],"provider":[],"provider_sources":[],
                  "platform_settings":{},"provider_settings":{}})
# apply_config 会等人格库就绪 —— 这里造一个空的 data_v4.db 模拟「已就绪」
import sqlite3
con=sqlite3.connect(m.persona_db_path(n))
# 表结构照抄 astrbot/core/db/po.py 的 Persona 模型
con.execute("""create table if not exists personas (
  id integer primary key autoincrement,
  persona_id text unique, system_prompt text, begin_dialogs text,
  tools text, skills text, custom_error_message text, folder_id text,
  sort_order integer, created_at text, updated_at text)""")
con.commit(); con.close()

print("== 1) 不填识图 → 完全不碰多模态配置 ==")
m.apply_config(n,"","","https://api.a.com/v1","sk-main","text-model","你是助手")
c=m.read_json_maybe_bom(m.astrbot_cfg_path(n))
ck("没有 dafeiyu-vision provider",
   not any(p.get("id")=="dafeiyu-vision" for p in (c.get("provider") or [])), c.get("provider"))
ck("没有 vision_source",
   not any(s.get("id")=="dafeiyu-vision_source" for s in (c.get("provider_sources") or [])))
ck("主 provider 在位", (c.get("provider") or [{}])[0].get("id")=="dafeiyu-main")

print("== 2) 填了识图 → 写入且排在主 provider 后面 ==")
m.apply_config(n,"","","https://api.a.com/v1","sk-main","text-model","你是助手",
               "", "https://api.b.com/v1","sk-vision","glm-4v")
c=m.read_json_maybe_bom(m.astrbot_cfg_path(n))
ids=[p.get("id") for p in (c.get("provider") or [])]
ck("provider 顺序 = [main, vision]", ids==["dafeiyu-main","dafeiyu-vision"], ids)
vp=[p for p in c["provider"] if p.get("id")=="dafeiyu-vision"][0]
ck("★ modalities 含 image", "image" in (vp.get("modalities") or []), vp.get("modalities"))
ck("模型名对", vp.get("model")=="glm-4v", vp.get("model"))
ck("绑到 vision_source", vp.get("provider_source_id")=="dafeiyu-vision_source")
vs=[s for s in c["provider_sources"] if s.get("id")=="dafeiyu-vision_source"][0]
ck("识图 API 地址对", vs.get("api_base")=="https://api.b.com/v1", vs.get("api_base"))
ck("识图 Key 写进去了", vs.get("key")==["sk-vision"], vs.get("key"))
ck("图片描述指向识图 provider",
   (c.get("provider_settings") or {}).get("default_image_caption_provider_id")=="dafeiyu-vision",
   (c.get("provider_settings") or {}).get("default_image_caption_provider_id"))
ck("主 provider 仍是第一个（文字不走识图模型）", ids[0]=="dafeiyu-main")

print("== 3) read_config 能读回识图配置 ==")
rc=m.read_config(n)
ck("vision_base", rc["vision_base"]=="https://api.b.com/v1", rc.get("vision_base"))
ck("vision_model", rc["vision_model"]=="glm-4v", rc.get("vision_model"))
ck("★ vision_key_set=True 但 Key 不回显", rc["vision_key_set"] is True and "sk-vision" not in json.dumps(rc), rc.get("vision_key_set"))
ck("主 API 也没受影响", rc["api_model"]=="text-model", rc.get("api_model"))

print("== 4) 缺地址/模型名 → 必须报错（不许写半套）==")
# 注意「缺 Key」不在此列：Key 留空=沿用已保存的（见 test-blank-key.py）。
# 地址和模型名则**不能**沿用 —— 用户想换成另一家的识图 API 时，
# 沿用旧地址会配出「新模型名 + 旧地址」的坏组合，报错还会指向模型名把人带偏。
for args,desc in ((("","sk-v","glm-4v"),"缺地址"),
                  (("https://api.b.com/v1","sk-v",""),"缺模型名")):
    try:
        m.apply_config(n,"","","https://api.a.com/v1","sk-main","text-model","p","",*args)
        ck("只填一半要报错（%s）"%desc, False, "竟然通过了")
    except m.ManagerError as e:
        ck("只填一半报错（%s）"%desc, "还差" in str(e) or "填全" in str(e), str(e))

print("== 4b) 缺 Key → 沿用已保存的（Key 不回显，用户改不动才怪）==")
m.apply_config(n,"","","https://api.a.com/v1","sk-main","text-model","p","",
               "https://api.b.com/v1","","glm-4v-3")
vs=[x for x in m.read_json_maybe_bom(m.astrbot_cfg_path(n))["provider_sources"]
    if x.get("id")=="dafeiyu-vision_source"][0]
# 此刻保存的 Key 是第 2 步写入的 sk-vision（sk-v2 要等第 6 步才写）
ck("★ 识图 Key 沿用了旧的", vs["key"]==["sk-vision"], vs["key"])
ck("识图模型名更新了",
   [p2 for p2 in m.read_json_maybe_bom(m.astrbot_cfg_path(n))["provider"]
    if p2.get("id")=="dafeiyu-vision"][0]["model"]=="glm-4v-3")

print("== 5) 识图和主聊天填成同一个 → 拦下来 ==")
try:
    m.apply_config(n,"","","https://api.a.com/v1","sk-main","same-model","p","",
                   "https://api.a.com/v1","sk-main","same-model")
    ck("同模型要报错", False, "竟然通过了")
except m.ManagerError as e:
    ck("同模型报错且给出建议", "同一个" in str(e) and "主聊天" in str(e), str(e))

print("== 6) 幂等：再写一次不会重复添加 provider ==")
m.apply_config(n,"","","https://api.a.com/v1","sk-main","text-model","p","",
               "https://api.b.com/v1","sk-v2","glm-4v-2")
c=m.read_json_maybe_bom(m.astrbot_cfg_path(n))
ids=[p.get("id") for p in (c.get("provider") or [])]
ck("provider 仍只有 2 个", ids==["dafeiyu-main","dafeiyu-vision"], ids)
ck("source 也没重复",
   len([s for s in (c.get("provider_sources") or []) if s.get("id")=="dafeiyu-vision_source"])==1)
ck("Key 更新成新的",
   [s for s in c["provider_sources"] if s.get("id")=="dafeiyu-vision_source"][0]["key"]==["sk-v2"])

print("== 7) 校验：地址/Key 格式 ==")
for bad,desc in ((("notaurl","sk","m"),"地址没有 http 前缀"),
                 (("https://a b.com/v1","sk","m"),"地址含空格"),
                 (("https://a.com/v1","sk\nx","m"),"Key 含换行"),
                 (("https://a.com/v1","sk","mo del"),"模型名含空格")):
    try:
        m.apply_config(n,"","","https://api.a.com/v1","sk-main","text-model","p","",*bad)
        ck("要拦下：%s"%desc, False, "竟然通过了")
    except m.ManagerError as e:
        ck("拦下：%s"%desc, True)

shutil.rmtree(tmp,ignore_errors=True)
print()
print("FAILURES: %d %s" % (len(fails), fails if fails else ""))
sys.exit(1 if fails else 0)
