#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dafeiyu-manager 的单元测试。

跑法：python3 test/test-manager.py
不需要 docker、不需要网络 —— 纯逻辑测试。
需要 docker 的部分（create/start）在这里只验证「生成的 compose 内容对不对」，
真机编排由 test/e2e-manager.sh 在服务器上验证。
"""

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "server", "dafeiyu-manager.py")

FAILED = []
PASSED = [0]


def ok(cond, label):
    if cond:
        PASSED[0] += 1
        print("  ✓ %s" % label)
    else:
        FAILED.append(label)
        print("  ✗ %s" % label)


def eq(got, want, label):
    if got == want:
        PASSED[0] += 1
        print("  ✓ %s" % label)
    else:
        FAILED.append("%s（想要 %r，得到 %r）" % (label, want, got))
        print("  ✗ %s（想要 %r，得到 %r）" % (label, want, got))


def raises(fn, needle, label):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        if needle in str(e):
            PASSED[0] += 1
            print("  ✓ %s" % label)
        else:
            FAILED.append("%s（错误信息不对：%s）" % (label, e))
            print("  ✗ %s（错误信息不对：%s）" % (label, e))
        return
    FAILED.append("%s（没有抛异常）" % label)
    print("  ✗ %s（没有抛异常）" % label)


def load_module(root):
    """每个测试组用独立的 DAFEIYU_ROOT，避免互相污染。"""
    os.environ["DAFEIYU_ROOT"] = root
    spec = importlib.util.spec_from_file_location("mgr_%d" % id(root), SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_name_validation():
    print("\n【实例名校验：防路径穿越】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        ok(m.NAME_RE.match("test1") is not None, "普通名字 test1 通过")
        ok(m.NAME_RE.match("a-b-c") is not None, "带短横线通过")
        # 这些是攻击面：绝不能通过
        for bad in ("../etc/passwd", "..", "a/b", "a\\b", "", "A", "-x",
                    "x" * 40, "a b", "a;rm -rf /", "a$(id)"):
            ok(m.NAME_RE.match(bad) is None, "拒绝危险实例名 %r" % bad)
        raises(lambda: m.instance_dir("../evil"), "实例名", "instance_dir 拒绝穿越")
        raises(lambda: m.instance_dir("a/b"), "实例名", "instance_dir 拒绝斜杠")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_normalize_ids():
    print("\n【QQ 号/群号解析】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        eq(m.normalize_ids("123456,789012", "群号"), ["123456", "789012"], "逗号分隔")
        eq(m.normalize_ids("123456，789012", "群号"), ["123456", "789012"], "中文逗号")
        eq(m.normalize_ids("123456、789012", "群号"), ["123456", "789012"], "顿号")
        eq(m.normalize_ids("123456 789012", "群号"), ["123456", "789012"], "空格")
        eq(m.normalize_ids("123456\n789012", "群号"), ["123456", "789012"], "换行")
        eq(m.normalize_ids("123456,123456", "群号"), ["123456"], "去重")
        eq(m.normalize_ids("", "群号"), [], "空串")
        eq(m.normalize_ids(None, "群号"), [], "None")
        # 非法输入必须报错（否则会写进服务器配置）
        raises(lambda: m.normalize_ids("abc", "群号"), "不是纯数字", "拒绝字母")
        raises(lambda: m.normalize_ids("123", "群号"), "不是纯数字", "拒绝过短")
        raises(lambda: m.normalize_ids("1234567890123", "群号"), "不是纯数字", "拒绝过长")
        raises(lambda: m.normalize_ids("12345,abc", "群号"), "不是纯数字", "混入非法要报错")
        raises(lambda: m.normalize_ids("12 34", "群号"), "不是纯数字", "带空格拆开后过短")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_port_allocation():
    print("\n【端口分配：不冲突、可回收】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        p1 = m.alloc_ports()
        eq(p1, (16000, 16001, 16002), "首个实例从 16000 起")
        # 手工造两个实例 meta，验证不会重分配
        for name, ports in (("a1", p1), ("a2", (16003, 16004, 16005))):
            m.save_meta(name, {"name": name, "webui_port": ports[0],
                               "onebot_port": ports[1], "panel_port": ports[2]})
        eq(m.alloc_ports(), (16006, 16007, 16008), "跳过已占用端口")
        eq(sorted(m.used_ports()), [16000, 16001, 16002, 16003, 16004, 16005],
           "used_ports 汇总正确")
        # 删掉一个实例后端口应能被重新用上（但按序分配，先给最小空位）
        m.save_meta("a1", {"name": "a1", "webui_port": 16000, "onebot_port": 16001,
                           "panel_port": 16002})
        eq(len(m.alloc_ports()), 3, "分配结果总是三元组")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_compose_render():
    print("\n【compose 生成：安全约束】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        meta = {"name": "t1", "webui_port": 16000, "onebot_port": 16001,
                "panel_port": 16002, "mac": "02:aa:bb:cc:dd:ee"}
        y = m.render_compose("t1", meta)
        ok("127.0.0.1:16000:6099" in y, "WebUI 端口绑 127.0.0.1")
        ok("127.0.0.1:16001:3001" in y, "OneBot 端口绑 127.0.0.1")
        ok("127.0.0.1:16002:6185" in y, "面板端口绑 127.0.0.1")
        ok("0.0.0.0" not in y, "★ 绝不出现 0.0.0.0（公网暴露）")
        ok("dafeiyu-t1-napcat" in y and "dafeiyu-t1-astrbot" in y, "容器名带实例前缀")
        ok('mac_address: "02:aa:bb:cc:dd:ee"' in y, "固定 MAC（QQ 设备指纹）")
        ok("mem_limit" in y, "有内存上限（防一个实例拖垮全机）")
        ok("dafeiyu-t1" in y or "container_name" in y, "容器名可识别")
        # 不同实例的 compose 不能互相串
        y2 = m.render_compose("t2", dict(meta, name="t2", webui_port=16003,
                                         onebot_port=16004, panel_port=16005))
        ok("16003" in y2 and "16000" not in y2, "实例间端口隔离")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_json_bom_roundtrip():
    print("\n【配置读写：BOM 与回读校验】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        p = os.path.join(root, "cfg.json")
        data = {"platform_settings": {"id_whitelist": ["123456"]},
                "人格": "测试中文"}
        m.write_json_bom(p, data)
        raw = open(p, "rb").read()
        ok(raw.startswith(b"\xef\xbb\xbf"), "★ 写出的是 utf-8-sig（带 BOM）")
        eq(m.read_json_maybe_bom(p), data, "能读回自己写的（含 BOM）")
        # 也必须有能力读不带 BOM 的文件（AstrBot 版本差异）
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        eq(m.read_json_maybe_bom(p), data, "也能读不带 BOM 的")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_apply_config_requires_instance():
    print("\n【配置写入：前置条件】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        # 实例不存在 → 明确报错，不能默默写坏
        raises(lambda: m.apply_config("nope", "123456", "", "", "", "", ""),
                "没有这个实例", "实例不存在时报错")
        # 实例存在但没跑过 → 提示先启动
        m.create_instance("t1")
        raises(lambda: m.apply_config("t1", "123456", "", "", "", "", ""),
                "还没跑起来过", "配置未生成时提示先启动")
        # 什么都没填 → 报错
        raises(lambda: m.apply_config("t1", "", "", "", "", "", ""),
                "没填任何", "空配置报错")
        # 人格要在数据库没生成时给出可行动提示（而不是静默失败）
        raises(lambda: m.apply_config("t1", "", "", "", "", "", "你是一只鱼。"),
                "还没生成", "数据库没生成时提示先启动")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_apply_config_writes_and_verifies():
    print("\n【配置写入：真的写进去 + 回读校验】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        m.create_instance("t1")
        # 造一份「AstrBot 已初始化」的配置
        d = os.path.join(root, "instances", "t1", "astrbot", "data")
        os.makedirs(d, exist_ok=True)
        cfg = {"platform": [{"id": "default", "type": "aiocqhttp"}],
               "platform_settings": {}, "provider_sources": [], "provider": [],
               "provider_settings": {}, "persona": []}
        m.write_json_bom(os.path.join(d, "cmd_config.json"), cfg)
        # 造一个带 personas 表的库 —— 人格现在写数据库，不写 cmd_config.json
        # （cmd_config.json 里的 persona 字段在 AstrBot 源码里标着 deprecated）
        import sqlite3 as _sq
        dbp = os.path.join(d, "data_v4.db")
        c = _sq.connect(dbp)
        c.execute("""CREATE TABLE personas (
            created_at TEXT, updated_at TEXT, id INTEGER PRIMARY KEY AUTOINCREMENT,
            persona_id VARCHAR(255) UNIQUE NOT NULL, system_prompt TEXT NOT NULL,
            begin_dialogs JSON, tools JSON, skills JSON,
            custom_error_message TEXT, folder_id VARCHAR(36), sort_order INTEGER)""")
        c.commit()
        c.close()

        r = m.apply_config("t1", "476573490,225400545", "2774067216",
                           "https://api.example.com/v1", "sk-test-key", "test-model",
                           "你是一只测试用的鱼。")
        ok(r.get("verified") is True, "返回 verified=true")
        eq(len(r.get("changed", [])), 3, "三块都改了（范围/API/人格）")

        back = m.read_json_maybe_bom(os.path.join(d, "cmd_config.json"))
        wl = back["platform_settings"]["id_whitelist"]
        ok("476573490" in wl, "群号以裸号写入白名单")
        ok("225400545" in wl, "第二个群号也写入")
        ok("default:FriendMessage:2774067216" in wl, "★ 私聊写成 平台id:FriendMessage:QQ")
        eq(back["provider_settings"]["default_provider_id"], "dafeiyu-main",
           "默认 provider 指向新配的")
        eq(back["provider"][0]["model"], "test-model", "模型名写入")
        eq(back["provider_sources"][0]["api_base"], "https://api.example.com/v1",
           "接口地址写入")
        eq(back["provider_sources"][0]["key"], ["sk-test-key"], "API Key 写入")

        # ★ 字段完整性：这些字段少了 AstrBot 会直接 KeyError 崩在加载阶段
        #   （实测：provider 缺 "enable" → KeyError: 'enable'，日志只有 traceback，
        #    界面上完全看不出是「少了个字段」，表现就是「设了 API 但不说话」）。
        #   断言写成「缺任何一个都失败」，而不是逐个 isTrue —— 这样删掉哪个都会红。
        prov = back["provider"][0]
        for f in ("id", "provider_source_id", "enable", "model",
                  "modalities", "custom_extra_body"):
            ok(f in prov, "★ provider 条目含 %s（缺了会 KeyError）" % f)
        src_entry = back["provider_sources"][0]
        for f in ("id", "provider", "type", "provider_type", "key",
                  "api_base", "timeout", "proxy", "custom_headers", "enable"):
            ok(f in src_entry, "★ provider_sources 条目含 %s" % f)
        # 人格要落到数据库（不是 cmd_config.json 的废弃字段）
        import sqlite3 as _sq2
        c2 = _sq2.connect(dbp)
        row = c2.execute("SELECT system_prompt FROM personas WHERE persona_id=?",
                         ("dafeiyu-mine",)).fetchone()
        c2.close()
        eq(row[0] if row else None, "你是一只测试用的鱼。", "★ 人格写进 personas 表")
        eq(back["provider_settings"].get("default_personality"), "dafeiyu-mine",
           "★ 默认人格指向新写的那条")
        eq(back.get("persona"), [], "★ 不再往废弃的 persona 字段里写")

        # 幂等：再写一次不应产生重复条目
        m.apply_config("t1", "476573490", "", "", "", "", "")
        back2 = m.read_json_maybe_bom(os.path.join(d, "cmd_config.json"))
        eq(len(back2["provider_sources"]), 1, "重复写入不产生重复 source")
        eq(len(back2["provider"]), 1, "重复写入不产生重复 provider")
        import sqlite3 as _sq3
        c3 = _sq3.connect(dbp)
        n3 = c3.execute("SELECT COUNT(*) FROM personas WHERE persona_id=?",
                        ("dafeiyu-mine",)).fetchone()[0]
        c3.close()
        eq(n3, 1, "重复写入不产生重复 persona（数据库里只有一条）")

        # 备份文件必须存在（改坏了能回滚）
        baks = [f for f in os.listdir(d) if ".bak." in f]
        ok(len(baks) >= 1, "改动前有备份文件")

        # read_config 要能读回来
        rc = m.read_config("t1")
        ok(rc["ready"] is True, "read_config ready")
        ok("476573490" in rc["groups"], "read_config 读回群号")
        ok(rc["api_key_set"] is True, "read_config 只报告 key 是否设置")
        ok("sk-test-key" not in json.dumps(rc), "★ read_config 不回显 API Key 明文")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_auth_token():
    print("\n【管理口令】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        t1 = m.load_or_create_token()
        t2 = m.load_or_create_token()
        eq(t1, t2, "重复读取是同一个口令（不是每次都换）")
        ok(len(t1) >= 32, "口令足够长")
        st = os.stat(m.TOKEN_FILE)
        eq(oct(st.st_mode & 0o777), "0o600", "★ 口令文件权限 600")
        # 文件内容不该有换行残留导致比对失败
        ok(t1 == t1.strip(), "口令无多余空白")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_webui_token_isolation():
    print("\n【WebUI token：只读实例自己的】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        m.create_instance("t1")
        # 没有 webui.json 时返回空串，而不是报错或读到别处
        eq(m.webui_token("t1"), "", "配置未生成时返回空串")
        p = os.path.join(root, "instances", "t1", "napcat", "persist",
                         "napcatcfg", "config", "webui.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"token": "instance-token-abc"}, fh)
        eq(m.webui_token("t1"), "instance-token-abc", "能读到实例自己的 token")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_readback_verification_catches_bad_write():
    """变异测试的产物：光验证「写对了」不够，还要验证「写坏时会被发现」。

    做法：把 write_json_bom 换成「写的时候偷偷把白名单抹掉」，
    回读校验必须报错。如果这条测试不报错，说明回读校验是摆设。
    """
    print("\n【回读校验：写坏时必须被发现（防它是摆设）】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        m.create_instance("t1")
        d = os.path.join(root, "instances", "t1", "astrbot", "data")
        os.makedirs(d, exist_ok=True)
        cfg = {"platform": [{"id": "default"}], "platform_settings": {},
               "provider_sources": [], "provider": [], "provider_settings": {},
               "persona": []}
        m.write_json_bom(os.path.join(d, "cmd_config.json"), cfg)

        # 篡改写入函数：落盘时把白名单清空（模拟「写入静默失败/被别的东西覆盖」）
        real_write = m.write_json_bom

        def evil_write(path, data):
            if "platform_settings" in data:
                data["platform_settings"]["id_whitelist"] = []
            real_write(path, data)

        m.write_json_bom = evil_write
        raises(lambda: m.apply_config("t1", "476573490", "", "", "", "", ""),
               "回读校验失败", "★ 写入被篡改时，回读校验必须报错")

        # 同理：API provider 没落盘也要被抓住。
        # 注意篡改的是 provider 列表 —— 判据已从 default_provider_id
        # 改成「provider[0] 是不是我们的」（4.28+ 会删掉 default_provider_id，
        # 所以那个键不能再当判据）。
        m.write_json_bom = lambda path, data: real_write(
            path, {k: v for k, v in data.items() if k != "provider"})
        raises(lambda: m.apply_config("t1", "", "", "https://x.example/v1",
                                      "sk-k", "mdl", ""),
               "回读校验失败", "★ provider 没落盘时也要报错")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_apply_config_verifies_after_restart():
    """★ 回归测试：写配置后必须**在重启之后**再回读一次。

    这个 bug 真的发生过，而且非常隐蔽：
      AstrBot 启动时会用内存里的配置**重写** cmd_config.json。
      用户在实例刚起来时写配置 → 我们写文件成功、回读也通过 →
      紧接着的重启把内存里的旧状态写回去 → 配置全没了。
      而接口返回的是 {"ok": true, "verified": true} —— 用户以为成功了。

    判据（静态检查源码结构）：
      ① apply_config 里要有「重启」调用；
      ② 重启之后还要有一次回读校验（不能只在重启前校验）。
    """
    src = open(SRC, encoding="utf-8").read()
    i_apply = src.find("def apply_config(")
    i_next = src.find("\ndef read_config(", i_apply)
    assert i_apply > 0 and i_next > i_apply, "找不到 apply_config 函数体"
    body = src[i_apply:i_next]

    i_restart = body.find('"restart"')
    ok(i_restart > 0, "apply_config 里有重启 astrbot 的调用")

    # 重启之后必须还有回读校验
    after = body[i_restart:]
    ok("read_json_maybe_bom" in after or "read_config" in after,
       "★ 重启之后还有回读校验（只在重启前校验不够）")
    ok("配置没保住" in after,
       "★ 重启后校验失败时给出可行动提示（而不是静默成功）")
    # 而且要等 AstrBot 起来
    ok("wait_astrbot_ready" in after,
       "★ 重启后等 AstrBot 启动完再校验（否则读到假象）")


def test_main_provider_is_first_in_list():
    """★ 主聊天 API 必须排在 provider 列表的**第一位**。

    为什么不能用 default_provider_id：
      AstrBot 4.27 有 provider_settings.default_provider_id，
      4.28 起这个键被**删掉了**（default.py 里已经没有它）。
      而 check_config_integrity 对 schema 里不认识的键是**直接删**，
      所以写进去也会被静默抹掉，日志里只有一句 "Config key removed"。

      4.28 选 provider 的实际逻辑（provider/manager.py:_resolve_using_provider）：
        先看 agent_runner 的 model.provider_id，没有就取 provider_insts[0]。
      也就是说 —— **列表第一个就是默认**。

    这个测试钉住「顺序」这个真正生效的机制：
    即使有人把 default_provider_id 那行删了，顺序对了功能就还对。
    """
    src = open(SRC, encoding="utf-8").read()
    i = src.find("def apply_config(")
    j = src.find("\ndef read_config(", i)
    body = src[i:j]

    # 必须把新条目拼在**前面**（[src] + [...] 而不是 [...] + [src]）
    ok('cfg["provider_sources"] = [src] + [' in body,
       "★ provider_sources 把主 API 放第一位（4.28 靠顺序选 provider）")
    ok('cfg["provider"] = [prov] + [' in body,
       "★ provider 把主 API 放第一位")
    # 判据也不能依赖被删掉的键
    ok('(back.get("provider_settings") or {}).get("default_provider_id")' not in body,
       "★ 回读校验不看 default_provider_id（4.28 会删它）")
    ok('provs[0].get("id") != "dafeiyu-main"' in body,
       "★ 回读校验改成看 provider[0]")


def test_read_config_reports_provider_ok():
    """★ read_config 的 provider_ok 必须反映「主 API 是否真的生效」。

    这个字段直接决定 App 上显示「已配置」还是「没配置」。
    踩过的坑：prov 变量从「列表」改成「字符串」后，判断里还在用 prov[0]，
    结果永远返回 False —— 用户明明配好了，界面却说没配。
    """
    src = open(SRC, encoding="utf-8").read()
    i = src.find("def read_config(")
    j = src.find("\ndef webui_token(", i)
    body = src[i:j]
    ok('"provider_ok": prov == "dafeiyu-main"' in body,
       "★ provider_ok 用字符串比较（别再当列表索引）")
    ok("prov[0]" not in body, "★ read_config 里不再出现 prov[0]")


def test_apply_config_fits_app_timeout():
    """★ 改配置的总耗时必须留在 App 的读超时之内。

    为什么单独测这个：apply_config 会等两次 AstrBot 就绪（写之前一次、
    重启之后一次）。如果哪天有人把等待上限调大（比如为了「更稳」调到 90 秒），
    总耗时就会超过 App 的 120 秒读超时 ——
    表现是 **App 报「连不上」，但服务端其实已经成功写好了**，
    用户会反复重试，很难查。

    这里把「两次等待 + 重启开销」的预算钉住，改大了就红。
    """
    src = open(SRC, encoding="utf-8").read()
    m = re.search(r"^READY_TIMEOUT\s*=\s*(\d+)", src, re.M)
    ok(m is not None, "有 READY_TIMEOUT 常量（等待上限集中定义，别散在调用处）")
    if not m:
        return
    per = int(m.group(1))

    # App 端读超时（ManagerClient.java）
    app_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "app", "src", "com", "dafeiyu", "controller",
                           "ManagerClient.java")
    app_timeout = 120
    if os.path.exists(app_src):
        t = open(app_src, encoding="utf-8").read()
        tm = re.search(r"setReadTimeout\((\d+)\)", t)
        if tm:
            app_timeout = int(tm.group(1)) // 1000
    ok(app_timeout >= 60, "App 读超时不少于 60 秒（现在 %d 秒）" % app_timeout)

    # apply_config 里等两次；再给重启本身留 30 秒
    worst = per * 2 + 30
    ok(worst < app_timeout,
       "★ 最坏耗时 %d 秒 < App 读超时 %d 秒（单次等待上限 %d 秒）"
       % (worst, app_timeout, per))

    # 等待次数也不能失控
    body = src[src.find("def apply_config("):src.find("\ndef read_config(")]
    ok(body.count("wait_astrbot_ready(") == 2,
       "★ apply_config 里正好等两次（写前一次、重启后一次）")


def test_route_ordering():
    """路由顺序：/instance/config 不能被 /instance/<名字> 抢走。

    这个 bug 真的发生过：GET /instance/config?name=qq1 报「没有这个实例：config」——
    看起来像实例不存在，实际是通用路由把 "config" 当成了实例名。
    这种错最难查，因为错误信息把你往完全错的方向引。
    """
    src = open(SRC, encoding="utf-8").read()
    i_cfg = src.find('path == "/instance/config"')
    i_gen = src.find('path.startswith("/instance/")')
    ok(i_cfg > 0, "config 路由存在")
    ok(i_gen > 0, "通用实例路由存在")
    ok(i_cfg < i_gen, "★ config 路由排在通用路由之前（否则 config 会被当实例名）")
    # GET 和 POST 两边都要检查
    get_part = src[src.find("def do_GET"):src.find("def do_POST")]
    ok(get_part.find('path == "/instance/config"') <
       get_part.find('path.startswith("/instance/")'),
       "★ GET 里 config 也排在通用路由前")


def test_proxy_path_safety():
    """代理接口的路径解析：实例名必须走同一套校验，不能被 ../ 绕过。"""
    print("\n【代理：路径安全】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        # 实例名非法 → 必须报错，而不是去连某个意外端口
        raises(lambda: m.proxy_webui("../etc", "GET", "api/x", {}, b""),
               "实例名", "代理拒绝穿越式实例名")
        raises(lambda: m.proxy_webui("nope", "GET", "api/x", {}, b""),
               "没有这个实例", "代理拒绝不存在的实例")
        # 正常实例但容器没起来 → 明确的「连不上」而不是崩
        m.create_instance("t1")
        raises(lambda: m.proxy_webui("t1", "GET", "api/x", {}, b""),
               "连不上", "容器没起来时给出可读错误")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    print("=" * 62)
    print("dafeiyu-manager 单元测试")
    print("=" * 62)
    tests = [
        test_name_validation,
        test_normalize_ids,
        test_port_allocation,
        test_compose_render,
        test_json_bom_roundtrip,
        test_apply_config_requires_instance,
        test_apply_config_writes_and_verifies,
        test_readback_verification_catches_bad_write,
        test_auth_token,
        test_webui_token_isolation,
        test_proxy_path_safety,
        test_route_ordering,
        test_apply_config_verifies_after_restart,
        test_main_provider_is_first_in_list,
        test_read_config_reports_provider_ok,
        test_apply_config_fits_app_timeout,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            # 一个测试组崩了也要继续跑完，并把「崩了」记为失败 ——
            # 否则变异测试（故意改坏源码）会因为异常直接中断，
            # 看起来像「没报错」，实际是没跑完。
            FAILED.append("%s 崩溃：%s: %s" % (t.__name__, type(e).__name__, e))
            print("  ✗ %s 崩溃：%s: %s" % (t.__name__, type(e).__name__, e))
    print("\n" + "=" * 62)
    print("通过 %d，失败 %d" % (PASSED[0], len(FAILED)))
    if FAILED:
        for f in FAILED:
            print("  ✗ %s" % f)
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
