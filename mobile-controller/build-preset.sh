#!/usr/bin/env bash
# 构建「内部定制版」APK —— 把服务器连接信息内置进去，构建完立刻还原。
#
# 为什么要有这个脚本，而不是直接改 Preset.java 再 build：
#   公开仓库 / 公开 Release 必须零真实信息（本项目硬规矩）。
#   定制版要「打开就能用」，就得把服务器地址、密钥、口令编进包里。
#   手工改文件最容易出的事故是「改完忘了还原，直接 commit 推上公开仓库」。
#   所以这里把注入→构建→还原做成一个原子流程：还原放在 trap 里，中途失败也会还原。
#
# 用法（在本机跑，不要把真实值写进任何提交的文件）：
#   bash build-preset.sh --host <服务器> --ssh-port <端口> \
#        --ssh-user <账号> --key-file <RSA私钥路径> \
#        --fingerprint <SHA256:...> --token <管理口令> \
#        [--out build/xxx.apk]
#
# 产物：build/dafeiyu-controller-mine.apk（**不要**提交、不要上传公开 Release）
#
# 安全说明（诚实版）：
#   * 这个 APK 里内置了一把能连服务器的密钥。**拿到 APK 的人就能管理
#     服务器上的机器人实例。** 所以只发给信得过的人（比如内部群），
#     不要公开分发。公开版不含任何预设，只能手填自己的服务器。
#   * 内置的账号在服务器上被限制成「只能转发到管理端口」：
#     登不了 shell，也连不了别的端口。这样即使 APK 泄露，
#     损失面也只限于机器人管理，不涉及整台服务器。
#   * 密钥必须是 RSA。JSch 的 ed25519 实现要求 Java 15+，
#     而 Android 的 Ed25519 要 API 33+ —— 用 ed25519 会一直报 Auth fail。

set -euo pipefail
cd "$(dirname "$0")"

HOST=""
SSH_PORT=""
SSH_USER=""
KEY_FILE=""
FINGERPRINT=""
TOKEN=""
OUT="build/dafeiyu-controller-mine.apk"

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="${2:-}"; shift 2 ;;
    --ssh-port) SSH_PORT="${2:-}"; shift 2 ;;
    --ssh-user) SSH_USER="${2:-}"; shift 2 ;;
    --key-file) KEY_FILE="${2:-}"; shift 2 ;;
    --fingerprint) FINGERPRINT="${2:-}"; shift 2 ;;
    --token) TOKEN="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

[ -n "$HOST" ]        || { echo "缺少 --host（服务器地址）" >&2; exit 2; }
[ -n "$SSH_PORT" ]    || { echo "缺少 --ssh-port" >&2; exit 2; }
[ -n "$SSH_USER" ]    || { echo "缺少 --ssh-user" >&2; exit 2; }
[ -n "$KEY_FILE" ]    || { echo "缺少 --key-file（RSA 私钥路径）" >&2; exit 2; }
[ -n "$TOKEN" ]       || { echo "缺少 --token（管理口令）" >&2; exit 2; }
[ -f "$KEY_FILE" ]    || { echo "找不到私钥文件：$KEY_FILE" >&2; exit 2; }

# 密钥必须是 RSA —— 见文件头说明，ed25519 在安卓上用不了。
if ! head -1 "$KEY_FILE" | grep -qE 'BEGIN (RSA |OPENSSH )?PRIVATE KEY'; then
  echo "私钥格式不认识：$(head -1 "$KEY_FILE")" >&2; exit 2
fi
if head -1 "$KEY_FILE" | grep -q "OPENSSH PRIVATE KEY"; then
  if ! ssh-keygen -y -f "$KEY_FILE" 2>/dev/null | head -1 | grep -q "^ssh-rsa"; then
    echo "❌ 这把密钥不是 RSA。安卓上的 JSch 用不了 ed25519（需要 Java 15+）。" >&2
    echo "   重新生成：ssh-keygen -t rsa -b 3072 -m PEM -f <路径> -N ''" >&2
    exit 2
  fi
fi

# 指纹强烈建议填：不填就没法防中间人
if [ -z "$FINGERPRINT" ]; then
  echo "警告：没提供 --fingerprint，App 将不校验服务器身份。" >&2
  echo "      这意味着中间人可以假冒服务器，拿走隧道里的口令和 API Key。" >&2
  echo "      强烈建议填上（用 ssh-keyscan -p <端口> <地址> | ssh-keygen -lf - 取）。" >&2
else
  case "$FINGERPRINT" in
    SHA256:*) ;;
    *) echo "指纹格式应为 SHA256:... 开头" >&2; exit 2 ;;
  esac
fi

PRESET="app/src/com/dafeiyu/controller/Preset.java"
BACKUP="$(mktemp)"
cp "$PRESET" "$BACKUP"
# 无论成功失败都还原：这是本脚本存在的全部意义。
restore() {
  cp "$BACKUP" "$PRESET"
  rm -f "$BACKUP"
  echo "已还原 $PRESET（公开仓库保持零真实信息）"
}
trap restore EXIT

# 私钥内容要嵌进 Java 字符串字面量：转义反斜杠和引号。
# PEM 里一般只有 base64 和换行，但转义一下更稳妥。
KEY_ESCAPED="$(python3 - "$KEY_FILE" <<'PY'
import sys
raw = open(sys.argv[1], encoding="utf-8").read().strip()
# Java 字符串字面量里不能有裸换行，用 \n 转义
out = raw.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
print(out)
PY
)"

# 注入：写成一个含真实连接信息的 Preset.java
cat > "$PRESET" <<JAVA
package com.dafeiyu.controller;

/**
 * 定制版预设 —— 本文件由 build-preset.sh 在**本地构建时**临时生成，
 * 构建结束会立即还原为空模板。**不要提交这个版本。**
 *
 * 里面装了：服务器地址、SSH 端口、专用账号、专用账号的 RSA 私钥、
 * 服务器指纹、管理口令。
 *
 * 安全边界（写在这里免得后来人踩）：拿到这个 APK 的人就能管理
 * 服务器上的机器人实例。专用账号在服务器上被限制成「只能转发到
 * 管理端口」，所以损失面限于机器人管理，不涉及整台服务器。
 */
public final class Preset {

    private Preset() {
    }

    public static final boolean HAS_PRESET = true;

    public static final String HOST = "$HOST";

    public static final int SSH_PORT = $SSH_PORT;

    public static final String SSH_USER = "$SSH_USER";

    public static final String SSH_KEY =
            "$KEY_ESCAPED";

    public static final String HOST_FINGERPRINT = "$FINGERPRINT";

    public static final String MANAGER_TOKEN = "$TOKEN";

    public static final String HINT = "内部版：已内置服务器，点「连接」即可。";

    /** 预设是否完整可用。 */
    public static boolean usable() {
        return HAS_PRESET && HOST.length() > 0 && SSH_PORT > 0
                && SSH_USER.length() > 0 && SSH_KEY.length() > 0
                && MANAGER_TOKEN.length() > 0;
    }

    // 旧版字段（保留以免破坏既有脚本与测试）
    public static final String WEBUI_BASE = "";
    public static final String TOKEN_SALT = "";
    public static final String TOKEN_IV = "";
    public static final String TOKEN_CT = "";
}
JAVA

echo "→ 构建内部版 APK…"
# 两个环境变量的作用：
#   DSH_PRESET_BUILD=1 —— 告诉 build.sh「这是内部版」，跳过「不许含内置地址」的公开版闸门；
#   OUT_APK           —— 产物写到独立文件名，**绝不覆盖**公开版产物
#                        （否则定制包会躺在公开版的路径上，极易误发）。
mkdir -p "$(dirname "$OUT")"
KS_PASS="${KS_PASS:-dafeiyu2026}" DSH_PRESET_BUILD=1 OUT_APK="$OUT" \
  bash build.sh >/tmp/preset-build.log 2>&1 \
  || { echo "构建失败，日志尾部：" >&2; tail -20 /tmp/preset-build.log >&2; exit 1; }

echo "→ 内部版产物：$OUT"
ls -la "$OUT"

# ---- 构建后自检 ----
fail=0
# ① 私钥必须真的进去了（否则装上连不上）
#
# 注意：classes.dex 在 zip 里是压缩的，`unzip -p | grep` 有时取不到，
# 会误报。直接在整个 APK 上 grep -a（原始字节搜索）才准 ——
# 一个「总是报警」的检查会训练人忽略警告，比没有检查更糟。
if ! grep -qa "BEGIN RSA PRIVATE KEY" "$OUT"; then
  echo "❌ 产物里没有私钥 —— 装上连不上服务器。" >&2
  fail=1
fi
# ② 管理口令必须在里面
if ! grep -qa "$TOKEN" "$OUT"; then
  echo "❌ 产物里没有管理口令 —— 装了也连不上。" >&2
  fail=1
fi
# ②b 主机地址必须在里面
if ! grep -qa "$HOST" "$OUT"; then
  echo "❌ 产物里没有服务器地址。" >&2
  fail=1
fi
# ③ 指纹必须在里面
if [ -n "$FINGERPRINT" ] && ! grep -qa "${FINGERPRINT#SHA256:}" "$OUT"; then
  echo "❌ 产物里没有服务器指纹。" >&2
  fail=1
fi
# ④ 公开版字段必须仍为空（防止把旧的加密 Token 路径带进来）
if grep -qa "TOKEN_CT = \"[A-Za-z0-9+/]" "$PRESET" 2>/dev/null; then
  echo "⚠ 检测到旧版 TOKEN_CT 有值（本次不应出现）"
fi

[ "$fail" = "0" ] || { echo "自检未通过，已删除产物。" >&2; rm -f "$OUT"; exit 1; }
echo "✓ 自检：口令、指纹都在产物里"
echo "✓ 自检：服务器已内置（$HOST）"
echo
echo "提醒：这个 APK 能管理服务器上的机器人实例，只发给信得过的人。"
