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
$SRC/PresetCrypto.java $SRC/Preset.java"

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

javac -encoding UTF-8 -nowarn -d "$OUT" -cp app/src \
  $PURE test/tests/*.java

java -cp "$OUT" tests.Main
