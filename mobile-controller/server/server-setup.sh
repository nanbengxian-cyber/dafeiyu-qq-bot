#!/bin/bash
# ============================================================================
# 服务器端一次性安装脚本 —— 在「湖北机」这类多开宿主机上跑。
#
# 做完这几件事：
#   ① 从传过来的镜像包导入 napcat + astrbot
#   ② 装管理服务到 /opt/dafeiyu/manager/，注册成 systemd 服务
#   ③ 打印管理口令和 App 需要的连接信息
#
# 这个脚本**只碰 /opt/dafeiyu 和 docker**，不动机器上别的东西。
# 特别是：不会碰 /srv/game（那台机器上已有的业务）。
#
# 用法：
#   sudo bash server-setup.sh [镜像包目录]      完整安装（默认目录 /srv/imgxfer）
#   sudo bash server-setup.sh --skip-import     跳过镜像导入（镜像已在本地时）
#                                               —— 用来重跑「装服务」这部分，
#                                                  必须幂等：不能把口令改掉、
#                                                  不能打断正在跑的实例。
# ============================================================================
set -euo pipefail

IMG_PKG=/srv/imgxfer
SKIP_IMPORT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-import) SKIP_IMPORT=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "未知参数：$1" >&2; exit 2 ;;
    *) IMG_PKG="$1"; shift ;;
  esac
done

ROOT=/opt/dafeiyu
MANAGER_DIR="$ROOT/manager"
PORT=6199

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; }

[ "$(id -u)" = "0" ] || { bad "要用 root 跑（sudo bash $0）"; exit 1; }

# ---------------------------------------------------------------- ① 导入镜像
say "① 导入容器镜像"
if [ "$SKIP_IMPORT" = "1" ]; then
  ok "跳过（--skip-import）"
  docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}' \
    | grep -Ei 'napcat|astrbot' || bad "本地没有 napcat/astrbot 镜像，实例起不来"
elif [ -f "$IMG_PKG/both.tar.zst" ]; then
  ok "找到镜像包 $IMG_PKG/both.tar.zst"
  if [ -f "$IMG_PKG/both.sha256" ]; then
    # 校验和必须对上：跨境链路传坏过，导入了坏镜像后面会很难查
    cd "$IMG_PKG"
    if sha256sum -c both.sha256 --status 2>/dev/null; then
      ok "sha256 校验通过"
    else
      bad "sha256 校验不通过！包可能在传输中损坏了，先重新传再导入。"
      exit 1
    fi
  else
    ok "没有校验和文件，跳过校验（建议补上）"
  fi
  zstd -d -f "$IMG_PKG/both.tar.zst" -o /tmp/both.tar
  docker load -i /tmp/both.tar
  rm -f /tmp/both.tar
  ok "镜像已导入"
elif ls "$IMG_PKG"/part_* >/dev/null 2>&1; then
  say "① 检测到分片，先合并"
  cd "$IMG_PKG"
  cat part_* > /tmp/both.tar.zst
  if [ -f both.sha256 ]; then
    got=$(sha256sum /tmp/both.tar.zst | awk '{print $1}')
    want=$(awk '{print $1}' both.sha256)
    if [ "$got" = "$want" ]; then
      ok "合并后 sha256 一致（$got）"
    else
      bad "合并后 sha256 不一致！"
      echo "     期望 $want"
      echo "     实际 $got"
      echo "     说明有分片没传完或传坏了。先检查 /srv/imgxfer 里分片是否齐全。"
      exit 1
    fi
  fi
  zstd -d -f /tmp/both.tar.zst -o /tmp/both.tar
  docker load -i /tmp/both.tar
  rm -f /tmp/both.tar /tmp/both.tar.zst
  ok "镜像已导入"
else
  bad "在 $IMG_PKG 里没找到镜像包（both.tar.zst 或 part_*）"
  echo "     先把镜像传过去，或者改参数指定路径。"
  exit 1
fi

# 镜像清单：--skip-import 时上面已打印过，不重复刷屏
if [ "$SKIP_IMPORT" != "1" ]; then
  say "① 已导入的镜像"
  docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}' | grep -Ei 'napcat|astrbot'
fi
docker images --format '{{.Repository}}:{{.Tag}}' | grep -Ei 'napcat|astrbot' >/dev/null || {
  bad "没看到 napcat / astrbot 镜像 —— 实例起不来"; exit 1; }

# ---------------------------------------------------------------- ② 装管理服务
say "② 安装管理服务"
mkdir -p "$MANAGER_DIR" "$ROOT/instances"
chmod 700 "$ROOT" "$ROOT/instances" "$MANAGER_DIR"

# 管理服务本体
#
# 注意 install 的坑：源和目标是同一个文件时 install 会报
# 「are the same file」并返回非 0，在 set -e 下直接中断整个脚本 ——
# 于是「重跑安装」会在这一步停住，后面的 systemd 注册全都不执行。
# 重跑是本脚本的正常用法（改完管理服务要重装），所以必须显式绕开。
SRC="$(cd "$(dirname "$0")" && pwd)/dafeiyu-manager.py"
DST="$MANAGER_DIR/dafeiyu-manager.py"
if [ ! -f "$SRC" ] && [ ! -f "$DST" ]; then
  bad "找不到 dafeiyu-manager.py（应和本脚本放一起）"
  exit 1
fi
if [ "$SRC" = "$DST" ]; then
  chmod 700 "$DST"
  ok "dafeiyu-manager.py 已在目标位置，只校正权限"
elif [ -f "$SRC" ]; then
  install -m 700 "$SRC" "$DST"
  ok "已安装 dafeiyu-manager.py"
else
  ok "dafeiyu-manager.py 已存在，保留"
fi

# 口令文件：没有就生成，有就保留（换口令会让已装好的 App 全部失效）
if [ ! -f "$MANAGER_DIR/manager.token" ]; then
  python3 "$MANAGER_DIR/dafeiyu-manager.py" token > "$MANAGER_DIR/manager.token"
  chmod 600 "$MANAGER_DIR/manager.token"
  ok "已生成管理口令"
else
  ok "管理口令已存在，保留原口令"
fi

# ---------------------------------------------------------------- ③ systemd
say "③ 注册开机自启服务"
cat > /etc/systemd/system/dafeiyu-manager.service <<EOF
[Unit]
Description=大肥鱼实例管理服务（只监听 127.0.0.1，手机经 SSH 隧道访问）
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 $MANAGER_DIR/dafeiyu-manager.py serve --port $PORT
Restart=always
RestartSec=5
# 只监听本机回环，公网不可达
Environment=DAFEIYU_ROOT=$ROOT

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now dafeiyu-manager >/dev/null 2>&1
sleep 2
if systemctl is-active --quiet dafeiyu-manager; then
  ok "服务已启动并设为开机自启"
else
  bad "服务没起来，看日志：journalctl -u dafeiyu-manager -n 30"
  exit 1
fi

# 确认真的只听 127.0.0.1（安全底线，必须验）
say "③ 监听地址核对（必须只有 127.0.0.1）"
if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
  ss -tlnp | grep ":$PORT " | sed 's/^/  /'
  if ss -tlnp | grep ":$PORT " | grep -qvE '127\.0\.0\.1|\[::1\]'; then
    bad "危险：$PORT 监听在非回环地址上！公网能直接访问管理服务。"
    exit 1
  fi
  ok "只监听回环，公网不可达"
else
  bad "没看到 $PORT 在监听"; exit 1
fi

# ---------------------------------------------------------------- ④ 输出信息
say "④ 完成 —— App 需要的连接信息"
TOKEN=$(cat "$MANAGER_DIR/manager.token")

# SSH 端口和受限账号从**实际系统状态**里读，不写死。
# 写死的坏处有两个：换台机器跑就会打印错的值（照着填必然连不上），
# 以及把「我们这台机器用哪个端口、哪个账号名」印进了公开仓库。
APP_SSH_PORT=$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}')
if [ -z "$APP_SSH_PORT" ]; then
  APP_SSH_PORT=$(grep -iE '^[[:space:]]*Port[[:space:]]+' /etc/ssh/sshd_config 2>/dev/null \
                 | awk '{print $2; exit}')
fi
[ -n "$APP_SSH_PORT" ] || APP_SSH_PORT=22

# 受限账号：取名字里带 dafeiyu 的那个；找不到就提示自己去填
APP_SSH_USER=$(awk -F: '$1 ~ /dafeiyu/ {print $1}' /etc/passwd 2>/dev/null | head -1)
[ -n "$APP_SSH_USER" ] || APP_SSH_USER="<你给 App 建的那个受限账号>"

cat <<EOF

  服务器地址   : $(hostname -I | awk '{print $1}')（对外用你连 SSH 的那个地址）
  SSH 端口     : $APP_SSH_PORT
  SSH 账号     : $APP_SSH_USER
  管理服务端口 : $PORT（仅 127.0.0.1）
  管理口令     : $TOKEN

  安全提示：$APP_SSH_USER 这个账号应当被限制成「只能转发到 $PORT」，
  不能登录 shell、不能转发别的端口。App 里内置的是它的专用密钥。
  建账号时在 ~/.ssh/authorized_keys 的行首加限制，形如：
    restrict,port-forwarding,permitopen="127.0.0.1:$PORT" <公钥> $APP_SSH_USER
  这样即便 App 的私钥泄露，也只能被用来管机器人，登不上整台机器。

  常用命令：
    systemctl status dafeiyu-manager      # 看服务状态
    journalctl -u dafeiyu-manager -f      # 看实时日志
    python3 $MANAGER_DIR/dafeiyu-manager.py list   # 列出所有实例

EOF
