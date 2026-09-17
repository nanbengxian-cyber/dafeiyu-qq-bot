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
import gzip
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
import zlib

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
                "webui_token": ""}
    return {"meta": meta,
            "lock": ls,
            "locked": False,
            "containers": container_state(name),
            "config": read_config(name),
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


def astrbot_started_marker(name):
    """看这个实例的 AstrBot 日志里有没有「启动完成」的标记。

    返回 True / False；**拿不到日志时返回 None**（不代表没启动）。
    区分 None 很重要：测试环境和容器名不同时 docker logs 必然失败，
    那时不能当成「没启动」去反复等 —— 会把测试拖死。
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
    return "AstrBot started" in out.stdout.decode("utf-8", "replace")


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
      * need_db=True  → data_v4.db 存在（要等 ORM 初始化完才落盘）

    need_db 为什么不能省：cmd_config.json 生成得很早（所以界面会显示「运行中」），
    但 data_v4.db 要晚得多。只等日志里的 "AstrBot started" 就去写人格，会撞上
    「文件还不存在」—— 用户看到的是「请先启动一次」，而他明明刚启动过。
    这正是实测踩到的坑。

    把 cfg 也并进同一个判据里（而不是在外面再补一次等待），是为了守住
    App 的读超时预算：apply_config 里只等两次，每次上限 READY_TIMEOUT。

    返回 True（已就绪）或 False（等超时）。
    """
    import time as _t

    def files_ready():
        if need_cfg and not os.path.exists(astrbot_cfg_path(name)):
            return False
        if need_db and not os.path.exists(persona_db_path(name)):
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
                 lock_password=""):
    """把三配置写进实例的 AstrBot。

    写之前先备份原文件；写之后**回读校验**（生产机的教训：写完不读回，
    写坏了也不知道）。

    lock_password：私密实例要改配置必须先给密码。**没有它就能改**的话，
    锁只挡住了「看」却没挡住「改」—— 别人可以直接把配置覆盖掉，
    等于没锁。
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
        if has_persona and not os.path.exists(persona_db_path(name)):
            raise ManagerError("这个机器人刚启动，内部数据库还在初始化，"
                               "请等半分钟再点「保存」。")
        raise ManagerError("这个机器人的聊天服务还没启动完，稍等半分钟再试。")

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
        src = {
            "id": src_id,
            "provider": "openai",
            "type": "openai_chat_completion",
            "provider_type": "chat_completion",
            "key": [api_key],
            "api_base": api_base,
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
            "modalities": ["text", "tool_use"],
            "custom_extra_body": {},
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
    if has_persona:
        got_pid = (((back2.get("agent_runner") or {}).get("config") or {})
                   .get("persona") or {}).get("persona_id") or ""
        if got_pid != "dafeiyu-mine":
            raise ManagerError("配置没保住：重启后人格又变回 %r 了。"
                               "请等实例完全起来再改。" % (got_pid,))

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
    for p in (cfg.get("provider") or []):
        if p.get("id") == "dafeiyu-main":
            model = p.get("model") or ""
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


def _main_api_of(name):
    """从实例配置里读回主 API 的三要素：接口地址 / Key / 模型名。"""
    path = astrbot_cfg_path(name)
    if not os.path.exists(path):
        raise ManagerError("这个机器人还没启动过，先点「启动」再测接口。")
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


def _api_request(url, key, payload=None):
    """向 API 发一次请求。返回 (状态码, 响应体文本)。异常原样抛出给调用方翻译。"""
    data = None
    headers = {"Authorization": "Bearer %s" % key, "Accept": "application/json"}
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


def list_api_models(name, base="", key=""):
    """拉取这个 API 支持的模型列表。

    base/key 传空则用实例里已保存的 —— 这样「还没保存就想先看看有哪些模型」
    也能用（先测通再保存，比「保存了才发现模型名错」友好得多）。

    ★ 注意：**不能拿这个接口的成败来判断 Key 对不对**。
    实测 OpenRouter 的 /models 用无效 Key 也返回 200（它不鉴权），
    但 /chat/completions 用同样的无效 Key 返回 401。
    所以「拉得到模型列表」只证明**地址通**，不证明 Key 可用 ——
    真正验 Key 要用 probe_api 里那次真实聊天调用。
    """
    saved_base, saved_key, _ = _main_api_of(name)
    use_base = (base or "").strip() or saved_base
    use_key = (key or "").strip() or saved_key
    if not use_base:
        raise ManagerError("还没填接口地址。")
    if not use_key:
        raise ManagerError("还没填 API Key。")

    url = _join_api(use_base, "/models")
    try:
        code, body = _api_request(url, use_key)
    except ManagerError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ManagerError(_api_error_hint(e, use_base))

    if code in (401, 403):
        raise ManagerError("能连上这个地址，但 API Key 不对（对方返回 %d）。"
                           "检查 Key 有没有复制全、有没有多余空格。" % code)
    if code == 404:
        raise ManagerError("能连上，但这个地址没有 /models 接口（404）。"
                           "多半是地址写得不完整 —— 检查结尾是不是少了 /v1。")
    if code == 429:
        raise ManagerError("能连上，但被限流了（429）。等一会儿再试，或检查额度。")
    if code >= 500:
        raise ManagerError("对方服务器出错（%d），不是你的配置问题，稍后再试。" % code)
    if code != 200:
        raise ManagerError("对方返回了 %d，没能取到模型列表。" % code)

    try:
        obj = json.loads(body)
    except ValueError:
        raise ManagerError("能连上，但对方返回的不是 JSON —— 这个地址可能不是 "
                           "OpenAI 兼容接口。")

    ids = []
    for m in (obj.get("data") or []):
        if isinstance(m, dict) and m.get("id"):
            ids.append(str(m["id"]))
        elif isinstance(m, str):
            ids.append(m)
    if not ids:
        # 有些网关 /models 返回空列表但实际可用 —— 不当失败，只是没得选。
        return []
    return sorted(set(ids))


def _chat_probe(base, key, model):
    """发一次**最小**的真实聊天请求 —— 这是唯一能证明「配置可用」的测试。

    为什么非要发聊天请求、不能只看 /models：
      ① 实测 OpenRouter 的 /models 对**任何** Key 都返回 200（不鉴权），
         只看它就会把坏 Key 判成好的 —— 用户看到「通了 ✓」，
         然后发现机器人根本不回话，比不做检测还糟；
      ② 地址通、Key 对，也可能因为**模型名不存在**而失败，
         这正是新手最常犯的错；
      ③ 用 max_tokens=1 让成本可以忽略。

    返回 (ok, 说明文字)。ok=False 时说明文字已经是给人看的话。
    """
    url = _join_api(base, "/chat/completions")
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}],
               "max_tokens": 1, "stream": False}
    try:
        code, body = _api_request(url, key, payload)
    except ManagerError:
        raise
    except Exception as e:  # noqa: BLE001
        return False, _api_error_hint(e, base)

    if code == 200:
        return True, ""
    if code in (401, 403):
        return False, ("Key 不对（对方返回 %d）。检查有没有复制全、"
                       "有没有多余空格。" % code)
    if code == 404:
        # 404 有两种可能：地址不对，或模型名不对 —— 看对方怎么说。
        low = body.lower()
        if "model" in low:
            return False, ("接口地址是对的，但对方不认识「%s」这个模型名。"
                           "点「获取可用模型」从列表里挑一个。" % model)
        return False, ("这个地址没有聊天接口（404）—— 地址多半写得不完整，"
                       "检查结尾是不是少了 /v1。")
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


def probe_api(name, base="", key="", model=""):
    """测主聊天 API 到底通不通。返回一份给人看的结论。

    结论分三层，分开说 —— 混成一句「失败」用户就不知道下一步做什么：
      reachable : 服务器能不能连上这个地址（网络层）
      auth_ok   : Key 对不对（认证层）
      model_ok  : 模型名在不在对方的列表里（配置层）
    """
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
        models = list_api_models(name, use_base, use_key)
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
        ok, why = _chat_probe(use_base, use_key, use_model)
    except ManagerError as e:
        out["message"] = str(e)
        return out

    if ok:
        out["auth_ok"] = True
        out["model_ok"] = True
        out["message"] = "通了 ✓ 接口、Key、模型名都对，机器人可以用了。"
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
            elif path == "/instances":
                self._handle(lambda: {"instances": [
                    dict(m, containers=container_state(m["name"]),
                         lock=lock_state(m))
                    for m in all_instances()]})
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
                    (q.get("key") or [""])[0])})
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
                    body.get("lock_password", "")))
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
                self._handle(lambda: probe_api(
                    body["name"], body.get("api_base", ""),
                    body.get("api_key", ""), body.get("api_model", "")))
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
