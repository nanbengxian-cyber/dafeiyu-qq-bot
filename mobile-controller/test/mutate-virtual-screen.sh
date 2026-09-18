#!/usr/bin/env bash
# 变异测试：逐项破坏虚拟屏接线 → 确认 check 报 FAIL → 还原。
# 4 个新 java 是 untracked、LoginView/MainActivity 是未提交修改，
# 都不能用 git checkout 还原，所以统一用 /tmp 备份 + cp 还原。
set -u
cd "$(dirname "$0")/.." || exit 1
SRC=app/src/com/dafeiyu/controller
CHK=test/check-virtual-screen-wiring.py
TMP=/tmp/vscreen-mutate
rm -rf "$TMP"; mkdir -p "$TMP"

run_case() { # $1=名字 $2=文件 成功后检查是否 FAIL
  local name="$1" file="$2"
  if python3 "$CHK" > "$TMP/$name.log" 2>&1; then
    echo "  ✗ [$name] 破坏后检查竟然 PASS —— 检查没抓到！"
    return 1
  else
    echo "  ✓ [$name] 破坏后检查 FAIL —— 检查有效"
    return 0
  fi
}

echo "== 变异 1：删 QrScreen.dispose 里的 recycle（位图泄漏）=="
cp "$SRC/QrScreen.java" "$TMP/QrScreen.java.bak"
python3 - "$SRC/QrScreen.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
old = """        if (currentQr != null && !currentQr.isRecycled()) {
            currentQr.recycle();
        }"""
new = """        currentQr = null;"""
assert old in s, "recycle 锚点没找到"
open(p, 'w', encoding='utf-8').write(s.replace(old, new, 1))
PY
run_case "recycle" "QrScreen.java"
cp "$TMP/QrScreen.java.bak" "$SRC/QrScreen.java"

echo "== 变异 2：删 LoginView 里 openScreen(new QrScreen（没接上）=="
cp "$SRC/LoginView.java" "$TMP/LoginView.java.bak"
python3 - "$SRC/LoginView.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
assert "host.openScreen(new QrScreen(" in s, "QrScreen 锚点没找到"
s = s.replace("host.openScreen(new QrScreen(", "host.openScreen(new android.widget.FrameLayout((" , 1)
open(p, 'w', encoding='utf-8').write(s)
PY
run_case "qr-wiring" "LoginView.java"
cp "$TMP/LoginView.java.bak" "$SRC/LoginView.java"

echo "== 变异 3：PwScreen 先置空引用再抹明文（顺序反了）=="
cp "$SRC/PwScreen.java" "$TMP/PwScreen.java.bak"
python3 - "$SRC/PwScreen.java" <<'PY'
import sys, re
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
# 找到 dispose 里「抹明文」与「置 null」相邻处，交换顺序
# 先看实际文本，用通用办法：把 setText("") 挪到同函数所有置 null 之后
m = re.search(r'(    public void dispose\(\) \{)(.*?)(\n    \})', s, re.S)
assert m, "dispose 方法体没找到"
head, body, tail = m.group(1), m.group(2), m.group(3)
clear = 'qqPassword.setText("")'
assert clear in body, "抹明文语句没找到"
body2 = body.replace(clear + ";\n", "", 1)          # 从原位拿走
# 在方法体最末尾（置 null 之后）重新插入 —— 顺序反了
body2 = body2.rstrip() + "\n        " + clear + ";\n"
open(p, 'w', encoding='utf-8').write(s[:m.start()] + head + body2 + tail + s[m.end():])
PY
run_case "pw-order" "PwScreen.java"
cp "$TMP/PwScreen.java.bak" "$SRC/PwScreen.java"

echo "== 变异 4：删 ScreenHost.open 里 overlay.addView(col)（屏没挂载）=="
cp "$SRC/ScreenHost.java" "$TMP/ScreenHost.java.bak"
python3 - "$SRC/ScreenHost.java" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
assert "overlay.addView(col);" in s, "addView 锚点没找到"
open(p, 'w', encoding='utf-8').write(s.replace("overlay.addView(col);", "/*MUT removed*/", 1))
PY
run_case "host-mount" "ScreenHost.java"
cp "$TMP/ScreenHost.java.bak" "$SRC/ScreenHost.java"

echo "== 还原后复核：检查必须重新全绿 =="
if python3 "$CHK" >/dev/null 2>&1; then
  echo "  ✓ 还原后 FAILS: 0 —— 变异测试完成，全部还原干净"
else
  echo "  ✗ 还原后检查仍失败 —— 还原有问题！"
  exit 1
fi