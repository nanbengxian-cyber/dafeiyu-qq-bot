#!/usr/bin/env bash
# 纯 JVM 单测 —— 不需要模拟器、不需要真机、不需要 adb（与 console/ 同一思路）。
#
# 能这么跑的前提是：所有会出错的逻辑都在**不碰 android.* 的类**里
# （Json / NapCatClient / Deployer / DeployConfig / Knobs / Totp）。
# Activity 与 View 类里只有画界面和点事件，那部分本来也不是单测能覆盖的东西。
#
# 用法：bash test/run-tests.sh

set -euo pipefail
cd "$(dirname "$0")/.."

OUT=test/out
SRC=app/src/com/dafeiyu/controller

# 只编不依赖 Android 的那几个类。故意不写 *.java：
# 一旦有人把 android.* 的 import 加进这些文件，这里会立刻编译失败 ——
# 这正是我们要的信号（可测的核心被污染了），而不是悄悄失去可测性。
PURE="$SRC/Json.java $SRC/NapCatClient.java $SRC/Deployer.java \
$SRC/DeployConfig.java $SRC/Knobs.java $SRC/Totp.java $SRC/ChatSetup.java \
$SRC/PresetCrypto.java $SRC/Preset.java $SRC/Tunnel.java $SRC/ManagerClient.java \
$SRC/ProxyTransport.java $SRC/WebProxyPath.java $SRC/ApiGuide.java $SRC/RobotFilter.java $SRC/QrLayout.java"

for f in $PURE; do
  [ -f "$f" ] || { echo "缺源码：$f" >&2; exit 1; }
done

# 反向检查：这几个文件里不许出现 android 的 import。
if grep -l '^import android\.' $PURE 2>/dev/null | grep -q .; then
  echo "以下文件混进了 android.* import，会失去可测性：" >&2
  grep -l '^import android\.' $PURE >&2
  exit 1
fi

# 反向检查（血的教训）：UI 类里的控件字段，凡是「声明了但从未赋值」又「被 .方法() 调用」的，
# 就是启动即崩的 NPE —— v1.0.0 的 testBtn/deployBtn 漏了创建，App 一打开就闪退。
# 这类 bug 编译期不报、单测也碰不到（View 类不进测试面），只能靠静态扫。
if ! python3 test/check-unassigned-fields.py; then
  echo "控件字段未赋值检查未通过：上面的字段会以 null 被调用，App 启动就会崩。" >&2
  exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT"

# Tunnel.java 用到 JSch（端口转发），测试面里要带上这个 jar
JSCH=lib/jsch-0.2.17.jar
[ -f "$JSCH" ] || { echo "缺 $JSCH" >&2; exit 1; }

ZXING=lib/zxing-core.jar:lib/zxing-jse.jar
[ -f lib/zxing-core.jar ] || { echo "缺 lib/zxing-core.jar（测试用独立二维码解码器）" >&2; exit 1; }

javac -encoding UTF-8 -nowarn -d "$OUT" -cp "app/src:$JSCH:$ZXING" \
  $PURE test/tests/*.java

java -cp "$OUT:$JSCH:$ZXING" tests.Main

# 注入脚本要用**真 JS 引擎**跑一遍 —— 单测只能验证脚本字符串长什么样，
# 证明不了它真的能工作。这段脚本坏了的表现是「页面能打开但一登录就失败」，
# 看起来一切正常，所以必须实测。
bash test/run-shim-test.sh

# 产物脱敏自检（有产物才跑 —— 纯测试时 build/ 可能是空的）
if [ -f build/dafeiyu-controller.apk ]; then
  bash test/check-desensitize.sh || exit 1
fi
