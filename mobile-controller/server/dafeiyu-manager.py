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
import calendar
import gzip
import hashlib
import json
import urllib.parse
import os
import re
import secrets
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib

# ---------------------------------------------------------------------------
# 常量：目录布局
# ---------------------------------------------------------------------------

ROOT = os.environ.get("DAFEIYU_ROOT", "/opt/dafeiyu")
INSTANCES_DIR = os.path.join(ROOT, "instances")
MANAGER_DIR = os.path.join(ROOT, "manager")
TOKEN_FILE = os.path.join(MANAGER_DIR, "manager.token")
COMPOSE_TEMPLATE = os.path.join(MANAGER_DIR, "compose.template.yml")
# App 公告 + 版本检查：数据文件（可改，改完不用重启）+ APK 存放目录。
# app-update.json 的字段：
#   latest_code   App 的 versionCode（App 拿它和本地 versionCode 比大小）
#   latest_name   显示名，如 "1.2.0"
#   announcement  公告正文（多行文本，App 弹窗显示）
#   sha256        APK 的 SHA256（App 下载完核对，防传/防换）
APP_UPDATE_FILE = os.path.join(MANAGER_DIR, "app-update.json")
APK_DIR = os.path.join(MANAGER_DIR, "apk")

NAPCAT_IMAGE = os.environ.get("DAFEIYU_NAPCAT_IMAGE", "mlikiowa/napcat-docker:latest")
ASTRBOT_IMAGE = os.environ.get("DAFEIYU_ASTRBOT_IMAGE", "soulter/astrbot:latest")

# 端口分配基数。每实例占 3 个连续端口：napcat WebUI / OneBot / astrbot 面板。
# 从 16000 起，避开生产机上的 6099/6185/3001 等。
PORT_BASE = 16000
PORTS_PER_INSTANCE = 3
MAX_INSTANCES = 20

# ---------------------------------------------------------------------------
# 额度限制（2026-09-18 用户要求）
# ---------------------------------------------------------------------------
#
# 用户原话：「增加额度限制限制现在目前只能注册15个机器人，机器人只要超过5天
# 不说话就会自动删除」。
#
# 为什么要有这两个限制：服务器是同一台，每个机器人两个容器（napcat+astrbot）
# 各吃 200MB 上下。不设上限的话，用户随手建几十个，全机内存和 QQ 风控都会出问题
# —— 而且出问题时是**所有人一起受影响**，不是那个乱建的人自己。
#
# 为什么是「15」而不是 MAX_INSTANCES(20)：20 是端口表能排下的物理上限
# （PORT_BASE + 20*3 个端口），是**技术**上限；15 是给用户用的**额度**。
# 留 5 个余量是为了：用户删掉旧的想新建时，不会因为端口表碎片而建不出来。
MAX_ROBOTS_PER_USER = 15

# 多少天没动静就自动清理。
#
# ★ 判据用「最后一次活动时间」，而不是「创建时间」—— 用户要的是
#   「不说话就删」，一个天天在用的机器人不该因为建得早就被删掉。
#   活动时间由 touch_activity() 刷新，在聊天/改配置/重启时都会更新。
IDLE_DAYS = 5
IDLE_SECONDS = IDLE_DAYS * 24 * 3600

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


# ---------------------------------------------------------------------------
# 私密机器人：把配置锁起来，要看/要改先输密码
# ---------------------------------------------------------------------------
#
# 需求原话：「还有没有可以设为私密的机器人配置，可以用密码来解锁」。
#
# 场景：一台服务器上开了好几个机器人，其中某个是「自己的号」，
# 配置里含 API Key 和私聊对象，不想让旁边的人拿手机点开就看到。
#
# ── 密码怎么存 ──────────────────────────────────────────────────────
# 只存 **PBKDF2-HMAC-SHA256** 的派生值 + 随机盐，**绝不存明文、也不可逆**。
# 参数取 200000 次迭代（Python 3.8 的 hashlib 支持），单次校验约几十毫秒 ——
# 够快不影响体验，又让离线暴力破解很贵。
#
# 为什么不用 sha256(salt+password) 这种「一把梭」：那玩意儿 GPU 每秒能算
# 几十亿次，用户多半会设 6 位数字，几秒就撞开了。PBKDF2 的迭代次数就是
# 专门用来把这种暴力破解拖慢到不可行的。
#
# ── 关于「锁」的诚实说明 ────────────────────────────────────────────
# 这个锁是**防「顺手点开看到」**，不是防「拿到服务器 root 的人」——
# 有 root 就能直接读配置文件，任何 App 层的锁都拦不住。这一点必须对用户
# 说清楚，否则他会误以为「设了密码就绝对安全」，把重要 Key 放进来。
PBKDF2_ITERATIONS = 200000
LOCK_SALT_BYTES = 16


def _hash_lock_password(password, salt_hex=None):
    """把密码派生成 (salt_hex, hash_hex)。密码永不明文落盘。"""
    if not password:
        raise ManagerError("密码不能为空。")
    if salt_hex:
        salt = bytes.fromhex(salt_hex)
    else:
        salt = secrets.token_bytes(LOCK_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             PBKDF2_ITERATIONS)
    return salt.hex(), dk.hex()


def _check_lock_password(password, meta):
    """校验密码。用 compare_digest 做定时安全比较，避免逐字节时序泄露。"""
    lock = meta.get("lock") or {}
    if not lock.get("enabled"):
        return True
    salt_hex = lock.get("salt") or ""
    want = lock.get("hash") or ""
    if not salt_hex or not want:
        # 元数据坏了：宁可锁着，也不要「因为读不到密码就放行」
        return False
    try:
        _, got = _hash_lock_password(password or "", salt_hex)
    except ManagerError:
        return False
    return secrets.compare_digest(got, want)


def set_instance_lock(name, enabled, password, old_password):
    """开启/关闭/改密码。返回更新后的 meta。

    改密码时要验旧密码 —— 否则任何拿到管理口令的人都能直接改掉别人的锁。
    """
    meta = load_meta(name)
    lock = meta.get("lock") or {}
    already = bool(lock.get("enabled"))

    # 已经锁着的话，任何改动都要先过旧密码
    if already and not _check_lock_password(old_password, meta):
        raise ManagerError("旧密码不对，改不了。")

    if not enabled:
        meta.pop("lock", None)
        save_meta(name, meta)
        return meta

    if not password:
        # 已经锁着、又没给新密码 = 只是想保持原样（比如只改了别的配置）
        if already:
            return meta
        raise ManagerError("要设成私密，得先设一个密码。")
    if len(password) < 4:
        raise ManagerError("密码至少 4 位，太短容易被猜到。")
    if len(password) > 128:
        raise ManagerError("密码太长了（最多 128 位）。")

    salt_hex, hash_hex = _hash_lock_password(password)
    meta["lock"] = {
        "enabled": True,
        "salt": salt_hex,
        "hash": hash_hex,
        "iterations": PBKDF2_ITERATIONS,
        "set_at": int(time.time()),
    }
    save_meta(name, meta)
    return meta


def unlock_instance(name, password):
    """校验密码；对就返回配置，不对就报错。

    注意：密码正确时**也不返回** api_key —— Key 永远不回显，
    只说「有没有配」。这样即便密码被人瞟到，Key 也不会跟着泄露。
    """
    meta = load_meta(name)
    if not (meta.get("lock") or {}).get("enabled"):
        return {"unlocked": True, "locked": False,
                "config": read_config(name)}
    if not _check_lock_password(password, meta):
        # 不要区分「没设过密码」和「密码错」，也别回显任何配置内容
        raise ManagerError("密码不对。")
    return {"unlocked": True, "locked": True, "config": read_config(name)}


def lock_state(meta):
    """给列表用的锁状态（**绝不含盐和哈希**）。"""
    lock = meta.get("lock") or {}
    if not lock.get("enabled"):
        return {"locked": False}
    return {"locked": True, "set_at": lock.get("set_at") or 0}


def instance_detail(name, password=""):
    """单个实例的详情。★ 锁着的实例**不吐配置、不吐 WebUI token**。

    这是私密功能的关键一环：光加个 /instance/unlock 接口没用 ——
    原来的通用路由 /instance/<名字> 本来就把 config 和 webui_token 一起
    返回了，锁着也照样能拿到，等于没锁。所有出口都必须过这道闸。

    password：解锁后 App 读配置时会带上它。**没有密码就一直锁着** ——
    不能因为「App 说自己解锁过了」就放行，服务器只认密码。
    """
    meta = load_meta(name)
    ls = lock_state(meta)
    if ls["locked"] and not _check_lock_password(password, meta):
        # 只给「这个实例存在、它是锁着的、它在不在跑」，
        # 够 App 画出列表和锁图标，但一个字都不泄露。
        #
        # ★ pairing 是**例外，要照给**：它只有 4 个布尔值
        #   （两端配没配、token 一不一致、总的是否配对），
        #   不含 token 本身、不含任何 ID、不含配置内容。
        #   不给的话有个真实的坏结果：用户给机器人设了私密，
        #   之后机器人不回话，App 因为拿不到 pairing 就不显示
        #   「修复消息通道」按钮 —— 恰恰是最需要它的那台机器修不了。
        #   实测踩到：ap/dfy/maon/wdf 都锁着，配对其实是好的，
        #   但 HTTP 响应里没这个字段，看起来像「坏了」。
        return {"meta": {"name": meta.get("name"),
                         "created_at": meta.get("created_at"),
                         "status": meta.get("status"),
                         "webui_port": meta.get("webui_port"),
                         "onebot_port": meta.get("onebot_port"),
                         "panel_port": meta.get("panel_port")},
                "lock": ls,
                "locked": True,
                "containers": container_state(name),
                "config": None,
                "pairing": pairing_state(name),
                "webui_token": ""}
    return {"meta": meta,
            "lock": ls,
            "locked": False,
            "containers": container_state(name),
            "config": read_config(name),
            # 「消息通道」状态。App 拿它给用户一句人话：
            # 没配对就等于「机器人收不到消息」，而用户在界面上完全看不出来 ——
            # 他会以为是自己 API 填错了，反复改配置也没用。
            "pairing": pairing_state(name),
            "webui_token": webui_token(name)}



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
      * **/app/.config/QQ 必须单独再挂一次**：napcat 镜像自带
        `VOLUME /app/.config/QQ`，Docker 会给它建一个**匿名卷**并盖在
        bind mount 之上 —— 只挂 /app/.config 是不够的，QQ 会话实际写进了
        匿名卷，宿主机目录永远是空的。后果：`docker compose down/up`
        （或任何容器重建）会配一个新匿名卷，登录态随之丢失，
        用户每次都得重新扫码。显式把该子目录也 bind 上去才能压住它。
        这个坑 2026-09-17 在全部 11 个实例上实测复现（宿主机 QQ 文件数=0）。
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
      - ./napcat/persist/qqconfig/.config/QQ:/app/.config/QQ
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

    # 额度闸门：最多 MAX_ROBOTS_PER_USER 个。
    #
    # 错误信息要**说清楚现在有几个、上限几个、怎么办** —— 用户看到
    # 「已达上限」但不知道怎么解决的话，只会反复点「新建」然后以为坏了。
    existing = [m.get("name") for m in all_instances() if m.get("name")]
    if len(existing) >= MAX_ROBOTS_PER_USER:
        raise ManagerError(
            "机器人数量已达上限：最多只能有 %d 个，你现在已经有 %d 个了。"
            "请先删掉不用的机器人再新建。"
            "（提示：超过 %d 天没说过话的机器人会被自动清理。）"
            % (MAX_ROBOTS_PER_USER, len(existing), IDLE_DAYS))

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

    # ★ 建实例就把 NapCat 的 OneBot 模板放好。
    #
    # 必须在**首次扫码登录之前**写好：NapCat 登录时才读这个模板并落盘成
    # onebot11_<uin>.json（见 ensure_pairing 的说明）。等登录完再补，
    # 那条连接不会生效，用户会以为「登录了却不回话」。
    ensure_pairing(name, meta)

    return meta


def destroy_instance(name):
    meta = load_meta(name)
    compose(name, "down", "-v", check=False)
    shutil.rmtree(instance_dir(name), ignore_errors=True)
    return {"name": meta["name"], "destroyed": True}


def start_instance(name):
    meta = load_meta(name)
    # 启动前先配对：NapCat 的 OneBot 模板必须在它启动**之前**放好
    # （它登录时才读模板，见 ensure_pairing 的说明）。
    ensure_pairing(name, meta)
    compose(name, "up", "-d")
    meta["status"] = "running"
    save_meta(name, meta)
    # ★ 起来之后再配对一次。
    #
    # 为什么要两次：**首次**启动这个实例时，cmd_config.json 还不存在
    # （那是 AstrBot 第一次跑起来自己生成的），所以上面那次写不进 astrbot 侧。
    # 实测过：新建实例 → start → astrbot 侧仍是未配对。再调一次就好了。
    #
    # 先等 cmd_config.json 出现再写：AstrBot 启动早期就会写这个文件，
    # 而它**退出/启动时会把内存里的配置回写**，写太早会被它覆盖掉。
    # 等待是有上限的（need_cfg 用的就是 READY_TIMEOUT），
    # 拿不到就照样往下走 —— 宁可这次没配上，也不能让「启动」这个操作卡住；
    # apply_config 和 App 自检都还会再补一次。
    try:
        wait_astrbot_ready(name, need_cfg=True)
    except ManagerError:
        pass
    ensure_pairing(name, meta)
    touch_activity(name)
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


# ---------------------------------------------------------------------------
# OneBot 配对：让 NapCat 和 AstrBot 真正连上
# ---------------------------------------------------------------------------
#
# ★★ 这是「机器人一个字都不回」的根因修复。★★
#
# 现象：用户装好 App、扫码登录成功、三配置也填了，机器人却完全不回话。
#
# 根因（2026-09-17 在服务器上逐个实例实测确认）：**两端都不知道对方存在**。
#   * AstrBot 侧 platform 列表是空的 → 它从来不开反向 WebSocket 端口，
#     等于没有任何消息通道。翻遍每个实例的历史备份，platform 全是 []。
#   * NapCat 侧 websocketClients 是空的 → 它从不主动连出去。
#   电话线两端都没插上：QQ 消息到了 NapCat 就断了，AstrBot 日志里
#   连一条「收到消息」都没有 —— 所以用户看到的只是「不回话」，
#   没有任何报错可循。
#
# 生产机能正常聊天，正是因为那两处都配了：
#   * astrbot platform = [{type: aiocqhttp, ws_reverse_port: 6199,
#                          ws_reverse_token: <32 位 hex>}]
#   * napcat  websocketClients = [{url: "ws://astrbot:6199/ws",
#                                  token: <同一个 32 位 hex>}]
#   两端 token 必须**完全一致**，否则 AstrBot 会拒绝连接。
#
# ── 为什么写的是 onebot11.json（不带 QQ 号）而不是 onebot11_<uin>.json ──
# NapCat 的 ConfigLoader.read()（napcat.mjs:38576）逻辑是：
#     优先读 onebot11_${uin}.json；不存在 → 读 onebot11.json 当模板，
#     紧接着 save() 写成 onebot11_${uin}.json。
# 而 uin 要**登录成功之后**才知道 —— 所以只能在首次登录前把模板放好，
# 让它登录时自动继承。等登录完再补写，那条连接不会生效。
#
# ── 为什么端口固定 6199、多实例也不冲突 ──
# 每个实例都是**独立的 bridge 网络**（见 render_compose），容器之间按
# 服务名互通，所以每个实例里的 "astrbot:6199" 都是自己那一对，
# 不存在端口抢占，也不需要按实例分配端口。
OB11_WS_PORT = 6199
OB11_WS_PATH = "/ws"
OB11_CLIENT_NAME = "astrbot"

# NapCat 自己配置里那些与 OneBot 无关、但缺了会被 schema 校验打回的键。
# 照抄 NapCat 首次启动生成的文件，避免它报 "读取配置文件时发生错误"。
_OB11_EXTRA = {
    "musicSignUrl": "",
    "enableLocalFile2Url": False,
    "parseMultMsg": False,
    "imageDownloadProxy": "",
    "timeout": {"baseTimeout": 10000, "uploadSpeedKBps": 256,
                "downloadSpeedKBps": 256, "maxTimeout": 1800000},
}


def napcat_cfg_dir(name):
    """NapCat 真正读配置的目录（compose 里挂到 /app/napcat/config）。"""
    return os.path.join(instance_dir(name), "napcat", "persist",
                        "napcatcfg", "config")


def _ob11_network(token):
    """NapCat 的 network 段：让 napcat 主动连到本实例的 astrbot。"""
    return {
        "httpServers": [],
        "httpSseServers": [],
        "httpClients": [],
        "websocketServers": [],
        "websocketClients": [{
            "name": OB11_CLIENT_NAME,
            "enable": True,
            "url": "ws://astrbot:%d%s" % (OB11_WS_PORT, OB11_WS_PATH),
            "messagePostFormat": "array",
            "reportSelfMessage": False,
            "token": token,
        }],
        "plugins": [],
    }


def _write_json_plain(path, data):
    """NapCat 的配置是**无 BOM** 的 UTF-8（和 AstrBot 正好相反）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)
    # NapCat 在容器里以 uid 1000 跑，文件得让它写得动
    try:
        os.chown(path, 1000, 1000)
    except OSError:
        pass


def _patch_napcat_onebot(path, token):
    """把 websocketClients 合并进一个 onebot11 配置文件（保留其它键）。"""
    cur = {}
    if os.path.exists(path):
        try:
            cur = read_json_maybe_bom(path) or {}
        except (ValueError, OSError):
            cur = {}
    for k, v in _OB11_EXTRA.items():
        cur.setdefault(k, v)
    cur["network"] = _ob11_network(token)
    _write_json_plain(path, cur)


def ensure_pairing(name, meta=None):
    """让这个实例的 NapCat 和 AstrBot 配对连上。**幂等**，可反复调用。

    返回 {"token": ..., "napcat": [...改动文件...], "astrbot": bool}。

    调用时机（三个都要，缺一个就有用户会踩坑）：
      * create_instance —— 放好 NapCat 模板，保证「首次扫码登录」就能连上；
      * start_instance  —— AstrBot 第一次启动后会生成 cmd_config.json，
                           这时才能写 platform；
      * apply_config    —— 已经等到了「AstrBot 完全就绪」，是最可靠的时机，
                           顺便给存量实例补配（老实例全都没有这两处配置）。
    """
    meta = meta or load_meta(name)
    token = (meta.get("ob11_token") or "").strip()
    if not re.match(r"^[0-9a-f]{32}$", token):
        token = secrets.token_hex(16)
        meta["ob11_token"] = token
        save_meta(name, meta)

    touched = []

    # ① NapCat 侧：模板 + 已经登录过的那份（有就一起更新）
    d = napcat_cfg_dir(name)
    if os.path.isdir(d):
        _patch_napcat_onebot(os.path.join(d, "onebot11.json"), token)
        touched.append("onebot11.json")
        for fn in sorted(os.listdir(d)):
            if re.match(r"^onebot11_[0-9]+\.json$", fn):
                _patch_napcat_onebot(os.path.join(d, fn), token)
                touched.append(fn)

    # ② AstrBot 侧：platform 里加一条 aiocqhttp（反向 WS 服务端）
    ok_astrbot = False
    path = astrbot_cfg_path(name)
    if os.path.exists(path):
        try:
            cfg = read_json_maybe_bom(path)
        except (ValueError, OSError):
            cfg = None
        if isinstance(cfg, dict):
            want = {
                "id": "default",
                "type": "aiocqhttp",
                "enable": True,
                "ws_reverse_host": "0.0.0.0",
                "ws_reverse_port": OB11_WS_PORT,
                "ws_reverse_token": token,
            }
            plats = cfg.get("platform") or []
            # 保留用户可能自己加过的别的平台，只替换同 id 的那条
            cfg["platform"] = [want] + [p for p in plats
                                        if isinstance(p, dict)
                                        and p.get("id") != "default"]
            write_json_bom(path, cfg)
            ok_astrbot = True

    return {"token": token, "napcat": touched, "astrbot": ok_astrbot}


def pairing_state(name):
    """只读检查：两端是否都配好、token 是否一致。给 App 做自检用。"""
    meta = load_meta(name)
    token = (meta.get("ob11_token") or "").strip()

    napcat_ok = False
    napcat_token = ""
    d = napcat_cfg_dir(name)
    if os.path.isdir(d):
        cands = [os.path.join(d, "onebot11.json")]
        cands += [os.path.join(d, f) for f in sorted(os.listdir(d))
                  if re.match(r"^onebot11_[0-9]+\.json$", f)]
        for p in cands:
            try:
                cur = read_json_maybe_bom(p)
            except (ValueError, OSError):
                continue
            cl = ((cur.get("network") or {}).get("websocketClients") or [])
            for c in cl:
                if c.get("enable") and (c.get("url") or "").endswith(
                        ":%d%s" % (OB11_WS_PORT, OB11_WS_PATH)):
                    napcat_ok = True
                    napcat_token = c.get("token") or ""
                    break
            if napcat_ok:
                break

    astrbot_ok = False
    astrbot_token = ""
    path = astrbot_cfg_path(name)
    if os.path.exists(path):
        try:
            cfg = read_json_maybe_bom(path)
        except (ValueError, OSError):
            cfg = {}
        for p in (cfg.get("platform") or []):
            if isinstance(p, dict) and p.get("type") == "aiocqhttp":
                astrbot_ok = True
                astrbot_token = p.get("ws_reverse_token") or ""
                break

    return {
        "napcat_configured": napcat_ok,
        "astrbot_configured": astrbot_ok,
        "tokens_match": bool(napcat_token) and napcat_token == astrbot_token,
        "paired": napcat_ok and astrbot_ok and napcat_token == astrbot_token,
    }


def repair_channel(name):
    """一键修复「消息通道」，然后让两端都重新连上。

    给 App 的「机器人不回话？点这里修」按钮用。用户不需要知道
    platform / websocketClients / token 这些词 —— 他只要知道
    「不回话就点一下」。

    做三件事：
      1. ensure_pairing 把两端配置写对（幂等）；
      2. 热加载 NapCat（不重启，保住 QQ 登录态）；
      3. 重启 AstrBot 让它重开反向 WS 端口（它只在启动时开）。

    第 3 步必须重启 astrbot：它是**服务端**，只有启动时才 bind 6199。
    重启 astrbot 不影响 QQ 登录（登录态在 napcat 那边）。
    """
    before = pairing_state(name)
    ensure_pairing(name)
    _napcat_hot_reload(name)
    compose(name, "restart", "astrbot", check=False)
    wait_astrbot_ready(name, need_cfg=True)
    after = pairing_state(name)
    touch_activity(name)
    return {"before": before, "after": after, "repaired": after["paired"]}


def touch_activity(name):
    """记下这个机器人「最后活动时间」。**不抛异常**。

    什么时候算活动：收到消息、改配置、重启、修通道 —— 任何说明
    「用户还在用它」的动作。自动清理只看这个时间，所以调用点要盖全，
    否则一个天天在聊的机器人可能因为「没改过配置」被误删。
    """
    try:
        meta = load_meta(name)
        meta["last_active_at"] = int(time.time())
        save_meta(name, meta)
    except (ManagerError, OSError, ValueError):
        pass


def _last_message_time(name):
    """从 AstrBot 数据库读「最后一次真的收到消息」的时间戳（epoch 秒）。

    为什么不能只看 last_active_at：那个字段是我们自己写的，老实例没有；
    而「机器人有没有在说话」这件事，AstrBot 自己的库最准 ——
    platform_stats 里每一行就是「某小时收到了 N 条消息」。

    读不到（库不存在/没表/权限）就返回 None，由调用方回退到
    last_active_at / created_at。**绝不能让读库失败变成「判定为空闲」** ——
    那会把用户的机器人误删。
    """
    p = os.path.join(instance_dir(name), "astrbot", "data", "data_v4.db")
    if not os.path.exists(p):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=3)
        try:
            row = con.execute("select max(timestamp) from platform_stats"
                              ).fetchone()
            if row and row[0]:
                # 库里的时间戳是 **UTC**（AstrBot 容器里 TZ=UTC，
                # 实测 platform_stats 的 16:20:02 与容器日志同一时刻，
                # 而宿主机的 napcat 日志那时是 00:20 —— 差 8 小时）。
                #
                # ★ 必须用 calendar.timegm，不能用 time.mktime：
                #   mktime 把 struct 当**本地时间**解释，于是结果随宿主机时区漂移。
                #   这里踩过真坑：本地（UTC）测试全过，一上服务器（UTC+8）
                #   算出来的活动时间就偏了 8 小时。timegm 把它当 UTC，
                #   与宿主机时区无关，两边结果一致。
                return calendar.timegm(time.strptime(str(row[0])[:19],
                                                     "%Y-%m-%d %H:%M:%S"))
        finally:
            con.close()
    except (sqlite3.Error, ValueError, OSError):
        return None
    return None


def last_active_at(name, meta=None):
    """这个机器人「最后活动时间」，取三个来源里最新的一个。

    优先级：库里真有消息 > 我们记的 last_active_at > 创建时间。
    """
    meta = meta or load_meta(name)
    cands = []
    mt = _last_message_time(name)
    if mt:
        cands.append(mt)
    if meta.get("last_active_at"):
        cands.append(int(meta["last_active_at"]))
    if meta.get("created_at"):
        cands.append(int(meta["created_at"]))
    return max(cands) if cands else 0


def idle_info(name, meta=None):
    """算这个机器人闲了多久，给 App 显示「还有几天被清理」。"""
    meta = meta or load_meta(name)
    la = last_active_at(name, meta)
    idle = int(time.time()) - la if la else 0
    left = IDLE_SECONDS - idle
    return {
        "last_active_at": la,
        "idle_seconds": idle,
        "idle_days": round(idle / 86400.0, 1),
        "days_left": max(0, int(left // 86400)),
        "will_be_removed": idle >= IDLE_SECONDS,
    }


def quota_info():
    """额度总览：用了几个、还能建几个、哪些快被清理了。给 App 显示。"""
    metas = [m for m in all_instances() if m.get("name")]
    items = []
    for meta in metas:
        try:
            it = idle_info(meta["name"], meta)
        except (ManagerError, OSError, ValueError):
            it = {"last_active_at": 0, "idle_days": 0.0, "days_left": IDLE_DAYS,
                  "will_be_removed": False}
        it["name"] = meta["name"]
        it["locked"] = bool(lock_state(meta).get("locked"))
        items.append(it)
    # 按「最久没动」排前面，用户一眼看到该删谁
    items.sort(key=lambda x: -x.get("idle_seconds", 0))
    return {
        "limit": MAX_ROBOTS_PER_USER,
        "used": len(metas),
        "remaining": max(0, MAX_ROBOTS_PER_USER - len(metas)),
        "idle_days": IDLE_DAYS,
        "instances": items,
    }


def cleanup_idle(dry_run=False):
    """删掉「超过 IDLE_DAYS 天没说过话」的机器人。返回删了哪些。

    为什么要自动删：用户明确要求（「机器人只要超过5天不说话就会自动删除」）。
    动机是资源 —— 每个实例两个容器，闲着的实例白占内存，还会一直挂着
    QQ 连接（长期不活动本身也更容易触发风控）。

    ★ 安全设计：**只删能确认「确实闲了很久」的实例**。
      * 读不到库、meta 里也没有任何时间戳（last_active_at 和 created_at
        都缺）→ **跳过，不删**。宁可留着一个闲实例，也不能误删在用的。
      * 锁着的实例不删（用户特意设为私密的，说明在意它）。
      * dry_run=True 只报告不删，给 App 做预览用。
    """
    removed, kept = [], []
    for meta in all_instances():
        name = meta.get("name")
        if not name:
            continue
        try:
            if lock_state(meta).get("locked"):
                kept.append({"name": name, "reason": "私密实例，不自动清理"})
                continue
            la = last_active_at(name, meta)
            if not la:
                kept.append({"name": name, "reason": "没有可用的活动时间，保守跳过"})
                continue
            idle = int(time.time()) - la
            if idle >= IDLE_SECONDS:
                if dry_run:
                    removed.append({"name": name,
                                    "idle_days": round(idle / 86400.0, 1)})
                else:
                    destroy_instance(name)
                    removed.append({"name": name,
                                    "idle_days": round(idle / 86400.0, 1)})
            else:
                kept.append({"name": name,
                             "idle_days": round(idle / 86400.0, 1)})
        except (ManagerError, OSError, ValueError) as e:
            kept.append({"name": name, "reason": "检查失败，跳过：%s" % str(e)[:60]})
    return {"removed": removed, "kept": kept, "dry_run": dry_run}


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


def astrbot_started_marker(name):
    """看这个实例的 AstrBot 是不是真的启动完成了。

    返回 True / False；**拿不到日志时返回 None**（不代表没启动）。
    区分 None 很重要：测试环境和容器名不同时 docker 命令必然失败，
    那时不能当成「没启动」去反复等 —— 会把测试拖死。

    ★ 2026-09-19 修复（「怎么都保存不了配置」的根因）：
       原来只查 `docker logs --tail 80` 里有没有 "AstrBot started"。
       对**长期运行**的容器，这个标记早就被后续日志挤出最近 80 行了
       （线上 dfy 实例运行 24h、日志 10555 行，标记在第 509 行），
       于是一律被判成「没启动完」，apply_config 每次写入都 400 拒绝
       —— 用户表现就是：怎么写配置都失败，重启服务也没用。
       所以补第二判据：容器**正在运行且已持续运行够久**，那它必然
       早就启动完成了（启动失败的话容器会退出），标记只是被挤出而已。
    """
    try:
        out = subprocess.run(
            ["docker", "logs", "--tail", "80", "dafeiyu-%s-astrbot" % name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
    except FileNotFoundError:
        return None          # 没有 docker 命令
    except Exception:
        return None          # 超时等其它异常
    if out.returncode != 0:
        return None          # 容器不存在
    if "AstrBot started" in out.stdout.decode("utf-8", "replace"):
        return True
    # 标记不在最近 80 行：要么还没启动完，要么跑太久被挤出。
    # 用「运行时长」区分这两者（docker inspect 拿启动时刻）。
    info = _astrbot_life(name)
    if info is None:
        return False         # 拿不到运行信息，按「没启动」处理（保守）
    running, started_epoch = info
    if not running:
        return False         # 容器没在跑 = 确实没启动 / 已退出
    return (time.time() - started_epoch) > ASTRBOT_RUN_OK_SECONDS


def _astrbot_life(name):
    """docker inspect：这个实例的 AstrBot（running, 启动时刻 epoch）。

    拿不到时返回 None（docker 不可用 / 容器不在 / 解析失败）。
    """
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format",
             "{{.State.Running}}|{{.State.StartedAt}}",
             "dafeiyu-%s-astrbot" % name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    txt = out.stdout.decode("utf-8", "replace").strip()
    try:
        running_s, started_s = txt.split("|", 1)
        st = started_s.strip()
        if st.endswith("Z"):
            st = st[:-1]
        st = st.split(".")[0]        # 去掉纳秒小数
        t = time.strptime(st, "%Y-%m-%dT%H:%M:%S")
        return running_s.strip() == "true", calendar.timegm(t)
    except Exception:
        return None


# 容器「运行多久就算已启动完成」（兜底判据）。
# 必须明显大于 READY_TIMEOUT（40s）：它只在「日志标记被挤出」时才
# 被用到，正常短时启动会更快被日志标记判据放行，这个阈值只兜长期
# 运行实例 —— 那种实例一定早就完成启动了。
ASTRBOT_RUN_OK_SECONDS = 90


def astrbot_container_exists(name):
    """这个实例的 AstrBot 容器**存在过**吗（不管现在跑没跑）。

    返回 True / False；拿不到结论（没有 docker 命令等）时返回 None。

    为什么需要它：缺 cmd_config.json 的有两种人，该说的话正好相反 ——
      * 从没点过「启动」→ 该说「请先点启动」（他确实还没启动）
      * 刚点完「启动」、容器还在拉镜像 → 该说「正在初始化，等一会儿」
    只看文件在不在是分不出这两者的，会有一半人被指错方向。
    """
    cname = "dafeiyu-%s-astrbot" % name
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=^%s$" % cname,
             "--format", "{{.Names}}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
    except FileNotFoundError:
        return None          # 没有 docker 命令
    except Exception:
        return None          # 超时等其它异常
    if out.returncode != 0:
        return None
    return cname in out.stdout.decode("utf-8", "replace")


# App 端读超时是 120 秒（ManagerClient.request 里 setReadTimeout(120000)）。
# apply_config 里会等两次（写之前一次、重启之后一次），所以每次的上限必须
# 让**总时长**留在 120 秒以内，否则 App 会先超时报「连不上」，
# 而服务端其实还在正常干活 —— 用户看到的就是「明明成功了却提示失败」。
# 40 + 40 秒 + 重启开销，留足余量。
READY_TIMEOUT = 40

# 拿不到容器日志时的等待上限（短）。
#
# 为什么单独设一个小值：docker logs 拿不到（测试环境、容器不存在、docker 不可用）
# 时，我们**没有任何证据**说明这个实例在往好的方向走 —— 文件可能 2 秒后出现，
# 也可能永远不会出现。这时等满 READY_TIMEOUT 只是在让用户干等，最后给的还是
# 同一句报错。给它几秒的宽限（磁盘慢、刚建目录），然后老实报「还在初始化」。
NO_LOG_WAIT = 5


def wait_astrbot_ready(name, timeout=READY_TIMEOUT, need_db=False, need_cfg=False):
    """等实例的 AstrBot 真正启动完，最多等 timeout 秒。

    为什么要等：AstrBot 启动/退出时会把**内存里**的配置写回 cmd_config.json。
    它没启动完就重启，那次写回会拿未初始化状态覆盖我们的配置 ——
    表现为「提示成功，过一会儿全空了」。

    就绪的判据是**一组文件**，不是一个瞬间：
      * need_cfg=True → cmd_config.json 存在（AstrBot 启动早期就会写出来）
      * need_db=True  → data_v4.db 存在**且 personas 表已建好**
                        （要等 ORM 建表完才算数）

    need_db 为什么不能省：cmd_config.json 生成得很早（所以界面会显示「运行中」），
    但 data_v4.db 要晚得多。只等日志里的 "AstrBot started" 就去写人格，会撞上
    「文件还不存在」—— 用户看到的是「请先启动一次」，而他明明刚启动过。
    这正是实测踩到的坑。

    ★ need_db 光等**文件存在**还不够（这是「提示词写入失败」那个 bug 的根因）：
      AstrBot 的 ORM 是「先落盘 data_v4.db 文件 → 再逐张建表」，两步之间有个
      几百毫秒到几秒的窗口。文件已经在、personas 表却还没建好时就去写人格，
      write_persona_db 会抛「这个 AstrBot 版本还没有 personas 表」——
      用户看到的就是「提示词写入失败」，而其实只是**等早了**。
      所以 need_db 的判据必须是「文件在 且 personas 表已建好」。

    把 cfg 也并进同一个判据里（而不是在外面再补一次等待），是为了守住
    App 的读超时预算：apply_config 里只等两次，每次上限 READY_TIMEOUT。

    返回 True（已就绪）或 False（等超时）。
    """
    import time as _t

    def files_ready():
        if need_cfg and not os.path.exists(astrbot_cfg_path(name)):
            return False
        if need_db:
            if not os.path.exists(persona_db_path(name)):
                return False
            # 文件在还不够 —— personas 表建好了才算 data_v4.db 真就绪。
            if not personas_table_ready(name):
                return False
        return True

    def ready_now():
        return astrbot_started_marker(name) is True and files_ready()

    first = astrbot_started_marker(name)
    if first is None:
        # 拿不到日志（测试环境、容器不存在、docker 不可用）：**没有任何证据**
        # 说明它在往好的方向走，所以不能按 READY_TIMEOUT 死等 —— 那只是让用户
        # 干等，最后给的还是同一句报错。给几秒宽限（磁盘慢、刚建目录），
        # 文件出现了就继续；否则老实说「还在初始化」，让用户过一会儿再点。
        deadline = _t.time() + (timeout if files_ready() else NO_LOG_WAIT)
        while _t.time() < deadline:
            if files_ready():
                return True
            _t.sleep(1)
        return files_ready()
    if ready_now():
        return True
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        _t.sleep(3)
        if ready_now():
            return True
    return False


def persona_db_path(name):
    return os.path.join(instance_dir(name), "astrbot", "data", "data_v4.db")


def personas_table_ready(name):
    """实例的 data_v4.db 里 personas 表建好了没有。

    为什么要单独判「表」而不是只看文件在不在：
    AstrBot 的 ORM 是**先落盘 data_v4.db 文件、再逐张建表**，两步之间有个
    几百毫秒到几秒的窗口。文件已经在、personas 表却还没建好时写人格，
    write_persona_db 会抛「这个 AstrBot 版本还没有 personas 表」——
    用户看到的就是「提示词写入失败」，而其实只是**等早了**。
    所以「数据库就绪」的判据必须是表建好了，不是文件在。

    只读打开：别在 AstrBot 建表的当口去锁库或建库。
    拿不准（打不开、还在锁、查询报错）一律返回 False（当「还没好」）——
    宁可多等几秒，也不要放行到 write_persona_db 里再抛「没有 personas 表」。
    """
    path = persona_db_path(name)
    if not os.path.exists(path):
        return False
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='personas'").fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


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


def apply_config(name, groups, friends, api_base, api_key, api_model, persona,
                 lock_password="", vision_base="", vision_key="",
                 vision_model="", protocol="", extra_body="",
                 vision_protocol="", vision_extra_body=""):
    """把配置写进实例的 AstrBot。

    写之前先备份原文件；写之后**回读校验**（生产机的教训：写完不读回，
    写坏了也不知道）。

    lock_password：私密实例要改配置必须先给密码。**没有它就能改**的话，
    锁只挡住了「看」却没挡住「改」—— 别人可以直接把配置覆盖掉，
    等于没锁。

    vision_* 是**可选**的「识图 API」。为什么要单独一套、而不是让用户
    把主 API 换成识图的：
      * 主聊天 API 用一个便宜、快的文本模型，识图用一个单独的视觉模型，
        是生产机一直在用的做法（省钱且效果好）；
      * 用户升级前已经在用文本 API 了，不该逼他换掉。
    所以：不填 vision_* 就完全不动多模态配置，保持原样。

    protocol：接口协议（PROTOCOLS 里的键）。空 = 沿用已保存的，
    实例上也没保存过 = 默认 OpenAI 兼容。
    ★ 为什么它必须是可选项：同一个地址可能只认某一种协议
    （Anthropic 原生 / Gemini 原生 / OpenAI Responses），
    选错就 404/400，而报错会指向「地址写错了」或「请求不合法」，
    把用户带去改完全无关的东西。

    extra_body：自定义请求体，一段 JSON 文本（如 {"temperature":0.7}）。
    空 = 沿用已保存的；要清空得显式传 "{}"。
    """
    # 检查顺序有讲究，按「最可能出错 + 最便宜」排：
    #   ① 先校验用户填的内容 —— 输错 QQ 号是最常见的情况，且不用碰磁盘；
    #   ② 再确认实例存在 —— 名字打错时立刻说清楚，而不是报「没跑起来过」把人带偏；
    #   ③ 最后看配置有没有生成（需要实例启动过一次）。
    gids = normalize_ids(groups, "群号")
    fids = normalize_ids(friends, "私聊 QQ 号")

    # 协议先规范化（不认识就报错，别等到写进配置后加载失败）。
    # 空 = 沿用已保存的；实例上没配过 = 默认值（老用户升级上来无感）。
    if protocol:
        use_proto = normalize_protocol(protocol)
    else:
        use_proto = protocol_of(name)

    # 自定义请求体：先解析 + 防呆。
    # 空串 = 沿用已保存的；"{}" = 清空。
    if (extra_body or "").strip():
        new_extra, extra_warns = parse_extra_body(extra_body)
        use_extra = new_extra
        extra_touched = True
    else:
        use_extra = extra_body_of(name, "main")
        extra_warns = []
        extra_touched = False

    api_any = bool(api_base or api_key or api_model)
    if api_any:
        # ★ 「Key 留空 = 不改」必须真的成立。
        #
        # App 的输入框一直写着「API Key（不回显，留空=不改）」—— 因为 Key
        # 出于安全不回显，用户想只改模型名时 Key 框必然是空的。
        # 但这里原来要求三样填全，于是用户**只改人格或只改模型名都做不到**，
        # 必须回官网把 Key 重新复制一遍。实测确认过：
        #   apply_config(base="…", key="", model="…") → 报「要填全」。
        # 那是把「安全上不回显」的代价转嫁给了用户，属于设计缺陷。
        #
        # 现在：给了任意一项就按「留空=沿用已保存的值」补齐，再校验。
        # 这样既保住了「不能写半套」的底线（补不齐照样报错），
        # 又让「只改模型名」这种最常见的操作真正可行。
        _sb, _sk, _sm = _main_api_of(name)
        if not api_base:
            api_base = _sb
        if not api_key:
            api_key = _sk
        if not api_model:
            api_model = _sm
        if not (api_base and api_key and api_model):
            # 补不齐说明实例上本来就没配过，或者用户只填了半套。
            # 分两种话说清楚，别让「第一次配」的人以为是自己填错了。
            if not (_sb or _sk or _sm):
                raise ManagerError("主聊天 API 要填全：接口地址、API Key、"
                                   "模型名缺一不可。")
            raise ManagerError("主聊天 API 还差一些：%s。"
                               "（Key 留空表示沿用已保存的，但实例上还没存过 Key。）"
                               % "、".join([n for n, v in
                                            (("接口地址", api_base),
                                             ("API Key", api_key),
                                             ("模型名", api_model)) if not v]))
        if not (api_base.startswith("http://") or api_base.startswith("https://")):
            raise ManagerError("接口地址要以 http:// 或 https:// 开头。")
        if " " in api_base:
            raise ManagerError("接口地址里不能有空格。")
        if "\n" in api_key or "\r" in api_key:
            raise ManagerError("API Key 里不能有换行。")
        if " " in api_model or "\n" in api_model or "\r" in api_model:
            raise ManagerError("模型名里不能有空格或换行。")

    has_persona = bool(persona and persona.strip())

    # 识图 API：要么全不填（不动它），要么填全。
    # 只填一半就报错，而不是「凑合写一半」—— 写一半的结果是机器人
    # 收得到图但识不了，用户完全看不出哪里不对。
    #
    # 「Key 留空 = 沿用已保存的」和主聊天 API 同理（见上面的说明）：
    # 识图 Key 也不回显，用户想只换模型名时 Key 框必然是空的。
    # 但地址和模型名**不能**沿用 —— 用户想换成另一家的识图 API 时，
    # 沿用旧地址会配出一个「新模型名 + 旧地址」的坏组合，
    # 报错信息还会指向模型名，把人带偏。
    vision_any = bool(vision_base or vision_key or vision_model)
    # 识图协议：空 = 沿用已保存的。
    # 注意这里**不**跟着主协议走 —— 识图常常是另一家（如主聊天用中转站、
    # 识图用智谱官方），两套协议可以完全不同。
    if vision_protocol:
        use_vproto = normalize_protocol(vision_protocol)
    elif vision_any:
        use_vproto = vision_protocol_of(name)
    else:
        use_vproto = DEFAULT_PROTOCOL
    if (vision_extra_body or "").strip():
        new_vextra, vextra_warns = parse_extra_body(
            vision_extra_body, "识图 API 的自定义请求体")
        use_vextra = new_vextra
        vision_extra_touched = True
    else:
        use_vextra = extra_body_of(name, "vision")
        vextra_warns = []
        vision_extra_touched = False
    if vision_any:
        _vb, _vk, _vm = _vision_api_of(name)
        if not vision_key:
            vision_key = _vk
        if not (vision_base and vision_key and vision_model):
            if not (_vb or _vk or _vm):
                raise ManagerError("识图 API 要填全：接口地址、API Key、"
                                   "模型名缺一不可。（不用识图的话，"
                                   "这三样都留空就行。）")
            raise ManagerError("识图 API 还差一些：%s。"
                               "（识图 Key 留空表示沿用已保存的，"
                               "但实例上还没存过。）"
                               % "、".join([n for n, v in
                                            (("接口地址", vision_base),
                                             ("API Key", vision_key),
                                             ("模型名", vision_model)) if not v]))
        if not (vision_base.startswith("http://")
                or vision_base.startswith("https://")):
            raise ManagerError("识图接口地址要以 http:// 或 https:// 开头。")
        if " " in vision_base:
            raise ManagerError("识图接口地址里不能有空格。")
        if "\n" in vision_key or "\r" in vision_key:
            raise ManagerError("识图 API Key 里不能有换行。")
        if " " in vision_model or "\n" in vision_model or "\r" in vision_model:
            raise ManagerError("识图模型名里不能有空格或换行。")
        if vision_model == api_model and vision_base == api_base:
            # 同一个模型既当主聊天又当识图 —— 只有它真能识图时才成立。
            # 这里拦一下：真能识图的话，把主 API 的模型换成它就行，
            # 不必配两遍；不能识图的话，配了也是白配。
            raise ManagerError("识图模型和主聊天模型是同一个，这样配没有意义。"
                               "如果这个模型本身就能识图，直接把它填在"
                               "「主聊天 API」那里就行。")

    if not (gids or fids or api_any or has_persona or vision_any
            or extra_touched or vision_extra_touched):
        raise ManagerError("没填任何要改的内容。")

    load_meta(name)  # 实例不存在 → 在这里就报清楚

    # 私密实例：改配置也要密码（否则锁只挡看不挡改）
    _meta_now = load_meta(name)
    if (not lock_password) and (_meta_now.get("lock") or {}).get("enabled"):
        raise ManagerError("这个机器人设了私密，要先解锁（输密码）才能改配置。")
    if lock_password and not _check_lock_password(lock_password, _meta_now):
        raise ManagerError("密码不对。")

    path = astrbot_cfg_path(name)

    # ★ 写之前先等 AstrBot 完全启动 —— 这一次等待同时管两件事。
    #
    # 为什么必须等（不只是在文件缺失时才等）：
    #   AstrBot 启动过程中会在退出时把**内存里**的配置存回文件。
    #   如果它还没启动完就重启它，那次「保存」会拿未初始化的状态覆盖我们的写入
    #   —— 表现为「提示成功，过一会儿配置全空」。等它起来再写，就没有这个窗口。
    #
    # 为什么把「文件存在」并进这一次等待，而不是文件缺失时另起一次：
    #   用户点完「启动」马上来填配置时，容器还在拉镜像/建目录，cmd_config.json
    #   还没生成。这时直接报「请先点启动」是在冤枉他 —— 他刚点过。
    #   但也不能在外面再补一次等待：apply_config 的等待总时长必须留在 App 的
    #   120 秒读超时之内（见 READY_TIMEOUT 的说明），三次等待会把它撑爆。
    #   所以判据合成一个：**文件都在 且 日志说启动完成**。
    #
    # need_db：要写人格时必须连 data_v4.db 一起等。只等日志标记是不够的 ——
    #   实测 cmd_config.json 早就有了（界面显示「运行中」），而 data_v4.db 还没
    #   落盘，于是刚点完「启动」就填配置的用户会收到「请先启动一次」，
    #   而他明明刚启动过。
    ready = wait_astrbot_ready(name, need_db=has_persona, need_cfg=True)

    if not os.path.exists(path):
        # 等过了还是没有。分两种人给话 —— 他们的下一步动作完全相反。
        if astrbot_container_exists(name) is False:
            # 容器压根没建过：他确实还没点过「启动」。
            raise ManagerError("这个机器人还没跑起来过，配置要等它先启动一次。"
                               "点上面的「启动」，等十几秒再回来。")
        # 容器在（或在建）：他刚点过启动，正在初始化。别把他打发回启动按钮。
        raise ManagerError("这个机器人还在初始化（第一次启动要拉镜像、建目录，"
                           "可能要一两分钟）。请稍等一会儿再点「保存」。")
    if not ready:
        if has_persona and not personas_table_ready(name):
            # 要写人格、而数据库还没就绪：可能是文件还没生成，也可能是
            # 文件在但 personas 表还没建好（ORM 建表比落盘晚）。
            # 两种都给「内部数据库还在初始化」——用户下一步动作一样：等一下再点。
            raise ManagerError("这个机器人刚启动，内部数据库还在初始化，"
                               "请等半分钟再点「保存」。")
        raise ManagerError("这个机器人的聊天服务还没启动完，稍等半分钟再试。")

    # ⓪ 先补上「消息通道」——没有它下面配什么都没用。
    #
    # 用户看到的现象是「机器人一个字都不回」，而根因不在这里配的东西里：
    # AstrBot 的 platform 是空的，它压根没开消息通道（详见 ensure_pairing）。
    #
    # ★ 必须在**读 cfg 之前**写：ensure_pairing 会往文件里加 platform 条目，
    #   而下面白名单要用 platform[0].id 拼私聊键。顺序反了，内存里的 cfg
    #   还是旧的（platform 为空），拼出来的私聊键就和生产机不一致 ——
    #   表现是「群里能回、私聊不回」这种极难查的半坏状态。
    pair = ensure_pairing(name)
    channel_added = bool(pair["astrbot"] or pair["napcat"])

    cfg = read_json_maybe_bom(path)
    backup = path + ".bak.%d" % int(time.time())
    shutil.copy2(path, backup)

    changed = []
    if channel_added:
        changed.append("消息通道（让 NapCat 和 AstrBot 连上）")

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
    #
    # 字段必须和生产机上能正常工作的条目**完全一致** —— 少一个就出问题，
    # 而且报错很难懂。实测踩到的：
    #   * provider 缺 "enable" → 加载时报 KeyError: 'enable'，
    #     日志里只有一段 traceback，界面上完全看不出是「配置少了个字段」。
    #   * 缺 "modalities" / "custom_extra_body" 同样会被下游代码直接索引。
    #   * source 里多写了 "model_config" 会干扰 provider→source 的迁移逻辑。
    # 所以这里照抄生产机的形状（provider_sources 9 个字段 / provider 5 个字段）。
    if api_base and api_key and api_model:
        src_id = "dafeiyu-main_source"
        pid = "dafeiyu-main"
        # 协议决定 type/provider 两个字段。
        # ★ type 是 AstrBot 查适配器的键（manager.py:700），
        #   写错就是加载失败，而报错只有一行 traceback。
        #   所以我们只从 PROTOCOLS 这张**已验证过存在**的表里取值，
        #   绝不把用户输入的字符串直接写进去。
        proto_meta = PROTOCOLS[use_proto]
        src = {
            "id": src_id,
            "provider": proto_meta["provider"],
            "type": use_proto,
            "provider_type": "chat_completion",
            "key": [api_key],
            # 地址按协议规范化：Anthropic 适配器内部会 removesuffix("/v1")
            # （anthropic_source.py:98），存进去的必须是它期望的形状，
            # 否则 App 回显的地址和实际用的地址不一致，用户会以为自己填错了。
            "api_base": normalize_api_base(api_base, use_proto),
            "timeout": 120,
            "proxy": "",
            "custom_headers": {},
            "enable": True,
        }
        prov = {
            "id": pid,
            "provider_source_id": src_id,
            "enable": True,
            "model": api_model,
            # ★ 识图能力靠 modalities 声明；文本模型必须**没有** image，
            #   否则 AstrBot 会把图丢给它（它看不懂，只会瞎猜）。
            "modalities": ["text", "tool_use"],
            # 用户自定义请求体：直接并进真实请求
            # （openai_source.py:552-555）。空 dict 是合法值。
            "custom_extra_body": use_extra,
        }
        # ★ 顺序很重要：把我们的 provider 放在**第一个**。
        #
        # AstrBot 4.28 起删掉了 provider_settings.default_provider_id
        # （4.27 还有，4.28 的 default.py 里已经没有这个键）。
        # 现在它选 provider 的逻辑是（provider/manager.py:_resolve_using_provider）：
        #   先看 agent_runner 的 model.provider_id → 没有就取 provider_insts[0]。
        # 而 check_config_integrity 会把 schema 里不认识的键**直接删掉**，
        # 所以我们写进去的 default_provider_id 会被静默抹掉 ——
        # 表现是「提示配置成功，但机器人用回默认的接口」。
        #
        # 结论：不能靠 default_provider_id，只能靠**列表顺序**。
        # 把主聊天 API 放第一位，它就成了 provider_insts[0]。
        cfg["provider_sources"] = [src] + [
            s for s in (cfg.get("provider_sources") or []) if s.get("id") != src_id]
        cfg["provider"] = [prov] + [
            p for p in (cfg.get("provider") or []) if p.get("id") != pid]

        # 老版本（4.27 及以前）认这个键，写上没坏处；
        # 新版本会在启动时把它删掉，那时靠上面的顺序生效。
        cfg.setdefault("provider_settings", {})["default_provider_id"] = pid
        changed.append("主聊天 API（%s · %s）"
                       % (api_model, proto_meta["label"]))
        if extra_touched:
            changed.append("自定义请求体（%d 项）" % len(use_extra))

    # ②'' 只改自定义请求体、不动 API 三要素时走这里。
    #
    # ★ 为什么必须单独一块：用户很常见的操作是「模型能用了，只想调一下
    #   temperature」。这时他不会重填地址/Key/模型名，于是上面的块整个跳过，
    #   use_extra 就白解析了 —— 表现为「提示什么都没填」，而用户明明填了。
    #   更坏的是「想清空请求体」（传 "{}"）也会走到这里，
    #   如果这里不处理，他就永远清不掉，只能靠重新填一遍 API 三要素。
    if extra_touched and not api_any:
        _provs_now = cfg.get("provider") or []
        _target = [p for p in _provs_now if p.get("id") == "dafeiyu-main"]
        if not _target:
            raise ManagerError(
                "这个机器人还没配过主聊天 API，没法只改请求体。"
                "请先把接口地址、API Key、模型名填好。")
        _target[0]["custom_extra_body"] = use_extra
        changed.append("自定义请求体（%d 项）" % len(use_extra))

    # ②' 识图 API（可选，用户填了才动）
    #
    # 为什么要写成**另一个** provider，而不是替换主 provider：
    # AstrBot 的选法是（astr_main_agent.py:_select_image_chat_provider）——
    #   主 provider 的 modalities 里有 "image" 就用它；
    #   没有的话，去 fallback 列表里找第一个支持 image 的；
    #   都找不到就打日志 "no image-capable fallback provider is available"
    #   然后**照旧用主 provider**（图被丢掉，模型只能瞎猜）。
    # 我们之前在所有实例上都看到过这条日志 —— 那正是「发图给机器人，
    # 它答得驴唇不对马嘴」的原因。
    #
    # 所以正确做法是：主 provider 保持纯文本，另加一个带 image 的
    # provider 排在后面当 fallback。这样文本走便宜的模型、图片才走视觉模型，
    # 和生产机的配置思路一致。
    if vision_any:
        vsrc_id = "dafeiyu-vision_source"
        vpid = "dafeiyu-vision"
        vproto_meta = PROTOCOLS[use_vproto]
        vsrc = {
            "id": vsrc_id,
            "provider": vproto_meta["provider"],
            "type": use_vproto,
            "provider_type": "chat_completion",
            "key": [vision_key],
            "api_base": normalize_api_base(vision_base, use_vproto),
            "timeout": 120,
            "proxy": "",
            "custom_headers": {},
            "enable": True,
        }
        vprov = {
            "id": vpid,
            "provider_source_id": vsrc_id,
            "enable": True,
            "model": vision_model,
            # ★ 关键：必须声明 image，AstrBot 就是靠这个字段决定回退的
            "modalities": ["text", "image"],
            "custom_extra_body": use_vextra,
        }
        cfg["provider_sources"] = [s for s in (cfg.get("provider_sources") or [])
                                   if s.get("id") != vsrc_id] + [vsrc]
        # 排在主 provider **后面**（它是 fallback，不是主选）
        cfg["provider"] = [p for p in (cfg.get("provider") or [])
                           if p.get("id") != vpid] + [vprov]
        # 图片描述也指过去 —— 有些流程（引用图片、图片转述）走的是这个键，
        # 不指的话它还是空的，那条路径照样识别不了图。
        cfg.setdefault("provider_settings", {})[
            "default_image_caption_provider_id"] = vpid
        changed.append("识图 API（%s · %s）"
                       % (vision_model, vproto_meta["label"]))
        if vision_extra_touched:
            changed.append("识图 API 自定义请求体（%d 项）" % len(use_vextra))

    # ③ 人格提示词
    #
    # 这里**不写 cmd_config.json**。AstrBot 源码里那个 persona 字段标着
    # `# deprecated`（core/config/default.py:306），写进去它根本不读 ——
    # 用户会以为设了人格，实际机器人还是老样子，而且没有任何报错。
    #
    # 真实的人格存在 SQLite 的 personas 表里（core/persona_mgr.py 用
    # db.get_persona_by_id 取），再由 provider_settings.default_personality
    # 指定当前用哪一条。所以这里要写库，并且把默认人格指过去。
    persona_id = "dafeiyu-mine"
    if has_persona:
        # 注意：这里**只改内存里的 cfg**，真正的写库放在「等就绪」之后。
        # 顺序反了就是实测踩到的那个坑（见下面 wait_astrbot_ready 的说明）。
        cfg.setdefault("provider_settings", {})["default_personality"] = persona_id
        ar = cfg.setdefault("agent_runner", {})
        ar.setdefault("config", {}).setdefault("persona", {})["persona_id"] = persona_id
        changed.append("人格提示词（%d 字）" % len(persona.strip()))

    if not changed:
        raise ManagerError("没填任何要改的内容。")

    # 就绪等待已经在上面做过了（和「配置文件存在」合并成同一次等待，
    # 见那里的说明）—— 这里直接写，不要再等一次，否则会把 App 的超时预算吃掉。

    # 人格写库（必须在就绪之后：数据库比配置文件晚落盘）
    if has_persona:
        write_persona_db(name, persona_id, persona.strip())

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
        # 判据用「provider 列表第一个是不是我们的」而不是 default_provider_id ——
        # 后者在 4.28+ 会被删掉（见上面写配置处的说明）。
        provs = back.get("provider") or []
        if not provs or provs[0].get("id") != "dafeiyu-main":
            raise ManagerError("回读校验失败：主聊天 API 没写进去。")

    # 重启 astrbot 让配置生效。
    #
    # ★ 这里有个**真会咬人**的坑：AstrBot 启动时会把 cmd_config.json
    #   用内存里的配置**重写一遍**（实测重启后文件 md5 就变了）。
    #   如果用户在实例刚启动、AstrBot 还没初始化完的时候写配置，
    #   那么紧接着的重启会拿旧的内存状态把我们的写入**覆盖掉** ——
    #   表现是「提示配置成功，但过一会儿全空了」，而且完全没有报错。
    #
    #   所以：写完必须**等重启完成后再回读一次**。只在重启前回读是不够的 ——
    #   那正是我们第一次踩的坑（回读通过 → 重启覆盖 → 用户看到空配置）。
    compose(name, "restart", "astrbot", check=False)

    # 等 AstrBot 起来（它启动要十几秒），再回读校验
    wait_astrbot_ready(name, need_db=has_persona)

    back2 = read_json_maybe_bom(path)
    if gids or fids:
        got = (back2.get("platform_settings") or {}).get("id_whitelist") or []
        if got != list(gids) + ["%s:FriendMessage:%s" % (
                ((back2.get("platform") or [{}])[0].get("id") or "default"), f)
                for f in fids]:
            raise ManagerError(
                "配置没保住：重启后聊天范围又变回 %r 了。"
                "这通常是 AstrBot 还没启动完就被写了配置。"
                "请等它完全起来（状态显示「运行中」约半分钟）再改。" % (got,))
    if api_base:
        provs2 = back2.get("provider") or []
        if not provs2 or provs2[0].get("id") != "dafeiyu-main":
            raise ManagerError("配置没保住：重启后主聊天 API 丢了。"
                               "请等实例完全起来再改。")
        # ★ 只验「provider 还在」是不够的：重启时 AstrBot 用内存配置
        #   重写 cmd_config.json，provider **条目**会被保住，但里面的
        #   **Key / 地址 / 模型名**可能被旧内存值覆盖回去 —— 表现正是
        #   「换了新 Key 却怎么都改不动」：保存提示成功，机器人还用旧 Key。
        #   provider id 判据完全看不出这种「壳还在、值回退」的失败。
        #   所以这里必须逐字段核对；发现被覆盖就**再写一次**（此时
        #   AstrBot 已完全起来，不会再有第二次重写把它盖掉），然后复验。
        def _main_src_of(c):
            for s in (c.get("provider_sources") or []):
                if s.get("id") == "dafeiyu-main_source":
                    return s
            return {}
        def _main_prov_of(c):
            for p in (c.get("provider") or []):
                if p.get("id") == "dafeiyu-main":
                    return p
            return {}
        want_base = normalize_api_base(api_base, use_proto)
        s2 = _main_src_of(back2)
        p2 = _main_prov_of(back2)
        got_key = (s2.get("key") or [""])[0] if s2.get("key") else ""
        drifted = (got_key != api_key
                   or (s2.get("api_base") or "") != want_base
                   or (p2.get("model") or "") != api_model)
        if drifted:
            # 重写一次并重启后复验（就绪后重写，重启只是让它读进内存生效）。
            write_json_bom(path, cfg)
            compose(name, "restart", "astrbot", check=False)
            wait_astrbot_ready(name, need_db=has_persona)
            back3 = read_json_maybe_bom(path)
            s3 = _main_src_of(back3)
            p3 = _main_prov_of(back3)
            got_key3 = (s3.get("key") or [""])[0] if s3.get("key") else ""
            if (got_key3 != api_key
                    or (s3.get("api_base") or "") != want_base
                    or (p3.get("model") or "") != api_model):
                raise ManagerError(
                    "主聊天 API 没保住：重启后又被覆盖回旧配置了"
                    "（换新 Key/地址/模型名不生效）。"
                    "请等这个机器人状态显示「运行中」约半分钟后再保存一次。")
            # 又重启了一次 —— 后面的识图/人格/通道校验必须看**最新**的文件，
            # 否则会拿被覆盖前的旧快照 back2 误判。
            back2 = back3
    if vision_any:
        # 识图 provider 要确认三件事，少一件机器人就还是「看不见图」：
        #   ① provider 还在；
        #   ② modalities 里真的有 image（AstrBot 靠它决定要不要回退）；
        #   ③ 它排在主 provider **后面**（排前面会变成主聊天模型，
        #      那样文本也走视觉模型，又慢又贵）。
        vp = [p for p in (back2.get("provider") or [])
              if p.get("id") == "dafeiyu-vision"]
        if not vp:
            raise ManagerError("配置没保住：重启后识图 API 丢了。"
                               "请等实例完全起来再改。")
        if "image" not in (vp[0].get("modalities") or []):
            raise ManagerError("配置没保住：识图 API 少了 image 标记，"
                               "机器人还是看不到图。请重新保存一次。")
        ids2 = [p.get("id") for p in (back2.get("provider") or [])]
        if ids2 and ids2[0] != "dafeiyu-main":
            raise ManagerError("识图 API 抢到主聊天的位置了（排到了第一个），"
                               "这样文字也会走识图模型。请重新保存一次。")
    if has_persona:
        got_pid = (((back2.get("agent_runner") or {}).get("config") or {})
                   .get("persona") or {}).get("persona_id") or ""
        if got_pid != "dafeiyu-mine":
            raise ManagerError("配置没保住：重启后人格又变回 %r 了。"
                               "请等实例完全起来再改。" % (got_pid,))

    # 消息通道必须也回读确认 —— 它是「机器人回不回话」的总开关，
    # 前面几项都通过、只有它丢了的话，用户看到的是「提示成功但依然不回话」，
    # 那是最难自查的一种失败。所以单独验一次。
    st = pairing_state(name)
    if not st["paired"]:
        raise ManagerError(
            "配置写进去了，但「消息通道」没配对成功"
            "（NapCat 侧=%s / AstrBot 侧=%s / token 一致=%s）。"
            "机器人会收不到消息。请把这个机器人停掉再启动一次。"
            % (st["napcat_configured"], st["astrbot_configured"],
               st["tokens_match"]))

    # NapCat 那侧的配置需要它自己重读才生效。
    #
    # 不重启容器：**重启会让 QQ 掉线**（实测 phoenix 重启 napcat 后
    # isLogin 变 false，用户得重新扫码）。改用 NapCat 自己的配置接口热加载，
    # 它内部会走 reloadNetwork() 重建适配器（napcat.mjs:80779）。
    # 接口失败也不报错 —— 下次容器自然重启时会读到新配置，不该因此让用户
    # 的保存操作显示失败。
    if pair["napcat"]:
        _napcat_hot_reload(name)

    touch_activity(name)
    return {"ok": True, "changed": changed, "verified": True,
            "backup": os.path.basename(backup)}


def read_config(name):
    path = astrbot_cfg_path(name)
    if not os.path.exists(path):
        # ready=False 有两种情况，App 要分开说话：
        #   started=False → 他真没点过「启动」，该让他去点启动
        #   started=True  → 他点过了，容器正在初始化，该让他稍等
        # 不区分的话，刚点完启动的人会被打发去反复点启动 —— 实测踩过。
        # 拿不到结论（没有 docker 等）时给 True：宁可让人稍等，别让他白点。
        return {"ready": False,
                "started": astrbot_container_exists(name) is not False}
    cfg = read_json_maybe_bom(path)
    ps = cfg.get("platform_settings") or {}
    wl = ps.get("id_whitelist") or []
    groups = [x for x in wl if re.match(r"^[0-9]+$", str(x))]
    friends = []
    for x in wl:
        m = re.match(r"^[^:]+:FriendMessage:([0-9]+)$", str(x))
        if m:
            friends.append(m.group(1))
    # 主 provider 的判据：**列表第一个**。
    # 4.27 有 provider_settings.default_provider_id，4.28 删掉了它，
    # 改为「agent_runner.config.model.provider_id → 没有就取 provider_insts[0]」。
    # 列表顺序在哪个版本都有效，所以统一用顺序判断。
    prov = ""
    _provs = cfg.get("provider") or []
    if _provs and _provs[0].get("id") == "dafeiyu-main":
        prov = "dafeiyu-main"
    src = {}
    for s in (cfg.get("provider_sources") or []):
        if s.get("id") == "dafeiyu-main_source":
            src = s
    model = ""
    main_extra = {}
    for p in (cfg.get("provider") or []):
        if p.get("id") == "dafeiyu-main":
            model = p.get("model") or ""
            eb = p.get("custom_extra_body")
            if isinstance(eb, dict):
                main_extra = eb
    # 识图 provider（可能没配）
    vmodel = ""
    vision_extra = {}
    for p in (cfg.get("provider") or []):
        if p.get("id") == "dafeiyu-vision":
            vmodel = p.get("model") or ""
            eb = p.get("custom_extra_body")
            if isinstance(eb, dict):
                vision_extra = eb
    vsrc = {}
    for s in (cfg.get("provider_sources") or []):
        if s.get("id") == "dafeiyu-vision_source":
            vsrc = s
    # 人格要从数据库读（cmd_config.json 里的 persona 字段是废弃的，永远是空）
    persona = ""
    # 人格 id 的取法也随版本变：
    #   4.27：provider_settings.default_personality
    #   4.28：agent_runner.config.persona.persona_id（provider_settings 里那个键
    #         同样已被 schema 删掉，写了也会被抹）
    # 两个都试，哪个有值用哪个。
    default_pid = ((cfg.get("provider_settings") or {}).get("default_personality")
                   or (((cfg.get("agent_runner") or {}).get("config") or {})
                       .get("persona") or {}).get("persona_id")
                   or "")
    if default_pid in ("default", "[%None]"):
        default_pid = ""
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
        # 接口协议：App 用它回填下拉框。老实例没这个字段 → 默认 OpenAI 兼容。
        "protocol": (src.get("type") if src.get("type") in PROTOCOLS
                     else DEFAULT_PROTOCOL),
        # 自定义请求体：转成 JSON 文本回显给 App 的输入框。
        # 空 dict 也回显成 "{}"（而不是空串）—— 让用户一眼看出
        # 「当前是空的」而不是「读失败了」，两者在界面上长得一样但含义相反。
        "extra_body": json.dumps(main_extra, ensure_ascii=False, indent=2)
                      if main_extra else "",
        "extra_body_set": bool(main_extra),
        # 识图 API（没配就是空串）。Key 同样不回显。
        "vision_base": vsrc.get("api_base") or "",
        "vision_key_set": bool(vsrc.get("key")),
        "vision_model": vmodel,
        "vision_protocol": (vsrc.get("type") if vsrc.get("type") in PROTOCOLS
                            else DEFAULT_PROTOCOL),
        "vision_extra_body": json.dumps(vision_extra, ensure_ascii=False, indent=2)
                             if vision_extra else "",
        "vision_extra_body_set": bool(vision_extra),
        "persona": persona,
        # provider_ok：第一个 provider 就是主聊天 API 才算配好。
        # 不能看 default_provider_id —— 4.28+ 会删掉那个键。
        # 注意 prov 是上面算好的字符串（不是列表），别再当列表索引。
        "provider_ok": prov == "dafeiyu-main",
    }


# ---------------------------------------------------------------------------
# 主聊天 API：连通性自检 + 拉模型列表
# ---------------------------------------------------------------------------
#
# 为什么这些探测必须在**服务器上**做，而不是在手机上：
#   真正要用这个 API 的是服务器上的 AstrBot。手机能连通、服务器连不上
#   （或服务器在海外、被墙、DNS 不同）是很常见的情况 ——
#   在手机上测会给出「通的」这个错误结论，用户就再也查不出为什么机器人不回话。
#
# 为什么要有「拉模型列表」：用户不知道该填什么模型名（这是新手最常卡住的一步）。
#   能列出来就直接给他选，不用他去别处抄。

API_PROBE_TIMEOUT = 12

# 响应体上限。★ 别调小：实测 OpenRouter 的 /models 有 **737KB**，
# 早先设 200000 会把它从中间截断，json.loads 报
# 「Unterminated string starting at ...」—— 于是把一个**完全正常**的接口
# 判成「对方返回的不是 JSON，可能不是 OpenAI 兼容接口」。
# 这个 bug 只在「模型列表特别长」的服务商上出现，本地测小列表根本发现不了。
MAX_API_BODY = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# 协议表（protocol）
# ---------------------------------------------------------------------------
# ★ 为什么要有这张表，而不是让用户自己填一个 type 字符串：
#
# AstrBot 用 provider_sources[].type 去 provider_cls_map 里查适配器
# （provider/manager.py:700 `if provider_config["type"] not in provider_cls_map`）。
# 查不到就**加载失败**，而报错只有一行 traceback —— 用户在 App 上看到的是
# 「保存成功」，机器人却再也不回话，完全查不出原因。
#
# 更麻烦的是：同一个模型可能只认某一种协议。实测踩过的两类：
#   * 中转站给的是 Anthropic 原生接口（/v1/messages + x-api-key），
#     用户按默认的 openai_chat_completion 去填 → 404，报错指向「地址写错了」，
#     于是他去反复改地址，永远改不好；
#   * 官方 OpenAI 的 /v1/responses 和 /v1/chat/completions 是两套不同的
#     请求体，填错就 400。
#
# 所以：协议必须是**用户能选的一项**，而且选完要能当场验通。
#
# 表里每个协议描述四件事：
#   provider : 写进 provider_sources[].provider 的值（AstrBot 用它做二次分组）
#   label    : 给用户看的名字（不写英文 type 字符串 —— 那是给机器看的）
#   auth     : 鉴权方式，决定探测时怎么带 Key
#   kind     : 请求体形状，决定探测时发什么 payload、打哪个路径
PROTOCOLS = {
    "openai_chat_completion": {
        "label": "OpenAI 兼容（最常见，选这个就对了）",
        "provider": "openai",
        "auth": "bearer",
        "kind": "openai_chat",
        "hint": "绝大多数中转站、DeepSeek、Kimi、智谱、通义都用这个。",
    },
    "anthropic_chat_completion": {
        "label": "Anthropic 原生（Claude 官方 / Claude 中转）",
        "provider": "anthropic",
        "auth": "x-api-key",
        "kind": "anthropic",
        "hint": "接口地址填到域名即可，程序会自己补 /v1/messages。",
    },
    "googlegenai_chat_completion": {
        "label": "Google Gemini 原生",
        # ★ 必须是 "google"，不是 "googlegenai"。
        #   依据是 AstrBot 自己的配置模板（core/config/default.py 里
        #   "Google Gemini" 那一项的 provider 就是 "google"）。
        #   这个字段**不参与适配器查找**（查找用 type，见 manager.py:700），
        #   所以写错不会加载失败 —— 但它决定**厂商专属的请求改写**是否生效
        #   （openai_source.py:412 按它判 nvidia/ollama，
        #    openai_responses_source.py:77 判 deepseek）。
        #   写错的后果是「静默少了一层兼容修正」，用户在界面上完全看不出来。
        "provider": "google",
        "auth": "x-goog-api-key",
        "kind": "gemini",
        "hint": "Gemini 官方接口。国内直连不通，需要能出国的服务器。",
    },
    "openai_responses": {
        "label": "OpenAI Responses（官方新接口）",
        "provider": "openai",
        "auth": "bearer",
        "kind": "openai_responses",
        "hint": "只有官方 /v1/responses 才用这个。填成聊天接口会 400。",
    },
    "zhipu_chat_completion": {
        "label": "智谱 GLM",
        "provider": "zhipu",
        "auth": "bearer",
        "kind": "openai_chat",
        "hint": "智谱官方接口，走 OpenAI 兼容格式。",
    },
    "groq_chat_completion": {
        "label": "Groq",
        "provider": "groq",
        "auth": "bearer",
        "kind": "openai_chat",
        "hint": "Groq 官方接口，速度很快。",
    },
    "openrouter_chat_completion": {
        "label": "OpenRouter",
        "provider": "openrouter",
        "auth": "bearer",
        "kind": "openai_chat",
        "hint": "OpenRouter 官方接口，一个 Key 用很多家模型。",
    },
    "xai_chat_completion": {
        "label": "xAI Grok",
        "provider": "xai",
        "auth": "bearer",
        "kind": "openai_chat",
        "hint": "xAI 官方接口。",
    },
    "xiaomi_chat_completion": {
        "label": "小米 MiMo",
        "provider": "xiaomi",
        "auth": "bearer",
        "kind": "openai_chat",
        "hint": "小米官方接口。",
    },
    "kimi_code_chat_completion": {
        "label": "Kimi Code（Anthropic 格式）",
        # ★ 对齐官方模板：AstrBot 的 "Kimi Coding Plan" 用的是 "kimi-code"
        #   （带连字符），不是 "anthropic"。
        #   虽然鉴权和请求体形状确实和 Anthropic 一样（所以 auth/kind 不变），
        #   但 provider 名要和官方一致 —— 否则以后 AstrBot 按 provider
        #   加厂商专属逻辑时，用 Kimi 的用户会静默漏掉那层修正。
        "provider": "kimi-code",
        "auth": "x-api-key",
        "kind": "anthropic",
        "hint": "Kimi 的 Anthropic 兼容端点。",
    },
    "longcat_chat_completion": {
        "label": "美团 LongCat",
        "provider": "longcat",
        "auth": "bearer",
        "kind": "openai_chat",
        "hint": "LongCat 官方接口。",
    },
    "aihubmix_chat_completion": {
        "label": "AiHubMix",
        "provider": "aihubmix",
        "auth": "bearer",
        "kind": "openai_chat",
        "hint": "AiHubMix 中转站。",
    },
    # ★ 这里原来有一项 "mirarouter_chat_completion"，**已删除**（2026-09-18）。
    #   原因：生产 AstrBot（/AstrBot/astrbot/core/provider/sources/）里
    #   **根本没有这个适配器** —— 全库 grep "mirarouter" 零命中。
    #   它是从一份过时的适配器清单里抄进来的（那份清单还多算了它一个）。
    #
    #   为什么这个错误特别危险：
    #     AstrBot 用 type 去 provider_cls_map 里查适配器
    #     （provider/manager.py:700），查不到就**加载失败**，
    #     而报错只有一行 traceback —— 用户在 App 上看到「保存成功」，
    #     机器人却再也不回话，完全查不出原因。
    #     换句话说：**提供一个不存在的协议 = 给用户埋一个「选了就坏」的选项。**
    #
    #   所以协议表的每一项都必须能在真实 AstrBot 里找到对应适配器，
    #   这条现在由 test/check-protocol-adapters.py 对着真实源码钉住。
    "ssycloud_chat_completion": {
        "label": "胜算云",
        "provider": "ssycloud",
        "auth": "bearer",
        "kind": "openai_chat",
        "hint": "胜算云接口。",
    },
}

# 默认协议。老用户升级上来没有这个字段 —— 必须当成 OpenAI 兼容，
# 否则他们的配置会在升级那一刻失效（而他们什么都没改）。
DEFAULT_PROTOCOL = "openai_chat_completion"

# 探测/写配置时**不能**被用户自定义请求体覆盖的键。
# 为什么必须拦：custom_extra_body 是直接并进请求体里的
# （openai_source.py:552-555 `extra_body.update(custom_extra_body)`），
# 覆盖了 model 就等于换模型、覆盖了 messages 就等于把用户的提问整条换掉。
# 这类错误在界面上完全看不出来，只会表现为「机器人答非所问」。
#
# ★ 这里**故意不含** temperature / max_tokens / top_p ——
#   它们恰恰是用户最需要调的东西（AstrBot 自己的 schema 就把它们
#   列为 custom_extra_body 的示例）。把它们拦掉等于把「自定义请求体」
#   这个功能做废。要拦的只是那些会破坏对话结构本身的键。
RESERVED_BODY_KEYS = {
    "model", "messages", "input", "contents", "stream", "tools",
    "tool_choice", "system",
}


def protocol_of(name):
    """读回实例当前用的是哪种协议。没配过 = 默认 OpenAI 兼容。"""
    path = astrbot_cfg_path(name)
    if not os.path.exists(path):
        return DEFAULT_PROTOCOL
    try:
        cfg = read_json_maybe_bom(path)
    except Exception:  # noqa: BLE001
        return DEFAULT_PROTOCOL
    for s in (cfg.get("provider_sources") or []):
        if s.get("id") == "dafeiyu-main_source":
            t = s.get("type") or ""
            return t if t in PROTOCOLS else DEFAULT_PROTOCOL
    return DEFAULT_PROTOCOL


def normalize_protocol(value):
    """把用户传上来的协议名规范化。空 = 默认；不认识 = 报错说清楚有哪些。"""
    p = (value or "").strip()
    if not p:
        return DEFAULT_PROTOCOL
    if p in PROTOCOLS:
        return p
    raise ManagerError(
        "不认识的接口协议「%s」。可选的有：%s。"
        % (p, "、".join(sorted(PROTOCOLS))))


def normalize_api_base(base, protocol):
    """按协议把接口地址规范成**能直接用**的形状。

    ★ 这是踩出来的：Anthropic 的适配器会做 `removesuffix("/v1")`
    （anthropic_source.py:98），所以用户按别家的习惯填成
    `https://api.anthropic.com/v1` 时，程序内部变成 `https://api.anthropic.com`，
    再拼 `/v1/messages` —— 结果是对的。
    但如果用户在 App 里、或者在我们自己的探测里也照着「带 /v1」去拼，
    就会拼出 `/v1/v1/messages` 这种 404。
    所以地址的规范化必须**跟适配器的真实行为对齐**，两边不能各写一套。

    Gemini 那边则是去掉结尾的 `/`（gemini_source.py:75-76）。
    """
    b = (base or "").strip()
    if not b:
        return b
    if protocol in ("anthropic_chat_completion", "kimi_code_chat_completion"):
        # 对齐 anthropic_source：先去掉结尾 /，再去掉结尾 /v1
        b = b.rstrip("/")
        if b.endswith("/v1"):
            b = b[:-3]
        return b
    if protocol == "googlegenai_chat_completion":
        # Gemini 官方模板的地址是 https://generativelanguage.googleapis.com/
        # （**不带** /v1，见 AstrBot 的 core/config/default.py）。
        # 而程序自己拼路径时会补 /v1beta/...。
        #
        # ★ 必须把用户可能多填的版本段剥掉，否则拼出
        #   `/v1/v1beta/models/...` 这种 404。
        #   为什么用户**一定会**多填：他多半是从别家（OpenAI 兼容）的
        #   配置复制过来的，那边习惯是 `https://xxx/v1`；切到 Gemini 时
        #   地址栏里那个 /v1 很容易留着。
        #   而 404 的报错会指向「地址写错了」，他会去反复改地址，永远改不好。
        #   实测（修复前）：https://x.example/v1
        #     → https://x.example/v1/v1beta/models/m:generateContent  ← 404
        b = b.rstrip("/")
        for suffix in ("/v1beta", "/v1"):
            if b.endswith(suffix):
                b = b[: -len(suffix)]
                break
        return b.rstrip("/")
    return b


def protocol_endpoint(base, protocol, which, model=""):
    """按协议拼出真正要请求的 URL。

    which: "chat"（发一次真实请求）/ "models"（拉模型列表）
    model: 只有 Gemini 的 chat 路径需要它（模型名在路径里）。
    返回 None 表示这个协议没有这个端点（比如 Responses 没有 /models）。
    """
    b = normalize_api_base(base, protocol)
    kind = PROTOCOLS[protocol]["kind"]
    if kind == "openai_chat":
        return b + ("/chat/completions" if which == "chat" else "/models")
    if kind == "openai_responses":
        # Responses 接口没有 /models（那是另一个端点），只探 chat。
        return b + "/responses" if which == "chat" else None
    if kind == "anthropic":
        # 对齐 anthropic_source 的 base_url（已去掉 /v1）+ SDK 自己拼的路径
        return b + ("/v1/messages" if which == "chat" else "/v1/models")
    if kind == "gemini":
        if which == "models":
            return b + "/v1beta/models"
        # ★ Gemini 的模型名在**路径**里，不是请求体字段：
        #   POST /v1beta/models/{model}:generateContent
        # 没有模型名就拼不出这个 URL。
        if not model:
            return None
        return b + "/v1beta/models/%s:generateContent" % model
    return None


def protocol_probe_payload(protocol, model):
    """按协议造一个**最小**的探测请求体。

    每家要的字段名都不一样（messages / input / contents），
    拿 OpenAI 的请求体去问 Anthropic 只会拿到 400，
    而 400 的报错会指向「请求不合法」，把用户带偏到完全无关的方向。
    """
    kind = PROTOCOLS[protocol]["kind"]
    if kind == "openai_chat":
        return {"model": model, "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1, "stream": False}
    if kind == "openai_responses":
        return {"model": model, "input": "hi", "max_output_tokens": 16,
                "stream": False}
    if kind == "anthropic":
        # Anthropic 的 max_tokens 是**必填**（不填直接 400），所以这里必须给。
        return {"model": model, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
    if kind == "gemini":
        return {"contents": [{"parts": [{"text": "hi"}]}],
                "generationConfig": {"maxOutputTokens": 16}}
    return {"model": model, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1}


def protocol_headers(protocol, key):
    """按协议造鉴权头。

    ★ 这里必须分开写，不能一律 Bearer：
    Anthropic 收 Bearer 会返回 401，而 401 在我们的报错表里对应
    「Key 不对」—— 于是用户会去重新复制一个**完全正确**的 Key，
    复制十遍也没用。错的是鉴权方式，不是 Key。
    """
    auth = PROTOCOLS[protocol]["auth"]
    h = {"Accept": "application/json"}
    if auth == "bearer":
        h["Authorization"] = "Bearer %s" % key
    elif auth == "x-api-key":
        h["x-api-key"] = key
        h["anthropic-version"] = "2023-06-01"
    elif auth == "x-goog-api-key":
        h["x-goog-api-key"] = key
    return h


def vision_protocol_of(name):
    """读回识图 API 用的是哪种协议。没配过 = 默认 OpenAI 兼容。

    ★ 单独一个函数而不是复用 protocol_of：识图常常是**另一家**
    （主聊天用中转站、识图用智谱官方），两套协议互不相干。
    如果这里错跟了主协议，用户改主 API 的协议会把识图一起改坏，
    而现象是「文字能聊、图看不懂」—— 极难联想到是主协议改动的副作用。
    """
    path = astrbot_cfg_path(name)
    if not os.path.exists(path):
        return DEFAULT_PROTOCOL
    try:
        cfg = read_json_maybe_bom(path)
    except Exception:  # noqa: BLE001
        return DEFAULT_PROTOCOL
    for s in (cfg.get("provider_sources") or []):
        if s.get("id") == "dafeiyu-vision_source":
            t = s.get("type") or ""
            return t if t in PROTOCOLS else DEFAULT_PROTOCOL
    return DEFAULT_PROTOCOL


def extra_body_of(name, which="main"):
    """读回用户自定义的请求体参数。没配过就返回 {}。

    which: "main" 读主聊天 API，"vision" 读识图 API。
    识图那一套也支持自定义请求体 —— 实测有些视觉模型必须显式关掉
    thinking（reasoning_effort / thinking），不关就只回思考过程、不回正文，
    表现为「发了图它答非所问」。
    """
    pid = "dafeiyu-main" if which == "main" else "dafeiyu-vision"
    path = astrbot_cfg_path(name)
    if not os.path.exists(path):
        return {}
    try:
        cfg = read_json_maybe_bom(path)
    except Exception:  # noqa: BLE001
        return {}
    for p in (cfg.get("provider") or []):
        if p.get("id") == pid:
            eb = p.get("custom_extra_body")
            return dict(eb) if isinstance(eb, dict) else {}
    return {}


def parse_extra_body(raw, label="自定义请求体"):
    """把用户填的一段 JSON 解析成 dict，并做防呆校验。

    ★ 为什么要校验得这么细，而不是 json.loads 完直接用：
    这段内容会被**原样并进发给模型的请求体**
    （openai_source.py:552-555 `extra_body.update(custom_extra_body)`），
    也就是说用户在这里写的每个字都会影响真实请求。踩过/可预见的坑：

      * 写成 `{"temperature": 0.7,}` 这种尾逗号 → 解析失败，
        如果只报「JSON 格式错」用户不知道该删哪个逗号，所以要把
        Python 的原始报错带上（它带行列号）。
      * 顶层写成数组 `[1,2]` 或字符串 → 不是 dict，update() 会炸，
        而且是在**机器人回复时**炸，用户完全联系不到是这里填错了。
      * 覆盖 model / messages → 等于把用户的提问整条换掉，
        表现为「机器人答非所问」，界面上看不出来。
      * 值写成字符串 "0.7" → 有些网关直接 400，有些静默按 0 处理。

    返回 (dict, 警告列表)。警告不阻断保存 —— 用户可能有正当理由
    （比如某个网关要一个非标准字段），但必须让他知道。
    """
    s = (raw or "").strip()
    if not s:
        return {}, []
    try:
        obj = json.loads(s)
    except ValueError as e:
        raise ManagerError(
            "%s不是合法的 JSON：%s。"
            "常见的错是：用了中文引号「」、多了一个逗号、"
            "或者忘了给字段名加英文双引号。" % (label, e))
    if not isinstance(obj, dict):
        raise ManagerError(
            "%s要写成一对大括号包起来的字段，比如 {\"temperature\": 0.7}。"
            "现在填的是一个%s，不是字段表。" % (
                label,
                "列表" if isinstance(obj, list) else
                "数字" if isinstance(obj, (int, float)) and not isinstance(obj, bool)
                else "字符串" if isinstance(obj, str) else "值"))
    warns = []
    for k in list(obj.keys()):
        if k in RESERVED_BODY_KEYS:
            raise ManagerError(
                "%s里不能写「%s」—— 这个字段由程序自己填，"
                "你在这里写会把它覆盖掉，机器人就会答非所问，"
                "而且在界面上完全看不出来。请删掉这一项。" % (label, k))
        if not isinstance(k, str) or not k.strip():
            raise ManagerError("%s里有空字段名，请删掉。" % label)
        v = obj[k]
        if isinstance(v, (dict, list)):
            continue          # 嵌套结构是合法的（如 thinking、extra_headers）
        if isinstance(v, str) and v.strip() == "":
            warns.append("「%s」是空字符串" % k)
        if isinstance(v, str):
            try:
                float(v)
            except ValueError:
                pass
            else:
                warns.append("「%s」的值被引号包成了字符串（%r）—— "
                             "数字应该写成不带引号的 %s" % (k, v, v))
    return obj, warns


def _extra_body_for_probe(raw):
    """探测接口时用的自定义请求体：解析失败就**不当失败**，返回 None。

    ★ 为什么不在这里抛错：用户点的是「测试接口」，不是「保存」。
    他可能正打到一半（JSON 还没写完）就想先测测地址通不通。
    这时报「JSON 格式错」会让他以为接口坏了 —— 而他只是没写完。
    真正要拦的是**保存**那一步（parse_extra_body 在 apply_config 里会拦），
    那一步不拦才会把坏配置写进机器人。
    """
    s = (raw or "").strip()
    if not s:
        return None
    try:
        obj, _ = parse_extra_body(raw)
    except ManagerError:
        return None
    return obj or None


def _vision_api_of(name):
    """从实例配置里读回「识图 API」的三要素。没配过就返回空串。

    和 _main_api_of 一样不抛异常 —— 调用方要能区分「没配过」和「读失败」。
    """
    path = astrbot_cfg_path(name)
    if not os.path.exists(path):
        return "", "", ""
    cfg = read_json_maybe_bom(path)
    base, key = "", ""
    for s in (cfg.get("provider_sources") or []):
        if s.get("id") == "dafeiyu-vision_source":
            base = s.get("api_base") or ""
            ks = s.get("key") or []
            if ks:
                key = ks[0] or ""
    model = ""
    for p in (cfg.get("provider") or []):
        if p.get("id") == "dafeiyu-vision":
            model = p.get("model") or ""
    return base, key, model


def _main_api_of(name, strict=False):
    """从实例配置里读回主 API 的三要素：接口地址 / Key / 模型名。

    strict=False（默认）时，配置还没生成就返回空串 —— 不抛异常。
    ★ 这是踩出来的：调用方（测接口）经常是**用户还没点启动、但已经在
      输入框里填好了地址和 Key**，这时它想测的是「我填的这套行不行」，
      跟实例有没有启动毫无关系。原来这里直接抛「先点启动」，
      把「测一下我填的对不对」这个正当需求挡死了 ——
      用户被迫先启动（要等拉镜像、一两分钟）才能验证自己填得对不对，
      而如果填错了，这一两分钟纯属白等。
    strict=True 保留给「确实必须有已保存配置」的场景。
    """
    path = astrbot_cfg_path(name)
    if not os.path.exists(path):
        if strict:
            raise ManagerError("这个机器人还没启动过，先点「启动」再测接口。")
        return "", "", ""
    cfg = read_json_maybe_bom(path)
    base, key = "", ""
    for s in (cfg.get("provider_sources") or []):
        if s.get("id") == "dafeiyu-main_source":
            base = s.get("api_base") or ""
            ks = s.get("key") or []
            if ks:
                key = ks[0] or ""
    model = ""
    for p in (cfg.get("provider") or []):
        if p.get("id") == "dafeiyu-main":
            model = p.get("model") or ""
    return base, key, model


def _dns_ok(host):
    """域名能不能解析出来。"""
    try:
        socket.gethostbyname(host)
        return True
    except Exception:  # noqa: BLE001
        return False


def _host_of(base):
    try:
        return urllib.parse.urlsplit(base).hostname or ""
    except Exception:  # noqa: BLE001
        return ""


def _reference_reachable():
    """服务器到底有没有网？用一个中立地址判断。

    ★ 这个函数是**踩过坑才加的**：实测把一个域名打错（api.deepsek.com），
    报错是「[Errno 101] Network is unreachable」—— 和「服务器没网」一模一样。
    照字面翻译就会告诉用户「检查服务器网络」，于是他跑去折腾服务器，
    而真正的问题只是他少打了一个 e。域名写错时会解析到某个不相干的 IP
    （实测 deepsek.com → 31.13.82.33，一个 Facebook 的地址段），
    连过去自然是 unreachable。

    所以：连不上时先确认「别人能不能连通」。能连通 → 问题在这个地址；
    连不通 → 才是服务器网络的问题。只在失败路径上多花这一次探测。
    """
    for ref in ("https://api.deepseek.com/v1/models", "https://www.baidu.com/"):
        try:
            urllib.request.urlopen(
                urllib.request.Request(ref, headers={"User-Agent": "dafeiyu-probe"}),
                timeout=6)
            return True
        except urllib.error.HTTPError:
            return True          # 有 HTTP 响应就说明网络通（401/403 也算）
        except Exception:  # noqa: BLE001
            continue
    return False


def _api_error_hint(e, base=""):
    """把底层网络异常翻译成**用户能照着做**的话。

    原始报错（如 <urlopen error [Errno 101] Network is unreachable>）
    对用户毫无意义，而且会把「地址写错」和「服务器网络不通」混成一句 ——
    前者要改一个字母，后者要运维，指错方向会让用户白折腾很久。
    """
    s = str(e)
    low = s.lower()
    host = _host_of(base)

    if "name or service not known" in low or "nodename nor servname" in low \
            or "getaddrinfo" in low or "name resolution" in low \
            or "no address associated" in low:
        return ("域名解析不了 —— 接口地址写错了（多打或少打了字母）。"
                "请回到官网复制，注意结尾要带 /v1。")

    # 连不上：先分清是「这个地址的问题」还是「服务器没网」。
    if ("unreachable" in low or "timed out" in low or "timeout" in low
            or "connection refused" in low or "connection reset" in low
            or "network is down" in low):
        if host and not _dns_ok(host):
            return ("域名解析不了 —— 接口地址写错了（多打或少打了字母）。"
                    "请回到官网复制，注意结尾要带 /v1。")
        if _reference_reachable():
            # 服务器有网，那就是这个地址的问题
            return ("服务器能上网，但这个地址连不上 —— 多半是地址写错了。"
                    "请逐字对照官网复制（常见的错：少一个字母、"
                    "把 /v1 漏掉、结尾多了斜杠）。")
        return ("服务器本身连不上外网（换别的网站也一样不通）。"
                "这是服务器的网络问题，不是配置问题 —— "
                "如果这台服务器在境外而 API 在国内、或反过来，"
                "都可能出现这种情况。")

    if "certificate" in low or "ssl" in low or "tls" in low:
        return "HTTPS 证书校验失败 —— 这个地址的证书有问题，别用它。"
    return "连不上：%s" % s


def _api_request(url, key, payload=None, protocol=DEFAULT_PROTOCOL):
    """向 API 发一次请求。返回 (状态码, 响应体文本)。异常原样抛出给调用方翻译。

    ★ 鉴权头按协议走（见 protocol_headers）—— 不能一律 Bearer，
    否则 Anthropic 会返回 401，被我们翻译成「Key 不对」，让用户白折腾。
    """
    data = None
    headers = protocol_headers(protocol, key)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=API_PROBE_TIMEOUT) as resp:
            return resp.getcode(), resp.read(MAX_API_BODY).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也是有效信息（Key 不对、地址不对），别当异常吞掉
        try:
            body = e.read(65536).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body


def _join_api(base, path):
    b = (base or "").strip().rstrip("/")
    if not b:
        raise ManagerError("还没填接口地址。")
    if not (b.startswith("http://") or b.startswith("https://")):
        raise ManagerError("接口地址要以 http:// 或 https:// 开头。")
    return b + path


def _models_from_body(protocol, body):
    """从各家**形状完全不同**的模型列表响应里挑出模型名。

    不分开写的话，Gemini 的 {"models":[{"name":"models/gemini-2.0-flash"}]}
    会被当成「对方没给模型列表」，用户于是只能手填 —— 而手填正是
    最容易错的那一步（多一个空格、少一个 -preview 就 404）。
    """
    obj = json.loads(body)
    kind = PROTOCOLS[protocol]["kind"]
    ids = []
    if kind == "gemini":
        for m in (obj.get("models") or []):
            if isinstance(m, dict) and m.get("name"):
                # Gemini 返回的是 "models/gemini-2.0-flash"，调用时要的是后半段
                ids.append(str(m["name"]).split("/")[-1])
    elif kind == "anthropic":
        for m in (obj.get("data") or []):
            if isinstance(m, dict) and m.get("id"):
                ids.append(str(m["id"]))
    else:
        for m in (obj.get("data") or []):
            if isinstance(m, dict) and m.get("id"):
                ids.append(str(m["id"]))
            elif isinstance(m, str):
                ids.append(m)
    return ids


def list_api_models(name, base="", key="", protocol=""):
    """拉取这个 API 支持的模型列表。

    base/key 传空则用实例里已保存的 —— 这样「还没保存就想先看看有哪些模型」
    也能用（先测通再保存，比「保存了才发现模型名错」友好得多）。

    ★ 注意：**不能拿这个接口的成败来判断 Key 对不对**。
    实测 OpenRouter 的 /models 用无效 Key 也返回 200（它不鉴权），
    但 /chat/completions 用同样的无效 Key 返回 401。
    所以「拉得到模型列表」只证明**地址通**，不证明 Key 可用 ——
    真正验 Key 要用 probe_api 里那次真实聊天调用。

    protocol 决定打哪个端点、怎么带 Key、怎么解析返回：
    Gemini 的列表在 {"models":[...]} 里且名字带 "models/" 前缀，
    Anthropic 的鉴权头是 x-api-key 而不是 Bearer —— 一律按 OpenAI 处理的话，
    前者会「拉不到列表」，后者会「Key 不对」，两种都会把用户带偏。
    """
    proto = normalize_protocol(protocol) if protocol else protocol_of(name)
    saved_base, saved_key, _ = _main_api_of(name)
    use_base = (base or "").strip() or saved_base
    use_key = (key or "").strip() or saved_key
    if not use_base:
        raise ManagerError("还没填接口地址。")
    if not use_key:
        raise ManagerError("还没填 API Key。")
    url = protocol_endpoint(use_base, proto, "models")
    if not url:
        # 这个协议没有独立的「列模型」端点（如 Responses）。
        # 不报错 —— 那会挡住用户保存。返回空列表让他手填。
        return []
    try:
        code, body = _api_request(url, use_key, protocol=proto)
    except ManagerError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ManagerError(_api_error_hint(e, use_base))

    if code in (401, 403):
        raise ManagerError("能连上这个地址，但 API Key 不对（对方返回 %d）。"
                           "检查 Key 有没有复制全、有没有多余空格；"
                           "也要确认「接口协议」选对了 —— 选错协议时"
                           "对方也会说 Key 不对。" % code)
    if code == 404:
        raise ManagerError("能连上，但这个地址没有模型列表接口（404）。"
                           "多半是地址写得不完整 —— 检查结尾是不是少了 /v1，"
                           "或者「接口协议」选得不对。")
    if code == 429:
        raise ManagerError("能连上，但被限流了（429）。等一会儿再试，或检查额度。")
    if code >= 500:
        raise ManagerError("对方服务器出错（%d），不是你的配置问题，稍后再试。" % code)
    if code != 200:
        raise ManagerError("对方返回了 %d，没能取到模型列表。" % code)

    try:
        ids = _models_from_body(proto, body)
    except ValueError:
        raise ManagerError("能连上，但对方返回的不是 JSON —— 这个地址可能不是 "
                           "%s 接口。" % PROTOCOLS[proto]["label"])

    if not ids:
        # 有些网关 /models 返回空列表但实际可用 —— 不当失败，只是没得选。
        return []
    return sorted(set(ids))


def _chat_probe(base, key, model, protocol=DEFAULT_PROTOCOL, extra_body=None):
    """发一次**最小**的真实聊天请求 —— 这是唯一能证明「配置可用」的测试。

    为什么非要发聊天请求、不能只看 /models：
      ① 实测 OpenRouter 的 /models 对**任何** Key 都返回 200（不鉴权），
         只看它就会把坏 Key 判成好的 —— 用户看到「通了 ✓」，
         然后发现机器人根本不回话，比不做检测还糟；
      ② 地址通、Key 对，也可能因为**模型名不存在**而失败，
         这正是新手最常犯的错；
      ③ 用 max_tokens=1 让成本可以忽略。

    ★ 按协议发对应的请求体和鉴权头：拿 OpenAI 的
    {"messages":[…]} 去问 Anthropic 只会拿到 400，
    而 400 的报错指向「请求不合法」，会把用户带去改完全无关的东西。
    extra_body 是用户自定义的请求体，这里也一起带上 ——
    否则「自定义了参数却测通、真跑起来 400」这种最难查的情况就会发生。

    返回 (ok, 说明文字)。ok=False 时说明文字已经是给人看的话。
    """
    proto = normalize_protocol(protocol)
    url = protocol_endpoint(base, proto, "chat", model)
    if not url:
        # 拼不出 URL（如 Gemini 没给模型名）。此时**不能**返回 ok=True ——
        # 那是没根据的假绿灯：用户会看到「通了 ✓」然后发现机器人不回话。
        # 也**不能**返回 False：那会把「我们测不了」说成「你的配置坏了」。
        # 正确做法是抛一个 ManagerError，让 probe_api 如实转述，
        # 并且**不**把它算成认证失败。
        raise ManagerError(
            "这个协议（%s）的地址里要带模型名，所以必须先填模型名才能测。"
            % PROTOCOLS[proto]["label"])
    payload = protocol_probe_payload(proto, model)
    if extra_body:
        # 探测时不覆盖 model —— 探测必须问的是用户填的那个模型。
        for k, v in extra_body.items():
            if k not in ("model", "messages", "input", "contents"):
                payload[k] = v
    try:
        code, body = _api_request(url, key, payload, protocol=proto)
    except ManagerError:
        raise
    except Exception as e:  # noqa: BLE001
        return False, _api_error_hint(e, base)

    if code == 200:
        return True, ""
    if code in (401, 403):
        return False, ("Key 不对，或者「接口协议」选错了（对方返回 %d）。"
                       "先确认协议选的是不是这一家的官方协议，"
                       "再检查 Key 有没有复制全、有没有多余空格。" % code)
    if code == 404:
        # 404 有两种可能：地址不对，或模型名不对 —— 看对方怎么说。
        low = body.lower()
        if "model" in low:
            return False, ("接口地址是对的，但对方不认识「%s」这个模型名。"
                           "点「获取可用模型」从列表里挑一个。" % model)
        return False, ("这个地址没有聊天接口（404）—— 地址多半写得不完整，"
                       "检查结尾是不是少了 /v1；如果地址是对的，"
                       "那就是「接口协议」选错了。")
    if code == 400:
        low = body.lower()
        if "model" in low:
            return False, ("接口和 Key 都通，但模型名「%s」不对。"
                           "点「获取可用模型」从列表里挑一个。" % model)
        # 400 也可能是 Key 格式问题（有些网关对格式不合法直接 400）
        return False, "对方说请求不合法（400）：%s" % body[:200]
    if code == 402:
        return False, "账户余额不足或未开通（402）—— 去官网充值/开通后再试。"
    if code == 429:
        return False, "被限流了（429）。稍等再试，或检查额度是否用完。"
    if code >= 500:
        return False, "对方服务器出错（%d），不是你的配置问题，稍后再试。" % code
    return False, "对方返回 %d：%s" % (code, body[:200])


def probe_api(name, base="", key="", model="", protocol="", extra_body=None):
    """测主聊天 API 到底通不通。返回一份给人看的结论。

    结论分三层，分开说 —— 混成一句「失败」用户就不知道下一步做什么：
      reachable : 服务器能不能连上这个地址（网络层）
      auth_ok   : Key 对不对（认证层）
      model_ok  : 模型名在不在对方的列表里（配置层）

    ★ 第四层是「协议对不对」：地址通、Key 对、模型名对，
    但协议选错（拿 OpenAI 的请求体去问 Anthropic）照样跑不起来，
    而报错会指向「请求不合法」。所以这里把协议也一起验，
    并且把选用的协议回显给用户，让他能对照官网确认。
    """
    proto = normalize_protocol(protocol) if protocol else protocol_of(name)
    saved_base, saved_key, saved_model = _main_api_of(name)
    use_base = (base or "").strip() or saved_base
    use_key = (key or "").strip() or saved_key
    use_model = (model or "").strip() or saved_model

    out = {
        "reachable": False,
        "auth_ok": False,
        "model_ok": None,        # None = 没测（没给模型名 / 拉不到列表）
        "models": [],
        "model_count": 0,
        "message": "",
        "api_base": use_base,
        "protocol": proto,
        "protocol_label": PROTOCOLS[proto]["label"],
        # 回显规范化后的地址 —— Anthropic 会去掉结尾的 /v1，
        # 用户看到这个才知道自己填的地址最终被用成了什么。
        "api_base_effective": normalize_api_base(use_base, proto),
    }

    if not use_base:
        out["message"] = "还没填接口地址。"
        return out
    if not use_key:
        out["message"] = "还没填 API Key。"
        return out

    # ① 先拉模型列表。
    #    它的作用是「地址通不通」+「有哪些模型可选」，**不是**验 Key ——
    #    实测 OpenRouter 的 /models 对任何 Key 都返回 200（不鉴权），
    #    所以不能拿它的成功来宣布「Key 没问题」（那会是假绿灯）。
    models = []
    try:
        models = list_api_models(name, use_base, use_key, proto)
        out["reachable"] = True
        out["models"] = models
        out["model_count"] = len(models)
    except ManagerError as e:
        msg = str(e)
        out["reachable"] = ("能连上" in msg)
        out["message"] = msg
        # 地址都不通/地址错 → 不用再发聊天请求了，省一次往返和一次可能的费用。
        if not out["reachable"]:
            return out
        # 能连上但 /models 说 Key 不对 → 以它为准（这类服务商确实会鉴权）。
        if "API Key 不对" in msg:
            out["message"] = msg
            return out
        # 其它情况（没有 /models 接口等）继续往下用聊天请求做终判。
    except Exception as e:  # noqa: BLE001
        out["message"] = _api_error_hint(e, use_base)
        return out

    # ② 真实聊天请求 —— 唯一能证明「这套配置真的能用」的测试。
    #
    #    这一步不能省：/models 可能不鉴权（假绿灯），而且就算地址和 Key 都对，
    #    模型名写错照样跑不起来 —— 那正是新手最常犯的错。
    #    max_tokens=1 把成本压到几乎为零。
    if not use_model:
        # 没有模型名就没法发聊天请求。此时只报「地址通、Key 待验证」，
        # 不能说「通了 ✓」—— 那是没根据的。
        out["auth_ok"] = False
        out["model_ok"] = None
        if models:
            out["message"] = ("接口地址能连上。还没填模型名 —— "
                              "从下面的列表里挑一个，然后再测一次。")
        else:
            out["message"] = ("接口地址能连上。还没填模型名，"
                              "而且对方没给模型列表 —— 请照官网文档填一个再测。")
        return out

    try:
        ok, why = _chat_probe(use_base, use_key, use_model, proto, extra_body)
    except ManagerError as e:
        out["message"] = str(e)
        return out

    if ok:
        out["auth_ok"] = True
        out["model_ok"] = True
        out["message"] = ("通了 ✓ 接口、Key、模型名、协议都对，机器人可以用了。"
                          "（当前协议：%s）" % PROTOCOLS[proto]["label"])
        return out

    # 失败：分清是 Key 的问题还是模型名的问题 —— 用户要改的地方不一样。
    out["auth_ok"] = ("Key 不对" not in why)
    out["model_ok"] = False if ("模型" in why) else None
    if models and use_model not in models:
        # 顺手核对一下列表：如果列表里确实没有这个名字，几乎可以确定是名字错了。
        out["message"] = ("接口和 Key 都对，但「%s」不在对方的模型列表里。"
                          "从下面挑一个正确的（这是最常见的错误）。" % use_model)
    else:
        out["message"] = why
    return out


def _make_test_png(rgb, size=64):
    """生成一张纯色 PNG（纯 Python，不依赖 PIL）。

    为什么要自己造图：验证「这个 API 到底能不能看图」，唯一的办法是
    **给它一张图，看它能不能说出图里是什么**。用固定图片的话，
    模型可能靠「背答案」蒙对；用纯色+随机颜色，就必须真的看。

    size 默认 64：够模型识别，base64 后只有几百字节，几乎不花 token。
    """
    w = h = size
    r, g, b = rgb
    raw = b"".join(b"\x00" + bytes([r, g, b]) * w for _ in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8bit truecolor
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


# 测试用的颜色。挑「差别极大、模型不可能混淆」的四种。
#
# 为什么用颜色而不是文字（OCR）：OCR 要求模型有文字识别能力，
# 有些能看图的模型（尤其是小模型）认字很弱，会被误判成「不能看图」。
# 颜色是最基础的视觉能力，能看图就一定能分辨。
#
# ★ 每个颜色带一串**同义词**。这是踩出来的：
#   原来只认「黄」，实测 deepseek-flash 看 (230,210,20) 答的是「金色」——
#   它明明看见了，却因为用词不同被判成「没看图」（假阴性）。
#   假阴性的危害和假阳性一样大：用户会去换一个本来没问题的 API。
#   所以宁可放宽：只要答出的词落在这个颜色的同义词里就算对。
#
#   另外把颜色改成**纯正的高饱和色**（255 而非 220/230），
#   减少「金黄」「深红」这类边界描述。
_VISION_COLORS = [
    ((255, 0, 0), ("红", ("红", "red", "大红", "鲜红", "正红", "朱红"))),
    ((0, 200, 0), ("绿", ("绿", "green", "翠绿", "草绿", "深绿", "青绿"))),
    ((0, 0, 255), ("蓝", ("蓝", "blue", "深蓝", "天蓝", "宝蓝", "湛蓝"))),
    ((255, 255, 0), ("黄", ("黄", "yellow", "金黄", "金色", "亮黄", "正黄"))),
]

# 「我看不到图」这类回答的特征词。模型不能看图时通常会这么说。
_VISION_REFUSAL_WORDS = (
    "无法查看", "看不到", "不能查看", "无法查看图片", "无法看到", "没有看到",
    "cannot see", "can't see", "unable to view", "cannot view", "no image",
    "don't see", "do not see", "无法识别图片", "不能识别图片", "我没有收到图",
    "没有图片", "无法处理图片", "不支持图片", "不支持图像",
)


def _vision_payload(protocol, model, png_b64, prompt):
    """按协议造「带图提问」的请求体。

    ★ 图片在三种协议里的表示**完全不同**：
      OpenAI 兼容 : content 数组里的 {"type":"image_url","image_url":{"url":"data:…"}}
      Anthropic   : content 数组里的 {"type":"image","source":{"type":"base64",…}}
      Gemini      : parts 里的 {"inline_data":{"mime_type":…,"data":…}}
    拿 OpenAI 的形状去问 Anthropic/Gemini，对方会返回 400 说「请求不合法」，
    而我们的报错表会把 400 解释成「它不接受图片」—— 于是用户被引导去
    换一个**本来没问题**的识图模型，白折腾一圈还是不行。
    """
    kind = PROTOCOLS[protocol]["kind"]
    if kind == "anthropic":
        return {"model": model, "max_tokens": 1000, "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": png_b64}},
            ]}]}
    if kind == "gemini":
        return {"contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/png", "data": png_b64}},
        ]}], "generationConfig": {"maxOutputTokens": 1000}}
    if kind == "openai_responses":
        return {"model": model, "max_output_tokens": 1000, "input": [{
            "role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image",
                 "image_url": "data:image/png;base64," + png_b64},
            ]}]}
    # OpenAI 兼容（默认）
    return {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + png_b64}},
            ],
        }],
        # ★ max_tokens 必须给足，否则会把「能识图的模型」误判成「没法判断」。
        #
        #   踩过的坑：一开始用 20，结果**每次**都拿到空回复，于是明明能看图的
        #   模型被判成「测不出来」。原因是现在很多模型（deepseek-flash、
        #   v4-pro 等都是）属于**推理模型**：先写一段 reasoning_content，
        #   再写 content。token 预算被「思考」吃光时，正文一个字都没轮到，
        #   返回 finish_reason=length、content 为空。
        #
        #   实测同一个模型同一张图（每档 8 次）：
        #       max_tokens=300  → 空 6/8   （思考长度 129~530 token，波动很大）
        #       max_tokens=600  → 空 2/8
        #       max_tokens=1000 → 空 0/8   ← 采用
        #   1000 看着大，实际用掉最多 532 token，一次测试成本可以忽略。
        #   这里**宁可给多**：给少了会误判，而误判的代价是用户去换一个
        #   本来没问题的 API，白折腾。
        "max_tokens": 1000,
        "stream": False,
    }


def _vision_text_of(protocol, body):
    """从各协议的响应里取出模型说的话。取不到返回 ""。"""
    kind = PROTOCOLS[protocol]["kind"]
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if kind == "anthropic":
        parts = data.get("content") or []
        return " ".join(str(b.get("text", "")) for b in parts
                        if isinstance(b, dict) and b.get("type") == "text").strip()
    if kind == "gemini":
        cands = data.get("candidates") or []
        if not cands:
            return ""
        parts = ((cands[0] or {}).get("content") or {}).get("parts") or []
        return " ".join(str(p.get("text", "")) for p in parts
                        if isinstance(p, dict)).strip()
    if kind == "openai_responses":
        # Responses 的正文在 output[].content[].text
        out = []
        for item in (data.get("output") or []):
            if not isinstance(item, dict):
                continue
            for c in (item.get("content") or []):
                if isinstance(c, dict) and c.get("text"):
                    out.append(str(c["text"]))
        if out:
            return " ".join(out).strip()
        # 有些网关仍给 output_text
        return str(data.get("output_text") or "").strip()
    # OpenAI 兼容
    ch = (data.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content = msg.get("content") or ""
    if isinstance(content, list):
        # 有些网关把 content 也做成数组
        content = " ".join(str(c.get("text", "")) if isinstance(c, dict)
                           else str(c) for c in content)
    return str(content).strip()


def probe_vision(name, base="", key="", model="", saved=None, protocol=""):
    """测「多模态（识图）API」能不能真的看图。返回给人看的结论。

    比测聊天 API 多一层，而且这层才是关键：
      reachable       : 服务器能不能连上这个地址
      auth_ok         : Key 对不对
      vision_capable  : **它到底能不能看图** ← 用户真正要的答案

    ★ 为什么必须实测、不能靠「模型名看着像」或「/models 里有它」：
      很多 OpenAI 兼容网关会把不识图的模型也列出来，甚至**默默接受**
      带图片的请求、然后完全忽略图片只回文字。用户以为自己配好了识图，
      实际上机器人一直在瞎猜 —— 这正是要防的呆。
      唯一可靠的办法：给一张**随机颜色的纯色图**，问它什么颜色。
      真能看图的必然答对；假装能看的会答错或说看不到。

    protocol：识图 API 的协议。它决定**图片怎么放进请求体** ——
    三种协议的图片表示完全不同，用错就会拿到 400，
    而 400 会被解释成「这个模型不识图」，把用户引去换一个没问题的模型。
    """
    saved = saved or {}
    use_base = (base or "").strip() or saved.get("api_base", "")
    use_key = (key or "").strip() or saved.get("api_key", "")
    use_model = (model or "").strip() or saved.get("api_model", "")
    proto = normalize_protocol(protocol) if protocol else vision_protocol_of(name)

    out = {
        "reachable": False,
        "auth_ok": False,
        "vision_capable": None,   # None = 没能测出来
        "models": [],
        "model_count": 0,
        "message": "",
        "api_base": use_base,
        "protocol": proto,
        "protocol_label": PROTOCOLS[proto]["label"],
        "api_base_effective": normalize_api_base(use_base, proto),
        "tested_color": "",
        "answered": "",
    }

    if not use_base:
        out["message"] = "还没填接口地址。"
        return out
    if not use_key:
        out["message"] = "还没填 API Key。"
        return out
    if not use_model:
        out["message"] = "还没填模型名。识图模型的名字通常带 vision 字样，请照官网文档填。"
        return out

    # ① 模型列表：只用来判断「地址通不通」和给用户挑名字，不当作能力证明。
    try:
        out["models"] = list_api_models(name, use_base, use_key, proto)
        out["reachable"] = True
        out["model_count"] = len(out["models"])
    except ManagerError as e:
        msg = str(e)
        out["reachable"] = ("能连上" in msg)
        if not out["reachable"]:
            out["message"] = msg
            return out
        if "API Key 不对" in msg:
            out["message"] = msg
            return out
    except Exception as e:  # noqa: BLE001
        out["message"] = _api_error_hint(e, use_base)
        return out

    # ② 真正的能力测试：随机挑个颜色，造图，问它。
    rgb, names = secrets.choice(_VISION_COLORS)
    out["tested_color"] = names[0]  # names = (中文名, 同义词元组)
    png_b64 = base64.b64encode(_make_test_png(rgb)).decode("ascii")

    vurl = protocol_endpoint(use_base, proto, "chat", use_model)
    if not vurl:
        out["message"] = ("这个协议（%s）暂时没法自动测试识图。"
                          "请保存后在群里发一张图试试。"
                          % PROTOCOLS[proto]["label"])
        return out
    prompt = "这张图是什么颜色？只回答颜色名称，不要别的字。"
    payload = _vision_payload(proto, use_model, png_b64, prompt)

    try:
        code, body = _api_request(vurl, use_key, payload, protocol=proto)
    except Exception as e:  # noqa: BLE001
        out["message"] = _api_error_hint(e, use_base)
        return out

    if code in (401, 403):
        out["message"] = ("Key 不对，或者「识图协议」选错了（对方返回 %d）。"
                          "先确认协议选的是不是这一家的官方协议，"
                          "再检查 Key 有没有复制全、有没有多余空格。" % code)
        return out
    if code == 404:
        # 404 有两种可能：地址不对，或**模型名不对** —— 看对方怎么说。
        # 不能一律报「地址不完整」：有些网关对不存在的模型回 404，
        # 那样用户会去改一个本来正确的地址，永远改不好。
        # （这个判断在 _chat_probe 里是对的，这里原来漏了。）
        low = body.lower()
        if "model" in low:
            out["auth_ok"] = True
            out["message"] = ("接口地址是对的，但对方不认识「%s」这个模型名。"
                              "点「获取可用模型」从列表里挑一个。" % use_model)
            return out
        out["message"] = ("这个地址没有聊天接口（404）—— 地址多半写得不完整，"
                          "检查结尾是不是少了 /v1；如果地址是对的，"
                          "那就是「识图协议」选错了。")
        return out
    if code == 402:
        out["message"] = "账户余额不足或未开通（402）—— 去官网充值/开通后再试。"
        return out
    if code == 429:
        out["message"] = "被限流了（429）。稍等再试，或检查额度是否用完。"
        return out
    if code >= 500:
        out["message"] = "对方服务器出错（%d），不是你的配置问题，稍后再试。" % code
        return out

    if code != 200:
        low = body.lower()
        # 400 且提到图片 → 这就是「这个模型/接口不接受图片」的铁证。
        # 这是最常见的失败，且信息量最大：用户换一个识图模型就好。
        if code == 400 and ("image" in low or "图片" in body or "content" in low):
            out["auth_ok"] = True
            out["vision_capable"] = False
            out["message"] = ("接口和 Key 都对，但**它不接受图片** —— "
                              "这个模型不是识图模型。请换成带 vision 字样的模型。")
            return out
        if "model" in low:
            out["auth_ok"] = True
            out["message"] = ("接口地址是对的，但对方不认识「%s」这个模型名。"
                              "点「获取可用模型」从列表里挑一个。" % use_model)
            return out
        out["message"] = "对方返回 %d：%s" % (code, body[:200])
        return out

    # 200：解析它到底说了什么颜色。
    #
    # ★ 空回复要**重试**，不能直接下结论。
    #   实测（deepseek-flash + 纯色图）：即使 max_tokens=300，
    #   仍有大约 1/3 的次数返回空 content —— 推理模型偶尔会把预算
    #   全花在思考上。如果不重试，用户点一次「测试」可能被告知
    #   「没法判断」，再点一次又是好的，体验很糟，还会让他误以为
    #   自己的 API 有问题。
    content = ""
    for attempt in range(3):
        content = _vision_text_of(proto, body)
        if content:
            break
        # 空了才重试；最后一次不再试
        if attempt < 2:
            try:
                code2, body2 = _api_request(vurl, use_key, payload, protocol=proto)
                if code2 == 200:
                    body = body2
                    continue
            except Exception:  # noqa: BLE001
                pass
            break

    out["answered"] = content[:80]
    out["auth_ok"] = True

    low = out["answered"].lower()
    if any(w in out["answered"] or w in low for w in _VISION_REFUSAL_WORDS):
        out["vision_capable"] = False
        out["message"] = ("接口和 Key 都对，但这个模型说它看不到图片 —— "
                          "它没有识图能力。请换成带 vision 字样的模型。")
        return out

    # 答对了：只要答案里出现这个颜色的**任一**同义词就算对。
    # 不能只认一个词 —— 模型可能说「金色」而不是「黄」，那也是在看图。
    if any(syn in out["answered"] or syn.lower() in low for syn in names[1]):
        out["vision_capable"] = True
        out["message"] = ("通了 ✓ 而且**它真的能看图**（测试图是%s色，它答对了）。"
                          "这个机器人可以用它识图。" % names[0])
        return out

    # 答了，但答错 —— 这是最阴险的一种：看起来能用，实际在瞎猜。
    if out["answered"]:
        out["vision_capable"] = False
        out["message"] = ("接口和 Key 都对，但它把%s色的图答成了「%s」—— "
                          "说明它并没有真的看图（可能只是忽略图片后瞎猜）。"
                          "请换一个真正支持识图的模型。"
                          % (names[0], out["answered"]))
        return out

    out["vision_capable"] = None
    out["message"] = ("接口和 Key 都对，但它返回了空内容，没法判断能不能识图。"
                      "请再测一次；如果一直这样，换一个模型。")
    return out


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


def _decompress(raw, encoding):
    """按 Content-Encoding 解压响应体。

    为什么必须在这里解、而不是把 Content-Encoding 透传给 App：
    NapCat（Node/express）只要看到 Accept-Encoding: gzip 就压，而**安卓的
    HttpURLConnection 和 OkHttp 默认就发 gzip**。原来的代码把压缩后的字节
    原样回吐、却只转发 Content-Type、丢掉 Content-Encoding —— 结果 WebView
    拿到的是「标着 text/html 的 gzip 二进制」，页面直接白屏。

    实测（2026-09-17，经真实 SSH 隧道）：
      /webui/assets/index-*.js 未压缩 314245 字节，gzip 后 109606 字节；
      经代理返回的头里没有 Content-Encoding，body 头两字节是 1f8b。
    这里统一解压，App 侧就永远只看到明文，不必依赖任何一方「记得协商」。
    """
    enc = (encoding or "").strip().lower()
    if not enc or enc == "identity":
        return raw
    try:
        if enc == "gzip":
            return gzip.decompress(raw)
        if enc == "deflate":
            # 有些实现发的是裸 deflate（没有 zlib 头），两种都试。
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error, EOFError):
        # 解压失败就原样返回：宁可让上游去报错，也别在这里把响应吞掉。
        return raw
    return raw


def proxy_webui(name, method, path, headers, body):
    """把请求转发到该实例的 NapCat WebUI，原样返回响应。"""
    meta = load_meta(name)
    url = "http://127.0.0.1:%d/%s" % (int(meta["webui_port"]), path.lstrip("/"))

    hdrs = {}
    for k, v in headers.items():
        lk = k.lower()
        # 丢掉会干扰转发的头（Host/长度/连接/编码）和我们自己的管理鉴权头。
        # accept-encoding 必须丢：我们**不转发压缩**，一律拿明文再回给 App，
        # 否则就会出现「压缩字节 + 明文声明」的错配（见 _decompress）。
        if lk in ("host", "content-length", "connection", "x-dafeiyu-token",
                  "accept-encoding"):
            continue
        hdrs[k] = v
    hdrs.setdefault("Content-Type", "application/json")
    # 明确只要明文；即便上游不理会，下面 _decompress 也会兜住。
    hdrs["Accept-Encoding"] = "identity"

    req = urllib.request.Request(url, data=body if body else None,
                                 headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT) as resp:
            raw = resp.read()
            return (resp.getcode(),
                    _decompress(raw, resp.headers.get("Content-Encoding")),
                    resp.headers.get("Content-Type", "application/json"))
    except urllib.error.HTTPError as e:
        # NapCat 用 4xx 表达业务错误（未授权等），原样透传，别吞成 500，
        # 否则 App 没法按自己的逻辑（比如自动重登）处理。
        raw = e.read()
        return (e.code, _decompress(raw, e.headers.get("Content-Encoding")),
                e.headers.get("Content-Type", "application/json"))
    except urllib.error.URLError as e:
        raise ManagerError("连不上这个实例的 WebUI（可能容器还没起来）：%s" % e.reason)


def _napcat_api(name, path, payload=None):
    """调 NapCat 自己的 WebUI API（自动完成登录换 Credential）。

    为什么要自动登录：NapCat 的 API 要 `Authorization: Bearer <Credential>`，
    而 Credential 是用 WebUI token 算出来的、1 小时就过期。管理服务本来就
    拿着那个 token（webui_token()），所以每次现换一个最省事，不必缓存。

    返回解析后的 JSON；任何一步失败都抛 ManagerError（调用方决定要不要吞）。
    """
    tok = webui_token(name)
    if not tok:
        raise ManagerError("拿不到这个实例的 WebUI token（容器可能没起来）。")

    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    _, raw, _ = proxy_webui(name, "POST", "/api/auth/login", {},
                            json.dumps({"hash": _napcat_pw_hash(tok)}
                                       ).encode("utf-8"))
    try:
        cred = (json.loads(raw.decode("utf-8")).get("data") or {}).get("Credential") or ""
    except (ValueError, AttributeError):
        cred = ""
    if not cred:
        raise ManagerError("登录 NapCat 失败（换 Credential 没成功）。")

    _, raw, _ = proxy_webui(name, "POST", path,
                            {"Authorization": "Bearer " + cred}, body)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, AttributeError):
        raise ManagerError("NapCat 返回的不是 JSON：%s" % raw[:120])


def _napcat_pw_hash(token):
    """NapCat WebUI 的登录口令 = sha256(token + ".napcat")。

    照抄生产机能登进去的算法（App 侧 NapCatClient.sha256hex 同款）。
    """
    return hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()


def _napcat_hot_reload(name):
    """让 NapCat 重读 OneBot 配置，**不重启容器**。

    为什么不用 docker restart：实测重启会让 QQ **掉线**（phoenix 重启后
    isLogin 从 true 变 false，用户得重新扫码）。而 NapCat 的
    OB11Config/SetConfig 会走内部 reloadNetwork() 重建适配器
    （napcat.mjs:80779），不碰 QQ 进程。

    把当前**文件里**的配置原样喂给它（我们刚写完文件），它会热加载。
    失败一律吞掉：这只是一次加速，配置已经在磁盘上，容器下次自然重启
    也会读到；不该因为热加载失败就让用户的「保存」显示失败。
    """
    try:
        d = napcat_cfg_dir(name)
        src = os.path.join(d, "onebot11.json")
        if not os.path.exists(src):
            return False
        cur = read_json_maybe_bom(src)
        # 接口要的是 JSON5 字符串（实测：直接传对象会回 "config is empty"）
        payload = {"config": json.dumps(cur, ensure_ascii=False)}
        res = _napcat_api(name, "/api/OB11Config/SetConfig", payload)
        return bool(res) and res.get("code") == 0
    except (ManagerError, OSError, ValueError):
        return False


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


def app_update_info():
    """App 的「公告 + 版本」信息（GET /app/update）。

    数据源是 app-update.json（MANAGER_DIR 下，可随时改、改完即生效）。
    文件不存在 = 还没发过版/还没写公告：返回空壳，App 据此判断「无更新」，
    绝不能让 App 因为没有 JSON 而报错。
    """
    empty = {"latest_code": 0, "latest_name": "", "announcement": "",
             "sha256": "", "apk_size": 0}
    try:
        with open(APP_UPDATE_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (IOError, ValueError):
        return empty
    out = {
        "latest_code": int(d.get("latest_code") or 0),
        "latest_name": str(d.get("latest_name") or ""),
        "announcement": str(d.get("announcement") or ""),
        "sha256": str(d.get("sha256") or "").strip().lower(),
        "apk_size": 0,
    }
    # APK 实际存在才填大小 —— 版本号写了但包还没放上去时，
    # App 不该看到一个「能更新但永远下不动」的状态。
    apk = os.path.join(APK_DIR, "dafeiyu-controller-mine.apk")
    if os.path.exists(apk):
        out["apk_size"] = os.path.getsize(apk)
    return out


def app_apk(handler):
    """把内置版 APK 原样吐给 App（GET /app/apk）。

    ★ 为什么由管理服务来发，而不是私有仓库：
       App 本来就经过 SSH 隧道连到本服务（127.0.0.1:6199），
       从私有仓库下载要内置 GitHub token（泄露 == 能进私有仓库），
       而走隧道 0 额外凭据、原样复用双认证。文件就是部署时放进去的那份。
    """
    apk = os.path.join(APK_DIR, "dafeiyu-controller-mine.apk")
    if not os.path.exists(apk):
        json_response(handler, 404, {"error": "还没有可下载的安装包。"})
        return
    size = os.path.getsize(apk)
    handler.send_response(200)
    handler.send_header("Content-Type",
                        "application/vnd.android.package-archive")
    handler.send_header("Content-Length", str(size))
    handler.end_headers()
    with open(apk, "rb") as fh:
        shutil.copyfileobj(fh, handler.wfile, 65536)


def make_server(port, token):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        server_version = "dafeiyu-manager/1"

        def end_headers(self):
            """每个响应都显式声明 Connection: close。

            ── 这是「连不上 WebUI：unexpected end of stream on
            com.android.okhttp.Address@xxxx」的真正病根 ──────────────────
            BaseHTTPRequestHandler 默认 protocol_version 是 HTTP/1.0，
            并且**不发 Connection 头**，发完就关连接。按 RFC，HTTP/1.0 无
            Connection 头确实等于「关闭」，但 OkHttp 2.x（安卓
            HttpURLConnection 底下就是它）只在**看到 Connection: close 这
            个响应头**时才把连接标成不可复用：

                HttpEngine.java:750
                if ("close".equalsIgnoreCase(networkResponse.request()
                        .header("Connection"))
                    || "close".equalsIgnoreCase(networkResponse
                        .header("Connection"))) {
                  streamAllocation.noNewStreams();
                }

            没有这个头 → 连接被放进连接池 → 下次请求从池里捞出一条**服务器
            早已关闭**的连接 → 写请求后读响应读到 EOF → 抛
            `unexpected end of stream on com.squareup.okhttp.Address@…`。

            实测复现（2026-09-17，经真实 SSH 隧道，OkHttp 2.7.5）：
              单线程复用同一个 client：ok=15  fail=15（约一半失败）
              8 线程共享 client：      ok=81  fail=79
              每个请求新建 client：    ok=160 fail=0   ← 不复用就没问题
              共享 client + retry=true：ok=160 fail=0  ← 重试把它盖住了
            也就是说这个 bug **只在高频/复用连接时出现**，单发一条看不出
            来 —— 这正是它难查的原因。WebUI 一开就几十个请求（HTML + JS +
            字体 + 轮询），必踩。

            放在 end_headers 这个唯一出口，是为了把 json_response、_proxy、
            send_error 三条路全覆盖住，避免「修了主路漏了错误路」。
            """
            buf = getattr(self, "_headers_buffer", None) or []
            if not any(b"Connection:" in b for b in buf):
                self.send_header("Connection", "close")
            BaseHTTPRequestHandler.end_headers(self)

        def log_message(self, fmt, *args):
            # 不把请求内容写进日志（可能含 token）。
            #
            # ★ 另外要把查询串里的敏感参数抹掉：BaseHTTPRequestHandler 的
            # log_request 会把**整条请求行**（含 ?password=… / ?key=…）传进来，
            # 直接写就明文留在 journald 里了。这里统一打码，
            # 属于「就算以后有人不小心把密码放进 URL 也不会泄露」的兜底。
            msg = fmt % args
            msg = re.sub(r"(password|key|token|secret)=[^&\s\"]*",
                         r"\1=***", msg, flags=re.IGNORECASE)
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))

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
            elif path == "/app/update":
                self._handle(app_update_info)
            elif path == "/app/apk":
                # 二进制下载，不走 _handle（那是 JSON 专用）。
                app_apk(self)
            elif path == "/instances":
                self._handle(lambda: {"instances": [
                    dict(m, containers=container_state(m["name"]),
                         lock=lock_state(m))
                    for m in all_instances()]})
            elif path == "/quota":
                # 额度与自动清理说明。**GET**：它是纯查询，不删任何东西。
                # 放在 /instance/<名字> 之前不重要（前缀不同），
                # 但必须放在 do_GET 里 —— 一开始误加到了 do_POST，
                # 结果 App 用 GET 拿就是 404「没有这个接口」。
                self._handle(quota_info)
            elif path == "/cleanup/preview":
                # 预览「哪些会被自动清理」，只读。让用户先看一眼再决定。
                self._handle(lambda: cleanup_idle(dry_run=True))
            elif path == "/instance/config":
                # 注意顺序：这条必须排在通用的 /instance/<名字> 之前。
                # 否则 "config" 会被当成实例名，报「没有这个实例：config」——
                # 一个看起来像「实例不存在」、实际是路由写错的坑。
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                          if "?" in self.path else "")
                self._handle(lambda: read_config((q.get("name") or [""])[0]))
            elif path == "/instance/unlock":
                # ★ 这里**故意只支持 POST**（见 do_POST）。
                # 原来这里有个 GET 版本，把密码放在查询串里 ——
                # 那是错的：BaseHTTPRequestHandler 会把整条请求行写进
                # journald，密码就明文留在系统日志里了。删掉，不留后门。
                json_response(self, 405, {"error": "请用 POST（密码不能放在网址里，"
                                                   "会被记进系统日志）。"})
            elif path == "/instance/api/models":
                # 也要排在通用的 /instance/<名字> 之前（同 config 那个坑）。
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                          if "?" in self.path else "")
                self._handle(lambda: {"models": list_api_models(
                    (q.get("name") or [""])[0],
                    (q.get("base") or [""])[0],
                    (q.get("key") or [""])[0],
                    (q.get("protocol") or [""])[0])})
            elif path.startswith("/instance/"):
                name = path[len("/instance/"):].split("/")[0]
                # ★ 密码走请求头，不走查询串。
                # 原因：BaseHTTPRequestHandler 会把**整条请求行**（含 ?password=xxx）
                # 写进 stderr，也就是 journald —— 密码会明文留在系统日志里。
                # 走头就只记录路径，不会留下密码。
                self._handle(lambda: instance_detail(
                    name, self.headers.get("X-Dafeiyu-Unlock") or ""))
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
                    body.get("api_model", ""), body.get("persona", ""),
                    body.get("lock_password", ""),
                    # 识图 API：不填就完全不动多模态配置（老用户升级不受影响）
                    body.get("vision_base", ""), body.get("vision_key", ""),
                    body.get("vision_model", ""),
                    # 接口协议：空 = 沿用已保存的（老用户升级无感）
                    body.get("protocol", ""),
                    # 自定义请求体：空 = 沿用；"{}" = 清空
                    body.get("extra_body", ""),
                    body.get("vision_protocol", ""),
                    body.get("vision_extra_body", "")))
            elif path == "/instance/vision/test":
                # 测识图 API 能不能**真的看图**（不只是「能不能连上」）。
                # 不传 base/key/model 就用实例里已保存的。
                self._handle(lambda: probe_vision(
                    body["name"], body.get("api_base", ""),
                    body.get("api_key", ""), body.get("api_model", ""),
                    protocol=body.get("protocol", "")))
            elif path == "/instance/lock":
                # 设为私密 / 取消私密 / 改密码
                self._handle(lambda: {"lock": lock_state(set_instance_lock(
                    body["name"], bool(body.get("enabled", True)),
                    body.get("password", ""), body.get("old_password", "")))})
            elif path == "/instance/unlock":
                self._handle(lambda: unlock_instance(
                    body["name"], body.get("password", "")))
            elif path == "/instance/api/test":
                # 测主聊天 API：**在服务器上**发请求，因为真正要用它的是
                # 服务器上的 AstrBot（手机通不代表服务器通）。
                # 带上 protocol 和 extra_body：这样「自定义了请求体却测通、
                # 真跑起来 400」这种最难查的情况会在测试这一步就暴露。
                self._handle(lambda: probe_api(
                    body["name"], body.get("api_base", ""),
                    body.get("api_key", ""), body.get("api_model", ""),
                    body.get("protocol", ""),
                    _extra_body_for_probe(body.get("extra_body", ""))))
            elif path == "/instance/repair-channel":
                # 「机器人不回话」一键修复。详见 repair_channel 的说明。
                self._handle(lambda: repair_channel(body["name"]))
            elif path == "/cleanup/run":
                # 真删。供 App 的「立即清理」按钮和定时任务用（POST，
                # 因为它会改状态，不该被浏览器预取/爬虫误触发）。
                self._handle(lambda: cleanup_idle(dry_run=False))
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
