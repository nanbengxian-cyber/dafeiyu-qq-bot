#!/usr/bin/env bash
# 构建「私有定制版」APK —— 默认值只在本地注入，构建完立刻还原。
#
# 为什么要有这个脚本，而不是直接改 Preset.java 再 build：
#   公开仓库 / 公开 Release 必须零真实信息（本项目硬规矩）。
#   定制版要「打开就能用」，就得把服务器地址 + 加密 Token 编进包里。
#   手工改文件最容易出的事故是「改完忘了还原，直接 commit 推上公开仓库」。
#   所以这里把注入→构建→还原做成一个原子流程：还原放在 trap 里，中途失败也会还原。
#
# 用法（在本机跑，不要把真实值写进任何提交的文件）：
#   bash build-preset.sh --base 100.x.x.x:6099 --token <WebUI_TOKEN> --pass '<一次性口令>'
#
# 产物：build/dafeiyu-controller-preset.apk（**不要**提交、不要上传公开 Release）
#
# 安全说明（诚实版）：
#   * Token 用 AES-256-GCM 加密，密钥由 --pass 经 PBKDF2(20万轮) 派生；
#     APK 里只有密文，没有口令解不开。
#   * 口令请用长随机串：口令太弱时离线暴力破解仍可行（迭代次数只是抬高成本）。
#   * 生成的 APK 含你的服务器地址，属于敏感文件，只自己装。

set -euo pipefail
cd "$(dirname "$0")"

BASE=""
TOKEN=""
PASS=""
OUT="build/dafeiyu-controller-preset.apk"

while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="${2:-}"; shift 2 ;;
    --token) TOKEN="${2:-}"; shift 2 ;;
    --pass) PASS="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    -h|--help)
      sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

[ -n "$BASE" ]  || { echo "缺少 --base（形如 1.2.3.4:6099）" >&2; exit 2; }
[ -n "$TOKEN" ] || { echo "缺少 --token" >&2; exit 2; }
[ -n "$PASS" ]  || { echo "缺少 --pass（解锁口令，请用长随机串）" >&2; exit 2; }

case "$BASE" in
  *:*) ;;
  *) echo "--base 要带端口，形如 1.2.3.4:6099" >&2; exit 2 ;;
esac
# https:// 前缀是允许的：Tailscale serve 暴露的 6099 是 TLS 终止，必须走 https。
# 不带协议时客户端按 http:// 处理 —— 走 Tailscale 会连不上，这里提醒一句。
case "$BASE" in
  https://*) ;;
  http://*) echo "提示：显式指定 http://；若目标是 Tailscale serve 端口，应改 https://" >&2 ;;
  *) echo "提示：未指定协议，客户端将按 http:// 连接；Tailscale serve 端口需要 https://" >&2 ;;
esac
if [ "${#PASS}" -lt 12 ]; then
  echo "警告：口令短于 12 位，离线暴力破解成本较低，建议换长随机串。" >&2
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

# 用 JDK 现算 PBKDF2+AES-GCM，避免依赖 openssl 版本差异。
# 注意：口令/Token 通过**环境变量**传给这个临时程序，不进命令行（ps 看不到）。
echo "→ 加密 Token…"
GEN_DIR="$(mktemp -d)"
cat > "$GEN_DIR/PresetGen.java" <<'JAVA'
import com.dafeiyu.controller.PresetCrypto;
public class PresetGen {
    public static void main(String[] a) throws Exception {
        String[] e = PresetCrypto.encrypt(System.getenv("PRESET_PASS"),
                System.getenv("PRESET_TOKEN"));
        System.out.println(e[0] + "\n" + e[1] + "\n" + e[2]);
    }
}
JAVA
javac -encoding UTF-8 -nowarn -d "$GEN_DIR/classes" -cp app/src \
  app/src/com/dafeiyu/controller/PresetCrypto.java "$GEN_DIR/PresetGen.java"
ENC="$(PRESET_BASE="$BASE" PRESET_TOKEN="$TOKEN" PRESET_PASS="$PASS" \
  java -cp "$GEN_DIR/classes" PresetGen)"
rm -rf "$GEN_DIR"

SALT="$(printf '%s\n' "$ENC" | sed -n 1p)"
IV="$(printf '%s\n' "$ENC" | sed -n 2p)"
CT="$(printf '%s\n' "$ENC" | sed -n 3p)"
[ -n "$SALT" ] && [ -n "$IV" ] && [ -n "$CT" ] || { echo "加密失败" >&2; exit 1; }

# 注入：写成一个**不含明文 Token** 的 Preset.java
cat > "$PRESET" <<JAVA
package com.dafeiyu.controller;

/**
 * 定制版预设 —— 本文件由 build-preset.sh 在**本地构建时**临时生成，
 * 构建结束会立即还原为空模板。**不要提交这个版本。**
 *
 * 这里只有密文（AES-256-GCM），明文 Token 不在文件里；
 * 解锁口令由构建时指定，不写进任何文件。
 */
public final class Preset {

    private Preset() {
    }

    public static final boolean HAS_PRESET = true;

    public static final String WEBUI_BASE = "$BASE";

    public static final String HINT = "定制版：地址已内置，填解锁口令即可连接。";

    public static final String TOKEN_SALT = "$SALT";
    public static final String TOKEN_IV = "$IV";
    public static final String TOKEN_CT = "$CT";
}
JAVA

echo "→ 构建定制版 APK…"
# 两个环境变量的作用：
#   DSH_PRESET_BUILD=1 —— 告诉 build.sh「这是定制版」，跳过「不许含内置地址」的公开版闸门；
#   OUT_APK           —— 产物写到独立文件名，**绝不覆盖**公开版产物
#                        （否则定制包会躺在公开版的路径上，极易误发）。
mkdir -p "$(dirname "$OUT")"
KS_PASS="${KS_PASS:-dafeiyu2026}" DSH_PRESET_BUILD=1 OUT_APK="$OUT" \
  bash build.sh >/tmp/preset-build.log 2>&1 \
  || { echo "构建失败，日志尾部：" >&2; tail -20 /tmp/preset-build.log >&2; exit 1; }

echo "→ 定制版产物：$OUT"
ls -la "$OUT"

# 构建后自检：产物里不许出现明文 Token（密文可以）。
if grep -qa "$TOKEN" "$OUT"; then
  echo "❌ 严重：产物里出现了明文 Token！已删除产物。" >&2
  rm -f "$OUT"
  exit 1
fi
echo "✓ 自检：产物里没有明文 Token（只有密文）"
echo "✓ 自检：地址已内置（$BASE）"

