#!/usr/bin/env bash
# 用真 JS 引擎跑一遍注入脚本 —— 单测只能验证「脚本字符串长什么样」，
# 不能证明它**真的能工作**。
#
# 为什么值得单独跑：这段脚本负责改写页面 JS 发出的 API 调用。
# 它坏了的表现是「页面能打开、但一登录就失败」（POST body 丢失），
# 看起来一切正常，最难查。所以要用 node 造一个假的浏览器环境，
# 实际执行脚本，断言：
#   * axios 风格的 XHR POST 地址被改写，且 **body 完整保留**；
#   * fetch 的地址被改写、body 不动；
#   * 外部地址不被改写（不能把外链也代理了）；
#   * 已经是代理地址的不会被重复套前缀；
#   * EventSource 被改写。
#
# 需要 node；没有 node 就跳过（不让缺工具卡住整个测试）。
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v node >/dev/null 2>&1; then
  echo "（跳过：本机没有 node，无法用真 JS 引擎验证注入脚本）"
  exit 0
fi

OUT=test/out
SRC=app/src/com/dafeiyu/controller
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# 编译出 shim 生成器（只依赖 WebProxyPath，不碰 android.*）
javac -encoding UTF-8 -nowarn -d "$TMP" "$SRC/WebProxyPath.java" \
  test/tools/ShimGen.java 2>/dev/null
java -cp "$TMP" ShimGen 41000 qq1 > "$TMP/shim.js"

if [ ! -s "$TMP/shim.js" ]; then
  echo "✗ 注入脚本生成失败（空文件）" >&2
  exit 1
fi

node test/tools/shim-check.js "$TMP/shim.js"
