#!/usr/bin/env bash
# 从零构建签名 APK —— 不用 Gradle（与 console/ 的构建同一条路线）。
#
# 直接调 build-tools 里的命令，把 Gradle 平时替我们做的七步显式写出来：
#
#   1. aapt2 compile   资源 → .flat 中间产物
#   2. aapt2 link      打包资源 + 生成 R.java（--java）
#   3. javac           R.java + 我们的源码 → .class（-source/-target 8）
#   4. d8              .class + JSch jar → classes.dex（Android 只认 dex）
#   5. zip -0          把 classes.dex 塞进 aapt2 出的 apk 骨架（必须不压缩）
#   6. zipalign -p 4   4 字节对齐（v2 签名要求先对齐后签）
#   7. apksigner       v1+v2+v3 签名
#
# 顺序不能反：zipalign 必须在 apksigner 之前 —— 先签再对齐会破坏 v2 签名块。
#
# 用法：bash build.sh          正常构建
#       bash build.sh --clean  先清干净再构建
#
# 环境变量（都有默认值，指向 /opt/android-sdk）：
#   ANDROID_BUILD_TOOLS  build-tools 目录
#   ANDROID_JAR          android.jar
#   KEYSTORE / KS_PASS / KS_ALIAS  签名密钥（仓库里不含任何密钥）

set -euo pipefail
cd "$(dirname "$0")"

TOOLS=${ANDROID_BUILD_TOOLS:-/opt/android-sdk/build-tools/35.0.0}
ANDROID_JAR=${ANDROID_JAR:-/opt/android-sdk/platforms/android-35/android.jar}
JSCH_JAR=$PWD/lib/jsch-0.2.17.jar
KEYSTORE=${KEYSTORE:-$PWD/release.jks}
KS_PASS=${KS_PASS:?请设置 KS_PASS 环境变量（签名密钥口令）}
KS_ALIAS=${KS_ALIAS:-release}

APP=app
BUILD=build
# 产物路径可用 OUT_APK 覆盖。定制版构建会指向另一个文件名 —— 否则它会覆盖掉
# 公开版产物（同一路径），一不小心就会把带内置服务器的包当成公开版发出去。
OUT=${OUT_APK:-$BUILD/dafeiyu-controller.apk}

say() { printf '\033[1m› %s\033[0m\n' "$1"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

[ "${1:-}" = "--clean" ] && rm -rf "$BUILD"

# ---------------------------------------------------------------- 前置检查
for f in "$TOOLS/aapt2" "$TOOLS/zipalign" "$TOOLS/apksigner" "$TOOLS/lib/d8.jar" \
         "$ANDROID_JAR" "$JSCH_JAR"; do
  [ -e "$f" ] || die "缺构建工具：$f"
done
[ -f "$KEYSTORE" ] || die "缺签名密钥：$KEYSTORE（生成方法见 README）"
command -v javac >/dev/null || die "没有 javac"
command -v zip   >/dev/null || die "没有 zip"

mkdir -p "$BUILD"/{flat,gen,classes,dex}

# ---------------------------------------------------------------- 1 编资源
say "编译资源"
find "$APP/res" -type f \( -name '*.png' -o -name '*.xml' \) -print0 \
  | while IFS= read -r -d '' f; do
      "$TOOLS/aapt2" compile -o "$BUILD/flat" "$f" >/dev/null
    done
# 踩坑（console/build.sh 记录过）：这版 aapt2 的 @参数文件只按空格切分不认换行，
# 所以必须空格分隔。
printf '%s ' "$BUILD"/flat/*.flat > "$BUILD/flat.args"
printf '\n' >> "$BUILD/flat.args"
FLAT_N=$(ls "$BUILD"/flat/*.flat 2>/dev/null | wc -l)
[ "$FLAT_N" -gt 0 ] || die "没有编出任何资源"
say "  $FLAT_N 份资源"

# ---------------------------------------------------------------- 2 链接
say "链接资源并生成 R.java"
"$TOOLS/aapt2" link \
  -o "$BUILD/base.apk" \
  -I "$ANDROID_JAR" \
  --manifest "$APP/AndroidManifest.xml" \
  --java "$BUILD/gen" \
  --min-sdk-version 26 \
  --target-sdk-version 34 \
  @"$BUILD/flat.args"

R_JAVA=$(find "$BUILD/gen" -name 'R.java')
[ -n "$R_JAVA" ] || die "aapt2 没生成 R.java"

# ---------------------------------------------------------------- 3 javac
say "编译 Java"
# -source/-target 8：d8 认得 Java 8 字节码。Ssh.java 用到的 JSch 也是 8。
# 编译错误必须让整个构建失败：javac 失败时旧 class 会被拿去继续打包，
# 产出「签名有效但装上闪退」的残废包，比构建失败恶劣得多。
if ! javac -encoding UTF-8 -nowarn \
  -source 8 -target 8 \
  -bootclasspath "$ANDROID_JAR" \
  -cp "$ANDROID_JAR:$JSCH_JAR" \
  -d "$BUILD/classes" \
  $(find "$APP/src" -name '*.java') "$R_JAVA" 2>"$BUILD/javac.err"; then
  grep -v 'bootstrap class path\|source value 8\|target value 8\|deprecat' "$BUILD/javac.err" >&2
  die "javac 失败（上面是错误详情）"
fi
grep -v 'bootstrap class path\|source value 8\|target value 8\|deprecat' "$BUILD/javac.err" >&2 || true

CLASS_N=$(find "$BUILD/classes" -name '*.class' | wc -l)
[ "$CLASS_N" -gt 0 ] || die "javac 没产出 class"
# 防残废包：核心类必须真的在产物里。
for must in MainActivity LoginView ConsoleView NapCatClient Deployer Knobs; do
  [ -f "$BUILD/classes/com/dafeiyu/controller/$must.class" ] || die "$must.class 缺失（编译被跳过？）"
done
[ -f "$BUILD/classes/io/nayuki/qrcodegen/QrCode.class" ] || die "QrCode.class 缺失"
say "  $CLASS_N 个 class"

# ---------------------------------------------------------------- 4 dex
say "转 dex（含 JSch）"
find "$BUILD/classes" -name '*.class' > "$BUILD/classes.list"
java -cp "$TOOLS/lib/d8.jar" com.android.tools.r8.D8 \
  --lib "$ANDROID_JAR" \
  --min-api 26 \
  --output "$BUILD/dex" \
  @"$BUILD/classes.list" "$JSCH_JAR" 2>&1 | grep -vi 'warning: \|info: ' || true
[ -f "$BUILD/dex/classes.dex" ] || die "d8 没产出 classes.dex"

# ---------------------------------------------------------------- 5 塞进 apk
say "组装 APK"
cp "$BUILD/base.apk" "$BUILD/unsigned.apk"
# -0 = 存储不压缩。classes.dex 压缩过 ART 就没法内存映射，装机时会被拒或变慢。
( cd "$BUILD/dex" && zip -q -0 -j "../unsigned.apk" classes.dex )

# ---------------------------------------------------------------- 6 对齐
say "对齐"
"$TOOLS/zipalign" -p -f 4 "$BUILD/unsigned.apk" "$BUILD/aligned.apk"
"$TOOLS/zipalign" -c -v 4 "$BUILD/aligned.apk" >/dev/null || die "对齐校验没过"

# ---------------------------------------------------------------- 7 签名
say "签名"
"$TOOLS/apksigner" sign \
  --ks "$KEYSTORE" \
  --ks-key-alias "$KS_ALIAS" \
  --ks-pass "pass:$KS_PASS" \
  --key-pass "pass:$KS_PASS" \
  --v1-signing-enabled true \
  --v2-signing-enabled true \
  --v3-signing-enabled true \
  --out "$OUT" \
  "$BUILD/aligned.apk"

"$TOOLS/apksigner" verify --print-certs "$OUT" >/dev/null || die "签名校验没过"

# ---------------------------------------------------------------- 交付前自检
say "自检"
BADGE=$("$TOOLS/aapt" dump badging "$OUT" 2>/dev/null)
printf '%s' "$BADGE" | grep -q "package: name='com.dafeiyu.controller'" \
  || die "包名不对"
printf '%s' "$BADGE" | grep -q "sdkVersion:'26'" || die "minSdk 不是 26"
printf '%s' "$BADGE" | grep -q "uses-permission: name='android.permission.INTERNET'" \
  || die "没有联网权限"
printf '%s' "$BADGE" | grep -q "application-icon" || die "没有图标"
# APK 里绝不允许出现密钥文件 —— 老版本 com.release.console 曾把 SSH 私钥打进
# assets，这条闸从那次起一直留着。
if unzip -l "$OUT" | grep -qiE '\.(jks|keystore|pem|key)$|id_rsa|id_ed25519'; then
  die "APK 里有密钥文件"
fi
if unzip -p "$OUT" classes.dex | grep -qa 'BEGIN .*PRIVATE KEY'; then
  die "dex 里有私钥"
fi
# dex 里必须真的有 JSch 和 QR 库（链接漏了会在运行时才炸）。
# 踩坑：unzip -p 直接进管道在这台机器上会误报 EOCD 且丢内容，先落盘再 grep。
unzip -p "$OUT" classes.dex > "$BUILD/check.dex" 2>/dev/null
[ -s "$BUILD/check.dex" ] || die "dex 解不出来"
if ! grep -qa 'com/jcraft/jsch/Session' "$BUILD/check.dex"; then
  die "dex 里没有 JSch"
fi
if ! grep -qa 'io/nayuki/qrcodegen/QrCode' "$BUILD/check.dex"; then
  die "dex 里没有 QR 库"
fi
# 预设闸门：**默认构建必须是空预设**（公开仓库零真实信息的硬规矩）。
# build-preset.sh 会注入真实地址后构建并自动还原；万一还原失败（脚本被杀/磁盘满），
# 真实地址就会被编进这个包 —— 这条闸拦住它。
# 定制版构建时用 DSH_PRESET_BUILD=1 显式声明，跳过本闸（它本来就要带预设）。
if [ "${DSH_PRESET_BUILD:-0}" != "1" ]; then
  if grep -qa '这是定制版：服务器地址已内置' "$BUILD/check.dex"; then
    die "这个包含内置服务器（定制版），不能当公开版发布 —— 请检查 Preset.java 是否已还原"
  fi
  # 只拦「真实基础设施特征」，不拦文档里的占位符（如 1.2.3.4:6099）：
  #   * *.ts.net —— Tailscale 域名，一定是真实服务器；
  #   * 非文档段的公网 IP（RFC 5737 的 192.0.2/198.51.100/203.0.113 与私网段不算）。
  if grep -qaE 'https?://[A-Za-z0-9.-]+\.ts\.net' "$BUILD/check.dex"; then
    die "dex 里出现 Tailscale 真实域名 —— 拒绝产出公开包"
  fi
  if grep -qaE '(^|[^0-9.])([0-9]{1,3}\.){3}[0-9]{1,3}:[0-9]{2,5}' "$BUILD/check.dex"; then
    HIT=$(grep -aoE '(^|[^0-9.])([0-9]{1,3}\.){3}[0-9]{1,3}:[0-9]{2,5}' "$BUILD/check.dex" | sort -u | tr '\n' ' ')
    case "$HIT" in
      *1.2.3.4*|*192.0.2.*|*198.51.100.*|*203.0.113.*|*0.0.0.0*|*127.0.0.1*) ;;
      *) die "dex 里出现疑似真实服务器地址（$HIT）—— 拒绝产出公开包" ;;
    esac
  fi
fi

SIZE=$(stat -c %s "$OUT")
printf '\n\033[32m✓ 构建完成\033[0m  %s（%s KB）\n' "$OUT" "$((SIZE / 1024))"
"$TOOLS/apksigner" verify --verbose "$OUT" | sed 's/^/  /'
