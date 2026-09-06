#!/bin/bash
# qq_watchdog.sh —— QQ 登录看守进程（v2）。
#
# 背景：账号 100000002 被 QQ 风控盯得很紧，每 20~60 分钟就掉线一次。
# 掉线后快速登录报「身份已失效」、NapCat 快速登录列表为空，扫码是唯一恢复路径；
# 而 NapCat **只在进程启动时出码**、且**不会自动续期**（实测 mtime 冻结在生成
# 那一刻，3 分钟无新码也无过期日志），QQ 码只活约 2 分钟。所以看守要做三件事：
#   1) 判定在线/离线；
#   2) 离线期间保证 8088 上永远挂着一张新鲜的码（过期就重启 napcat 换码）；
#   3) 把状态写成 status.json 供网页显示。
#
# ── v2 修的是一个真实误报（2026-09-01 21:11 静默掉线，网页一直显示在线）──
#
# v1 只按日志判定：把在线标记和离线标记一起 grep，取最后一行。问题是
# **QQ 静默掉线时 NapCat 什么都不打**——没有 [KickedOffLine]，没有
# 「账号状态变更为离线」，连接也还挂在 ESTAB 上。于是最后一行永远停在掉线
# 前那条「接收 <-」，看守就一直认为在线，网页也一直绿着，人以为没事。
#
# v2 改成以「主动问 NapCat」为主，日志只作兜底：
#   - NapCat 的 OneBot HTTP 接口（容器内 127.0.0.1:3000，未映射到宿主，
#     外部访问不到）提供 get_status，data.online 是登录态的权威答案；
#   - 该接口只在**登录成功后**才启动，所以「连不上」本身就是掉线的证据；
#   - 但「连不上」也可能是配置被覆盖/端口冲突等自身故障。误判成掉线会重启
#     napcat，把一个活着的会话打断——这比误报在线更糟。所以加了两道保险：
#       ① 最近 ACTIVITY_WINDOW 秒内有收发消息 = 铁证在线，直接压过探针；
#       ② 只有当探针在**本次 napcat 生命周期内成功过至少一次**时，才认可
#         「探针连不上 = 掉线」；否则退回 v1 的日志判定，绝不贸然重启。
#
# 另外 status.json 新增 last_seen_seconds（距上次收发消息多久）和
# probe 字段，网页把它显示出来——万一judgment再出错，人一眼能看出
# 「显示在线但已经 40 分钟没动静」这种矛盾。

set -uo pipefail

PUBLIC_DIR=/opt/qqbot/public
STATUS_JSON="$PUBLIC_DIR/status.json"
QR_PUB="$PUBLIC_DIR/qrcode.png"
QR_IN_CONTAINER=/app/napcat/cache/qrcode.png
STATE_FILE=/var/lib/qqbot-watchdog.state
PROBE_SEEN_FILE=/var/lib/qqbot-watchdog.probeseen
LOG=/var/log/qqbot-watchdog.log

# 二维码存活时间（秒）。QQ 官方约 120s，留 20s 余量。
QR_TTL=${QR_TTL:-100}
# 两次重启 napcat 的最小间隔，避免打断正在进行的扫码
RESTART_COOLDOWN=${RESTART_COOLDOWN:-150}
# 主循环间隔
INTERVAL=${INTERVAL:-10}
# 日志里取不到账号时显示的兜底号码（当前在用的 2 号）
FALLBACK_UIN=${FALLBACK_UIN:-100000002}
# NapCat 容器内 OneBot HTTP 探针地址（host=127.0.0.1，不对外）
PROBE_URL=${PROBE_URL:-http://127.0.0.1:3000/get_status}
PROBE_TIMEOUT=${PROBE_TIMEOUT:-5}
# 最近多少秒内有收发消息就算铁证在线
ACTIVITY_WINDOW=${ACTIVITY_WINDOW:-180}
# napcat 刚启动后的宽限期，这段时间探针还没起来属正常
STARTUP_GRACE=${STARTUP_GRACE:-90}

# ── 邮件告警（v3，2026-09-02 加入）──────────────────────────────
# 为什么要加：掉线只能靠人扫码恢复，而原来的提醒只有 8088 页面里的
# 「掉线声音」——要求浏览器开着、标签页没静音、人在屏幕前，手机一锁屏
# 就完全收不到。邮件是不装 App、不花钱、手机能弹系统通知的唯一通道。
NOTIFY_SCRIPT=${NOTIFY_SCRIPT:-/opt/qqbot/notify_mail.py}
# 告警状态记录（.since=本次掉线起点 .sent=本轮已发过 .lastmail=最近一封掉线邮件）
NOTIFY_STATE=/var/lib/qqbot-watchdog.notify
# 两封「掉线」邮件之间的最小间隔。掉线→扫码→又掉线的抖动很频繁
# （这个号 20~60 分钟掉一次），没有这道闸邮箱会被刷爆。
NOTIFY_MIN_GAP=${NOTIFY_MIN_GAP:-600}
# 一直没恢复时每隔多久再催一次；0 = 只在掉线那一刻发一封，不重复
NOTIFY_REPEAT=${NOTIFY_REPEAT:-0}
# 恢复上线要不要补一封「已恢复」（用来确认扫码真的生效了）
NOTIFY_RECOVER=${NOTIFY_RECOVER:-1}
# 邮件正文里给出的扫码页地址
STATUS_URL=${STATUS_URL:-http://your-server.example.com:8088/}

mkdir -p "$PUBLIC_DIR" "$(dirname "$STATE_FILE")"
touch "$LOG"

log() { echo "$(date '+%F %T') $*" >>"$LOG"; }

# 去掉 NapCat 日志里的 ANSI 颜色码，否则 grep 会被转义序列干扰
strip_ansi() { sed 's/\x1b\[[0-9;]*m//g'; }

# 当前实际登录的账号号码。
# 从日志里取而不是扫 config 目录：那里残留着已封的 1 号 100000001 的
# 配置文件，按文件名排序会取错人。
# 结果缓存 60s——docker logs --since 6h 是这个脚本里最贵的一次调用，
# 而 write_status 每 10s 就要用一次，加上告警邮件里也要用，不缓存会白烧 CPU。
UIN_CACHE=""
UIN_CACHE_AT=0
current_uin() {
  local now uin
  now=$(date +%s)
  if [ -n "$UIN_CACHE" ] && [ $((now - UIN_CACHE_AT)) -lt 60 ]; then
    echo "$UIN_CACHE"; return
  fi
  uin=$(docker logs napcat --since 6h 2>&1 | strip_ansi \
        | grep -aoE 'napcat_[0-9]{5,}\.json' | tail -1 \
        | sed -n 's/napcat_\([0-9]\+\)\.json/\1/p')
  [ -z "$uin" ] && uin="$FALLBACK_UIN"
  UIN_CACHE="$uin"
  UIN_CACHE_AT="$now"
  echo "$uin"
}

# napcat 本次启动至今多少秒
napcat_uptime() {
  local started now
  started=$(docker inspect napcat --format '{{.State.StartedAt}}' 2>/dev/null)
  [ -z "$started" ] && { echo 99999; return; }
  started=$(date -d "$started" +%s 2>/dev/null) || { echo 99999; return; }
  now=$(date +%s)
  echo $((now - started))
}

# 本次 napcat 生命周期的唯一标识，用于判断探针「曾经成功过」是否还算数
napcat_start_id() {
  docker inspect napcat --format '{{.State.StartedAt}}' 2>/dev/null || echo unknown
}

# 主动问 NapCat 登录态。输出 online / offline / unreachable
probe_state() {
  local out
  out=$(docker exec napcat curl -s -m "$PROBE_TIMEOUT" "$PROBE_URL" 2>/dev/null)
  if [ -z "$out" ]; then
    echo unreachable
    return
  fi
  # 期望 {"status":"ok","retcode":0,"data":{"online":true,"good":true}}
  case "$out" in
    *'"online":true'*|*'"online": true'*)
      echo online ;;
    *'"online":false'*|*'"online": false'*)
      echo offline ;;
    *)
      # 有响应但读不出 online 字段，当作不可用，别乱下结论
      echo unreachable ;;
  esac
}

# 距最后一次收发消息多少秒；没有记录回 99999
last_activity_age() {
  local ts now
  ts=$(docker logs napcat --since 6h --timestamps 2>&1 | strip_ansi \
       | grep -aE '接收 <-|发送 ->' | tail -1 | awk '{print $1}')
  [ -z "$ts" ] && { echo 99999; return; }
  ts=$(date -d "$ts" +%s 2>/dev/null) || { echo 99999; return; }
  now=$(date +%s)
  echo $((now - ts))
}

# v1 的日志判定，作为探针不可信时的兜底
log_state() {
  local last
  last=$(docker logs napcat --since 6h 2>&1 | strip_ansi | grep -aE \
    'KickedOffLine|账号状态变更为离线|将使用二维码登录方式|二维码已保存|OneBot11 适配器初始化完成|账号状态变更为在线|接收 <-|发送 ->' \
    | tail -1)
  if [ -z "$last" ]; then
    echo unknown
    return
  fi
  case "$last" in
    *KickedOffLine*|*账号状态变更为离线*|*将使用二维码登录方式*|*二维码已保存*)
      echo offline ;;
    *)
      echo online ;;
  esac
}

# 综合判定。输出「状态|依据」两段，用 | 分隔。
#
# 注意：不能把依据写进全局变量——调用方用 state=$(detect_state) 取值，
# 那是个子 shell，里面的赋值传不回来（v2 初稿踩过，detect_reason 一直是空）。
# 所以依据必须随 stdout 一起返回。
detect_state() {
  local p act uptime seen_id cur_id
  p=$(probe_state)
  act=$(last_activity_age)
  uptime=$(napcat_uptime)
  cur_id=$(napcat_start_id)

  # 探针成功过就记下来（连同本次启动标识，napcat 重启后自动失效）
  if [ "$p" != "unreachable" ]; then
    echo "$cur_id" >"$PROBE_SEEN_FILE"
  fi
  seen_id=""
  [ -f "$PROBE_SEEN_FILE" ] && seen_id=$(cat "$PROBE_SEEN_FILE" 2>/dev/null)

  # 1) 探针明确回答，优先采信
  if [ "$p" = "online" ]; then
    echo "online|探针 get_status online=true"; return
  fi
  if [ "$p" = "offline" ]; then
    echo "offline|探针 get_status online=false"; return
  fi

  # 2) 探针连不上：最近有收发就是铁证在线（压过探针，防误杀活会话）
  if [ "$act" -le "$ACTIVITY_WINDOW" ]; then
    echo "online|探针不可用，但 ${act}s 前仍有收发消息"; return
  fi

  # 3) napcat 刚起来，探针还没就位属正常
  if [ "$uptime" -le "$STARTUP_GRACE" ]; then
    echo "$(log_state)|napcat 刚启动 ${uptime}s，探针尚未就位，按日志判定"; return
  fi

  # 4) 探针本次生命周期里成功过，现在连不上 = OneBot 适配器已随掉线停止
  if [ -n "$seen_id" ] && [ "$seen_id" = "$cur_id" ]; then
    echo "offline|探针曾可用但现已失联（${act}s 无收发）"; return
  fi

  # 5) 探针从未可用（可能配置没生效），退回日志判定，不贸然重启
  echo "$(log_state)|探针从未可用，退回日志判定（${act}s 无收发）"
}

# 容器内二维码的年龄（秒）；取不到时回 99999
qr_age() {
  local mt now
  mt=$(docker exec napcat stat -c %Y "$QR_IN_CONTAINER" 2>/dev/null)
  if [ -z "$mt" ]; then echo 99999; return; fi
  now=$(date +%s)
  echo $((now - mt))
}

# 重新出码：NapCat 只在启动时生成二维码，所以必须重启。
# 先删掉旧码（容器内 + 网页目录），让网页明确显示「正在生成」，
# 避免人对着一张死码扫半天。
mint_new_qr() {
  log "二维码过期，重启 napcat 重新出码"
  docker exec napcat rm -f "$QR_IN_CONTAINER" 2>/dev/null || true
  rm -f "$QR_PUB" 2>/dev/null || true
  write_status "renewing" 0 "正在生成新的二维码…" 99999
  docker restart napcat >/dev/null 2>&1 || log "docker restart napcat 失败"
  rm -f "$PROBE_SEEN_FILE" 2>/dev/null || true
  date +%s >"$STATE_FILE.lastrestart"
}

write_status() {
  local state="$1" age="$2" msg="$3" act="${4:-99999}"
  local uin qr_ok
  uin=$(current_uin)
  [ -f "$QR_PUB" ] && qr_ok=true || qr_ok=false
  cat >"$STATUS_JSON" <<EOF
{
  "state": "$state",
  "message": "$msg",
  "qr_age_seconds": $age,
  "qr_ttl_seconds": $QR_TTL,
  "qr_available": $qr_ok,
  "account": "${uin:-unknown}",
  "last_seen_seconds": $act,
  "detect_reason": "$(echo "$REASON" | sed 's/"/\\"/g')",
  "updated_at": "$(date '+%F %T %Z')",
  "updated_epoch": $(date +%s)
}
EOF
}

# ── 邮件告警 ────────────────────────────────────────────────────

# 把秒数说成人话：邮件主题里「已掉线 23 分钟」比「1380s」有用得多
human_dur() {
  local s=$1
  if [ "$s" -ge 99999 ]; then echo "未知"; return; fi
  if [ "$s" -lt 60 ]; then echo "${s} 秒"; return; fi
  if [ "$s" -lt 3600 ]; then echo "$((s / 60)) 分钟"; return; fi
  echo "$((s / 3600)) 小时 $(((s % 3600) / 60)) 分钟"
}

# 后台发信。**必须后台**：notify_mail.py 自带 3 次重试（最坏 ~1 分钟），
# 而主循环是 10s 一轮，前台调用会把看码/换码整个拖住。
send_mail_async() {
  local subject="$1" body="$2"
  if [ ! -x "$NOTIFY_SCRIPT" ] && [ ! -f "$NOTIFY_SCRIPT" ]; then
    log "告警脚本不存在：$NOTIFY_SCRIPT，跳过发信"
    return
  fi
  (
    printf '%s' "$body" | /usr/bin/python3 "$NOTIFY_SCRIPT" "$subject" - \
      >/dev/null 2>&1
  ) &
}

# 掉线告警。三道闸，缺一个都会出问题：
#   1) 跨次冷却 NOTIFY_MIN_GAP（记在 .lastmail，恢复时**不清**）——
#      这个号 20~60 分钟掉一次，掉线→扫码→又掉线的抖动没这道闸会刷爆邮箱；
#   2) 同一次掉线只发一封（记在 .sent，恢复时清掉）；
#   3) 只有 NOTIFY_REPEAT>0 才周期性催办，默认 0 = 不催。
notify_offline() {
  local age="$1" act="$2" now last_any last_sent since down_for uin
  now=$(date +%s)

  # 闸 1：跨次冷却
  last_any=0
  [ -f "$NOTIFY_STATE.lastmail" ] && last_any=$(cat "$NOTIFY_STATE.lastmail" 2>/dev/null || echo 0)
  if [ $((now - last_any)) -lt "$NOTIFY_MIN_GAP" ]; then
    return
  fi

  # 闸 2/3：本次掉线是否已经发过
  if [ -f "$NOTIFY_STATE.sent" ]; then
    last_sent=$(cat "$NOTIFY_STATE.sent" 2>/dev/null || echo 0)
    if [ "$NOTIFY_REPEAT" -le 0 ]; then
      return
    fi
    if [ $((now - last_sent)) -lt "$NOTIFY_REPEAT" ]; then
      return
    fi
  fi

  since=$now
  [ -f "$NOTIFY_STATE.since" ] && since=$(cat "$NOTIFY_STATE.since" 2>/dev/null || echo "$now")
  down_for=$((now - since))
  [ "$down_for" -lt 0 ] && down_for=0
  uin=$(current_uin)

  send_mail_async \
    "⚠️ QQ机器人掉线了（已 $(human_dur "$down_for")）需要扫码" \
    "$(cat <<EOF
机器人已掉线，需要你用手机 QQ 扫码重新登录。

打开这个页面扫码： ${STATUS_URL}
（页面需要密码，忘了就在服务器上跑：python3 /opt/qqbot/qrweb_auth.py --set-password）

────────────────────
账号：$uin
掉线开始：$(date -d "@$since" '+%F %T %Z')
已持续：$(human_dur "$down_for")
上次收发消息：$(human_dur "$act")前
判定依据：$REASON
当前二维码年龄：${age}s（超过 ${QR_TTL}s 看守会自动重启 napcat 换新码）
本邮件发送时间：$(date '+%F %T %Z')
────────────────────

说明：二维码只活约 2 分钟，看守进程会自动换新码，所以你打开页面时
看到的一定是能扫的那张。扫码成功后会再收到一封「已恢复」邮件。
EOF
)"

  echo "$now" >"$NOTIFY_STATE.sent"
  echo "$now" >"$NOTIFY_STATE.lastmail"
  : >"$NOTIFY_STATE.pending"      # 记账：欠一封「已恢复」
  log "已触发掉线告警邮件（掉线 ${down_for}s）"
}

# 恢复告警。只有真发过掉线邮件才发，避免「无头无尾只有恢复」的困惑。
notify_recover() {
  local now since down_for uin
  [ "$NOTIFY_RECOVER" = "1" ] || { rm -f "$NOTIFY_STATE.pending"; return; }
  [ -f "$NOTIFY_STATE.pending" ] || return

  now=$(date +%s)
  since=$now
  [ -f "$NOTIFY_STATE.since" ] && since=$(cat "$NOTIFY_STATE.since" 2>/dev/null || echo "$now")
  down_for=$((now - since))
  [ "$down_for" -lt 0 ] && down_for=0
  uin=$(current_uin)

  send_mail_async \
    "✅ QQ机器人已恢复在线（本次掉线 $(human_dur "$down_for")）" \
    "$(cat <<EOF
机器人已重新登录，恢复正常工作，不需要再扫码了。

账号：$uin
恢复时间：$(date '+%F %T %Z')
本次掉线持续：$(human_dur "$down_for")
判定依据：$REASON

状态页：${STATUS_URL}
EOF
)"

  rm -f "$NOTIFY_STATE.pending"
  log "已触发恢复告警邮件（本次掉线 ${down_for}s）"
}


prev_state=""
[ -f "$STATE_FILE" ] && prev_state=$(cat "$STATE_FILE" 2>/dev/null)
REASON=""

log "watchdog v3 启动 (QR_TTL=${QR_TTL}s RESTART_COOLDOWN=${RESTART_COOLDOWN}s INTERVAL=${INTERVAL}s ACTIVITY_WINDOW=${ACTIVITY_WINDOW}s 邮件告警: 最小间隔=${NOTIFY_MIN_GAP}s 重复=${NOTIFY_REPEAT}s 恢复通知=${NOTIFY_RECOVER})"

while true; do
  verdict=$(detect_state)
  state=${verdict%%|*}
  REASON=${verdict#*|}
  age=$(qr_age)
  act=$(last_activity_age)

  if [ "$state" != "$prev_state" ]; then
    log "状态变化: ${prev_state:-初始} -> $state （依据：$REASON）"
    echo "$state" >"$STATE_FILE"
    prev_state="$state"
  fi

  # ── 告警：只认 online / offline 两个确定态，unknown 一律不发信 ──
  # 掉线起点单独记在 .since，不跟 prev_state 绑在一起：看守自身重启后
  # prev_state 是从文件读回来的，不会产生「转换」，但邮件里的「已掉线多久」
  # 仍然要准。
  if [ "$state" = "offline" ]; then
    [ -f "$NOTIFY_STATE.since" ] || date +%s >"$NOTIFY_STATE.since"
    notify_offline "$age" "$act"
  elif [ "$state" = "online" ]; then
    notify_recover
    rm -f "$NOTIFY_STATE.since" "$NOTIFY_STATE.sent" 2>/dev/null || true
  fi

  if [ "$state" = "online" ]; then
    write_status online "$age" "机器人在线" "$act"
  elif [ "$state" = "offline" ]; then
    last_restart=0
    [ -f "$STATE_FILE.lastrestart" ] && last_restart=$(cat "$STATE_FILE.lastrestart" 2>/dev/null || echo 0)
    since_restart=$(( $(date +%s) - last_restart ))

    if [ "$age" -gt "$QR_TTL" ] && [ "$since_restart" -gt "$RESTART_COOLDOWN" ]; then
      # 重启前再确认一次：万一刚好在这几秒内扫码成功了，就别打断
      recheck=$(detect_state)
      if [ "${recheck%%|*}" = "offline" ]; then
        mint_new_qr
      fi
    else
      left=$((QR_TTL - age))
      [ "$left" -lt 0 ] && left=0
      write_status offline "$age" "掉线了，请尽快扫码（本张码剩约 ${left}s）" "$act"
    fi
  else
    write_status unknown "$age" "状态未知，正在确认…" "$act"
  fi

  sleep "$INTERVAL"
done
