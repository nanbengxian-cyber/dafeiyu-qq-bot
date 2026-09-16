#!/usr/bin/env bash
# 脱敏自检 —— 公开产物里不许出现任何真实基础设施信息。
#
# 这是本项目的硬规矩：公开仓库 / 公开 Release 必须零真实信息。
#
# 用法：
#   bash test/check-desensitize.sh                       # 检查公开版 APK
#   bash test/check-desensitize.sh <文件>                # 检查指定产物
#   bash test/check-desensitize.sh --internal <文件>     # 检查内部版（应含真实值）
#
# ── 两层检查 ────────────────────────────────────────────────────────────
#
# ① 结构检查（本脚本自带，可进公开仓库）
#    不依赖任何具体值，直接找「形状像真实基础设施」的东西：
#    非文档用途的 IP、Tailscale 域名、服务器路径、私钥体等。
#    这样即使具体值变了（换服务器、换 IP），检查依然有效 ——
#    写死一份 IP 清单的话，换个服务器就失效了，而且**清单本身就是泄露**。
#
# ② 具体值检查（从本地文件读，**该文件不进仓库**）
#    历史遗留的、形状上不显眼的串（某些主机名、口令片段、QQ 号）。
#    放在 test/desensitize-patterns.local.txt（已 gitignore）。
#    没有这个文件时只跑第 ① 层，不报错。
#
# ── 踩过的坑 ────────────────────────────────────────────────────────────
#
# * 不要写 `n=$(grep -c ... || echo 0)`：grep -c 没匹配到时会**同时**
#   输出 0 并返回退出码 1，于是 `|| echo 0` 又补一行，变量变成 "0\n0"，
#   任何比较都不等于 "0" —— 结果所有模式全部误报。统一用 `if grep -qa`。
#
# * 例外不能只判「字符串在不在」：公开版里确实有 "BEGIN RSA PRIVATE KEY"
#   （来自 JSch 库的 PEM 格式标记）。但若因此放行这个串，那么往公开版塞
#   一段真实私钥也能蒙混过关 —— 塞进去之后它当然也「来自库」。
#   所以例外按**出现次数**判（见下）。

set -uo pipefail
cd "$(dirname "$0")/.."

mode="public"
target="build/dafeiyu-controller.apk"
if [ "${1:-}" = "--internal" ]; then
  mode="internal"
  target="${2:-build/dafeiyu-controller-mine.apk}"
elif [ -n "${1:-}" ]; then
  target="$1"
fi

[ -f "$target" ] || { echo "找不到产物：$target" >&2; exit 2; }

echo "检查：$target（$mode）"
echo

# ---------------------------------------------------------------- 工具

# 数某个字节串出现几次（python 比 grep -c 可靠：grep 在二进制上的
# 行计数语义容易踩坑，前面就栽过）
count_bytes() {
  python3 - "$1" "$2" <<'PYEOF'
import sys
data = open(sys.argv[1], "rb").read()
print(data.count(sys.argv[2].encode()))
PYEOF
}

# 抽出所有「形状像真实 IP」的串，排除文档保留段与通用占位
find_bad_ips() {
  python3 - "$1" <<'PYEOF'
import re, sys
data = open(sys.argv[1], "rb").read()
# 找出所有 IPv4 形状的串，并把「属于某个更长点分数字串」的排除掉。
#
# 为什么必须排除：ASN.1 OID 长得就像 IP。例如 JSch 里的
# "1.2.840.113554.1.2.2.1"（Kerberos OID），用 \b 匹配会切出
# "1.2.2.1" 这种片段 —— 报成「泄露了一个 IP」是误报。
# 误报的害处和漏报一样大：会训练人忽略这个检查。
#
# 判据：匹配位置的前后字符如果是数字或点，说明它只是长串的一部分，跳过。
pat = re.compile(rb"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
out = []
for m in sorted(set(pat.findall(data))):
    ip = m.decode()
    if any(int(x) > 255 for x in ip.split(".")):
        continue
    # RFC 5737 文档保留段 —— 文档里举例子用它们，不算泄露
    if ip.startswith(("192.0.2.", "198.51.100.", "203.0.113.")):
        continue
    if ip in ("0.0.0.0", "127.0.0.1", "255.255.255.255", "1.2.3.4"):
        continue
    out.append(ip)
for ip in out:
    print(ip)
PYEOF
}

bad=0

# ---------------------------------------------------------------- ① 结构检查

echo "① 结构检查（不依赖具体值，换服务器也有效）"

# 内部版本来就该内置服务器 IP（那是它的用途），所以这里只对公开版判定；
# 内部版的「有没有 IP」由 ③ 反向检查负责。
if [ "$mode" = "public" ]; then
  while IFS= read -r ip; do
    [ -n "$ip" ] || continue
    echo "  ❌ 含 IP 形状的串：$ip"
    bad=$((bad + 1))
  done < <(find_bad_ips "$target")
else
  echo "  · 内部版允许内置服务器 IP（由 ③ 反向检查确认）"
fi

if grep -qa "\.ts\.net" "$target"; then
  echo "  ❌ 含 Tailscale 域名（.ts.net）"
  bad=$((bad + 1))
fi

for p in "/opt/qqbot" "/opt/dafeiyu"; do
  if grep -qa "$p" "$target"; then
    echo "  ❌ 含服务器路径：$p"
    bad=$((bad + 1))
  fi
done

# 私钥标记：JSch 库提供 1 处格式标记；公开版 ≥2 说明内置了真密钥
keymark=$(count_bytes "$target" "-----BEGIN RSA PRIVATE KEY-----")
keymark2=$(count_bytes "$target" "-----BEGIN OPENSSH PRIVATE KEY-----")
keytotal=$((keymark + keymark2))
if [ "$mode" = "public" ]; then
  if [ "$keytotal" -ge 2 ]; then
    echo "  ❌ 私钥标记出现 $keytotal 处（公开版只应有 1 处，来自 JSch）"
    echo "     多出来的说明内置了真实私钥。"
    bad=$((bad + 1))
  elif [ "$keytotal" = "1" ]; then
    echo "  · 私钥标记 1 处，来自 JSch（PEM 格式标记），放行"
  fi
fi

[ "$bad" = "0" ] && echo "  ✓ 结构检查通过"

# ---------------------------------------------------------------- ② 具体值

# 只在公开版上跑。内部版本来就该含这些值 ——
# 拿同一份清单去查内部版必然全红，那这个检查就没法用了。
PATTERN_FILE=test/desensitize-patterns.local.txt
echo
if [ "$mode" != "public" ]; then
  echo "② 具体值检查：内部版跳过（它本就该含服务器信息）"
elif [ -f "$PATTERN_FILE" ]; then
  echo "② 具体值检查（$PATTERN_FILE）"
  n_pat=0
  while IFS= read -r pat; do
    case "$pat" in ""|\#*) continue ;; esac
    n_pat=$((n_pat + 1))
    if grep -qa "$pat" "$target"; then
      echo "  ❌ 含「$pat」"
      bad=$((bad + 1))
    fi
  done < "$PATTERN_FILE"
  echo "  （查了 $n_pat 个具体值）"
else
  echo "② 具体值检查：跳过（没有 $PATTERN_FILE）"
  echo "   要加历史遗留的具体值，就建这个文件（已 gitignore），一行一个。"
  echo "   它不能进仓库 —— 否则检查脚本自己就把真实值泄了。"
fi

# ---------------------------------------------------------------- ③ 内部版

if [ "$mode" = "internal" ]; then
  echo
  echo "③ 内部版反向检查（连接信息必须齐全，否则装了连不上）"
  if [ "$keytotal" -ge 2 ]; then
    echo "  ✓ 内置了私钥"
  else
    echo "  ❌ 没内置私钥 —— 装了连不上服务器"
    bad=$((bad + 1))
  fi
  ipcount=$(find_bad_ips "$target" | wc -l)
  if [ "$ipcount" -ge 1 ]; then
    echo "  ✓ 内置了服务器地址"
  else
    echo "  ❌ 没内置服务器地址"
    bad=$((bad + 1))
  fi
  if grep -qa "/opt/qqbot" "$target"; then
    echo "  ❌ 混入了生产机信息（/opt/qqbot）"
    bad=$((bad + 1))
  fi
fi

echo
if [ "$bad" = "0" ]; then
  if [ "$mode" = "public" ]; then
    echo "✓ 公开产物零真实信息"
  else
    echo "✓ 内部版自检通过"
  fi
  exit 0
fi
echo "✗ 发现 $bad 处问题 —— 不要发布这个产物。"
exit 1
