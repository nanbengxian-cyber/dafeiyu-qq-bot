#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dafeiyu-manager —— 手机 App 的服务器端管理服务。

设计目标（对应「三个配置就启动、并且能多开」）：
  * App 只管三件事：① 主聊天 API ② 要聊天的群/私聊号 ③ 人格提示词。
  * 服务器侧一切（目录、端口、容器名、WebUI token）由本服务自动分配，
    用户永远不用碰。
  * 多开 = 一个「实例」= 一套 napcat + astrbot 容器 + 独立数据目录 + 独立端口。
    实例之间完全隔离，各登各的 QQ 号。

安全模型（重要，别削弱）：
  * 本服务只监听 127.0.0.1，**绝不监听 0.0.0.0**。手机通过 SSH 本地转发访问，
    所以公网上永远没有这个端口。
  * 管理口令（manager token）存在 0600 的文件里，App 用 SSH 隧道读到后放在
    Authorization 头里。SSH 本身就是第一道认证，token 是第二道。
  * 所有对外写文件的操作都做路径校验，实例名只允许 [a-z0-9-]，杜绝路径穿越。
  * 本服务**不接触** QQ 密码、不接触主聊天 API Key 的明文落盘以外的地方：
    API Key 写进实例自己的 astrbot 配置里（0600），管理服务不额外留存。

用法：
    dafeiyu-manager.py serve [--port 6199]
    dafeiyu-manager.py list
    dafeiyu-manager.py create --name test1
    dafeiyu-manager.py destroy --name test1
"""

import argparse
import base64
import hashlib
import json
import urllib.parse
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 常量：目录布局
# ---------------------------------------------------------------------------

ROOT = os.environ.get("DAFEIYU_ROOT", "/opt/dafeiyu")
INSTANCES_DIR = os.path.join(ROOT, "instances")
MANAGER_DIR = os.path.join(ROOT, "manager")
TOKEN_FILE = os.path.join(MANAGER_DIR, "manager.token")
COMPOSE_TEMPLATE = os.path.join(MANAGER_DIR, "compose.template.yml")

NAPCAT_IMAGE = os.environ.get("DAFEIYU_NAPCAT_IMAGE", "mlikiowa/napcat-docker:latest")
ASTRBOT_IMAGE = os.environ.get("DAFEIYU_ASTRBOT_IMAGE", "soulter/astrbot:latest")

# 端口分配基数。每实例占 3 个连续端口：napcat WebUI / OneBot / astrbot 面板。
# 从 16000 起，避开生产机上的 6099/6185/3001 等。
PORT_BASE = 16000
PORTS_PER_INSTANCE = 3
MAX_INSTANCES = 20

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


class ManagerError(Exception):
    """给 App 看的、可以直接展示的错误。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def ensure_dirs():
    for d in (ROOT, INSTANCES_DIR, MANAGER_DIR):
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)


def load_or_create_token():
    """管理口令：首次运行生成，之后复用。只存 0600 文件，不进日志。"""
    ensure_dirs()
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    # 先写临时文件再原子改名，避免并发下读到半截
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(tok)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_FILE)
    return tok


def instance_dir(name):
    if not NAME_RE.match(name or ""):
        raise ManagerError("实例名只能用 小写字母/数字/短横线，且不超过 31 个字符。")
    return os.path.join(INSTANCES_DIR, name)


def load_meta(name):
    d = instance_dir(name)
    meta_path = os.path.join(d, "instance.json")
    if not os.path.exists(meta_path):
        raise ManagerError("没有这个实例：%s" % name)
    with open(meta_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_meta(name, meta):
    d = instance_dir(name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "instance.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def all_instances():
    if not os.path.isdir(INSTANCES_DIR):
        return []
    out = []
    for n in sorted(os.listdir(INSTANCES_DIR)):
        p = os.path.join(INSTANCES_DIR, n, "instance.json")
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (ValueError, OSError):
                continue
    return out


def used_ports():
    """已被实例占用的端口集合（从 meta 读，不依赖运行状态，避免误分配）。"""
    ports = set()
    for m in all_instances():
        for k in ("webui_port", "onebot_port", "panel_port"):
            if m.get(k):
                ports.add(int(m[k]))
    return ports


def alloc_ports():
    used = used_ports()
    for i in range(MAX_INSTANCES):
        base = PORT_BASE + i * PORTS_PER_INSTANCE
        trio = (base, base + 1, base + 2)
        if not any(p in used for p in trio):
            return trio
    raise ManagerError("端口用满了（最多 %d 个实例）。" % MAX_INSTANCES)


def port_free(port):
    """确认端口在本机真的没被占用（分配前再查一次，防止和别的服务撞）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def run(cmd, cwd=None, timeout=300, check=True):
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    if check and p.returncode != 0:
        raise ManagerError("命令失败：%s\n%s" % (" ".join(cmd), out[-1500:]))
    return p.returncode, out


def compose(name, *args, **kw):
    d = instance_dir(name)
    return run(["docker", "compose", "-p", "dafeiyu-" + name] + list(args), cwd=d, **kw)


# ---------------------------------------------------------------------------
# 实例编排
# ---------------------------------------------------------------------------

def render_compose(name, meta):
    """按实例生成 compose 文件。

    要点：
      * 端口全部绑 127.0.0.1 —— 公网不可达，手机走 SSH 隧道。
      * 每实例独立 bridge 网络，避免跨实例串门。
      * napcat 固定 MAC：QQ 把 MAC 算进设备指纹，随机 MAC 会让每次登录
        都被当成新设备（生产机上踩过这个坑，这里一开始就固定）。
    """
    mac = meta.get("mac") or ""
    return """services:
  napcat:
    image: {napcat_image}
    container_name: dafeiyu-{name}-napcat
    restart: always
    mem_limit: 1g
    logging:
      driver: json-file
      options: {{ max-size: "20m", max-file: "3" }}
    ports:
      - "127.0.0.1:{webui_port}:6099"
      - "127.0.0.1:{onebot_port}:3001"
    volumes:
      - ./napcat/config:/app/config
      - ./napcat/persist/qqconfig/.config:/app/.config
      - ./napcat/persist/napcatcfg/config:/app/napcat/config
    environment:
      - NAPCAT_UID=1000
      - NAPCAT_GID=1000
    mac_address: "{mac}"
    networks: [net]

  astrbot:
    image: {astrbot_image}
    container_name: dafeiyu-{name}-astrbot
    restart: always
    mem_limit: 1g
    depends_on: [napcat]
    logging:
      driver: json-file
      options: {{ max-size: "20m", max-file: "3" }}
    ports:
      - "127.0.0.1:{panel_port}:6185"
    volumes:
      - ./astrbot/data:/AstrBot/data
    networks: [net]

networks:
  net:
    driver: bridge
""".format(name=name, napcat_image=NAPCAT_IMAGE, astrbot_image=ASTRBOT_IMAGE,
           webui_port=meta["webui_port"], onebot_port=meta["onebot_port"],
           panel_port=meta["panel_port"], mac=mac)


def gen_mac():
    """本地管理位 + 随机：保证单机内唯一即可。"""
    b = [0x02] + [secrets.randbelow(256) for _ in range(5)]
    return ":".join("%02x" % x for x in b)


def create_instance(name):
    if os.path.exists(instance_dir(name)):
        raise ManagerError("实例 %s 已经存在了。" % name)
    webui, onebot, panel = alloc_ports()
    for p in (webui, onebot, panel):
        if not port_free(p):
            raise ManagerError("端口 %d 被别的程序占用了，换个实例名再试。" % p)

    meta = {
        "name": name,
        "webui_port": webui,
        "onebot_port": onebot,
        "panel_port": panel,
        "mac": gen_mac(),
        "created_at": int(time.time()),
        "status": "created",
    }
    d = instance_dir(name)
    for sub in ("napcat/config", "napcat/persist/qqconfig/.config",
                "napcat/persist/napcatcfg/config", "astrbot/data"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    save_meta(name, meta)
    with open(os.path.join(d, "docker-compose.yml"), "w", encoding="utf-8") as fh:
        fh.write(render_compose(name, meta))
    os.chmod(os.path.join(d, "docker-compose.yml"), 0o600)
    return meta


def destroy_instance(name):
    meta = load_meta(name)
    compose(name, "down", "-v", check=False)
    shutil.rmtree(instance_dir(name), ignore_errors=True)
    return {"name": meta["name"], "destroyed": True}


def start_instance(name):
    meta = load_meta(name)
    compose(name, "up", "-d")
    meta["status"] = "running"
    save_meta(name, meta)
    return meta


def stop_instance(name):
    meta = load_meta(name)
    compose(name, "stop")
    meta["status"] = "stopped"
    save_meta(name, meta)
    return meta


def container_state(name):
    """返回实例两个容器的实际状态（以 docker 为准，不信 meta）。"""
    out = {}
    for role in ("napcat", "astrbot"):
        cname = "dafeiyu-%s-%s" % (name, role)
        rc, txt = run(["docker", "inspect", "-f",
                       "{{.State.Status}}", cname], check=False)
        out[role] = txt.strip() if rc == 0 else "absent"
    return out


# ---------------------------------------------------------------------------
# 三配置：写进实例自己的 astrbot 配置
# ---------------------------------------------------------------------------

def astrbot_cfg_path(name):
    return os.path.join(instance_dir(name), "astrbot", "data", "cmd_config.json")


def read_json_maybe_bom(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return json.loads(raw.decode("utf-8"))


def write_json_bom(path, data):
    """AstrBot 的配置是 utf-8-sig（带 BOM）—— 不按它的格式写，它会读不进去。"""
    tmp = path + ".tmp"
    text = json.dumps(data, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8-sig") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def normalize_ids(raw, label):
    """逗号/空格/顿号/换行分隔 → 去重列表；只允许 5-12 位数字。"""
    if not raw:
        return []
    s = raw.replace("，", ",").replace("、", ",").replace(" ", ",")
    s = s.replace("\n", ",").replace("\r", ",").replace("\t", ",")
    out = []
    for part in s.split(","):
        p = part.strip()
        if not p:
            continue
        if not re.match(r"^[0-9]{5,12}$", p):
            raise ManagerError("%s「%s」不是纯数字的 QQ 号/群号。" % (label, p))
        if p not in out:
            out.append(p)
    return out


def persona_db_path(name):
    return os.path.join(instance_dir(name), "astrbot", "data", "data_v4.db")


def write_persona_db(name, persona_id, prompt):
    """把人格写进 AstrBot 的 SQLite（personas 表）。

    表结构来自 astrbot/core/db/po.py 的 Persona 模型：
      persona_id(唯一) / system_prompt / begin_dialogs(JSON) /
      tools(JSON, NULL=用全部) / skills / custom_error_message /
      folder_id(NULL=根目录) / sort_order / created_at / updated_at

    tools 写 NULL 表示「用全部工具」—— 用户只填了人格提示词，
    没表达任何工具限制意图，所以不该替他收窄能力。
    """
    import sqlite3

    path = persona_db_path(name)
    if not os.path.exists(path):
        raise ManagerError("实例的数据库还没生成。请先「启动」一次，等 AstrBot "
                           "初始化完再设人格。")

    conn = sqlite3.connect(path, timeout=15)
    try:
        # 先确认表在（不同版本可能还没建）
        has = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='personas'"
        ).fetchone()
        if not has:
            raise ManagerError("这个 AstrBot 版本还没有 personas 表，"
                               "没法写人格。请先把实例升级或手动设置。")

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute(
            "SELECT id FROM personas WHERE persona_id=?", (persona_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE personas SET system_prompt=?, updated_at=? WHERE persona_id=?",
                (prompt, now, persona_id))
        else:
            conn.execute(
                "INSERT INTO personas (created_at, updated_at, persona_id,"
                " system_prompt, begin_dialogs, tools, skills,"
                " custom_error_message, folder_id, sort_order)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now, now, persona_id, prompt, "[]", None, None, None, None, 0))
        conn.commit()

        # 回读校验：写完必须能读回来，且内容一致
        back = conn.execute(
            "SELECT system_prompt FROM personas WHERE persona_id=?",
            (persona_id,)).fetchone()
        if not back or back[0] != prompt:
            raise ManagerError("回读校验失败：人格没写进数据库。")
    finally:
        conn.close()


def apply_config(name, groups, friends, api_base, api_key, api_model, persona):
    """把三配置写进实例的 AstrBot。

    写之前先备份原文件；写之后**回读校验**（生产机的教训：写完不读回，
    写坏了也不知道）。
    """
    # 检查顺序有讲究，按「最可能出错 + 最便宜」排：
    #   ① 先校验用户填的内容 —— 输错 QQ 号是最常见的情况，且不用碰磁盘；
    #   ② 再确认实例存在 —— 名字打错时立刻说清楚，而不是报「没跑起来过」把人带偏；
    #   ③ 最后看配置有没有生成（需要实例启动过一次）。
    gids = normalize_ids(groups, "群号")
    fids = normalize_ids(friends, "私聊 QQ 号")

    api_any = bool(api_base or api_key or api_model)
    if api_any and not (api_base and api_key and api_model):
        raise ManagerError("主聊天 API 要填全：接口地址、API Key、模型名缺一不可。")
    if api_any:
        if not (api_base.startswith("http://") or api_base.startswith("https://")):
            raise ManagerError("接口地址要以 http:// 或 https:// 开头。")
        if " " in api_base:
            raise ManagerError("接口地址里不能有空格。")
        if "\n" in api_key or "\r" in api_key:
            raise ManagerError("API Key 里不能有换行。")
        if " " in api_model or "\n" in api_model or "\r" in api_model:
            raise ManagerError("模型名里不能有空格或换行。")

    has_persona = bool(persona and persona.strip())
    if not (gids or fids or api_any or has_persona):
        raise ManagerError("没填任何要改的内容。")

    load_meta(name)  # 实例不存在 → 在这里就报清楚

    path = astrbot_cfg_path(name)
    if not os.path.exists(path):
        raise ManagerError("实例还没跑起来过，AstrBot 配置还没生成。"
                           "请先点「启动」，等它初始化完再填配置。")

    cfg = read_json_maybe_bom(path)
    backup = path + ".bak.%d" % int(time.time())
    shutil.copy2(path, backup)

    changed = []

    # ① 聊天范围：白名单
    if gids or fids:
        plat = "default"
        plats = cfg.get("platform") or []
        if plats and plats[0].get("id"):
            plat = plats[0]["id"]
        wl = list(gids) + ["%s:FriendMessage:%s" % (plat, f) for f in fids]
        ps = cfg.setdefault("platform_settings", {})
        ps["enable_id_white_list"] = True
        ps["id_whitelist"] = wl
        changed.append("聊天范围（%d 个群 / %d 个私聊）" % (len(gids), len(fids)))

    # ② 主聊天 API
    if api_base and api_key and api_model:
        src_id = "dafeiyu-main_source"
        pid = "dafeiyu-main"
        src = {
            "id": src_id, "provider": "openai", "type": "openai_chat_completion",
            "provider_type": "chat_completion", "key": [api_key],
            "api_base": api_base, "model_config": {}, "timeout": 120,
        }
        prov = {"id": pid, "provider_source_id": src_id, "model": api_model}
        cfg["provider_sources"] = [s for s in (cfg.get("provider_sources") or [])
                                   if s.get("id") != src_id] + [src]
        cfg["provider"] = [p for p in (cfg.get("provider") or [])
                           if p.get("id") != pid] + [prov]
        cfg.setdefault("provider_settings", {})["default_provider_id"] = pid
        changed.append("主聊天 API（%s）" % api_model)

    # ③ 人格提示词
    #
    # 这里**不写 cmd_config.json**。AstrBot 源码里那个 persona 字段标着
    # `# deprecated`（core/config/default.py:306），写进去它根本不读 ——
    # 用户会以为设了人格，实际机器人还是老样子，而且没有任何报错。
    #
    # 真实的人格存在 SQLite 的 personas 表里（core/persona_mgr.py 用
    # db.get_persona_by_id 取），再由 provider_settings.default_personality
    # 指定当前用哪一条。所以这里要写库，并且把默认人格指过去。
    if has_persona:
        pid = "dafeiyu-mine"
        write_persona_db(name, pid, persona.strip())
        cfg.setdefault("provider_settings", {})["default_personality"] = pid
        changed.append("人格提示词（%d 字）" % len(persona.strip()))

    if not changed:
        raise ManagerError("没填任何要改的内容。")

    write_json_bom(path, cfg)

    # 回读校验：确认真的落盘了
    back = read_json_maybe_bom(path)
    if gids or fids:
        got = (back.get("platform_settings") or {}).get("id_whitelist")
        if got != list(gids) + ["%s:FriendMessage:%s" % (
                ((back.get("platform") or [{}])[0].get("id") or "default"), f)
                for f in fids]:
            raise ManagerError("回读校验失败：聊天范围没写进去。")
    if api_base:
        got = (back.get("provider_settings") or {}).get("default_provider_id")
        if got != "dafeiyu-main":
            raise ManagerError("回读校验失败：主聊天 API 没写进去。")

    # 重启实例的 astrbot 让配置生效
    compose(name, "restart", "astrbot", check=False)

    return {"ok": True, "changed": changed, "verified": True,
            "backup": os.path.basename(backup)}


def read_config(name):
    path = astrbot_cfg_path(name)
    if not os.path.exists(path):
        return {"ready": False}
    cfg = read_json_maybe_bom(path)
    ps = cfg.get("platform_settings") or {}
    wl = ps.get("id_whitelist") or []
    groups = [x for x in wl if re.match(r"^[0-9]+$", str(x))]
    friends = []
    for x in wl:
        m = re.match(r"^[^:]+:FriendMessage:([0-9]+)$", str(x))
        if m:
            friends.append(m.group(1))
    prov = (cfg.get("provider_settings") or {}).get("default_provider_id") or ""
    src = {}
    for s in (cfg.get("provider_sources") or []):
        if s.get("id") == "dafeiyu-main_source":
            src = s
    model = ""
    for p in (cfg.get("provider") or []):
        if p.get("id") == "dafeiyu-main":
            model = p.get("model") or ""
    # 人格要从数据库读（cmd_config.json 里的 persona 字段是废弃的，永远是空）
    persona = ""
    default_pid = (cfg.get("provider_settings") or {}).get("default_personality") or ""
    if default_pid:
        db = persona_db_path(name)
        if os.path.exists(db):
            import sqlite3
            try:
                conn = sqlite3.connect(db, timeout=10)
                try:
                    row = conn.execute(
                        "SELECT system_prompt FROM personas WHERE persona_id=?",
                        (default_pid,)).fetchone()
                    if row:
                        persona = row[0] or ""
                finally:
                    conn.close()
            except sqlite3.Error:
                # 读不到就当没设，不要让整个配置读取失败
                persona = ""
    return {
        "ready": True,
        "persona_id": default_pid,
        "groups": groups,
        "friends": friends,
        "api_base": src.get("api_base") or "",
        # Key 不回显：只告诉 App「有没有配」，避免密钥在网络上往返
        "api_key_set": bool(src.get("key")),
        "api_model": model,
        "persona": persona,
        "provider_ok": bool(prov),
    }


# ---------------------------------------------------------------------------
# 实例的 WebUI 访问信息（给 App 建 SSH 隧道用）
# ---------------------------------------------------------------------------

def webui_token(name):
    """读实例 NapCat WebUI 的 token（App 需要它才能登录 WebUI）。

    注意：这里读的是**实例自己**的 webui.json，不是生产机的。
    """
    d = instance_dir(name)
    cands = [
        os.path.join(d, "napcat", "persist", "napcatcfg", "config", "webui.json"),
        os.path.join(d, "napcat", "config", "webui.json"),
    ]
    for p in cands:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    return json.load(fh).get("token") or ""
            except (ValueError, OSError):
                continue
    return ""


# ---------------------------------------------------------------------------
# WebUI 反向代理
#
# 为什么要有它：App 到服务器只开**一条** SSH 隧道（连管理服务）。如果每个实例
# 的 WebUI 都要单独一条隧道，authorized_keys 的 permitopen 就得放行一大片端口；
# 让管理服务在本机替 App 转发，App 侧只需放行 6199 一个端口 —— 权限收得最窄。
# ---------------------------------------------------------------------------

PROXY_TIMEOUT = 30


def proxy_webui(name, method, path, headers, body):
    """把请求转发到该实例的 NapCat WebUI，原样返回响应。"""
    meta = load_meta(name)
    url = "http://127.0.0.1:%d/%s" % (int(meta["webui_port"]), path.lstrip("/"))

    hdrs = {}
    for k, v in headers.items():
        lk = k.lower()
        # 丢掉会干扰转发的头（Host/长度/连接）和我们自己的管理鉴权头
        if lk in ("host", "content-length", "connection", "x-dafeiyu-token"):
            continue
        hdrs[k] = v
    hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=body if body else None,
                                 headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT) as resp:
            return resp.getcode(), resp.read(), resp.headers.get(
                "Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        # NapCat 用 4xx 表达业务错误（未授权等），原样透传，别吞成 500，
        # 否则 App 没法按自己的逻辑（比如自动重登）处理。
        return e.code, e.read(), e.headers.get("Content-Type", "application/json")
    except urllib.error.URLError as e:
        raise ManagerError("连不上这个实例的 WebUI（可能容器还没起来）：%s" % e.reason)


# ---------------------------------------------------------------------------
# HTTP 服务（只监听 127.0.0.1）
# ---------------------------------------------------------------------------

def json_response(handler, code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_server(port, token):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        server_version = "dafeiyu-manager/1"

        def log_message(self, fmt, *args):
            # 不把请求内容写进日志（可能含 token）
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

        def _auth(self):
            """校验管理口令。

            两种头都认，是因为 /proxy/ 这条路要用 Authorization 干别的事：
            NapCat 自己的 WebUI 凭据就是放在 Authorization 里的，而我们要把它
            原样转发给 NapCat。所以代理请求改用 X-Dafeiyu-Token 带管理口令，
            免得两个令牌抢同一个头。
            """
            got = self.headers.get("X-Dafeiyu-Token") or ""
            if not got:
                auth = self.headers.get("Authorization") or ""
                if auth.startswith("Bearer "):
                    got = auth[7:]
            if not got:
                return False
            return secrets.compare_digest(got.strip(), token)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                raise ManagerError("请求体不是合法 JSON。")

        def _handle(self, fn):
            try:
                json_response(self, 200, fn())
            except ManagerError as e:
                json_response(self, 400, {"error": str(e)})
            except subprocess.TimeoutExpired:
                json_response(self, 500, {"error": "服务器上执行超时了，稍后再试。"})
            except Exception as e:  # noqa: BLE001 - 兜底，别让服务崩
                json_response(self, 500, {"error": "服务器内部错误：%s" % e})

        def _proxy(self, method):
            """把 /proxy/<实例名>/<WebUI 路径> 转发给该实例的 WebUI。

            WebUI 路径原样透传（含查询串），响应体原样回吐 ——
            App 里跑的就是 NapCat 的原生 WebUI 协议，不用为每个接口写适配。
            """
            rest = self.path[len("/proxy/"):]
            if "/" in rest:
                name, sub = rest.split("/", 1)
            else:
                name, sub = rest, ""
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n > 0 else b""
                code, payload, ctype = proxy_webui(name, method, sub, self.headers, raw)
            except ManagerError as e:
                json_response(self, 400, {"error": str(e)})
                return
            except Exception as e:  # noqa: BLE001
                json_response(self, 502, {"error": "代理失败：%s" % e})
                return
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self._auth():
                json_response(self, 401, {"error": "未授权。"})
                return
            if self.path.startswith("/proxy/"):
                self._proxy("GET")
                return
            path = self.path.split("?")[0]
            if path == "/health":
                self._handle(lambda: {"ok": True, "version": 1})
            elif path == "/instances":
                self._handle(lambda: {"instances": [dict(
                    m, containers=container_state(m["name"])) for m in all_instances()]})
            elif path == "/instance/config":
                # 注意顺序：这条必须排在通用的 /instance/<名字> 之前。
                # 否则 "config" 会被当成实例名，报「没有这个实例：config」——
                # 一个看起来像「实例不存在」、实际是路由写错的坑。
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                          if "?" in self.path else "")
                self._handle(lambda: read_config((q.get("name") or [""])[0]))
            elif path.startswith("/instance/"):
                name = path[len("/instance/"):].split("/")[0]
                self._handle(lambda: {"meta": load_meta(name),
                                      "containers": container_state(name),
                                      "config": read_config(name),
                                      "webui_token": webui_token(name)})
            else:
                json_response(self, 404, {"error": "没有这个接口。"})

        def do_POST(self):
            if not self._auth():
                json_response(self, 401, {"error": "未授权。"})
                return
            if self.path.startswith("/proxy/"):
                self._proxy("POST")
                return
            path = self.path.split("?")[0]
            try:
                body = self._body()
            except ManagerError as e:
                json_response(self, 400, {"error": str(e)})
                return

            if path == "/instance/create":
                self._handle(lambda: {"meta": create_instance(body.get("name") or "")})
            elif path == "/instance/start":
                self._handle(lambda: {"meta": start_instance(body["name"])})
            elif path == "/instance/stop":
                self._handle(lambda: {"meta": stop_instance(body["name"])})
            elif path == "/instance/destroy":
                self._handle(lambda: destroy_instance(body["name"]))
            elif path == "/instance/config":
                self._handle(lambda: apply_config(
                    body["name"], body.get("groups", ""), body.get("friends", ""),
                    body.get("api_base", ""), body.get("api_key", ""),
                    body.get("api_model", ""), body.get("persona", "")))
            else:
                json_response(self, 404, {"error": "没有这个接口。"})

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return srv


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="大肥鱼实例管理服务")
    sub = ap.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务（只监听 127.0.0.1）")
    p_serve.add_argument("--port", type=int, default=6199)
    p_serve.add_argument("--print-token", action="store_true",
                         help="打印管理口令（供部署脚本读取，别写进日志）")

    sub.add_parser("list", help="列出实例")
    sub.add_parser("token", help="打印管理口令")

    p_create = sub.add_parser("create", help="新建实例")
    p_create.add_argument("--name", required=True)
    p_start = sub.add_parser("start", help="启动实例")
    p_start.add_argument("--name", required=True)
    p_stop = sub.add_parser("stop", help="停止实例")
    p_stop.add_argument("--name", required=True)
    p_destroy = sub.add_parser("destroy", help="删除实例（连数据）")
    p_destroy.add_argument("--name", required=True)

    args = ap.parse_args()

    if args.cmd == "serve":
        ensure_dirs()
        tok = load_or_create_token()
        if args.print_token:
            sys.stderr.write("manager token: %s\n" % tok)
        srv = make_server(args.port, tok)
        sys.stderr.write("dafeiyu-manager 监听 127.0.0.1:%d\n" % args.port)
        srv.serve_forever()
    elif args.cmd == "list":
        print(json.dumps(all_instances(), ensure_ascii=False, indent=2))
    elif args.cmd == "token":
        print(load_or_create_token())
    elif args.cmd == "create":
        print(json.dumps(create_instance(args.name), ensure_ascii=False, indent=2))
    elif args.cmd == "start":
        print(json.dumps(start_instance(args.name), ensure_ascii=False, indent=2))
    elif args.cmd == "stop":
        print(json.dumps(stop_instance(args.name), ensure_ascii=False, indent=2))
    elif args.cmd == "destroy":
        print(json.dumps(destroy_instance(args.name), ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
