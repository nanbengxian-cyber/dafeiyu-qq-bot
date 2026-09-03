# -*- coding: utf-8 -*-
"""控制台后端 —— 挂在 8088 上的 /api/console/*。

为什么挂在 8088 而不是新开端口：**阿里云安全组只放行了 22 / 6099 / 6185 / 8088**
（实测 80/443/8080/8443/9000/3001/6186 全部不通，临时起个 8090 监听从外网也连不上，
本机 ufw inactive、iptables INPUT ACCEPT，所以拦在云厂商那一层，我改不了）。
8088 已经有一套 scrypt 密码 + HMAC 无状态 cookie + IP 指数退避锁定的鉴权，
复用它比再造一套更靠谱，也不用你去控制台加规则。

配置怎么改（关键设计）：**绝不自己写 cmd_config.json**。
全部走 AstrBot 自己的 dashboard API（127.0.0.1:6185）：
  · 配置项  → POST /api/config/astrbot/update  → 它内部 validate_config + reload_pipeline_scheduler
  · 聊天模型 → POST /api/config/provider/update → 它内部 provider_manager.reload（返回「已经实时生效」）
  · 插件开关 → POST /api/plugin/on|off          → turn_on_plugin / turn_off_plugin
这样能白拿三件事：① 格式校验，写坏了它自己 400；② UTF-8 BOM 与字段顺序由框架维护；
③ 热生效，不用重启容器、不掉 QQ 连接。自己写文件这三样全没有，还得重启。

6185 的鉴权用 jwt_secret 现签一个 HS256 token（纯 stdlib，8ms；调 docker exec 用容器里的
pyjwt 要 151ms）。secret 从 cmd_config.json 读，只在进程内存里用，不落盘、不进日志、不回传手机。

写操作的并发保护：GET 配置 → 改 → POST 回去 是读改写，中间要是有人在 WebUI 上也保存了，
后写的会覆盖前一个。所以每次写之前记 cmd_config.json 的 md5，POST 之前再核一次，
不一致直接 409 让你重试 —— 宁可失败也不静默吞掉别人的改动。
"""

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

from console_spec import (
    ACTION_IDS,
    ACTIONS,
    ALLOWED_PATHS,
    KNOBS,
    MODE_IDS,
    MODES,
    PLUGIN_LABELS,
)

CFG_PATH = os.environ.get("CONSOLE_CFG", "/opt/qqbot/astrbot/data/cmd_config.json")
DASH = os.environ.get("CONSOLE_DASH", "http://127.0.0.1:6185")
PUBLIC_DIR = os.environ.get("QRWEB_DIR", "/opt/qqbot/public")
COMPOSE_DIR = os.environ.get("CONSOLE_COMPOSE_DIR", "/opt/qqbot")
CONTAINERS = ("astrbot", "napcat")

# 状态缓存：手机上下拉刷新可能连点，docker inspect ×2 + plugin/get 每次约 200ms，
# 2 秒内复用同一份，连点也不会把服务器 docker 打爆。
STATUS_TTL = float(os.environ.get("CONSOLE_STATUS_TTL", "2"))
_status_cache = {"at": 0.0, "data": None}


class ApiError(Exception):
    """带 HTTP 状态码的业务错误。message 会原样给手机看，所以别塞敏感信息。"""

    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------- 基础读写


def read_cfg():
    """读 cmd_config.json。UTF-8 **BOM**，必须 utf-8-sig，否则 json 解析炸在第一个字符。"""
    raw = open(CFG_PATH, "rb").read()
    return json.loads(raw.decode("utf-8-sig")), hashlib.md5(raw).hexdigest()


def cfg_fingerprint():
    return hashlib.md5(open(CFG_PATH, "rb").read()).hexdigest()


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _mint_jwt(secret, username, ttl=120):
    """给 6185 签一个短命 HS256 token。载荷只有 username + exp，和 dashboard 自己签的一致。"""
    head = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64u(
        json.dumps(
            {"username": username, "exp": int(time.time()) + ttl},
            separators=(",", ":"),
        ).encode()
    )
    sig = _b64u(hmac.new(secret.encode(), head + b"." + body, hashlib.sha256).digest())
    return (head + b"." + body + b"." + sig).decode()


def dash_call(path, body=None, timeout=60):
    """调 AstrBot dashboard API。返回 data 字段；status != ok 抛 ApiError。"""
    cfg, _ = read_cfg()
    db = cfg.get("dashboard") or {}
    secret = db.get("jwt_secret")
    user = db.get("username")
    if not secret or not user:
        raise ApiError("AstrBot 配置里没有 dashboard.jwt_secret，无法调它的接口", 500)
    token = _mint_jwt(secret, user)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        DASH + path,
        data=data,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ApiError("AstrBot 接口 %s 返回 %d" % (path, exc.code), 502) from exc
    except OSError as exc:
        raise ApiError("连不上 AstrBot（%s）：%s" % (DASH, exc), 502) from exc
    if out.get("status") != "ok":
        raise ApiError(out.get("message") or "AstrBot 拒绝了这次修改", 400)
    return out.get("data") or {}


# ---------------------------------------------------------------- 点分路径


def dig(obj, path, default=None):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def plant(obj, path, value):
    """按点分路径写值。中途缺 dict 就报错而不是造一个 —— 路径写错时要炸得明显。"""
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            raise ApiError("配置里没有这条路径：%s" % path, 400)
        cur = cur[part]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        raise ApiError("配置里没有这条路径：%s" % path, 400)
    cur[parts[-1]] = value


KNOB_BY_PATH = {k["path"]: k for k in KNOBS}


def coerce(knob, value):
    """按 KNOBS 的声明校验并转型。越界直接报错，**不静默钳制** ——
    钳制会让手机上显示的值和真实值不一样，比报错难查得多。"""
    kind = knob["type"]
    path = knob["path"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if value in (0, 1, "0", "1", "true", "false", "True", "False"):
            return value in (1, "1", "true", "True")
        raise ApiError("%s 要 true/false，收到 %r" % (path, value), 400)
    if kind in ("int", "float"):
        if isinstance(value, bool):
            raise ApiError("%s 要数字，收到布尔" % path, 400)
        try:
            num = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise ApiError("%s 要数字，收到 %r" % (path, value), 400) from None
        lo, hi = knob.get("min"), knob.get("max")
        if lo is not None and num < lo:
            raise ApiError("%s 不能小于 %s（收到 %s）" % (path, lo, num), 400)
        if hi is not None and num > hi:
            raise ApiError("%s 不能大于 %s（收到 %s）" % (path, hi, num), 400)
        return num
    if kind == "enum":
        opts = [o["value"] for o in knob.get("options") or []]
        if value not in opts:
            raise ApiError("%s 只能是 %s 之一" % (path, opts), 400)
        return value
    raise ApiError("未知的旋钮类型 %s" % kind, 500)


# ---------------------------------------------------------------- 状态采集


def _sh(cmd, timeout=10):
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return out.returncode, (out.stdout or "").strip(), (out.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as exc:
        return 127, "", str(exc)


def _read_json(name):
    try:
        with open(os.path.join(PUBLIC_DIR, name), "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def _container_mem(cid):
    """容器内存直接读 cgroup（4ms）。`docker stats --no-stream` 要 2.1 秒，太贵。"""
    for path in (
        "/sys/fs/cgroup/system.slice/docker-%s.scope/memory.current" % cid,
        "/sys/fs/cgroup/docker/%s/memory.current" % cid,
    ):
        try:
            with open(path) as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            continue
    return None


def _containers():
    out = []
    fmt = "{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}"
    for name in CONTAINERS:
        rc, txt, _ = _sh(["docker", "inspect", "-f", fmt, name], timeout=8)
        item = {"name": name}
        if rc != 0 or "|" not in txt:
            item.update({"state": "missing", "up_seconds": None, "mem_bytes": None})
            out.append(item)
            continue
        cid, state, started, restarts = txt.split("|", 3)
        item["state"] = state
        item["restarts"] = int(restarts) if restarts.isdigit() else None
        item["mem_bytes"] = _container_mem(cid)
        item["up_seconds"] = _uptime_from(started)
        out.append(item)
    return out


def _uptime_from(started):
    """`2026-09-02T14:41:53.216306727Z` → 已运行秒数。

    docker 给的是 **UTC**，纳秒精度。`time.mktime` 会按本地时区(CST)再偏 8 小时，
    算出来能差出 28800 秒，所以必须用 `calendar.timegm` 按 UTC 解。
    """
    import calendar

    try:
        tup = time.strptime(started[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None
    return int(time.time() - calendar.timegm(tup))


def _host():
    info = {}
    try:
        mem = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                mem[key] = int(rest.strip().split()[0]) * 1024
        info["mem_total"] = mem.get("MemTotal")
        info["mem_available"] = mem.get("MemAvailable")
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open("/proc/loadavg") as fh:
            info["load1"] = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        st = os.statvfs("/")
        info["disk_total"] = st.f_blocks * st.f_frsize
        info["disk_free"] = st.f_bavail * st.f_frsize
    except OSError:
        pass
    try:
        with open("/proc/uptime") as fh:
            info["uptime"] = int(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    return info


def _recent_tool_calls(minutes=60):
    """最近多久调过工具 —— 这是 tool_use 修复有没有失效的活体证据，
    比「插件是否启用」有用得多（插件开着但模型不调，等于没有）。"""
    rc, txt, _ = _sh(
        ["docker", "logs", "astrbot", "--since", "%dm" % minutes], timeout=20
    )
    if rc != 0:
        return None
    names = re.findall(r"Agent 使用工具: \[([^\]]*)\]", txt)
    flat = []
    for group in names:
        flat += [n.strip().strip("'\"") for n in group.split(",") if n.strip()]
    return {"window_minutes": minutes, "count": len(flat), "tools": flat[-8:]}


def _plugins():
    try:
        data = dash_call("/api/plugin/get", timeout=20)
    except ApiError:
        return None
    out = []
    for item in data if isinstance(data, list) else []:
        name = item.get("name") or ""
        if not name.startswith("dsh-"):
            continue
        out.append(
            {
                "name": name,
                "label": PLUGIN_LABELS.get(name, name),
                "enabled": bool(item.get("activated")),
                "version": item.get("version"),
            }
        )
    out.sort(key=lambda x: x["name"])
    return out


def current_mode(cfg):
    """反推当前处于哪个模式：模式声明的 knobs 全部命中才算。
    命中不了返回 None（显示「自定义」）—— 不猜、不取最接近的那个。"""
    for mode in MODES:
        knobs = mode.get("knobs") or {}
        if not knobs:
            continue
        if all(dig(cfg, path) == value for path, value in knobs.items()):
            models = mode.get("models") or {}
            if models:
                if not _models_match(cfg, models):
                    continue
            return mode["id"]
    return None


def _models_match(cfg, want):
    if "chat" in want:
        pid = dig(cfg, "provider_settings.default_provider_id")
        cur = next(
            (p.get("model") for p in cfg.get("provider") or [] if p.get("id") == pid),
            None,
        )
        if cur != want["chat"]:
            return False
    if "vision" in want:
        if dig(cfg, "provider_settings.default_image_caption_provider_id") != want["vision"]:
            return False
    return True


def build_status(force=False):
    now = time.time()
    if not force and _status_cache["data"] and now - _status_cache["at"] < STATUS_TTL:
        return _status_cache["data"]

    cfg, _ = read_cfg()
    qq = _read_json("status.json") or {}
    proxy = _read_json("proxy.json") or {}

    chat_id = dig(cfg, "provider_settings.default_provider_id") or ""
    chat_model = next(
        (p.get("model") for p in cfg.get("provider") or [] if p.get("id") == chat_id),
        None,
    )
    vision_id = dig(cfg, "provider_settings.default_image_caption_provider_id") or ""
    vision_model = next(
        (p.get("model") for p in cfg.get("provider") or [] if p.get("id") == vision_id),
        None,
    )

    qq_age = None
    if qq.get("updated_epoch"):
        qq_age = int(now - qq["updated_epoch"])

    data = {
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "qq": {
            "state": qq.get("state"),
            "message": qq.get("message"),
            "account": qq.get("account"),
            "reason": qq.get("detect_reason"),
            "last_seen_seconds": qq.get("last_seen_seconds"),
            "qr_available": qq.get("qr_available"),
            "qr_age_seconds": qq.get("qr_age_seconds"),
            # 看门狗每 10 秒写一次；超过 60 秒没写说明它自己挂了，
            # 这时上面那些字段是旧的，手机上必须标出来别当真。
            "watchdog_age_seconds": qq_age,
            "watchdog_stale": bool(qq_age is not None and qq_age > 60),
        },
        "proxy": {
            "active": proxy.get("proxy_active"),
            "tunnel_healthy": proxy.get("tunnel_healthy"),
            "egress_ip": proxy.get("egress_ip"),
        },
        "containers": _containers(),
        "host": _host(),
        "models": {
            "chat_provider": chat_id,
            "chat_model": chat_model,
            "vision_provider": vision_id,
            "vision_model": vision_model,
            "persona": dig(cfg, "provider_settings.default_personality"),
        },
        "runtime": {
            "active_reply": dig(cfg, "provider_ltm_settings.active_reply.enable"),
            "possibility": dig(
                cfg, "provider_ltm_settings.active_reply.possibility_reply"
            ),
            "rate_limit": "%s 条 / %s 秒"
            % (
                dig(cfg, "platform_settings.rate_limit.count"),
                dig(cfg, "platform_settings.rate_limit.time"),
            ),
            "context_turns": dig(cfg, "provider_settings.max_context_length"),
            "tool_use_ok": "tool_use"
            in (
                next(
                    (
                        p.get("modalities") or []
                        for p in cfg.get("provider") or []
                        if p.get("id") == chat_id
                    ),
                    [],
                )
            ),
        },
        "tools": _recent_tool_calls(),
        "plugins": _plugins(),
        "mode": current_mode(cfg),
    }
    _status_cache["at"] = now
    _status_cache["data"] = data
    return data


def invalidate_status():
    _status_cache["at"] = 0.0
    _status_cache["data"] = None


# ---------------------------------------------------------------- schema


def build_schema():
    cfg, _ = read_cfg()
    chat_id = dig(cfg, "provider_settings.default_provider_id") or ""
    try:
        models = dash_call(
            "/api/config/provider/model_list?provider_id=" + chat_id, timeout=40
        ).get("models") or []
    except ApiError:
        # 渠道方接口抖了不该让整页打不开，回落成「至少有当前这个」。
        models = []
    cur = next(
        (p.get("model") for p in cfg.get("provider") or [] if p.get("id") == chat_id),
        None,
    )
    if cur and cur not in models:
        models.insert(0, cur)
    visions = [
        {
            "id": p.get("id"),
            "model": p.get("model"),
        }
        for p in cfg.get("provider") or []
        if "image" in (p.get("modalities") or [])
    ]
    return {
        "version": 1,
        "knobs": KNOBS,
        "modes": MODES,
        "actions": [a for a in ACTIONS if a.get("enabled", True)],
        "chat_models": models,
        "vision_providers": visions,
        "chat_provider": chat_id,
    }


def build_config():
    cfg, _ = read_cfg()
    return {"values": {k["path"]: dig(cfg, k["path"]) for k in KNOBS}}


# ---------------------------------------------------------------- 写操作


def apply_knobs(values):
    """一次性写一批配置项。空 dict 直接返回，不做无意义的保存。"""
    if not values:
        return []
    unknown = [p for p in values if p not in ALLOWED_PATHS]
    if unknown:
        raise ApiError("这些配置项不允许改：%s" % ", ".join(sorted(unknown)), 403)

    before = cfg_fingerprint()
    payload = dash_call("/api/config/get", timeout=40)
    cfg = payload.get("config")
    if not isinstance(cfg, dict):
        raise ApiError("AstrBot 没给出完整配置，放弃写入", 502)

    changed = []
    for path, raw in values.items():
        knob = KNOB_BY_PATH[path]
        value = coerce(knob, raw)
        old = dig(cfg, path)
        if old == value:
            continue
        plant(cfg, path, value)
        changed.append({"path": path, "from": old, "to": value})
    if not changed:
        return []

    if cfg_fingerprint() != before:
        raise ApiError("配置在这期间被别处改了（WebUI？），本次没写，请刷新重试", 409)
    dash_call("/api/config/astrbot/update", {"conf_id": "default", "config": cfg})
    invalidate_status()
    return changed


def set_chat_model(model):
    """换聊天模型。只动 provider[].model，**绝不碰 modalities** ——
    那里少了 "tool_use" 框架会静默丢掉整个工具集（tool_loop_agent_runner.py:657
    只打 logger.debug，线上 INFO 级根本看不见），空头承诺就是这么来的。"""
    if not isinstance(model, str) or not model.strip():
        raise ApiError("模型名不能为空", 400)
    model = model.strip()
    cfg, _ = read_cfg()
    pid = dig(cfg, "provider_settings.default_provider_id")
    prov = next((p for p in cfg.get("provider") or [] if p.get("id") == pid), None)
    if prov is None:
        raise ApiError("找不到聊天渠道 %s" % pid, 500)
    if prov.get("model") == model:
        return {"changed": False, "model": model}
    new = json.loads(json.dumps(prov))
    new["model"] = model
    dash_call("/api/config/provider/update", {"id": pid, "config": new})

    after, _ = read_cfg()
    got = next((p for p in after.get("provider") or [] if p.get("id") == pid), {})
    if got.get("model") != model:
        raise ApiError("模型没写进去（现在还是 %s）" % got.get("model"), 500)
    if "tool_use" not in (got.get("modalities") or []):
        # 真发生了就必须喊出来：这是「机器人只会嘴上答应」的唯一根因。
        raise ApiError(
            "模型换成了 %s，但 modalities 丢了 tool_use，工具会全部失效，请立刻检查" % model,
            500,
        )
    invalidate_status()
    return {"changed": True, "model": model}


def set_vision_provider(pid):
    """换识图渠道。走 default_image_caption_provider_id，不动 dsh-vischain 的 env ——
    改 env 得 `docker compose up -d astrbot` 重建容器（哑 15~25 秒），换这个指针是热的。
    指到 vision-opus5 就是走三档故障转移链（约 25% 概率被 Cloudflare 403、链条兜住），
    指到 zhipu-vision 就是直连 glm flash（1.9 秒、便宜）。"""
    cfg, _ = read_cfg()
    ids = [p.get("id") for p in cfg.get("provider") or [] if "image" in (p.get("modalities") or [])]
    if pid not in ids:
        raise ApiError("识图渠道只能是 %s 之一" % ids, 400)
    if dig(cfg, "provider_settings.default_image_caption_provider_id") == pid:
        return {"changed": False, "provider": pid}
    before = cfg_fingerprint()
    payload = dash_call("/api/config/get", timeout=40)
    full = payload.get("config")
    if not isinstance(full, dict):
        raise ApiError("AstrBot 没给出完整配置，放弃写入", 502)
    plant(full, "provider_settings.default_image_caption_provider_id", pid)
    if cfg_fingerprint() != before:
        raise ApiError("配置在这期间被别处改了，本次没写，请刷新重试", 409)
    dash_call("/api/config/astrbot/update", {"conf_id": "default", "config": full})
    invalidate_status()
    return {"changed": True, "provider": pid}


def set_plugin(name, enabled):
    if not isinstance(name, str) or not name.startswith("dsh-"):
        raise ApiError("只允许开关 dsh-* 插件", 403)
    known = {p["name"] for p in (_plugins() or [])}
    if name not in known:
        raise ApiError("没有这个插件：%s" % name, 404)
    dash_call("/api/plugin/on" if enabled else "/api/plugin/off", {"name": name})
    invalidate_status()
    return {"name": name, "enabled": bool(enabled)}


def apply_mode(mode_id):
    """套用一键模式。分三段做，每段的结果都单独回报 ——
    某一段失败不会让手机以为整个模式都没生效（那会让人重复点，越点越乱）。"""
    if mode_id not in MODE_IDS:
        raise ApiError("没有这个模式：%s" % mode_id, 404)
    mode = next(m for m in MODES if m["id"] == mode_id)
    steps = []

    try:
        changed = apply_knobs(mode.get("knobs") or {})
        steps.append({"step": "配置", "ok": True, "changed": len(changed), "detail": changed})
    except ApiError as exc:
        steps.append({"step": "配置", "ok": False, "error": exc.message})

    models = mode.get("models") or {}
    if "chat" in models:
        try:
            res = set_chat_model(models["chat"])
            steps.append({"step": "聊天模型", "ok": True, "detail": res})
        except ApiError as exc:
            steps.append({"step": "聊天模型", "ok": False, "error": exc.message})
    if "vision" in models:
        try:
            res = set_vision_provider(models["vision"])
            steps.append({"step": "识图渠道", "ok": True, "detail": res})
        except ApiError as exc:
            steps.append({"step": "识图渠道", "ok": False, "error": exc.message})

    for name, want in (mode.get("plugins") or {}).items():
        try:
            set_plugin(name, want)
            steps.append(
                {"step": "插件 " + PLUGIN_LABELS.get(name, name), "ok": True}
            )
        except ApiError as exc:
            steps.append(
                {"step": "插件 " + PLUGIN_LABELS.get(name, name), "ok": False, "error": exc.message}
            )

    invalidate_status()
    ok = all(s.get("ok") for s in steps)
    return {"mode": mode_id, "name": mode["name"], "ok": ok, "steps": steps}


ACTION_CMDS = {
    "restart_astrbot": ["docker", "restart", "astrbot"],
    "recompose_astrbot": ["docker", "compose", "up", "-d", "astrbot"],
    "restart_napcat": ["docker", "restart", "napcat"],
}


def run_action(action_id):
    if action_id not in ACTION_IDS or action_id not in ACTION_CMDS:
        raise ApiError("没有这个动作：%s" % action_id, 404)
    cmd = ACTION_CMDS[action_id]
    # `docker compose` 必须在 compose 文件所在目录跑，否则找不到 service。
    cwd = COMPOSE_DIR if "compose" in cmd else None
    try:
        done = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=180, check=False
        )
        rc, out, err = done.returncode, (done.stdout or "").strip(), (done.stderr or "").strip()
    except subprocess.TimeoutExpired:
        raise ApiError("执行超时（180 秒），请登服务器手工确认", 504) from None
    except OSError as exc:
        raise ApiError("起不了 docker：%s" % exc, 500) from exc
    invalidate_status()
    if rc != 0:
        raise ApiError("执行失败（rc=%d）：%s" % (rc, (err or out)[:200]), 500)
    return {"action": action_id, "output": (out or err)[-400:]}


# ---------------------------------------------------------------- 路由


def handle(method, path, query, body):
    """qrweb 的 Handler 把已鉴权的请求转进来。返回 (状态码, dict)。

    这里只认完全匹配的路径。任何未知路径回 404，不做前缀匹配 ——
    前缀匹配将来加端点时容易把 /api/console/mode 和 /api/console/modes 搞混。
    """
    if method == "GET":
        if path == "/api/console/status":
            return 200, build_status(force=query.get("force") == "1")
        if path == "/api/console/schema":
            return 200, build_schema()
        if path == "/api/console/config":
            return 200, build_config()
        return 404, {"error": "no such endpoint"}

    if method != "POST":
        return 405, {"error": "method not allowed"}

    body = body if isinstance(body, dict) else {}
    if path == "/api/console/config":
        values = body.get("values")
        if not isinstance(values, dict):
            raise ApiError("要 {\"values\": {路径: 值}}", 400)
        return 200, {"changed": apply_knobs(values)}
    if path == "/api/console/model":
        out = {}
        if "chat" in body:
            out["chat"] = set_chat_model(body["chat"])
        if "vision" in body:
            out["vision"] = set_vision_provider(body["vision"])
        if not out:
            raise ApiError("要 chat 或 vision", 400)
        return 200, out
    if path == "/api/console/mode":
        return 200, apply_mode(body.get("id"))
    if path == "/api/console/plugin":
        return 200, set_plugin(body.get("name"), bool(body.get("enabled")))
    if path == "/api/console/action":
        return 200, run_action(body.get("id"))
    return 404, {"error": "no such endpoint"}


CONSOLE_PREFIX = "/api/console/"
