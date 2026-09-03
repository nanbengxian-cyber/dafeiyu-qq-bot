#!/usr/bin/env bash
# 纯 JVM 单测 —— 不需要模拟器、不需要真机、不需要 adb。
#
# 能这么跑的前提是：所有会出错的逻辑都在**不碰 android.* 的类**里
# （Json / StatusFmt / KnobModel / Ui / Api）。Activity 里只有画界面和点事件，
# 那部分本来也不是单测能覆盖的东西。
#
# 用法：bash test/run-tests.sh

set -euo pipefail
cd "$(dirname "$0")/.."

OUT=test/out
SRC=app/src/com/dafeiyu/console

# 只编不依赖 Android 的那几个类。故意不写 *.java：
# 一旦有人把 android.* 的 import 加进这些文件，这里会立刻编译失败 ——
# 这正是我们要的信号（可测的核心被污染了），而不是悄悄失去可测性。
PURE="$SRC/Json.java $SRC/StatusFmt.java $SRC/KnobModel.java $SRC/Ui.java $SRC/Api.java"

for f in $PURE; do
  [ -f "$f" ] || { echo "缺源码：$f" >&2; exit 1; }
done

# 反向检查：这几个文件里不许出现 android 的 import。
# 出现了就说明有人把界面逻辑混进了可测层，长期会让测试覆盖不到真正的逻辑。
if grep -l '^import android\.' $PURE 2>/dev/null | grep -q .; then
  echo "以下文件混进了 android.* import，会失去可测性：" >&2
  grep -l '^import android\.' $PURE >&2
  exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT"

javac -encoding UTF-8 -nowarn -d "$OUT" -cp app/src \
  $PURE test/tests/*.java

java -cp "$OUT" tests.Main
