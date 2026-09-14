#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feed_sources + 新端点的单测 —— 全部用临时文件/临时库，不碰生产。

跑法：python3 test_feed.py（py3.8 兼容，和 test_console.py 同一套约定）
失败即非零退出。
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PASS = 0
FAIL = 0


def ok(what):
    global PASS
    PASS += 1
    print("  \033[32m✓\033[0m %s" % what)


def bad(what, detail=""):
    global FAIL
    FAIL += 1
    print("  \033[31m✗\033[0m %s  %s" % (what, detail))


def eq(what, want, got):
    if want == got:
        ok(what)
    else:
        bad(what, "want=%r got=%r" % (want, got))


def setup_env(tmp):
    """临时生产形状：日志 + 5 个库 + dynamics + config + env。"""
    now = time.time()

    log = os.path.join(tmp, "astrbot.log")
    lines = []
    # 容器 1 小时前启动；插件加载在启动之后
    def ts(epoch):
        return time.strftime("[%Y-%m-%d %H:%M:%S.000", time.gmtime(epoch + 8 * 3600))
    # active 插件：加载 + 运行日志
    lines.append("%s [Core] [INFO] [star.star_manager:1]: Loading plugin dsh-voice ..." % ts(now - 3500))
    lines.append("%s [Core] [INFO] [star.star_manager:1]: Plugin dsh-voice (1.2.5) by 大肥鱼: x" % ts(now - 3500))
    lines.append("%s [Plug] [INFO] [dsh-voice.main:9]: [voice] 已加载" % ts(now - 3490))
    lines.append("%s [Plug] [INFO] [dsh-voice.main:99]: [voice] 发了一条" % ts(now - 60))
    # 错误插件
    lines.append("%s [Core] [INFO] [star.star_manager:1]: Loading plugin dsh-crash ..." % ts(now - 3500))
    lines.append("%s [Plug] [INFO] [dsh-crash.main:9]: [crash] 已加载" % ts(now - 3490))
    lines.append("%s [Plug] [ERRO] [dsh-crash.main:99]: [crash] 炸了" % ts(now - 30))
    # 历史脏名（一次性测试脚本的日志，不该出现在 dashboard join 之后的结果里）
    lines.append("%s [Plug] [INFO] [aw.main:1]: [awareness] 噪音" % ts(now - 50))
    # silent 插件：只有加载行
    lines.append("%s [Core] [INFO] [star.star_manager:1]: Loading plugin dsh-quiet ..." % ts(now - 3500))
    lines.append("%s [Plug] [INFO] [dsh-quiet.main:9]: [quiet] 已加载" % ts(now - 3490))
    # 老于容器启动的行（stale 判定用）
    lines.append("%s [Plug] [INFO] [dsh-zombie.main:9]: [zombie] 上辈子的日志" % ts(now - 7200))
    with open(log, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    mem = os.path.join(tmp, "dsh_memory.db")
    con = sqlite3.connect(mem)
    con.executescript(
        "CREATE TABLE buffer(id INTEGER PRIMARY KEY, group_id TEXT, user_id TEXT,"
        " name TEXT, text TEXT, ts REAL);")
    con.execute("insert into buffer(group_id,user_id,name,text,ts) values(?,?,?,?,?)",
                ("111", "u1", "甲", "你好", now - 300))
    con.execute("insert into buffer(group_id,user_id,name,text,ts) values(?,?,?,?,?)",
                ("111", "u2", "乙", "在吗", now - 200))
    con.execute("insert into buffer(group_id,user_id,name,text,ts) values(?,?,?,?,?)",
                ("222", "u1", "甲", "另一个群", now - 100))
    con.commit()
    con.close()

    eff = os.path.join(tmp, "dsh_effect.db")
    con = sqlite3.connect(eff)
    con.executescript(
        "CREATE TABLE reply(id INTEGER PRIMARY KEY, group_id TEXT, ts REAL, due REAL,"
        " text TEXT, addressed INTEGER, status TEXT, reactions INTEGER, strategy TEXT,"
        " stance TEXT, target TEXT, contribution TEXT, why TEXT);")
    con.execute("insert into reply(group_id,ts,due,text,addressed,status,reactions,"
                "strategy,stance,target,contribution,why) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("111", now - 250, now - 70, "在的", 1, "done", 2,
                 "other", "ignored", "none", "none", "群里跟着玩了"))
    con.commit()
    con.close()

    mind = os.path.join(tmp, "dsh_mind.db")
    con = sqlite3.connect(mind)
    con.executescript(
        "CREATE TABLE observe(id INTEGER PRIMARY KEY, ts REAL, gid TEXT, uid TEXT,"
        " n_blocks INTEGER, inject_chars INTEGER, ctx_chars INTEGER, n_islands INTEGER,"
        " draft_len INTEGER, n_conflicts INTEGER, islands TEXT, conflicts TEXT,"
        " notes TEXT, errors TEXT, flags TEXT);"
        "CREATE TABLE observe_block(id INTEGER PRIMARY KEY, obs_id INTEGER,"
        " tag TEXT, chars INTEGER);")
    con.execute(
        "insert into observe(ts,gid,uid,n_blocks,inject_chars,ctx_chars,n_islands,"
        "draft_len,n_conflicts,islands,conflicts,notes,errors,flags) "
        "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now - 120, "111", "u1", 20, 4800, 7000, 5, 130, 0,
         '["好奇(2)·指向我"]', "[]", '["今日馋夜宵"]', "[]", ""))
    con.execute("insert into observe_block(obs_id,tag,chars) values(?,?,?)",
                (1, "system_reminder", 220))
    con.commit()
    con.close()

    selfdb = os.path.join(tmp, "dsh_selfaware.db")
    con = sqlite3.connect(selfdb)
    con.executescript(
        "CREATE TABLE self_revision(id INTEGER PRIMARY KEY, ts REAL, layer TEXT,"
        " subject TEXT, old_value TEXT, new_value TEXT, reason TEXT);"
        "CREATE TABLE sense_event(id INTEGER PRIMARY KEY, ts REAL, capability TEXT,"
        " status TEXT, success INTEGER, latency_ms INTEGER, source TEXT, detail TEXT,"
        " fingerprint TEXT);")
    con.execute("insert into self_revision(ts,layer,subject,old_value,new_value,reason)"
                " values(?,?,?,?,?,?)",
                (now - 90, "short", "vision", "degraded", "available", "imgctx 修复"))
    con.execute("insert into sense_event(ts,capability,status,success,latency_ms,"
                "source,detail,fingerprint) values(?,?,?,?,?,?,?,?)",
                (now - 90, "chat", "available", 1, None, "llm_response", "本轮已生成", "fp"))
    con.commit()
    con.close()

    dyn = os.path.join(tmp, "dynamics.json")
    with open(dyn, "w", encoding="utf-8") as fh:
        json.dump({"generated": "2026-09-14 03:00:00", "window_h": 3.0,
                   "n_user_msg": 100, "n_bot_msg": 20, "n_mention": 9,
                   "p1_mention_miss": [{"t": "1", "who": "x", "text": "y"}],
                   "p2_owner_ignored": [],
                   "p3_share": 0.2,
                   "p4_repeat": [{"text": "大半夜的", "n": 9}],
                   "p5_burst": [{"start": "1", "n": 5}] * 2,
                   "p6_fails": {"voice审核拦截": 3}, "p7_mech": 6,
                   "top_talkers": [["难谓言", 117]]}, fh)

    cfg = os.path.join(tmp, "cmd_config.json")
    with open(cfg, "wb") as fh:
        fh.write(json.dumps({
            "dashboard": {"username": "u", "jwt_secret": "s" * 32},
            "provider": [{"id": "p1", "model": "m1", "api_key": "sk-real-secret"}],
            "tencent": {"apikey": "real-key"},
        }).encode())

    envf = os.path.join(tmp, "imagegen.env")
    with open(envf, "w", encoding="utf-8") as fh:
        fh.write("# 注释保留\nDSH_VOICE_AUTO=1\nWUSOUND_API_KEY=sk-123\nDSH_REPEAT=0\n")

    os.environ.update({
        "CONSOLE_FEED_LOG": log,
        "CONSOLE_FEED_MEMDB": mem,
        "CONSOLE_FEED_EFFECTDB": eff,
        "CONSOLE_FEED_MINDB": mind,
        "CONSOLE_FEED_SELFAWAREDB": selfdb,
        "CONSOLE_FEED_DYNAMICS": dyn,
        "CONSOLE_CFG": cfg,
        "CONSOLE_ENV": envf,
    })


def main():
    tmp = tempfile.mkdtemp(prefix="feedtest-")
    try:
        setup_env(tmp)
        # 必须在环境变量设好之后 import（路径常量在 import 时固化）
        for mod in list(sys.modules):
            if mod == "feed_sources":
                del sys.modules[mod]
        import feed_sources as fs
        now = time.time()

        print("\n[1] 日志增量扫描")
        scan = fs.scan_log()
        eq("行数", 11, len(scan.rows))
        scan2 = fs.scan_log()
        eq("二次调用不重扫", 11, len(scan2.rows))
        # 追加一行，增量消化
        with open(fs.ASTRBOT_LOG, "a", encoding="utf-8") as fh:
            fh.write("[%s] [Plug] [INFO] [dsh-voice.main:100]: [voice] 又发了\n"
                     % time.strftime("%Y-%m-%d %H:%M:%S.000", time.gmtime(now + 8 * 3600)))
        scan3 = fs.scan_log()
        eq("追加一行被增量消化", 12, len(scan3.rows))

        print("\n[2] 插件活性")
        plugs = scan3.runtime_plugins(started_epoch=now - 3600)
        by = {p["name"]: p for p in plugs}
        eq("活跃插件", "active", by["dsh-voice"]["status"])
        eq("错误插件", "error", by["dsh-crash"]["status"])
        # 加载行也算本周期活动：真正 events=0 的是从未打过 [x.main] 行的插件
        eq("安静插件（只有加载行→active）", "active", by["dsh-quiet"]["status"])
        eq("僵尸标签", "stale", by["dsh-zombie"]["status"])
        eq("脏名也收集但不该进 dashboard join", True, "aw" in by)

        print("\n[3] 群聊合并流")
        gl = fs.group_list()
        eq("群按活跃排序", "222", gl[0]["id"])
        eq("另一群第二", "111", gl[1]["id"])
        d = fs.dialog(group_id="111", limit=10)
        seq = [m["dir"] for m in d["dialog"]]
        eq("时间倒序（新在前）", ["in", "out", "in"], seq)
        bot = [m for m in d["dialog"] if m["dir"] == "out"][0]
        eq("bot 消息带 reactions", 2, bot.get("reactions"))
        eq("bot 标签", "机器人", bot["who"])
        d2 = fs.dialog(group_id="111", limit=2)
        eq("more 标记", True, d2["more"])
        before = d["dialog"][-1]["ts"]
        d3 = fs.dialog(group_id="111", limit=10, before=before + 1)
        eq("before 翻页不重复", True,
           all(m["ts"] <= before + 1 for m in d3["dialog"]))

        print("\n[4] 内在状态")
        m = fs.mind_snapshot(5)
        eq("mind recent 条数", 1, len(m["recent"]))
        eq("islands 解析", ["好奇(2)·指向我"], m["recent"][0]["islands"])
        eq("notes 解析", ["今日馋夜宵"], m["recent"][0]["notes"])
        eq("blocks_24h", 1, len(m["blocks_24h"]))
        e = fs.effect_recent(5)
        eq("effect 条数", 1, len(e))
        eq("effect why", "群里跟着玩了", e[0]["why"])
        sa = fs.selfaware_recent(5)
        eq("revision", "available", sa["revisions"][0]["new"])
        dy = fs.dynamics_summary()
        eq("p1_miss_n", 1, dy["p1_miss_n"])
        eq("p5_burst_n", 2, dy["p5_burst_n"])

        print("\n[5] 日志尾巴")
        lt = fs.log_tail(minutes=60, level="all", limit=50)
        # 10 行都在 1h 窗口内（加载行在 -3500s≈58 分钟前）+ 追加的 1 行 = 11
        eq("1h 内行数", 11, len(lt["lines"]))
        eq("新行在前", True, "又发了" in lt["lines"][0]["text"])
        lerr = fs.log_tail(minutes=60, level="err", limit=50)
        eq("err 只剩错误", 1, len(lerr["lines"]))
        eq("err 命中", True, "炸了" in lerr["lines"][0]["text"])
        # 5 = voice 的加载/版本/已加载/运行 + 追加行；aw 噪音行不含 dsh-voice
        lname = fs.log_tail(minutes=6000, level="all", name="dsh-voice", limit=50)
        eq("按名过滤", 5, len(lname["lines"]))

        print("\n[6] 配置脱敏")
        rc = fs.raw_config()
        prov = rc["config"]["provider"][0]
        eq("provider api_key 掩码", "***已设置***", prov["api_key"])
        eq("普通字段不掩", "m1", prov["model"])
        eq("嵌套 apikey 掩码", "***已设置***", rc["config"]["tencent"]["apikey"])
        envmap = dict(l.split("=", 1) for l in rc["env"] if "=" in l)
        eq("env 密钥掩码", "***已设置***", envmap.get("WUSOUND_API_KEY"))
        eq("env 普通值保留", "1", envmap.get("DSH_VOICE_AUTO"))
        eq("注释行保留", True, rc["env"][0].startswith("#"))

        print("\n[7] 缺文件降级")
        os.environ["CONSOLE_FEED_MEMDB"] = os.path.join(tmp, "nope.db")
        for mod in list(sys.modules):
            if mod == "feed_sources":
                del sys.modules[mod]
        import importlib
        fs2 = importlib.import_module("feed_sources")
        eq("库不在回空列表", [], fs2._rows(os.path.join(tmp, "nope.db"), "select 1"))
        # buffer 没了但 effect 还在：群不消失，只是 user_msgs=0 —— 降级不装死
        g2 = fs2.group_list()
        eq("群列表从 effect 兜底", ["111"], [g["id"] for g in g2])
        eq("user_msgs 归零", 0, g2[0]["user_msgs"])
        d2b = fs2.dialog()
        eq("dialog 只剩 bot 侧", True,
           all(m["dir"] == "out" for m in d2b["dialog"]))
        os.environ["CONSOLE_FEED_MEMDB"] = os.path.join(tmp, "dsh_memory.db")

        print("\n[8] console_api 路由")
        os.environ["CONSOLE_FEED_OBSERVE_STATE"] = os.path.join(tmp, "state.json")
        for mod in list(sys.modules):
            if mod in ("feed_sources", "console_api"):
                del sys.modules[mod]
        import console_api
        code, data = console_api.handle(
            "GET", "/api/console/live", {"group": "111"}, None)
        eq("live 200", 200, code)
        eq("live 有 dialog", True, len(data["dialog"]) > 0)
        code, data = console_api.handle(
            "GET", "/api/console/mind", {}, None)
        eq("mind 200", 200, code)
        eq("mind 带诚实标注", True, "dsh-mind" in data["honesty_note"])
        code, data = console_api.handle(
            "GET", "/api/console/log", {"level": "err", "minutes": "6000"}, None)
        eq("log 200", 200, code)
        eq("log err 一条", 1, len(data["lines"]))
        try:
            console_api.handle(
                "GET", "/api/console/log", {"level": "bogus"}, None)
            bad("log 坏 level 应抛 ApiError")
        except console_api.ApiError as exc:
            eq("log 坏 level 抛 ApiError(400)", 400, exc.code)
        code, data = console_api.handle(
            "GET", "/api/console/rawconfig", {}, None)
        eq("rawconfig 200", 200, code)
        envmap = dict(l.split("=", 1) for l in data["env"] if "=" in l)
        eq("rawconfig env 掩码", "***已设置***", envmap.get("WUSOUND_API_KEY"))
        code, data = console_api.handle(
            "GET", "/api/console/live2", {}, None)
        eq("未知路径 404", 404, code)
        code, data = console_api.handle("POST", "/api/console/live", {}, {})
        eq("POST 观察端点按约定 404", 404, code)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n通过 %d，失败 %d" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
