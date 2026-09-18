#!/usr/bin/env bash
# 变异测试：逐项破坏「公告/检查更新/安装」与第三块虚拟屏的接线
# → 确认对应的检查真的报 FAIL → 还原 → 复核全绿。
#
# 为什么必须做：本项目被同一类坑咬过多次 —— 检查写好了，但它其实抓不到
# 它声称要抓的东西（正则写错、锚点漂移、检查的是注释而不是代码）。
# 「检查 PASS」本身不能证明检查有效，必须证明它**会失败**。
#
# 备份用 /tmp + cp：新增的 java 是 untracked、改过的 java 未提交，
# git checkout 还原不了（同 mutate-virtual-screen.sh 的理由）。
set -u
cd "$(dirname "$0")/.." || exit 1
SRC=app/src/com/dafeiyu/controller
UPCHK=test/check-app-update-wiring.py
VSCHK=test/check-virtual-screen-wiring.py
WBCHK=test/check-weblogin-wiring.py
TMP=/tmp/appupdate-mutate
rm -rf "$TMP"; mkdir -p "$TMP"

PURE="$SRC/Json.java $SRC/NapCatClient.java $SRC/Deployer.java \
$SRC/DeployConfig.java $SRC/Knobs.java $SRC/Totp.java $SRC/ChatSetup.java \
$SRC/PresetCrypto.java $SRC/Preset.java $SRC/Tunnel.java $SRC/ManagerClient.java \
$SRC/ProxyTransport.java $SRC/WebProxyPath.java $SRC/ApiGuide.java $SRC/RobotFilter.java $SRC/QrLayout.java \
$SRC/Protocols.java $SRC/AppUpdate.java"

FAILED=0

# 期望失败：跑一条命令，非 0 才算「变异被抓到」
expect_fail() { # $1=名字 $2=说明
  local name="$1" why="$2"
  shift 2
  if "$@" > "$TMP/$name.log" 2>&1; then
    echo "  ✗ [$name] 破坏后仍然 PASS —— 检查没抓到！（$why）"
    FAILED=1
  else
    echo "  ✓ [$name] 破坏后 FAIL —— 检查有效（$why）"
  fi
}

# 期望通过（还原后复核）
expect_pass() { # $1=名字 $2=说明
  local name="$1" why="$2"
  shift 2
  if "$@" > "$TMP/$name.log" 2>&1; then
    echo "  ✓ [$name] 还原后 PASS（$why）"
  else
    echo "  ✗ [$name] 还原后仍 FAIL —— 还原不干净！（$why）"
    FAILED=1
  fi
}

jvm_tests() {
  rm -rf test/out-mutate; mkdir -p test/out-mutate
  javac -encoding UTF-8 -nowarn -d test/out-mutate \
    -cp "app/src:lib/jsch-0.2.17.jar:lib/zxing-core.jar:lib/zxing-jse.jar" \
    $PURE test/tests/*.java 2>"$TMP/javac.err" || return 1
  java -cp "test/out-mutate:lib/jsch-0.2.17.jar:lib/zxing-core.jar:lib/zxing-jse.jar" \
    tests.Main
}

backup() { cp "$1" "$TMP/$(basename "$1").bak"; }
restore() { cp "$TMP/$(basename "$1").bak" "$1"; }

# ─────────────────────────────────────────────────────────────────────
echo "== 变异 1：hasUpdate 的比较方向反过来（永远不弹/永远弹）=="
backup "$SRC/AppUpdate.java"
python3 - "$SRC/AppUpdate.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
old = "return latestCode > 0 && latestCode > localCode;"
new = "return latestCode > 0 && latestCode < localCode;"
assert old in s, "hasUpdate 锚点没找到"
open(p, 'w', encoding='utf-8').write(s.replace(old, new, 1))
PY
expect_fail "jvm-hasUpdate" "AppUpdateTest 必须抓到方向反了" jvm_tests
restore "$SRC/AppUpdate.java"
expect_pass "jvm-hasUpdate-restored" "JVM 单测还原后全绿" jvm_tests

echo "== 变异 2：sha256 校验永远放行（传坏的包也装上）=="
backup "$SRC/AppUpdate.java"
python3 - "$SRC/AppUpdate.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
old = "return sha256.equals(actual);"
new = "return true;"
assert old in s, "sha256 比较锚点没找到"
open(p, 'w', encoding='utf-8').write(s.replace(old, new, 1))
PY
expect_fail "jvm-sha256" "AppUpdateTest 必须抓到不校验" jvm_tests
restore "$SRC/AppUpdate.java"
expect_pass "jvm-sha256-restored" "JVM 单测还原后全绿" jvm_tests

echo "== 变异 3：解析时索引字段（服务器少字段就崩）=="
backup "$SRC/AppUpdate.java"
python3 - "$SRC/AppUpdate.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
old = "if (m == null) {\n            return empty();\n        }"
new = "if (m == null) {\n            throw new RuntimeException(\"boom\");\n        }"
assert old in s, "from() 空判锚点没找到"
open(p, 'w', encoding='utf-8').write(s.replace(old, new, 1))
PY
expect_fail "jvm-null-parse" "AppUpdateTest 必须抓到 null 响应会崩" jvm_tests
restore "$SRC/AppUpdate.java"
expect_pass "jvm-null-parse-restored" "JVM 单测还原后全绿" jvm_tests

echo "== 变异 4：连接成功后不查更新（功能整个不生效）=="
backup "$SRC/MainActivity.java"
python3 - "$SRC/MainActivity.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
assert "UpdateFlow.maybeCheck(" in s, "触发点锚点没找到"
s = s.replace("UpdateFlow.maybeCheck(", "/*MUT*/ UpdateFlow_noop(", 1)
open(p, 'w', encoding='utf-8').write(s)
PY
expect_fail "trigger" "check-app-update-wiring 必须抓到没触发" python3 "$UPCHK"
restore "$SRC/MainActivity.java"

echo "== 变异 5：下载后不校验 sha256 =="
backup "$SRC/UpdateFlow.java"
python3 - "$SRC/UpdateFlow.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
assert ".sha256Matches(" in s, "sha256Matches 锚点没找到"
s = s.replace("!upd.sha256Matches(apk)", "false", 1)
open(p, 'w', encoding='utf-8').write(s)
PY
expect_fail "no-verify" "check-app-update-wiring 必须抓到不校验" python3 "$UPCHK"
restore "$SRC/UpdateFlow.java"

echo "== 变异 6：下载完不调系统安装器（用户停在「下载并安装」）=="
backup "$SRC/UpdateFlow.java"
python3 - "$SRC/UpdateFlow.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
assert "Intent.ACTION_VIEW" in s, "ACTION_VIEW 锚点没找到"
s = s.replace("new Intent(Intent.ACTION_VIEW)", "new Intent(\"com.example.NOPE\")", 1)
open(p, 'w', encoding='utf-8').write(s)
PY
expect_fail "no-installer" "check-app-update-wiring 必须抓到不安装" python3 "$UPCHK"
restore "$SRC/UpdateFlow.java"

echo "== 变异 7：manifest 去掉 REQUEST_INSTALL_PACKAGES =="
backup app/AndroidManifest.xml
python3 - <<'PY'
p = "app/AndroidManifest.xml"
s = open(p, encoding='utf-8').read()
assert "REQUEST_INSTALL_PACKAGES" in s, "权限锚点没找到"
s = s.replace('android:name="android.permission.REQUEST_INSTALL_PACKAGES"',
              'android:name="android.permission.NOPE"', 1)
open(p, 'w', encoding='utf-8').write(s)
PY
expect_fail "no-perm" "check-app-update-wiring 必须抓到权限缺失" python3 "$UPCHK"
restore app/AndroidManifest.xml

echo "== 变异 8：manifest 版本号退回 1（新包永远「已是最新」）=="
backup app/AndroidManifest.xml
python3 - <<'PY'
p = "app/AndroidManifest.xml"
s = open(p, encoding='utf-8').read()
assert 'android:versionCode="2"' in s, "versionCode 锚点没找到"
s = s.replace('android:versionCode="2"', 'android:versionCode="1"', 1)
open(p, 'w', encoding='utf-8').write(s)
PY
expect_fail "old-version" "check-app-update-wiring 必须抓到版本没升" python3 "$UPCHK"
restore app/AndroidManifest.xml

echo "== 变异 9：manifest 删掉 provider 注册 =="
backup app/AndroidManifest.xml
python3 - <<'PY'
p = "app/AndroidManifest.xml"
s = open(p, encoding='utf-8').read()
assert 'android:name=".ApkProvider"' in s, "provider 锚点没找到"
s = s.replace('android:name=".ApkProvider"', 'android:name=".GoneProvider"', 1)
open(p, 'w', encoding='utf-8').write(s)
PY
expect_fail "no-provider" "check-app-update-wiring 必须抓到 provider 没注册" python3 "$UPCHK"
restore app/AndroidManifest.xml

echo "== 变异 10：WebScreen 不拼 webui token（又看到「请输入token」）=="
backup "$SRC/WebScreen.java"
python3 - "$SRC/WebScreen.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
assert "WebProxyPath.withToken(" in s, "withToken 锚点没找到"
s = s.replace("WebProxyPath.withToken(", "/*MUT*/ (", 1)
open(p, 'w', encoding='utf-8').write(s)
PY
# token 正确性只由 check-weblogin-wiring 负责 —— 不让两个检查都断言同一件事：
# 重复断言会各自漂移，哪天一个漏了，另一个「碰巧」还在，反而掩盖问题。
expect_fail "ws-token" "check-weblogin-wiring 必须抓到 token 没拼" python3 "$WBCHK"
restore "$SRC/WebScreen.java"

echo "== 变异 10b：onEnter 里不 loadUrl（屏打开了却是空白）=="
backup "$SRC/WebScreen.java"
python3 - "$SRC/WebScreen.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
assert "loaded = true;\n            webView.loadUrl" in s, "loadUrl 锚点没找到"
s = s.replace("loaded = true;\n            webView.loadUrl", "loaded = true;\n            (new Object()).hashCode(); // noop", 1)
open(p, 'w', encoding='utf-8').write(s)
PY
expect_fail "ws-loadurl" "check-virtual-screen-wiring 必须抓到不加载" python3 "$VSCHK"
restore "$SRC/WebScreen.java"

echo "== 变异 11：WebScreen.dispose 不 destroy（WebView 泄漏）=="
backup "$SRC/WebScreen.java"
python3 - "$SRC/WebScreen.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
assert "webView.destroy();" in s, "destroy 锚点没找到"
s = s.replace("webView.destroy();", "/*MUT*/;", 1)
open(p, 'w', encoding='utf-8').write(s)
PY
expect_fail "ws-destroy" "check-virtual-screen-wiring 必须抓到没销毁" python3 "$VSCHK"
restore "$SRC/WebScreen.java"

echo "== 变异 12：第三块验证退回独立 Activity（用户看到的「之前的网页」）=="
backup "$SRC/LoginView.java"
python3 - "$SRC/LoginView.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
# ★ 必须替换**全部** host.openScreen(new WebScreen( 调用（代理路径 + 直连路径两处）：
#   第一版只换了一处，另一处还在 —— 检查当然 PASS，因为接线确实还在。
#   那是变异写错了，不是检查失灵：改坏一半不叫改坏。
n = s.count("host.openScreen(new WebScreen(")
assert n == 2, "期望 2 处 WebScreen 接线，实际 %d 处" % n
s = s.replace("host.openScreen(new WebScreen(", "host.openWebLogin(")
open(p, 'w', encoding='utf-8').write(s)
PY
expect_fail "third-is-activity" "check-virtual-screen-wiring 必须抓到退回 Activity" python3 "$VSCHK"
expect_fail "third-is-activity-wb" "check-weblogin-wiring 也要抓到" python3 "$WBCHK"
restore "$SRC/LoginView.java"

echo "== 变异 13：把 WebLoginActivity.java 复活（回归到旧网页）=="
cat > "$SRC/WebLoginActivity.java" <<'JAVA'
package com.dafeiyu.controller;
public class WebLoginActivity { }
JAVA
expect_fail "activity-revived" "check-virtual-screen-wiring 必须抓到旧 Activity 复活" python3 "$VSCHK"
rm -f "$SRC/WebLoginActivity.java"

# ─────────────────────────────────────────────────────────────────────
echo
echo "== 全部还原后复核：所有检查必须重新全绿 =="
expect_pass "restore-up" "check-app-update-wiring" python3 "$UPCHK"
expect_pass "restore-vs" "check-virtual-screen-wiring" python3 "$VSCHK"
expect_pass "restore-wb" "check-weblogin-wiring" python3 "$WBCHK"
expect_pass "restore-jvm" "JVM 单测" jvm_tests

rm -rf test/out-mutate

echo
if [ "$FAILED" -ne 0 ]; then
  echo "变异测试未通过：有检查抓不到它声称要抓的问题（见上面 ✗）。"
  exit 1
fi
echo "变异测试完成：13 组破坏全部被对应检查抓到，还原后全部重新全绿。"
