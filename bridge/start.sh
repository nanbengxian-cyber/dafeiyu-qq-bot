#!/data/data/com.dsharnessmobile.shell/files/usr/bin/bash
# start.sh — start.bat 的 Android/Linux 等价物（守护模式：崩溃 5 秒后自动拉起）。
#
# 用法：
#   bash start.sh              前台守护（关闭当前 shell 即停止）
#   nohup setsid bash start.sh >/dev/null 2>&1 &   后台守护（脱离进程树，日志见 state/bridge.log）
#
# 退出码 2 = 已有实例在运行（单实例锁），此时不重启，直接退出。
set -u
cd "$(dirname "$0")" || exit 1

: "${TMPDIR:=$HOME/tmp}"
export TMPDIR
mkdir -p "$TMPDIR" state

while true; do
  node src/bridge.js
  code=$?
  if [ "$code" = "2" ]; then
    echo "[$(date '+%F %T')] bridge already running in another process. Exiting."
    exit 2
  fi
  echo "[$(date '+%F %T')] bridge exited (code $code), restarting in 5 seconds..."
  sleep 5
done
