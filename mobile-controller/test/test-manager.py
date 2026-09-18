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
        # 实例存在但没跑过 → 提示先启动。
        #
        # 这里把「容器是否存在」钉成 False（= 从没启动过），而不是靠环境：
        # 本机 docker 是 permission denied，函数会返回 None（未知），
        # 那时走的是「还在初始化」那一支。两条支路各有各的测试，
        # 这里要验的是「真没启动过的人得到正确的话」。
        m.create_instance("t1")
        m.astrbot_container_exists = lambda n: False
        raises(lambda: m.apply_config("t1", "123456", "", "", "", "", ""),
                "还没跑起来过", "配置未生成时提示先启动")
        # 什么都没填 → 报错
        raises(lambda: m.apply_config("t1", "", "", "", "", "", ""),
                "没填任何", "空配置报错")
        # 人格要在数据库没生成时给出可行动提示（而不是静默失败）
        #
        # 注意这里期望的是「还在初始化」而不是「请先启动一次」：
        # 容器存在（= 他刚点过启动、还在拉镜像/初始化），
        # 所以走的是「不确定就说正在初始化」那一支 —— 宁可让刚启动的人多等一会儿，
        # 也不要把「请先启动」甩给一个明明刚点过启动的用户（实测踩过这个坑）。
        m.astrbot_container_exists = lambda n: True
        raises(lambda: m.apply_config("t1", "", "", "", "", "", "你是一只鱼。"),
                "还在初始化", "数据库没生成时提示还在初始化")
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

        r = m.apply_config("t1", "123456789,225400545", "987654321",
                           "https://api.example.com/v1", "sk-test-key", "test-model",
                           "你是一只测试用的鱼。")
        ok(r.get("verified") is True, "返回 verified=true")
        # 4 块：消息通道 + 范围 + API + 人格。
        # 「消息通道」是后加的（NapCat 和 AstrBot 的配对，见 ensure_pairing）——
        # 没有它，其余三块配得再对，机器人也一个字都不回。
        eq(len(r.get("changed", [])), 4, "四块都改了（通道/范围/API/人格）")
        ok(any("消息通道" in c for c in r.get("changed", [])),
           "★ changed 里明确提到「消息通道」（用户才知道这是修了不回话）")

        back = m.read_json_maybe_bom(os.path.join(d, "cmd_config.json"))
        wl = back["platform_settings"]["id_whitelist"]
        ok("123456789" in wl, "群号以裸号写入白名单")
        ok("225400545" in wl, "第二个群号也写入")
        ok("default:FriendMessage:987654321" in wl, "★ 私聊写成 平台id:FriendMessage:QQ")
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
        m.apply_config("t1", "123456789", "", "", "", "", "")
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
        ok("123456789" in rc["groups"], "read_config 读回群号")
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
        raises(lambda: m.apply_config("t1", "123456789", "", "", "", "", ""),
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

    # ready=False 时必须带 started，App 靠它区分「没启动过」和「正在初始化」
    i_not = body.find("if not os.path.exists(path):")
    ok(i_not > 0, "read_config 会检查配置文件是否存在")
    seg = body[i_not:i_not + 400]
    ok('"started"' in seg,
       "★ 未就绪时带上 started（App 据此区分两种人，否则一半人被指错方向）")
    ok("astrbot_container_exists" in seg,
       "★ started 用容器是否存在来判断")

    # 行为验证：容器不存在 → started=False；存在 → started=True；未知 → True（别冤枉人）
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        m.create_instance("t1")
        m.astrbot_container_exists = lambda n: False
        eq(m.read_config("t1").get("started"), False,
           "★ 容器不存在时 started=False（真没启动过）")
        m.astrbot_container_exists = lambda n: True
        eq(m.read_config("t1").get("started"), True,
           "★ 容器存在时 started=True（正在初始化）")
        m.astrbot_container_exists = lambda n: None
        eq(m.read_config("t1").get("started"), True,
           "★ 查不到容器状态时 started=True（宁可让人稍等，别让他白点启动）")
        eq(m.read_config("t1").get("ready"), False, "未就绪时 ready 仍是 False")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_app_does_not_block_save_when_not_ready():
    """★ App 不能因为 ready=false 就禁用「保存」。

    服务器侧的 apply_config 已经会先等 AstrBot 就绪再写（连配置文件没生成
    都会等），所以「刚点完启动就来填配置」是完全正常的操作顺序。
    App 如果在这里把「保存」灰掉，用户就会卡住 —— 明明能成功的事做不了。

    而且提示文案也要分开：刚点过启动的人该被告知「正在初始化，稍等」，
    而不是「你还没启动过」（他刚点过，这句话把他往错方向引）。
    """
    print("\n【App：未就绪时的保存与提示】")
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "app", "src", "com", "dafeiyu", "controller", "RobotsView.java")
    if not os.path.exists(p):
        ok(False, "找得到 RobotsView.java")
        return
    src = open(p, encoding="utf-8").read()
    i = src.find('!Json.bool(cfg, "ready", false)')
    ok(i > 0, "RobotsView 会检查 ready")
    seg = src[i:i + 1200]
    ok("save.setEnabled(false)" not in seg,
       "★ 未就绪时不再禁用「保存」（服务器会等就绪，用户不该被卡住）")
    ok('Json.bool(cfg, "started"' in seg,
       "★ 按 started 区分两种人的提示文案")
    ok("正在初始化" in seg, "★ 刚点过启动的人看到「正在初始化」")
    ok("还没启动过" in seg, "★ 真没启动过的人看到「还没启动过」")


def test_persona_written_after_ready():
    """★ 回归测试：写人格必须排在「等就绪」**之后**。

    这是实测踩到的真 bug（用 App 自己的代码跑端到端时撞上）：
      apply_config 里先调 write_persona_db()，再调 wait_astrbot_ready()。
      但 data_v4.db 比 cmd_config.json **晚落盘**：
      cmd_config.json 早就有了（所以界面显示「运行中」），
      而 AstrBot 的 ORM 还没初始化完，data_v4.db 还不存在。
      于是刚点完「启动」就来填三配置的用户会收到
      「实例的数据库还没生成。请先「启动」一次」——
      他明明刚启动过，这句话把他往完全错的方向引。

    判据（静态检查源码顺序）：
      ① wait_astrbot_ready 的调用位置必须早于 write_persona_db；
      ② 等就绪时要带 need_db（否则只等日志标记，照样撞上文件不存在）；
      ③ 真撞上时给的是「正在初始化、等半分钟」，不是「请先启动一次」。
    """
    src = open(SRC, encoding="utf-8").read()
    i = src.find("def apply_config(")
    j = src.find("\ndef read_config(", i)
    assert i > 0 and j > i, "找不到 apply_config 函数体"
    body = src[i:j]

    i_wait = body.find("wait_astrbot_ready(")
    i_db = body.find("write_persona_db(")
    ok(i_wait > 0, "apply_config 里会等 AstrBot 就绪")
    ok(i_db > 0, "apply_config 里会写人格库")
    ok(i_wait < i_db,
       "★ 等就绪排在写人格之前（数据库比配置文件晚落盘，反了就会误报「请先启动一次」）")

    # 注意：不能只写 `"need_db=has_persona" in body`。
    # apply_config 里有**两处** wait 调用（写前、重启后），
    # 只匹配「出现过」的话，把第一处的 need_db 去掉测试照样绿 —— 假绿。
    # 必须逐个调用点检查：两处都要带 need_db。
    calls = [ln.strip() for ln in body.splitlines() if "wait_astrbot_ready(" in ln]
    eq(len(calls), 2, "apply_config 里正好两处就绪等待（写前 + 重启后）")
    for c in calls:
        ok("need_db=has_persona" in c,
           "★ 每处就绪等待都带 need_db：%s" % c)

    # 写前那次还要带 need_cfg：刚点完「启动」的用户，cmd_config.json 可能还没生成。
    # 这一条不能省 —— 缺了它，「文件还没生成就直接报错」会回来。
    ok("need_cfg=True" in calls[0],
       "★ 写前的就绪等待带 need_cfg（配置文件还没生成时先等，不直接报错）")

    # 就绪等待函数本身要认 need_db，且拿不到日志时的降级路径也不能跳过等库
    k = src.find("def wait_astrbot_ready(")
    l = src.find("\ndef ", k + 1)
    fn = src[k:l]
    ok("need_db" in fn, "wait_astrbot_ready 支持 need_db 参数")
    ok("persona_db_path" in fn, "★ 就绪判据里包含数据库文件是否存在")
    ok("数据库还在初始化" in body or "数据库还没生成" in body,
       "★ 撞上未就绪时给的是可行动提示")

    # 「请先启动一次」这句话不能再作为「刚启动就填配置」的答案 ——
    # 用户确实启动过了，这句话是误导。它只该在真正没启动时出现。
    ok("请先「启动」一次，等 AstrBot" not in body,
       "★ 不再对「刚启动」的用户说「请先启动一次」")

    # 第二个人口：配置**文件**还没生成时（刚点启动、容器还在初始化）
    # 也不能直接报错，要先等。
    #
    # 判据是「写前那次等待带 need_cfg」—— 它把「文件在不在」并进了同一个
    # 就绪判据里。这样既先等了，又没多加一次等待（多等一次会把 App 的
    # 120 秒读超时撑爆）。
    i_wait = body.find("wait_astrbot_ready(")
    first_call = body[i_wait:body.find("\n", i_wait)]
    ok("need_cfg=True" in first_call,
       "★ 配置文件还没生成时先等（写前的等待带 need_cfg），而不是直接报「请先点启动」")
    ok("请先点「启动」" not in body,
       "★ 不再把「刚点过启动」的用户打发回启动按钮")
    # 而且要和「真没启动过」区分开：容器不存在才说「还没跑起来过」。
    ok("astrbot_container_exists" in body,
       "★ 用容器是否存在区分「从没启动」和「正在初始化」两种人")


def test_ready_waits_for_personas_table():
    """★ 回归测试：need_db 的就绪判据必须是「文件在 且 personas 表已建好」。

    这是用户反馈的「提示词写入失败」bug 的根因：
      AstrBot 的 ORM 先落盘 data_v4.db 文件、再逐张建表，两步之间有个窗口。
      老的 wait_astrbot_ready 只看**文件存在**就放行，于是在这个窗口里
      write_persona_db 会撞上「这个 AstrBot 版本还没有 personas 表」——
      用户看到的就是「提示词写入失败」，其实只是等早了。

    判据（行为验证，不是抠字符串）：
      ① 库文件在、但没有 personas 表时，need_db 就绪等待必须返回 False；
      ② personas 表建好后，同样的等待必须返回 True。
    这样才真挡住那个 bug；只静态检查「源码里有没有 personas」会假绿。
    """
    print("\n【就绪等待：必须等到 personas 表建好，不只是文件存在】")
    import sqlite3
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        # ★ 故意不调 create_instance：它会去「占一个端口」，在真实管理机上
        #   会撞到正在跑的线上实例（实测在服务器上跑就因端口被占而崩）。
        #   这个用例只关心「就绪判据」，自己把目录和文件摆出来就够了。
        name = "t1"
        # 钉住「日志说已启动」，把变量收敛到只剩「表建没建好」。
        m.astrbot_started_marker = lambda n: True
        # ★ 必须**显式传 timeout**：wait_astrbot_ready 的默认值是
        #   `timeout=READY_TIMEOUT`，那是**定义时**就绑定的常量 ——
        #   在测试里改 m.READY_TIMEOUT 根本不起作用，会老老实实等满 40 秒
        #   （实测这个用例因此要跑 42 秒，还把整个测试文件拖到超时）。
        FAST = 2

        dbp = m.persona_db_path(name)
        cfgp = m.astrbot_cfg_path(name)
        os.makedirs(os.path.dirname(dbp), exist_ok=True)
        os.makedirs(os.path.dirname(cfgp), exist_ok=True)
        open(cfgp, "w", encoding="utf-8").write("{}")

        # 场景一：库文件在，但只有别的表、没有 personas —— ORM 还没建到它。
        c = sqlite3.connect(dbp)
        c.execute("CREATE TABLE other (x INTEGER)")
        c.commit(); c.close()
        r1 = m.wait_astrbot_ready("t1", timeout=FAST, need_db=True, need_cfg=True)
        eq(r1, False,
           "★ 库文件在、但 personas 表还没建时返回 False（别放行到「写入失败」）")

        # 场景二：personas 表建好了 —— 这才算 data_v4.db 真就绪。
        c = sqlite3.connect(dbp)
        c.execute("CREATE TABLE personas (id INTEGER NOT NULL, "
                  "persona_id VARCHAR NOT NULL, system_prompt TEXT NOT NULL, "
                  "PRIMARY KEY(id))")
        c.commit(); c.close()
        r2 = m.wait_astrbot_ready("t1", timeout=FAST, need_db=True, need_cfg=True)
        eq(r2, True, "★ personas 表建好后返回 True（可以安全写人格了）")

        # 判据本身要能单独调用（apply_config 的报错分支也用它来决定说什么话）
        ok(hasattr(m, "personas_table_ready"),
           "★ personas_table_ready 是模块级函数（报错话术也要用它）")
        eq(m.personas_table_ready("t1"), True, "表在时说就绪")
        os.remove(dbp)
        eq(m.personas_table_ready("t1"), False, "★ 文件不在时也说没好（不抛异常）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_missing_config_tells_two_people_apart():
    """★ 缺 cmd_config.json 的有两种人，说的话必须相反。

    这是实测踩到的坑：用户点完「启动」马上来填配置，容器还在拉镜像，
    cmd_config.json 还没生成，服务端回「请先点启动」—— 他刚点过。
    这句话把他往完全错的方向引（他会去反复点启动，而问题只是"再等等"）。

    判据：
      ① 容器存在（他点过启动）→ 说「还在初始化，等一会儿」
      ② 容器不存在（真没启动过）→ 才说「还没跑起来过」
      ③ 拿不到结论（没有 docker）→ 宁可说「还在初始化」（不冤枉刚启动的人）
    """
    print("\n【配置写入：区分「从没启动」和「正在初始化」】")
    src = open(SRC, encoding="utf-8").read()

    # ① 有一个能查容器是否存在的函数，且**查不动时**返回 None（不是 False）。
    #
    # 这里必须用行为验证，不能只 grep "return None"：
    # 函数里有好几处 return None（FileNotFoundError、Exception 分支），
    # 把 docker ps 失败那处的 None 改成 False，字符串断言照样绿 —— 假绿（实测踩过）。
    k = src.find("def astrbot_container_exists(")
    ok(k > 0, "有 astrbot_container_exists（用来区分两种人）")
    fn = src[k:src.find("\ndef ", k + 1)]
    ok("docker" in fn, "用 docker ps 判断容器是否存在")

    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        import subprocess as _sp

        class _R:
            def __init__(self, rc, out):
                self.returncode = rc
                self.stdout = out

        real_run = _sp.run
        try:
            # docker ps 失败（权限不足 / 守护进程没跑）→ 必须是 None（不知道）
            _sp.run = lambda *a, **kw: _R(1, b"permission denied")
            eq(m.astrbot_container_exists("t1"), None,
               "★ docker ps 失败时返回 None（不知道），不是 False（不存在）")
            # docker 命令都没有 → 也是 None
            def _boom(*a, **kw):
                raise FileNotFoundError("no docker")
            _sp.run = _boom
            eq(m.astrbot_container_exists("t1"), None,
               "★ 没有 docker 命令时返回 None")
            # 正常查到容器 → True；查不到 → False
            _sp.run = lambda *a, **kw: _R(0, b"dafeiyu-t1-astrbot\n")
            eq(m.astrbot_container_exists("t1"), True, "查到容器时返回 True")
            _sp.run = lambda *a, **kw: _R(0, b"")
            eq(m.astrbot_container_exists("t1"), False, "查不到容器时返回 False")
        finally:
            _sp.run = real_run
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # ② apply_config 里：文件缺失时先按容器存在与否分流
    i = src.find("def apply_config(")
    j = src.find("\ndef read_config(", i)
    body = src[i:j]
    i_missing = body.find("if not os.path.exists(path):")
    ok(i_missing > 0, "apply_config 会检查配置文件是否存在")
    seg = body[i_missing:i_missing + 600]
    ok("astrbot_container_exists" in seg,
       "★ 文件缺失时用容器是否存在来分流（否则一半人被指错方向）")
    ok("还没跑起来过" in seg, "★ 真没启动过的人得到「还没跑起来过」")
    ok("还在初始化" in seg, "★ 刚点过启动的人得到「还在初始化」")

    # ③ 拿不到结论（没有 docker / 查不动）时走「还在初始化」那一支 ——
    #    宁可让刚启动的人多等一会儿，也不要把「请先启动」甩给他。
    #    显式钉住 None，别依赖跑测试的机器上 docker 能不能用。
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        m.create_instance("t1")
        m.astrbot_container_exists = lambda n: None
        raises(lambda: m.apply_config("t1", "123456", "", "", "", "", ""),
               "还在初始化", "★ 查不到容器状态时给的是「还在初始化」而不是「请先启动」")
        # 真没启动过（容器不存在）才说「还没跑起来过」
        m.astrbot_container_exists = lambda n: False
        raises(lambda: m.apply_config("t1", "123456", "", "", "", "", ""),
               "还没跑起来过", "★ 容器不存在时说「还没跑起来过」")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_no_log_wait_is_capped():
    """★ 拿不到容器日志时不能按 READY_TIMEOUT 死等。

    为什么：docker logs 拿不到（测试环境、容器不存在）时，我们没有任何证据
    说明实例在往好的方向走 —— 文件可能 2 秒后出现，也可能永远不会出现。
    等满 40 秒只是让用户干等，最后给的还是同一句报错。实测把测试拖到超时
    60 秒就是这么来的。

    判据：有一个明显更小的 NO_LOG_WAIT，且远小于 READY_TIMEOUT。
    """
    print("\n【就绪等待：拿不到日志时的上限】")
    src = open(SRC, encoding="utf-8").read()
    m1 = re.search(r"^READY_TIMEOUT\s*=\s*(\d+)", src, re.M)
    m2 = re.search(r"^NO_LOG_WAIT\s*=\s*(\d+)", src, re.M)
    ok(m2 is not None, "有 NO_LOG_WAIT 常量（拿不到日志时的上限）")
    if not (m1 and m2):
        return
    ready, nolog = int(m1.group(1)), int(m2.group(1))
    ok(nolog < ready,
       "★ NO_LOG_WAIT(%d) < READY_TIMEOUT(%d)" % (nolog, ready))
    ok(nolog <= 10, "★ 拿不到日志时最多等 %d 秒（用户不该干等）" % nolog)

    # 且 wait_astrbot_ready 真的用了它
    k = src.find("def wait_astrbot_ready(")
    fn = src[k:src.find("\ndef ", k + 1)]
    ok("NO_LOG_WAIT" in fn, "★ wait_astrbot_ready 在无日志分支里用了 NO_LOG_WAIT")

    # 行为验证：无 docker 环境下，文件缺失时不该等满 40 秒
    import time as _t
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        m.create_instance("t1")
        # 显式钉住「拿不到日志」，别依赖跑测试的机器上 docker 的权限状态
        m.astrbot_started_marker = lambda n: None
        t0 = _t.time()
        r = m.wait_astrbot_ready("t1", need_cfg=True)
        dt = _t.time() - t0
        eq(r, False, "文件缺失且无日志时返回 False（老实说没就绪）")
        ok(dt < 15, "★ 实际只等了 %.1f 秒（没按 40 秒死等）" % dt)
    finally:
        shutil.rmtree(root, ignore_errors=True)


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


def test_proxy_decompresses_gzip():
    """代理必须解压 gzip，绝不能把压缩字节当明文回吐。

    ★ 这是真机反馈「WebUI 打不开 / 白屏」的根因之一的回归测试。
    NapCat（Node/express）只要看到 Accept-Encoding: gzip 就压，而安卓的
    HttpURLConnection / OkHttp **默认就发 gzip**。原实现把压缩字节原样回吐、
    却只转发 Content-Type、丢掉 Content-Encoding —— WebView 拿到的是
    「标着 text/html 的 gzip 二进制」，页面直接白屏。

    实测（2026-09-17，经真实 SSH 隧道）：
      /webui/assets/index-*.js 明文 314245 字节，gzip 后 109606 字节；
      经代理返回的头里没有 Content-Encoding，body 头两字节是 1f8b。
    """
    print("\n【代理：gzip 解压】")
    import gzip as _gz
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()

        plain = ("<html><body>" + "中文内容" * 200 + "</body></html>").encode("utf-8")
        packed = _gz.compress(plain)
        ok(len(packed) < len(plain), "测试数据确实是压缩过的（%d < %d）"
           % (len(packed), len(plain)))
        ok(packed[:2] == b"\x1f\x8b", "压缩数据带 gzip 魔数 1f8b")

        eq(m._decompress(packed, "gzip"), plain, "★ gzip 被正确解压回明文")
        eq(m._decompress(plain, "identity"), plain, "identity 原样返回")
        eq(m._decompress(plain, None), plain, "没有 Content-Encoding 时原样返回")
        eq(m._decompress(packed, "GZIP"), plain, "大小写不敏感（GZIP 也认）")
        # 裸 deflate（无 zlib 头）也要能解 —— 有些实现就是这么发的
        import zlib as _zl
        co = _zl.compressobj(9, _zl.DEFLATED, -_zl.MAX_WBITS)
        raw_deflate = co.compress(plain) + co.flush()
        eq(m._decompress(raw_deflate, "deflate"), plain, "裸 deflate 也能解")
        # 坏数据不能把响应吞掉：解不开就原样返回，让上游去报错
        eq(m._decompress(b"\x1f\x8b\x08garbage", "gzip"), b"\x1f\x8b\x08garbage",
           "★ 解压失败时原样返回，不抛异常、不吞响应")

        # 转发时必须主动丢掉 accept-encoding 并声明 identity ——
        # 否则 NapCat 还是会压，就又有「压缩字节 + 明文声明」的错配。
        src = open(SRC, encoding="utf-8").read()
        seg = src[src.find("def proxy_webui("):src.find("def json_response(")]
        ok('"accept-encoding"' in seg,
           "★ 转发时丢掉 accept-encoding（不让 NapCat 压缩）")
        ok('hdrs["Accept-Encoding"] = "identity"' in seg,
           "★ 转发时明确要 identity")
        ok("_decompress(" in seg, "★ 回吐前调用了 _decompress（兜底）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_responses_declare_connection_close():
    """每个响应都必须带 Connection: close。

    ★ 这是真机反馈「连不上 WebUI: unexpected end of stream on
    com.android.okhttp.Address@9ce3d659」的根因的回归测试。

    BaseHTTPRequestHandler 默认 protocol_version=HTTP/1.0 且**不发
    Connection 头**，发完就关。按 RFC 这确实等于关闭，但 OkHttp 2.x
    （安卓 HttpURLConnection 底下就是它）只在**看到 Connection: close
    这个响应头**时才把连接标成不可复用（HttpEngine.java:750）。

    没有这个头 → 连接被放进连接池 → 下次从池里捞出一条服务器早已关闭的
    连接 → 读响应读到 EOF → `unexpected end of stream on ...Address@…`。

    实测复现（2026-09-17，OkHttp 2.7.5，经真实 SSH 隧道）：
      修复前：共享 client + 8 线程 → ok=81  fail=79
      变异回退后：                    ok=160 fail=160（正是用户报的那句）
      修复后：                        ok=320 fail=0
    只在复用连接时出现 —— 单发一条看不出来，所以必须有这条回归测试。
    """
    print("\n【HTTP：Connection: close】")
    src = open(SRC, encoding="utf-8").read()
    ok("def end_headers(self):" in src,
       "★ 重写了 end_headers（唯一的响应头出口，覆盖所有路径）")
    seg = src[src.find("def end_headers(self):"):src.find("def log_message(")]
    ok('self.send_header("Connection", "close")' in seg,
       "★ end_headers 里补了 Connection: close")
    ok("_headers_buffer" in seg,
       "★ 先查已有的头，避免重复发 Connection")

    # 真起一个服务，把所有响应路径都打一遍 —— 光看源码不够，
    # 要证明 send_error（401/404/400）这些**错误路径**也带上了这个头。
    import http.client
    import socket
    import threading
    import time as _t
    root = tempfile.mkdtemp()
    srv = None
    try:
        m = load_module(root)
        m.ensure_dirs()
        tok = m.load_or_create_token()
        srv = m.make_server(0, tok)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        _t.sleep(0.3)

        def head(path, with_token=True, method="GET", body=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            h = {}
            if with_token:
                h["X-Dafeiyu-Token"] = tok
            if body is not None:
                h["Content-Type"] = "application/json"
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            r.read()
            conn = r.getheader("Connection")
            c.close()
            return r.status, conn

        cases = [
            ("/health", True, "GET", None, "正常 JSON 接口"),
            ("/instances", True, "GET", None, "列表接口"),
            ("/nope", True, "GET", None, "404 路径"),
            ("/health", False, "GET", None, "★ 401 未授权（错误路径）"),
            ("/instance/config?name=x", True, "GET", None, "config 接口"),
            ("/instance/start", True, "POST", b'{"name":"x"}', "POST 接口"),
        ]
        for path, wt, meth, body, label in cases:
            try:
                code, conn = head(path, wt, meth, body)
                ok(conn is not None and conn.lower() == "close",
                   "%s → Connection: close（实际 %r，HTTP %s）"
                   % (label, conn, code))
            except Exception as e:  # noqa: BLE001
                ok(False, "%s 请求失败：%s: %s" % (label, type(e).__name__, e))

        # 服务器发完必须真的关连接，不能留着 —— 否则 OkHttp 池里那条
        # 连接还是会被复用，问题照旧。
        s = socket.create_connection(("127.0.0.1", port), 5)
        s.settimeout(5)
        s.sendall(("GET /health HTTP/1.1\r\nHost: x\r\nX-Dafeiyu-Token: %s\r\n\r\n"
                   % tok).encode())
        _t.sleep(0.3)
        s.recv(65536)
        try:
            s.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
            _t.sleep(0.3)
            r2 = s.recv(65536)
            ok(not r2, "★ 服务器发完就关连接（第二次 recv 得到 EOF，不是新响应）")
        except (ConnectionError, OSError):
            ok(True, "★ 服务器发完就关连接（第二次写入被拒）")
        finally:
            s.close()
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        shutil.rmtree(root, ignore_errors=True)


def test_private_lock():
    """私密机器人：密码锁必须真的挡得住「看」和「改」。

    需求原话：「还有没有可以设为私密的机器人配置，可以用密码来解锁」。

    ★ 这组测试的重点不是「有没有锁」，而是**每个出口都过了闸**。
    加个 /instance/unlock 接口很容易，但原来通用的 /instance/<名字> 路由
    本来就把 config 和 webui_token 一起返回 —— 不堵住它，锁就形同虚设。
    所以这里逐个出口验证。
    """
    print("\n【私密机器人：密码锁】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        m.create_instance("p1")

        # ① 默认不锁
        meta = m.load_meta("p1")
        eq(m.lock_state(meta), {"locked": False}, "默认不锁")

        # ② 设锁：密码不能明文落盘
        m.set_instance_lock("p1", True, "hunter2", "")
        meta = m.load_meta("p1")
        ok(m.lock_state(meta)["locked"], "设锁后状态为 locked")
        raw = open(os.path.join(m.instance_dir("p1"), "instance.json"),
                   encoding="utf-8").read()
        ok("hunter2" not in raw, "★ 密码不明文落盘")
        ok("hash" in raw and "salt" in raw, "存的是盐 + 派生值")
        lock = meta["lock"]
        ok(lock.get("iterations") == m.PBKDF2_ITERATIONS,
           "记录了迭代次数（%s）" % lock.get("iterations"))
        ok(m.PBKDF2_ITERATIONS >= 100000,
           "★ 迭代次数够高（%d，抗暴力破解）" % m.PBKDF2_ITERATIONS)

        # ③ 盐必须每次不同 —— 否则两个用户设同样的密码会得到同样的哈希，
        #    撞库一下就全暴露了
        m.create_instance("p2")
        m.set_instance_lock("p2", True, "hunter2", "")
        ok(m.load_meta("p1")["lock"]["salt"] != m.load_meta("p2")["lock"]["salt"],
           "★ 相同密码的盐不同（防撞库）")
        ok(m.load_meta("p1")["lock"]["hash"] != m.load_meta("p2")["lock"]["hash"],
           "★ 相同密码的派生值不同")

        # ④ 密码校验
        ok(m._check_lock_password("hunter2", meta), "正确密码通过")
        ok(not m._check_lock_password("hunter3", meta), "错误密码不通过")
        ok(not m._check_lock_password("", meta), "空密码不通过")
        ok(not m._check_lock_password("HUNTER2", meta), "大小写敏感")
        # 元数据坏掉时必须 fail-closed（宁可锁着，也不能因为读不到就放行）
        broken = {"lock": {"enabled": True, "salt": "", "hash": ""}}
        ok(not m._check_lock_password("hunter2", broken),
           "★ 元数据损坏时 fail-closed（不放行）")

        # ⑤ ★ 关键：通用详情路由不能泄露配置和 WebUI token
        d = m.instance_detail("p1")
        ok(d["locked"], "详情报告 locked=True")
        ok(d["config"] is None, "★ 锁着时 config 为 None（不泄露配置）")
        eq(d["webui_token"], "", "★ 锁着时 webui_token 为空（不泄露登录凭据）")
        ok("lock" in d and "salt" not in json.dumps(d),
           "★ 详情里不含盐/哈希")

        # ⑥ 解锁：密码对才给配置，错就报错
        raises(lambda: m.unlock_instance("p1", "wrong"),
               "密码不对", "★ 密码错时不返回任何配置")
        r = m.unlock_instance("p1", "hunter2")
        ok(r["unlocked"], "密码对时解锁成功")
        ok("config" in r, "解锁后给出配置")
        ok("api_key" not in json.dumps(r.get("config") or {}),
           "★ 解锁后仍然不回显 API Key")

        # ⑦ ★ 锁只挡「看」不挡「改」= 没锁。改配置必须验密码。
        raises(lambda: m.apply_config("p1", "123456", "", "", "", "", ""),
               "私密", "★ 没密码改不了配置")
        raises(lambda: m.apply_config("p1", "123456", "", "", "", "", "", "bad"),
               "密码不对", "★ 密码错改不了配置")

        # ⑧ 改密码要验旧密码
        raises(lambda: m.set_instance_lock("p1", True, "newpass", "wrong"),
               "旧密码不对", "★ 改密码要验旧密码")
        m.set_instance_lock("p1", True, "newpass", "hunter2")
        ok(m._check_lock_password("newpass", m.load_meta("p1")), "改密码成功")
        ok(not m._check_lock_password("hunter2", m.load_meta("p1")), "旧密码失效")

        # ⑨ 取消锁
        m.set_instance_lock("p1", False, "", "newpass")
        ok(not m.lock_state(m.load_meta("p1"))["locked"], "能取消私密")
        d2 = m.instance_detail("p1")
        ok(not d2["locked"], "取消后详情不锁")
        ok("lock" not in m.load_meta("p1"), "取消后元数据里不留锁信息")

        # ⑩ 密码长度校验
        raises(lambda: m.set_instance_lock("p2", True, "1", "hunter2"),
               "至少 4 位", "★ 太短的密码被拒（防被猜到）")
        raises(lambda: m.set_instance_lock("p2", True, "x" * 200, "hunter2"),
               "太长", "超长密码被拒")

        # ⑪ 列表接口不能泄露盐/哈希
        lst = json.dumps([dict(x, lock=m.lock_state(x)) for x in m.all_instances()])
        ok("salt" not in lst and "hash" not in lst,
           "★ 列表里不含盐/哈希")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_api_probe_error_hints():
    """API 探测的错误提示：必须把「地址写错」和「服务器没网」分开。

    ★ 这一组是**真机实测发现的 bug** 的回归测试。
    实测把 api.deepseek.com 打成 api.deepsek.com，底层报
    「[Errno 101] Network is unreachable」—— 照字面翻译就是
    「服务器出不去，检查服务器网络」，把用户指去折腾服务器，
    而真正的问题只是少打了一个字母。域名写错时会解析到某个不相干的 IP
    （deepsek.com → 31.13.82.33，一个 Facebook 地址段），连过去自然 unreachable。
    """
    print("\n【API 探测：错误提示要指对方向】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()

        # ① 域名解析不了 → 必须说「地址写错了」，不能说服务器没网
        msg = m._api_error_hint(
            Exception("<urlopen error [Errno -2] Name or service not known>"),
            "https://api.deepsek.com/v1")
        ok("解析失败 → 提示地址写错" in msg or "域名解析不了" in msg,
           "域名解析不了时说「地址写错」：%s" % msg[:40])
        ok("服务器" not in msg.split("——")[0],
           "解析失败时不提服务器网络")

        # ② 网络不可达 + 服务器**有网** → 必须说「地址写错了」
        m._dns_ok = lambda h: True
        m._reference_reachable = lambda: True
        msg = m._api_error_hint(
            Exception("<urlopen error [Errno 101] Network is unreachable>"),
            "https://api.deepsek.com/v1")
        ok("地址写错" in msg,
           "★ 服务器有网而地址连不上 → 指出是地址写错（真机 bug 的回归）")
        ok("服务器的网络问题" not in msg,
           "★ 不再甩锅给「服务器的网络问题」")

        # ③ 服务器**确实没网** → 这时才该说服务器网络
        m._reference_reachable = lambda: False
        msg = m._api_error_hint(
            Exception("<urlopen error [Errno 101] Network is unreachable>"),
            "https://api.deepseek.com/v1")
        ok("服务器的网络问题" in msg,
           "服务器真没网时才说服务器网络")

        # ④ 域名解析不出来时也走「地址写错」分支（不依赖参考探测）
        m._dns_ok = lambda h: False
        m._reference_reachable = lambda: True
        msg = m._api_error_hint(Exception("connection refused"),
                                "https://api.deepsek.com/v1")
        ok("地址写错" in msg or "解析不了" in msg,
           "域名解析不了时指出地址写错")

        # ⑤ 证书问题单独说
        msg = m._api_error_hint(Exception("certificate verify failed"),
                                "https://x/v1")
        ok("证书" in msg, "证书问题单独提示")

        # ⑥ _host_of 能取出主机名（解析失败判断依赖它）
        eq(m._host_of("https://api.deepseek.com/v1"), "api.deepseek.com",
           "_host_of 取出主机名")
        eq(m._host_of(""), "", "_host_of 空串返回空")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_api_probe_no_false_green():
    """★ 最重要的一组：不能给出**假绿灯**。

    真机实测：OpenRouter 的 /models 用**无效 Key** 也返回 200（它不鉴权），
    但 /chat/completions 用同样的无效 Key 返回 401。
    早先的实现拿 /models 的成功当作「Key 没问题」，于是对一个坏 Key 报
    「通了 ✓」—— 用户看到绿灯，然后发现机器人根本不回话。
    这比不做检测更糟：他会以为是别的地方坏了，查很久。

    所以：只有真实聊天请求成功才算通。
    """
    print("\n【API 探测：绝不能假绿灯】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        m.create_instance("t1")
        # 造一份配置，让 _main_api_of 能读出来
        os.makedirs(os.path.dirname(m.astrbot_cfg_path("t1")), exist_ok=True)
        with open(m.astrbot_cfg_path("t1"), "w", encoding="utf-8") as fh:
            json.dump({
                "provider_sources": [{"id": "dafeiyu-main_source",
                                      "api_base": "https://openrouter.ai/api/v1",
                                      "key": ["sk-bad"]}],
                "provider": [{"id": "dafeiyu-main", "model": "openai/gpt-4o-mini"}],
            }, fh)

        # 模拟 OpenRouter 那种服务商：/models 不鉴权（200），chat 才鉴权（401）
        def fake_request(url, key, payload=None):
            if url.endswith("/models"):
                return 200, json.dumps({"data": [{"id": "openai/gpt-4o-mini"},
                                                 {"id": "anthropic/claude-3"}]})
            return 401, '{"error":{"message":"Missing Authentication header"}}'
        m._api_request = fake_request

        r = m.probe_api("t1", "", "", "")
        eq(r["reachable"], True, "地址可达（/models 通了）")
        eq(r["auth_ok"], False,
           "★ 坏 Key 必须 auth_ok=False（/models 通不算数，要真聊天请求）")
        ok("Key 不对" in r["message"], "★ 明确说 Key 不对，而不是「通了 ✓」")
        ok("通了 ✓" not in r["message"], "★ 不给假绿灯")

        # 好的 Key（chat 返回 200）→ 这时才该报通
        def fake_ok(url, key, payload=None):
            if url.endswith("/models"):
                return 200, json.dumps({"data": [{"id": "openai/gpt-4o-mini"}]})
            return 200, '{"choices":[{"message":{"content":"hi"}}]}'
        m._api_request = fake_ok
        r = m.probe_api("t1", "", "", "")
        eq(r["auth_ok"], True, "真聊天请求成功才算 Key 可用")
        eq(r["model_ok"], True, "模型名核对通过")
        ok("通了 ✓" in r["message"], "全对时明确报通")

        # 模型名写错（chat 返回 404/400 且提到 model）→ 要指出是模型名的问题
        def fake_badmodel(url, key, payload=None):
            if url.endswith("/models"):
                return 200, json.dumps({"data": [{"id": "openai/gpt-4o-mini"}]})
            return 400, '{"error":{"message":"model not found: bogus-model"}}'
        m._api_request = fake_badmodel
        r = m.probe_api("t1", "https://openrouter.ai/api/v1", "sk-good",
                        "bogus-model")
        eq(r["model_ok"], False, "★ 模型名错时 model_ok=False")
        ok("模型" in r["message"], "★ 指出是模型名的问题（最常见的错误）")
        ok("挑一个" in r["message"] or "列表" in r["message"],
           "★ 告诉用户从列表里挑（可执行）")

        # 没有模型名时不能说「通了」—— 没法验证，就是没法验证。
        # 注意：传空会**回落到配置里已保存的模型名**（和 base/key 同一套约定），
        # 所以这里要造一个「配置里也没模型名」的实例，才是真正的「没填」。
        m._api_request = fake_ok
        with open(m.astrbot_cfg_path("t1"), "w", encoding="utf-8") as fh:
            json.dump({
                "provider_sources": [{"id": "dafeiyu-main_source",
                                      "api_base": "https://openrouter.ai/api/v1",
                                      "key": ["sk-good"]}],
                "provider": [{"id": "dafeiyu-main", "model": ""}],
            }, fh)
        r = m.probe_api("t1", "https://openrouter.ai/api/v1", "sk-good", "")
        eq(r["auth_ok"], False, "★ 没填模型名时不能宣称 Key 已验证")
        ok("通了 ✓" not in r["message"], "★ 没模型名时不给绿灯")
        ok("模型名" in r["message"], "提示去填模型名")
        eq(r["model_ok"], None, "没模型名时 model_ok 是 None（没测，不是失败）")

        # 地址不通 → 不必再发聊天请求（省一次往返和一次可能的费用）
        calls = []

        def counting_request(url, key, payload=None):
            calls.append(url)
            raise OSError("Network is unreachable")
        m._api_request = counting_request
        m._dns_ok = lambda h: False
        r = m.probe_api("t1", "https://api.deepsek.com/v1", "sk-x", "m")
        eq(len(calls), 1, "★ 地址不通时只发一次请求（不浪费一次聊天调用）")
        eq(r["reachable"], False, "地址不通时 reachable=False")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_api_probe_big_model_list():
    """★ 大模型列表不能被截断（真机 bug）。

    OpenRouter 的 /models 实测有 **737KB**。早先把响应体截到 200000 字节，
    json.loads 报「Unterminated string starting at ...」，
    于是把一个**完全正常**的接口判成「对方返回的不是 JSON，
    可能不是 OpenAI 兼容接口」—— 用户会以为这家不能用，转头换一家。

    这个 bug 只在「模型列表特别长」的服务商上出现，
    用一个小列表在本地测永远发现不了，所以必须专门造一个大的。
    """
    print("\n【API 探测：大模型列表不能被截断】")
    root = tempfile.mkdtemp()
    try:
        m = load_module(root)
        m.ensure_dirs()
        m.create_instance("t1")
        os.makedirs(os.path.dirname(m.astrbot_cfg_path("t1")), exist_ok=True)
        with open(m.astrbot_cfg_path("t1"), "w", encoding="utf-8") as fh:
            json.dump({
                "provider_sources": [{"id": "dafeiyu-main_source",
                                      "api_base": "https://openrouter.ai/api/v1",
                                      "key": ["sk-x"]}],
                "provider": [{"id": "dafeiyu-main", "model": "m"}],
            }, fh)

        # 上限必须远大于 737KB —— 写死一个具体数字，防止有人又调小
        ok(m.MAX_API_BODY >= 1024 * 1024,
           "★ 响应体上限 >= 1MB（实测 OpenRouter 是 737KB）")

        # 造一个 ~700KB 的模型列表，走**真实的解析路径**（不 stub _api_request，
        # 而是 stub 到 socket 层，确保测的是真的 json.loads）
        big = {"data": [{"id": "vendor%d/model-%d" % (i, i),
                         "description": "x" * 200} for i in range(3000)]}
        body = json.dumps(big)
        ok(len(body) > 600000, "造出的响应体 > 600KB（实际 %d）" % len(body))

        class FakeResp:
            def __init__(self, data):
                self._d = data
            def getcode(self):
                return 200
            def read(self, n=None):
                return self._d if n is None else self._d[:n]
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        m.urllib.request.urlopen = lambda req, timeout=None: FakeResp(
            body.encode("utf-8"))
        got = m.list_api_models("t1", "https://openrouter.ai/api/v1", "sk-x")
        eq(len(got), 3000, "★ 大列表完整解析（截断的话这里会抛异常/数量不对）")
        ok("vendor0/model-0" in got, "列表内容正确")
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
        # 真机反馈「WebUI 连不上 / 白屏」两个根因的回归
        test_proxy_decompresses_gzip,
        test_responses_declare_connection_close,
        # 私密机器人（密码解锁）
        test_private_lock,
        test_route_ordering,
        test_apply_config_verifies_after_restart,
        test_main_provider_is_first_in_list,
        test_read_config_reports_provider_ok,
        test_app_does_not_block_save_when_not_ready,
        test_persona_written_after_ready,
        test_ready_waits_for_personas_table,
        test_missing_config_tells_two_people_apart,
        test_no_log_wait_is_capped,
        test_apply_config_fits_app_timeout,
        # API 探测（「API 是否连通」这条反馈）—— 含真机实测发现的 3 个 bug 的回归
        test_api_probe_error_hints,
        test_api_probe_no_false_green,
        test_api_probe_big_model_list,
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
