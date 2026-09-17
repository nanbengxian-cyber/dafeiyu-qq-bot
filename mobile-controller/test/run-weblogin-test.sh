#!/usr/bin/env bash
# 用真 JS 引擎验证「打开内置网页登录页会自动登录」。
#
# 单测（WebProxyPathTest.withToken）只证明地址**拼得对**；
# 这里证明 NapCat 的登录页**真的会**因为这个地址而自动登录 ——
# 两者缺一不可，因为「拼对了但对方不认」和「对方认但拼错了」症状一样：
# 用户看到一个写着「请输入token」的输入框，不知道该填什么。
#
# 需要 node；没有 node 就跳过（不让缺工具卡住整个测试）。
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v node >/dev/null 2>&1; then
  echo "（跳过：本机没有 node，无法用真 JS 引擎验证网页自动登录）"
  exit 0
fi

SRC=app/src/com/dafeiyu/controller
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# 编译地址生成器（只依赖 WebProxyPath，不碰 android.*）
javac -encoding UTF-8 -nowarn -d "$TMP" "$SRC/WebProxyPath.java" \
  test/tools/TokenGen.java 2>/dev/null

echo "== 网页自动登录（真 JS 引擎跑 NapCat 登录页的逻辑）=="

# ① 常规 token（线上实测三个实例都是 12 位字母数字）
URL=$(java -cp "$TMP" TokenGen 41000 666 "Ab3xY9zQ7wEr")
echo "-- 地址：$URL"
node test/tools/weblogin-check.js "$URL"

# ② 含特殊字符的 token：+ / = 必须被编码，否则页面取到的 token 是错的，
#    会「自动提交了但登录失败」—— 比不提交更难查。
URL2=$(java -cp "$TMP" TokenGen 41000 666 "a+b/c=d")
echo
echo "-- 地址（token 含 + / =）：$URL2"
node test/tools/weblogin-check.js "$URL2"

# ③ 没有 token（直连模式或没解锁）：必须**不加** ?token=，
#    并且此时不该自动提交（复现用户看到的界面）。
URL3=$(java -cp "$TMP" TokenGen 41000 666 "")
echo
echo "-- 地址（无 token）：$URL3"
if [ "$URL3" != "http://127.0.0.1:41000/proxy/666/webui/" ]; then
  echo "  ✗ 没有 token 时不该加 ?token=（实际：$URL3）" >&2
  exit 1
fi
echo "  ✓ 没有 token 时不加参数（会显示登录页，提示里已告诉用户怎么办）"
