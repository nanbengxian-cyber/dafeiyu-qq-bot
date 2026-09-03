# 服务端脚本 / Server-side Scripts

## `qq_watchdog.sh` —— QQ 掉线看守 / Login Watchdog

QQ 小号会被风控，20~60 分钟掉线一次。掉线后「快速登录」报身份失效，扫码是唯一恢复路径 —— 而 NapCat **只在进程启动时出码，且不会自动续期**（实测 mtime 冻结在生成那一刻），QQ 码只活约 2 分钟。这个看守做三件事：判定在线/离线、离线期间保证网页上永远挂着一张新鲜的码、把状态写成 `status.json` 供网页显示。

The QQ account gets rate-limited and drops every 20–60 minutes. After a drop, "quick login" reports an invalid identity, so QR scanning is the only recovery path — and NapCat **only emits a QR at process start and never refreshes it** (measured: mtime frozen at generation), while a QQ QR lives about 2 minutes. This watchdog does three things: decide online/offline, keep a fresh QR served while offline, and write `status.json` for the web page.

### 为什么 v1 是错的 / Why v1 was wrong

v1 纯靠日志判定。问题是 **QQ 静默掉线时 NapCat 什么日志都不打** —— 没有 `KickedOffLine`，没有「账号状态变更为离线」，socket 还挂在 `ESTAB` 上。于是最后一行日志永远停在掉线前那条「接收 <-」，看守一直认为在线。真实后果：21:11 掉线，到 22:09 网页都还是绿的，人以为没事。

v1 judged purely from logs. The problem: **when QQ drops the session silently, NapCat logs nothing** — no `KickedOffLine`, no state-change line, and the socket stays `ESTAB`. The last log line stays frozen at the pre-drop "received <-", so the watchdog believed it was online. Real consequence: dropped at 21:11, the page was still green at 22:09.

### v2 的判定顺序 / v2 decision order

主动探针优先，日志只作兜底：

Active probe first, logs as fallback:

1. **最近 `ACTIVITY_WINDOW` 秒内有收发消息** ⇒ 铁证在线，直接压过探针结果。
2. **探针**：`docker exec napcat curl http://127.0.0.1:3000/get_status`，读 `data.online`。该 HTTP 适配器只在登录成功后启动，所以「连不上」本身也是掉线证据。
3. **但**「连不上」也可能是配置被覆盖、端口冲突等自身故障。误判成掉线会重启 napcat、打断一个活着的会话 —— 这比误报在线更糟。所以只有当探针在**本次 napcat 生命周期内成功过至少一次**时，才认可「失联 = 掉线」；否则退回日志判定，绝不贸然重启。
4. 启动后有 `STARTUP_GRACE` 秒宽限。

探针端口 `127.0.0.1:3000` 只在容器内监听、compose 未映射，宿主/其他容器/公网都访问不到（四向验证过）。

The probe port `127.0.0.1:3000` listens inside the container only and isn't mapped by compose — unreachable from the host, other containers, or the internet (verified from all four directions).

### 部署 / Deploy

```bash
cp qq_watchdog.sh /opt/qqbot/
chmod +x /opt/qqbot/qq_watchdog.sh

# 需要先给 NapCat 的 onebot11 配置加一个仅本机的 HTTP 适配器：
#   network.httpServers += {name:"watchdog-probe", enable:true, port:3000,
#     host:"127.0.0.1", enableCors:false, enableWebsocket:false,
#     messagePostFormat:"array", token:"", debug:false}
# 改完必须 docker restart napcat（onebot11 配置没有热重载）
```

配 systemd 常驻：

```ini
[Unit]
Description=QQ bot login watchdog
After=docker.service

[Service]
ExecStart=/opt/qqbot/qq_watchdog.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 旋钮 / Knobs

脚本顶部可调：`QR_TTL`（码存活秒数，超了就重启换码）、`RESTART_COOLDOWN`（重启冷却，防抖）、`INTERVAL`（检查间隔）、`ACTIVITY_WINDOW`（活跃窗口）、`STARTUP_GRACE`（启动宽限）。

### 一个 shell 坑 / A shell pitfall

`detect_state` 用 `$(...)` 调用时跑在**子 shell** 里，全局变量传不回来。判定依据必须随 stdout 以 `"state|reason"` 的形式返回，否则网页永远显示不出「凭什么判成掉线」。

Calling `detect_state` via `$(...)` runs it in a **subshell**, so globals don't propagate. The reason has to ride stdout as `"state|reason"`, or the web page can never show *why* it decided the bot was offline.
