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

# 反向检查（血的教训之二）：逻辑单测全绿，但生产代码**根本没调用**它。
# 「修 APK 不显示二维码」时就是这么被骗过去的：WebProxyPath 测得好好的，
# 实际四个接口没剥 data 外壳。这次的同类风险是网页自动登录的三处接线，
# View/Activity 不进单测面，只能静态扫。
if ! python3 test/check-weblogin-wiring.py; then
  echo "网页自动登录接线检查未通过：逻辑对了但没接上，用户看到的现象和没修一样。" >&2
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

# 网页自动登录也要用真 JS 引擎跑一遍 —— 单测只能证明地址拼得对，
# 证明不了 NapCat 的登录页**真的会**因为它而自动登录。
# 两者症状一样（用户对着「请输入token」发愣），所以必须分开验证。
bash test/run-weblogin-test.sh

# 消息通道配对（「机器人一个字都不回」的根因修复）：
# 光有配置逻辑不够，必须证明「两端 token 一致才算配对成功」——
# 这条判据错了的话，用户看到的是「保存成功但依然不回话」。
if ! python3 test/test-pairing.py; then
  echo "消息通道配对检查未通过：机器人会收不到消息（表现是一个字都不回）。" >&2
  exit 1
fi

# 额度与闲置清理。★ 重点是「宁可留着也不误删」的那几条：
# 读不到活动时间必须跳过，锁着的实例必须跳过。
if ! python3 test/test-quota.py; then
  echo "额度/闲置清理检查未通过：可能误删用户的机器人，或额度没生效。" >&2
  exit 1
fi

# 时区无关性。踩过的真坑：用 time.mktime 解析 UTC 时间戳，
# 本地(UTC)测试全过，一上服务器(UTC+8)就偏 8 小时。
# 这里显式在三个时区各跑一遍，任何时区不一致都会失败。
for tz in UTC Asia/Shanghai America/New_York; do
  if ! TZ=$tz python3 test/test-tz.py; then
    echo "时区无关性检查未通过（TZ=$tz）：闲置时间会算错，可能导致误删或漏删。" >&2
    exit 1
  fi
done

# 产物脱敏自检（有产物才跑 —— 纯测试时 build/ 可能是空的）
if [ -f build/dafeiyu-controller.apk ]; then
  bash test/check-desensitize.sh || exit 1
fi
