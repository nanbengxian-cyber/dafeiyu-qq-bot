#!/data/data/com.dsharnessmobile.shell/files/usr/bin/bash
# restart.sh — restart.bat 的 Android/Linux 等价物：杀旧守护与旧桥接 → 清理过期锁 → 后台重启守护。
#
# 用法：bash restart.sh
set -u
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"

echo "停止旧守护进程（start.sh）…"
pkill -f "bash $ROOT/start.sh" 2>/dev/null
pkill -f "start.sh" -P 1 2>/dev/null

echo "停止旧桥接进程（src/bridge.js，不动 MCP 子进程）…"
# 只杀 bridge.js 主进程；mcp-*.js 由 DSH 的 MCP 客户端管理，不在此处理。
for pid in $(pgrep -f "node .*src/bridge\.js" 2>/dev/null); do
  case "$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)" in
    *mcp-*) continue ;;
  esac
  kill "$pid" 2>/dev/null && echo "  killed $pid"
done

sleep 2
rm -f state/bridge.lock

echo "后台启动守护…"
: "${TMPDIR:=$HOME/tmp}"
export TMPDIR
nohup setsid bash "$ROOT/start.sh" >/dev/null 2>&1 &
sleep 1
echo "已启动。日志：state/bridge.log；控制台：http://127.0.0.1:3100"

# ── 保活层（双层看门狗）────────────────────────────────────────────
# Android 会整组清理后台进程：桥接能被 watchdog.sh 拉起，但看门狗自己也会被清，
# 一旦看门狗死掉，桥接被杀后就再没人拉。所以这里确保 watchdog.sh 和
# watchdog-super.sh（盯看门狗）都常驻。幂等：已在跑就跳过。
ensure_watchdog() {
  # 已有 watchdog 实例就跳过（排除 super 自身）
  if ps -eo pid,args 2>/dev/null | grep 'bash .*watchdog\.sh' | grep -v 'grep' | grep -v 'watchdog-super' | grep -q .; then
    echo "看门狗已在运行，跳过启动"
    return 0
  fi
  echo "启动看门狗（watchdog.sh）…"
  nohup setsid bash "$ROOT/watchdog.sh" >/dev/null 2>&1 < /dev/null &
  disown 2>/dev/null || true
}

ensure_super() {
  if [ -f state/watchdog-super.pid ]; then
    local old
    old=$(cat state/watchdog-super.pid 2>/dev/null | tr -d '\n')
    if [ -n "$old" ] && [ -d "/proc/$old" ]; then
      echo "看门狗监督已在运行（pid $old），跳过启动"
      return 0
    fi
    rm -f state/watchdog-super.pid
  fi
  echo "启动看门狗监督（watchdog-super.sh）…"
  nohup setsid bash "$ROOT/watchdog-super.sh" >/dev/null 2>&1 < /dev/null &
  disown 2>/dev/null || true
}

ensure_watchdog
ensure_super
echo "保活层就绪：watchdog.sh + watchdog-super.sh 双层看门狗"
