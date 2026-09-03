#!/usr/bin/env bash
# 从零构建签名 APK —— 不用 Gradle。
#
# 这台机器上没有 Gradle、没有 Android Studio、没有 sdkmanager，装它们要几百兆
# 外网流量而且 1.9G 内存跑 Gradle daemon 很勉强。所以直接调 build-tools 里的
# 命令，把 Gradle 平时替我们做的七步显式写出来：
#
#   1. aapt2 compile   资源 → .flat 中间产物
#   2. aapt2 link      打包资源 + 生成 R.java（--java）
#   3. javac           R.java + 我们的源码 → .class（-source/-target 8）
#   4. d8              .class → classes.dex（Android 只认 dex，不认 class）
#   5. zip -0          把 classes.dex 塞进 aapt2 出的 apk 骨架（必须不压缩）
#   6. zipalign -p 4   4 字节对齐（-p 让 .so 按页对齐；v2 签名要求先对齐后签）
#   7. apksigner       v1+v2+v3 签名
#
# 顺序不能反：zipalign 必须在 apksigner 之前 —— 先签再对齐会破坏 v2 签名块。
#
# 用法：bash build.sh          正常构建
#       bash build.sh --clean  先清干净再构建

set -euo pipefail
cd "$(dirname "$0")"

TOOLS=${ANDROID_BUILD_TOOLS:-$HOME/apkbuild/tools}
BT=$TOOLS/android-14
ANDROID_JAR=$TOOLS/android-34/android.jar
KEYSTORE=${KEYSTORE:-$PWD/release.jks}
KS_PASS=${KS_PASS:?请设置 KS_PASS 环境变量（签名密钥口令）}
KS_ALIAS=release

APP=app
BUILD=build
OUT=$BUILD/dafeiyu-console.apk

say() { printf '\033[1m› %s\033[0m\n' "$1"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

[ "${1:-}" = "--clean" ] && rm -rf "$BUILD"

# ---------------------------------------------------------------- 前置检查
for f in "$BT/aapt2" "$BT/zipalign" "$BT/apksigner" "$BT/lib/d8.jar" "$ANDROID_JAR"; do
  [ -e "$f" ] || die "缺构建工具：$f"
done
[ -f "$KEYSTORE" ] || die "缺签名密钥：$KEYSTORE"
command -v javac >/dev/null || die "没有 javac"
command -v zip   >/dev/null || die "没有 zip"

# 图标是脚本生成的（没有 PIL，见 app/tools/mkicon.py）。缺了就现生成，
# 这样 clone 下来直接 build 也能过，不必记得先跑一遍图标脚本。
if [ ! -f "$APP/res/mipmap-xxxhdpi/ic_launcher.png" ]; then
  say "生成启动图标"
  python3 "$APP/tools/mkicon.py" "$APP/res" >/dev/null
fi

mkdir -p "$BUILD"/{flat,gen,classes,dex}

# ---------------------------------------------------------------- 1 编资源
say "编译资源"
find "$APP/res" -type f \( -name '*.png' -o -name '*.xml' \) -print0 \
  | while IFS= read -r -d '' f; do
      "$BT/aapt2" compile -o "$BUILD/flat" "$f" >/dev/null
    done
# 踩坑：这版 aapt2（2.19-10229193）的 @参数文件**只按空格切分，不认换行**。
# 一行一个路径时它把 "\n下一个路径" 当成同一个词，报
# "failed to open file: No such file or directory" —— 而文件其实好好躺在那儿，
# 单独 compile 那个文件也完全正常，很容易误以为是资源本身有问题。
# 所以必须空格分隔。
printf '%s ' "$BUILD"/flat/*.flat > "$BUILD/flat.args"
printf '\n' >> "$BUILD/flat.args"
FLAT_N=$(ls "$BUILD"/flat/*.flat 2>/dev/null | wc -l)
[ "$FLAT_N" -gt 0 ] || die "没有编出任何资源"
say "  $FLAT_N 份资源"

# ---------------------------------------------------------------- 2 链接
say "链接资源并生成 R.java"
"$BT/aapt2" link \
  -o "$BUILD/base.apk" \
  -I "$ANDROID_JAR" \
  --manifest "$APP/AndroidManifest.xml" \
  --java "$BUILD/gen" \
  --min-sdk-version 21 \
  --target-sdk-version 34 \
  @"$BUILD/flat.args"

R_JAVA=$(find "$BUILD/gen" -name 'R.java')
[ -n "$R_JAVA" ] || die "aapt2 没生成 R.java"

# ---------------------------------------------------------------- 3 javac
say "编译 Java"
# -source/-target 8：d8 认得 Java 8 字节码；再高会因为 android.jar 缺少
# 新版本需要的运行时类型而失败。--release 不能用（它会屏蔽 -bootclasspath）。
javac -encoding UTF-8 -nowarn \
  -source 8 -target 8 \
  -bootclasspath "$ANDROID_JAR" \
  -cp "$ANDROID_JAR" \
  -d "$BUILD/classes" \
  "$R_JAVA" $APP/src/com/dafeiyu/console/*.java 2>&1 \
  | grep -v 'bootstrap class path\|source value 8\|target value 8\|deprecat' || true

CLASS_N=$(find "$BUILD/classes" -name '*.class' | wc -l)
[ "$CLASS_N" -gt 0 ] || die "javac 没产出 class"
say "  $CLASS_N 个 class"

# ---------------------------------------------------------------- 4 dex
say "转 dex"
find "$BUILD/classes" -name '*.class' > "$BUILD/classes.list"
java -cp "$BT/lib/d8.jar" com.android.tools.r8.D8 \
  --lib "$ANDROID_JAR" \
  --min-api 21 \
  --output "$BUILD/dex" \
  @"$BUILD/classes.list" 2>&1 | grep -vi 'warning: \|info: ' || true
[ -f "$BUILD/dex/classes.dex" ] || die "d8 没产出 classes.dex"

# ---------------------------------------------------------------- 5 塞进 apk
say "组装 APK"
cp "$BUILD/base.apk" "$BUILD/unsigned.apk"
# -0 = 存储不压缩。classes.dex 压缩过 ART 就没法内存映射，装机时会被拒或变慢。
# -j 去掉目录层级，让 dex 落在 apk 根。
( cd "$BUILD/dex" && zip -q -0 -j "../unsigned.apk" classes.dex )

# ---------------------------------------------------------------- 6 对齐
say "对齐"
"$BT/zipalign" -p -f 4 "$BUILD/unsigned.apk" "$BUILD/aligned.apk"
"$BT/zipalign" -c -v 4 "$BUILD/aligned.apk" >/dev/null || die "对齐校验没过"

# ---------------------------------------------------------------- 7 签名
say "签名"
"$BT/apksigner" sign \
  --ks "$KEYSTORE" \
  --ks-key-alias "$KS_ALIAS" \
  --ks-pass "pass:$KS_PASS" \
  --key-pass "pass:$KS_PASS" \
  --v1-signing-enabled true \
  --v2-signing-enabled true \
  --v3-signing-enabled true \
  --out "$OUT" \
  "$BUILD/aligned.apk"

"$BT/apksigner" verify --print-certs "$OUT" >/dev/null || die "签名校验没过"

# ---------------------------------------------------------------- 交付前自检
# 这三条是「装上去能不能用」的最后一道闸，比签名通过更重要 ——
# 签名没问题但 minSdk 写错、包名写错、或者不小心把密钥打进了 apk，
# 都是要等到装机才发现的错，太晚了。
say "自检"
BADGE=$("$BT/aapt" dump badging "$OUT" 2>/dev/null)
printf '%s' "$BADGE" | grep -q "package: name='com.dafeiyu.console'" \
  || die "包名不对"
printf '%s' "$BADGE" | grep -q "sdkVersion:'21'" || die "minSdk 不是 21"
printf '%s' "$BADGE" | grep -q "uses-permission: name='android.permission.INTERNET'" \
  || die "没有联网权限"
printf '%s' "$BADGE" | grep -q "application-icon" || die "没有图标"
# APK 里绝不允许出现私钥或密钥文件 —— 老版本 com.release.console 曾把
# SSH 私钥打进 assets，这次刻意不走那条路。
if unzip -l "$OUT" | grep -qiE '\.(jks|keystore|pem|key)$|id_rsa|id_ed25519'; then
  die "APK 里有密钥文件"
fi
if unzip -p "$OUT" classes.dex | grep -qa 'BEGIN .*PRIVATE KEY'; then
  die "dex 里有私钥"
fi

SIZE=$(stat -c %s "$OUT")
printf '\n\033[32m✓ 构建完成\033[0m  %s（%s KB）\n' "$OUT" "$((SIZE / 1024))"
"$BT/apksigner" verify --verbose "$OUT" | sed 's/^/  /'
