#!/data/data/com.dsharnessmobile.shell/files/usr/bin/bash
# watchdog-super.sh — 看门狗的看门狗（双层保活）。
#
# 为什么需要它：
#   Android 系统会整组清理后台进程。watchdog.sh 能拉起被杀的桥接，但
#   看门狗自己也会被清理（实测 17:46 启动的实例十几分钟后就消失），
#   一旦看门狗死了，桥接被杀后就再没人拉 —— 一停就是几小时。
#   本脚本只盯一件事：watchdog.sh 是否还活着，死了就把它拉起来。
#   桥接仍由 watchdog.sh 负责，这里绝不直接碰桥接，避免双重接管抢端口。
#
# 用法：
#   bash watchdog-super.sh                前台运行（调试用）
#   nohup setsid bash watchdog-super.sh >/dev/null 2>&1 &   后台常驻
#   bash watchdog-super.sh --once         检查一次并在需要时拉起 watchdog，然后退出
#   bash watchdog-super.sh --status       打印一次当前判定
#   bash watchdog-super.sh --stop         停掉 super 自身（不要停看门狗）
#
# 停止：kill 掉本脚本 pid（见 state/watchdog-super.pid）。
set -u

cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"

: "${TMPDIR:=$HOME/tmp}"
export TMPDIR
mkdir -p "$TMPDIR" state

LOG="state/watchdog-super.log"
PIDFILE="state/watchdog-super.pid"
# 检查间隔：比 watchdog 的 20s 略长，避免与它同时抢锁
INTERVAL_SEC="${QQB_WATCHDOG_SUPER_INTERVAL_SEC:-30}"
# 刚拉起 watchdog 后的宽限期：给它时间去接管桥接，避免疯狂重启
GRACE_SEC="${QQB_WATCHDOG_SUPER_GRACE_SEC:-60}"
LOG_MAX_BYTES="${QQB_WATCHDOG_SUPER_LOG_MAX:-131072}"

log() {
  printf '%s [watchdog-super] %s\n' "$(date '+%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

rotate_log() {
  [ -f "$LOG" ] || return 0
  local size
  size=$(wc -c < "$LOG" 2>/dev/null | tr -d ' ')
  [ -n "$size" ] || return 0
  [ "$size" -le "$LOG_MAX_BYTES" ] && return 0
  tail -c $((LOG_MAX_BYTES / 2)) "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
  log "日志已轮转（原 ${size} 字节）"
}

# 找真正的 watchdog.sh 进程（排除本脚本自身、排除 grep）。
# 注意：
#  ① grep 'watchdog\.sh' 不会匹配 watchdog-super.sh（名字是 watchdog-SUPER.sh，
#     正则 watchdog\.sh 要求 watchdog 后紧跟 .）；
#  ② 必须用 'bash .*watchdog\.sh' 显式匹配 bash 启动的看门狗 —— 不能用
#     grep -v 'bash' 排除（看门狗本身就是 bash 进程，会把真看门狗一起滤掉）。
find_watchdog_pids() {
  local pid cwd
  ps -eo pid,args 2>/dev/null \
    | grep 'bash .*watchdog\.sh' \
    | grep -v 'grep' \
    | grep -v 'watchdog-super' \
    | awk '{print $1}' \
    | while read -r pid; do
        [ -n "$pid" ] || continue
        cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null)
        [ "$cwd" = "$ROOT" ] && echo "$pid"
      done
}

start_watchdog() {
  # 注意：绝不碰 bridge.lock！桥接可能正在运行（锁由 bridge.js 自己写，
  # 由 watchdog.sh 的 start_bridge() 在真正重启时清理）。super 只负责
  # 拉起 watchdog.sh，不接管桥接状态，避免双重接管抢端口。
  log "拉起看门狗…"
  nohup setsid bash "$ROOT/watchdog.sh" >/dev/null 2>&1 < /dev/null &
  disown 2>/dev/null || true
}

# 返回：0=健康 2=看门狗不存在（需要拉起）
assess() {
  if [ -z "$(find_watchdog_pids)" ]; then
    return 2
  fi
  return 0
}

print_status() {
  local pids
  pids=$(find_watchdog_pids | tr '\n' ' ')
  if [ -n "$pids" ]; then
    echo "看门狗   : ${pids}（健康）"
  else
    echo "看门狗   : 无 → 需要拉起"
  fi
  if [ -f "$PIDFILE" ]; then
    echo "本脚本 pid: $(cat "$PIDFILE" 2>/dev/null | tr -d '\n')"
  fi
}

check_and_repair() {
  if [ -z "$(find_watchdog_pids)" ]; then
    log "❌ 看门狗进程不存在（很可能被系统后台清理），拉起"
    start_watchdog
    return 1
  fi
  return 0
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
  --stop)
    if [ -f "$PIDFILE" ]; then
      local me
      me=$(cat "$PIDFILE" 2>/dev/null | tr -d '\n')
      [ -n "$me" ] && [ -d "/proc/$me" ] && kill "$me" 2>/dev/null && echo "已停止 super（pid $me），看门狗仍在运行"
    fi
    rm -f "$PIDFILE"
    exit 0
    ;;
esac

# 单实例：已有 super 在跑就退出
if [ -f "$PIDFILE" ]; then
  old=$(cat "$PIDFILE" 2>/dev/null | tr -d '\n')
  if [ -n "$old" ] && [ -d "/proc/$old" ]; then
    echo "watchdog-super already running (pid $old)"
    exit 2
  fi
fi
echo $$ > "$PIDFILE"

trap 'log "super 退出（收到信号）"; rm -f "$PIDFILE"; exit 0' INT TERM

log "super 启动（pid=$$，检查间隔 ${INTERVAL_SEC}s）"

# 启动时先宽限：给可能正在启动的 watchdog 时间
sleep 5

while true; do
  rotate_log
  check_and_repair
  repaired=$?
  if [ "$repaired" = "1" ]; then
    log "已拉起看门狗，等待 ${GRACE_SEC}s 宽限期"
    sleep "$GRACE_SEC"
  else
    sleep "$INTERVAL_SEC"
  fi
done
