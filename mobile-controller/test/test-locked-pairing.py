# -*- coding: utf-8 -*-
"""锁着的实例：必须给 pairing，但绝不能泄露任何秘密。"""
import importlib.util, json, os, shutil, sys, tempfile

spec = importlib.util.spec_from_file_location("mgr", "server/dafeiyu-manager.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

fails=[]
def ck(n,c,e=""):
    print("  %s %s%s" % ("PASS" if c else "FAIL", n, ("  <- "+str(e)) if not c and e!="" else ""))
    if not c: fails.append(n)

tmp=tempfile.mkdtemp(); m.INSTANCES_DIR=tmp
n="locked"
d=m.instance_dir(n)
for sub in ("napcat/persist/napcatcfg/config","astrbot/data"):
    os.makedirs(os.path.join(d,sub),exist_ok=True)
salt,h=m._hash_lock_password("secret123")
m.save_meta(n,{"name":n,"webui_port":16000,"onebot_port":16001,"panel_port":16002,
               "mac":"02:00:00:00:00:01","created_at":0,"status":"running",
               "lock":{"enabled":True,"salt":salt,"hash":h,"set_at":0}})

# 造一个「未配对」状态，确保 pairing 字段有信息量
SECRET_TOKEN="deadbeef"*4
cfg={"platform":[{"id":"default","type":"aiocqhttp","enable":True,
                  "ws_reverse_host":"0.0.0.0","ws_reverse_port":6199,
                  "ws_reverse_token":SECRET_TOKEN}],
     "provider_sources":[{"id":"s","key":"sk-SUPERSECRETKEY"}],
     "platform_settings":{"id_whitelist":["123456789"]}}
m.write_json_bom(m.astrbot_cfg_path(n),cfg)

print("== 不带密码（锁着）==")
r=m.instance_detail(n,"")
ck("locked=True", r["locked"] is True)
ck("config 为 None（不吐配置）", r["config"] is None, r["config"])
ck("webui_token 为空", r["webui_token"]=="", r["webui_token"])
ck("★ 给了 pairing 字段", "pairing" in r, sorted(r.keys()))
ck("pairing 是布尔组", all(isinstance(v,bool) for v in r["pairing"].values()), r["pairing"])

blob=json.dumps(r,ensure_ascii=False)
print("== 安全检查：锁着时响应里不能出现任何秘密 ==")
ck("不含 ws_reverse_token", SECRET_TOKEN not in blob)
ck("不含 token 前 8 位", SECRET_TOKEN[:8] not in blob)
ck("不含 API key", "SUPERSECRETKEY" not in blob)
ck("不含白名单里的 QQ 号", "123456789" not in blob)
ck("不含锁密码哈希", h not in blob)
ck("不含锁盐", salt not in blob)
ck("不含密码明文", "secret123" not in blob)

print("== 带正确密码（解锁）==")
r2=m.instance_detail(n,"secret123")
ck("locked=False", r2["locked"] is False)
ck("有 config", r2["config"] is not None)
ck("也有 pairing", "pairing" in r2)

print("== 带错密码：仍按锁着处理 ==")
r3=m.instance_detail(n,"wrongpw")
ck("locked=True", r3["locked"] is True)
ck("config 仍为 None", r3["config"] is None)
ck("仍然给 pairing（修通道不受锁影响）", "pairing" in r3)
ck("不含 API key", "SUPERSECRETKEY" not in json.dumps(r3,ensure_ascii=False))

shutil.rmtree(tmp,ignore_errors=True)
print()
print("FAILURES: %d %s" % (len(fails), fails if fails else ""))
sys.exit(1 if fails else 0)
