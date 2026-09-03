#!/data/data/com.dsharnessmobile.shell/files/usr/bin/bash
# watchdog.sh — qq-bridge 外部看门狗（心跳驱动）。
#
# 为什么需要它：
#   start.sh 自带 5 秒重启循环，但它只能救「node 进程自己退出」。
#   实测在 Android（vivo/OriginOS）上，桥接是被系统整组 SIGKILL 掉的 ——
#   start.sh 守护和 node 一起消失，没有任何进程留下来重启，于是一停就是几小时。
#   本脚本是独立进程，只做一件事：盯着心跳文件，停了就把桥接拉起来。
#
# 判据用心跳文件而不是 pid：
#   进程被 SIGKILL 时不写日志、不留退出码，心跳停止更新是唯一可靠信号；
#   而且它还能覆盖「进程活着但卡死」这种 pid 检查发现不了的情况。
#
# 用法：
#   bash watchdog.sh                 前台运行（调试用，Ctrl+C 停止）
#   nohup setsid bash watchdog.sh >/dev/null 2>&1 &   后台常驻
#   bash watchdog.sh --status        只打印一次当前判定，不做任何动作
#   bash watchdog.sh --once          检查一次并在需要时重启，然后退出
#
# 停止：kill 掉本脚本的 pid（见 state/watchdog.pid）。
set -u

cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"

: "${TMPDIR:=$HOME/tmp}"
export TMPDIR
mkdir -p "$TMPDIR" state

STATUS_FILE="state/bridge-status.json"
LOG="state/watchdog.log"
PIDFILE="state/watchdog.pid"
LOCK="state/bridge.lock"

# 心跳周期是 5 秒；45 秒无更新才判定为死，避免误杀正在做长回合的桥接。
STALE_SEC="${QQB_WATCHDOG_STALE_SEC:-45}"
# 检查间隔
INTERVAL_SEC="${QQB_WATCHDOG_INTERVAL_SEC:-20}"
# 刚重启后的宽限期：给桥接足够时间连上 DSH 并写出第一次心跳
GRACE_SEC="${QQB_WATCHDOG_GRACE_SEC:-60}"
# 日志上限（字节），超过就截断保留后半
LOG_MAX_BYTES="${QQB_WATCHDOG_LOG_MAX:-262144}"
# 「进程在但读不到心跳」连续多少轮后强制重启（防止旧版本/卡在启动阶段永久失修）
NO_HEARTBEAT_MAX_STREAK="${QQB_WATCHDOG_NO_HB_STREAK:-3}"

# 连续无心跳轮次计数（check_and_repair 内维护）
no_heartbeat_streak=0

log() {
  printf '%s [watchdog] %s\n' "$(date '+%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

# 日志轮转：只保留后半，避免长期运行把存储写满。
rotate_log() {
  [ -f "$LOG" ] || return 0
  local size
  size=$(wc -c < "$LOG" 2>/dev/null | tr -d ' ')
  [ -n "$size" ] || return 0
  [ "$size" -le "$LOG_MAX_BYTES" ] && return 0
  tail -c $((LOG_MAX_BYTES / 2)) "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
  log "日志已轮转（原 ${size} 字节）"
}

# 读心跳时间戳（毫秒）。用 node 解析而不是 grep：字段顺序变化时不会读错。
read_heartbeat_ms() {
  node -e '
    const fs = require("fs");
    try {
      const s = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
      const v = Number(s.heartbeatAt);
      process.stdout.write(Number.isFinite(v) && v > 0 ? String(v) : "0");
    } catch { process.stdout.write("0"); }
  ' "$STATUS_FILE" 2>/dev/null || echo 0
}

read_status_pid() {
  node -e '
    const fs = require("fs");
    try {
      const s = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
      const v = Number(s.pid);
      process.stdout.write(Number.isFinite(v) && v > 0 ? String(v) : "0");
    } catch { process.stdout.write("0"); }
  ' "$STATUS_FILE" 2>/dev/null || echo 0
}

# 找出真正的桥接进程 pid。
#
# 两道过滤，缺一不可：
#   ① 排除 grep/bash 自身 —— 否则诊断命令的 argv 会把自己匹配进来（实测踩过）；
#   ② 按 /proc/<pid>/cwd 限定在本仓库 —— ps 里只有相对路径 `node src/bridge.js`，
#      无法区分是哪份仓库的桥接。不限定的话，测试用的看门狗会去杀真实桥接，
#      同机跑两份仓库时也会互相误杀。
find_bridge_pids() {
  local pid cwd
  ps -eo pid,args 2>/dev/null \
    | grep 'src/bridge\.js' \
    | grep -v 'grep' \
    | grep -v 'bash' \
    | awk '{print $1}' \
    | while read -r pid; do
        [ -n "$pid" ] || continue
        cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null)
        [ "$cwd" = "$ROOT" ] && echo "$pid"
      done
}

find_daemon_pids() {
  local pid cwd
  ps -eo pid,args 2>/dev/null \
    | grep 'start\.sh' \
    | grep -v 'grep' \
    | grep -v 'watchdog' \
    | awk '{print $1}' \
    | while read -r pid; do
        [ -n "$pid" ] || continue
        cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null)
        [ "$cwd" = "$ROOT" ] && echo "$pid"
      done
}

# 彻底停掉旧实例。
# 顺序很关键：必须先杀 start.sh 守护，否则它会在 5 秒内把 node 重新拉起来，
# 结果新旧两个实例抢 3100 端口，报 EADDRINUSE（实测踩过）。
stop_bridge() {
  local d n
  d=$(find_daemon_pids)
  n=$(find_bridge_pids)
  if [ -n "$d" ]; then
    log "停止守护进程：$(echo "$d" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill -9 $d 2>/dev/null
  fi
  if [ -n "$n" ]; then
    log "停止桥接进程：$(echo "$n" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill -9 $n 2>/dev/null
  fi
  # 等端口真正释放，最多 10 秒
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    [ -z "$(find_bridge_pids)" ] && [ -z "$(find_daemon_pids)" ] && return 0
  done
  log "⚠️ 旧进程在 10 秒内未完全退出，仍继续启动（可能出现端口冲突）"
  return 1
}

start_bridge() {
  # 单实例锁在进程被 SIGKILL 后不会被清理，残留锁会让新实例 exit 2，
  # 而 exit 2 又会让 start.sh 直接退出 —— 必须先清掉。
  if [ -f "$LOCK" ]; then
    log "清理残留锁文件（pid=$(cat "$LOCK" 2>/dev/null | tr -d '\n'))"
    rm -f "$LOCK"
  fi
  log "启动桥接…"
  nohup setsid bash start.sh > state/start-sh.log 2>&1 < /dev/null &
  disown 2>/dev/null || true
}

# 返回：0=健康 1=心跳停滞 2=完全没进程 3=从未写过心跳
assess() {
  local pids hb now age
  pids=$(find_bridge_pids)
  hb=$(read_heartbeat_ms)
  now=$(( $(date +%s) * 1000 ))

  if [ -z "$pids" ]; then
    return 2
  fi
  if [ "$hb" = "0" ]; then
    return 3
  fi
  age=$(( (now - hb) / 1000 ))
  if [ "$age" -gt "$STALE_SEC" ]; then
    echo "$age"
    return 1
  fi
  echo "$age"
  return 0
}

print_status() {
  local pids hb age rc
  pids=$(find_bridge_pids | tr '\n' ' ')
  hb=$(read_heartbeat_ms)
  if [ "$hb" = "0" ]; then
    age="从未"
  else
    age="$(( ( $(date +%s) * 1000 - hb ) / 1000 )) 秒前"
  fi
  echo "桥接进程 : ${pids:-无}"
  echo "状态文件 : $STATUS_FILE"
  echo "心跳     : $age（阈值 ${STALE_SEC}s）"
  set +e
  assess > /dev/null 2>&1
  rc=$?
  # 注意：不要在此处 set -e —— 本脚本只用 set -u。误开 errexit 会让
  # find_*_pids 里 while 循环最后一次判断的非零退出码直接中止整个脚本，
  # 表现为「日志写了要重启，但 start.sh 从没被调用」（已实测踩坑）。
  case "$rc" in
    0) echo "判定     : ✅ 健康" ;;
    1) echo "判定     : ❌ 心跳停滞（进程在但卡死）→ 需要重启" ;;
    2) echo "判定     : ❌ 桥接进程不存在 → 需要重启" ;;
    3) echo "判定     : ⚠️ 未写过心跳（刚启动？或版本过旧）" ;;
  esac
}

check_and_repair() {
  local rc age
  set +e
  age=$(assess)
  rc=$?
  # 注意：不要在此处 set -e —— 本脚本只用 set -u。误开 errexit 会让
  # find_*_pids 里 while 循环最后一次判断的非零退出码直接中止整个脚本，
  # 表现为「日志写了要重启，但 start.sh 从没被调用」（已实测踩坑）。

  case "$rc" in
    0)
      no_heartbeat_streak=0
      return 0
      ;;
    1)
      no_heartbeat_streak=0
      log "❌ 心跳停滞 ${age}s（阈值 ${STALE_SEC}s）：进程存在但已卡死，强制重启"
      stop_bridge
      start_bridge
      return 1
      ;;
    2)
      no_heartbeat_streak=0
      log "❌ 桥接进程不存在（很可能被系统后台清理），重启"
      stop_bridge
      start_bridge
      return 1
      ;;
    3)
      # 进程在、但状态文件里没有心跳字段。两种可能：
      #   ① 刚启动，还没写出第一次心跳（正常，等一轮就好）；
      #   ② 跑的是不带心跳功能的旧版本，或卡在启动阶段（永远等不到）。
      # 只「等」会让 ② 永久失修，所以连续若干轮仍无心跳就重启一次。
      no_heartbeat_streak=$((no_heartbeat_streak + 1))
      if [ "$no_heartbeat_streak" -ge "$NO_HEARTBEAT_MAX_STREAK" ];then
        log "❌ 连续 ${no_heartbeat_streak} 轮无心跳（进程在但可能卡死/旧版本），重启"
        no_heartbeat_streak=0
        stop_bridge
        start_bridge
        return 1
      fi
      log "⚠️ 状态文件无心跳（第 ${no_heartbeat_streak}/${NO_HEARTBEAT_MAX_STREAK} 轮），先等待"
      return 0
      ;;
  esac
}

case "${1:-}" in
  --status)
    print_status
    exit 0
    ;;
  --once)
    rotate_log
    check_and_repair
    exit 0
    ;;
esac

# 单实例：已有看门狗在跑就退出，避免两个看门狗互相重启桥接。
if [ -f "$PIDFILE" ]; then
  old=$(cat "$PIDFILE" 2>/dev/null | tr -d '\n')
  if [ -n "$old" ] && [ -d "/proc/$old" ]; then
    echo "watchdog already running (pid $old)"
    exit 2
  fi
fi
echo $$ > "$PIDFILE"

trap 'log "看门狗退出（收到信号）"; rm -f "$PIDFILE"; exit 0' INT TERM

log "看门狗启动（pid=$$，检查间隔 ${INTERVAL_SEC}s，心跳阈值 ${STALE_SEC}s）"

# 启动时先给一个宽限期：如果桥接正在启动，别急着判它死。
sleep 5

while true; do
  rotate_log
  set +e
  check_and_repair
  repaired=$?
  # 注意：不要在此处 set -e —— 本脚本只用 set -u。误开 errexit 会让
  # find_*_pids 里 while 循环最后一次判断的非零退出码直接中止整个脚本，
  # 表现为「日志写了要重启，但 start.sh 从没被调用」（已实测踩坑）。
  if [ "$repaired" = "1" ]; then
    # 刚重启过，等宽限期，让桥接连上 DSH 并写出第一次心跳
    log "已重启，等待 ${GRACE_SEC}s 宽限期"
    sleep "$GRACE_SEC"
  else
    sleep "$INTERVAL_SEC"
  fi
done
