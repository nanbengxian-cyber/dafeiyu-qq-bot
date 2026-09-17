# -*- coding: utf-8 -*-
"""额度上限 + 闲置自动清理 的测试。"""
import importlib.util, json, os, shutil, sqlite3, sys, tempfile, time

spec = importlib.util.spec_from_file_location("mgr", "server/dafeiyu-manager.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

fails = []
def ck(n, c, e=""):
    print("  %s %s%s" % ("PASS" if c else "FAIL", n, ("  <- " + str(e)) if not c and e != "" else ""))
    if not c: fails.append(n)

tmp = tempfile.mkdtemp()
m.INSTANCES_DIR = tmp
m.ROOT = tmp
now = int(time.time())

def mk(name, last_active=None, created=None, locked=False):
    # created_at 故意设得很旧：否则「刚创建」会让 last_active_at 的兜底值
    # 变成 now，闲置天数永远算成 0 —— 那是测试自己的 bug，不是被测代码的。
    d = m.instance_dir(name)
    for sub in ("napcat/config", "astrbot/data"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    meta = {"name": name, "webui_port": 16000, "onebot_port": 16001,
            "panel_port": 16002, "mac": "02:00:00:00:00:01",
            "created_at": created or (now - 400 * 86400), "status": "running"}
    if last_active is not None:
        meta["last_active_at"] = last_active
    if locked:
        salt, h = m._hash_lock_password("pw")
        meta["lock"] = {"enabled": True, "salt": salt, "hash": h,
                        "set_at": int(time.time())}
    m.save_meta(name, meta)

def mkdb(name, ts_str):
    """造一个带 platform_stats 的库，模拟「最后一次收到消息的时间」。"""
    p = os.path.join(m.instance_dir(name), "astrbot", "data", "data_v4.db")
    con = sqlite3.connect(p)
    con.execute("create table platform_stats (id integer primary key, timestamp text, platform_id text, platform_type text, count integer)")
    con.execute("insert into platform_stats (timestamp, platform_id, platform_type, count) values (?,?,?,?)",
                (ts_str, "aiocqhttp", "unknown", 3))
    con.commit(); con.close()

print("== 1) 额度：满 15 个后必须拒绝新建 ==")
for i in range(m.MAX_ROBOTS_PER_USER):
    mk("r%02d" % i)
ck("已建满 %d 个" % m.MAX_ROBOTS_PER_USER, len(m.all_instances()) == m.MAX_ROBOTS_PER_USER)
try:
    m.create_instance("overflow")
    ck("第 16 个应被拒绝", False, "竟然建成功了")
except m.ManagerError as e:
    ck("第 16 个被拒绝", "上限" in str(e), str(e))
    ck("错误信息含上限和现状", "15" in str(e) and "已经有 15 个" in str(e), str(e))

print("== 2) 额度：删掉一个后可以再建 ==")
m.destroy_instance("r00")
ck("删后剩 %d" % (m.MAX_ROBOTS_PER_USER - 1), len(m.all_instances()) == m.MAX_ROBOTS_PER_USER - 1)
newm = m.create_instance("fresh")
ck("能再建", newm["name"] == "fresh")

print("== 3) quota_info 数字正确 ==")
q = m.quota_info()
ck("limit=15", q["limit"] == 15, q["limit"])
ck("used=15", q["used"] == 15, q["used"])
ck("remaining=0", q["remaining"] == 0, q["remaining"])
ck("idle_days=5", q["idle_days"] == 5)

print("== 4) 闲置判定：刚好 5 天 / 4 天 / 6 天 ==")
for n in list(os.listdir(tmp)):
    shutil.rmtree(os.path.join(tmp, n), ignore_errors=True)
mk("exact5", last_active=now - 5 * 86400)
mk("under4", last_active=now - 4 * 86400)
mk("over6", last_active=now - 6 * 86400)
ck("正好 5 天 → 会被删", m.idle_info("exact5")["will_be_removed"] is True, m.idle_info("exact5"))
ck("4 天 → 不删", m.idle_info("under4")["will_be_removed"] is False, m.idle_info("under4"))
ck("6 天 → 会删", m.idle_info("over6")["will_be_removed"] is True)

print("== 5) 数据库里的真实消息时间优先于 last_active_at ==")
mk("dbnew", last_active=now - 30 * 86400)          # meta 说很旧
mkdb("dbnew", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 3600)))  # 库里说 1 小时前
ck("以库为准 → 不删", m.idle_info("dbnew")["will_be_removed"] is False, m.idle_info("dbnew"))
ck("idle 约 1 小时", m.idle_info("dbnew")["idle_seconds"] < 7200, m.idle_info("dbnew"))

print("== 6) ★安全：读不到任何时间戳 → 必须跳过，绝不误删 ==")
mk("notime")
meta = m.load_meta("notime")
meta.pop("created_at", None); meta.pop("last_active_at", None)
m.save_meta("notime", meta)
ck("last_active_at 返回 0", m.last_active_at("notime") == 0, m.last_active_at("notime"))
r = m.cleanup_idle(dry_run=True)
ck("notime 没被判为待删", all(x["name"] != "notime" for x in r["removed"]), r["removed"])
ck("notime 在 kept 里且说明原因",
   any(x["name"] == "notime" and "保守跳过" in x.get("reason", "") for x in r["kept"]), r["kept"])

print("== 7) 私密实例不自动清理 ==")
mk("mylove", last_active=now - 99 * 86400, locked=True)
r = m.cleanup_idle(dry_run=True)
ck("锁着的不在待删列表", all(x["name"] != "mylove" for x in r["removed"]), r["removed"])
ck("kept 里说明是私密", any(x["name"] == "mylove" and "私密" in x.get("reason", "") for x in r["kept"]), r["kept"])

print("== 8) dry_run 不真删 ==")
before = len(m.all_instances())
m.cleanup_idle(dry_run=True)
ck("dry_run 后数量不变", len(m.all_instances()) == before, (before, len(m.all_instances())))

print("== 9) 真删：只删该删的 ==")
r = m.cleanup_idle(dry_run=False)
names_removed = sorted(x["name"] for x in r["removed"])
ck("删了 exact5 和 over6", names_removed == ["exact5", "over6"], names_removed)
ck("under4 还在", os.path.isdir(m.instance_dir("under4")))
ck("dbnew 还在（库里有近期消息）", os.path.isdir(m.instance_dir("dbnew")))
ck("mylove 还在（私密）", os.path.isdir(m.instance_dir("mylove")))
ck("notime 还在（无法判定）", os.path.isdir(m.instance_dir("notime")))

print("== 10) 幂等：再跑一次不会重复删 ==")
r2 = m.cleanup_idle(dry_run=False)
ck("第二次没删任何东西", r2["removed"] == [], r2["removed"])

shutil.rmtree(tmp, ignore_errors=True)
print()
print("FAILURES: %d %s" % (len(fails), fails if fails else ""))
sys.exit(1 if fails else 0)
