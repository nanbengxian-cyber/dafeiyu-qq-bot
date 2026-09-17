# -*- coding: utf-8 -*-
"""时区回归：同一个库，在 UTC 和 UTC+8 下必须算出同一个活动时间。"""
import importlib.util, os, shutil, sqlite3, subprocess, sys, tempfile, time

spec = importlib.util.spec_from_file_location("mgr", "server/dafeiyu-manager.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

tmp = tempfile.mkdtemp()
m.INSTANCES_DIR = tmp
d = m.instance_dir("tz")
os.makedirs(os.path.join(d, "astrbot", "data"), exist_ok=True)

# 造一个「1 小时前（UTC）」的消息记录
now = int(time.time())
want = now - 3600
con = sqlite3.connect(os.path.join(d, "astrbot", "data", "data_v4.db"))
con.execute("create table platform_stats (id integer primary key, timestamp text, count integer)")
con.execute("insert into platform_stats (timestamp, count) values (?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(want)), 1))
con.commit(); con.close()

got = m._last_message_time("tz")
drift = abs(got - want)
print("  期望(UTC epoch) = %d" % want)
print("  实际            = %d" % got)
print("  偏差            = %d 秒 (%.2f 小时)" % (drift, drift / 3600.0))
ok = drift <= 2
print("  %s 偏差 <= 2 秒（与宿主机时区无关）" % ("PASS" if ok else "FAIL"))
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if ok else 1)
