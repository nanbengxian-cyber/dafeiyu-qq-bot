#!/usr/bin/env bash
# 构建「内部定制版」APK —— 把服务器连接信息内置进去，构建完立刻还原。
#
# 为什么要有这个脚本，而不是直接改 Preset.java 再 build：
#   公开仓库 / 公开 Release 必须零真实信息（本项目硬规矩）。
#   定制版要「打开就能用」，就得把服务器地址、密钥、口令编进包里。
#   手工改文件最容易出的事故是「改完忘了还原，直接 commit 推上公开仓库」。
#   所以这里把注入→构建→还原做成一个原子流程：还原放在 trap 里，中途失败也会还原。
#
# 用法（在本机跑，不要把真实值写进任何提交的文件）：
#   bash build-preset.sh --host <服务器> --ssh-port <端口> \
#        --ssh-user <账号> --key-file <RSA私钥路径> \
#        --fingerprint <SHA256:...> --token <管理口令> \
#        [--out build/xxx.apk]
#
# 产物：build/dafeiyu-controller-mine.apk（**不要**提交、不要上传公开 Release）
#
# 安全说明（诚实版）：
#   * 这个 APK 里内置了一把能连服务器的密钥。**拿到 APK 的人就能管理
#     服务器上的机器人实例。** 所以只发给信得过的人（比如内部群），
#     不要公开分发。公开版不含任何预设，只能手填自己的服务器。
#   * 内置的账号在服务器上被限制成「只能转发到管理端口」：
#     登不了 shell，也连不了别的端口。这样即使 APK 泄露，
#     损失面也只限于机器人管理，不涉及整台服务器。
#   * 密钥必须是 RSA。JSch 的 ed25519 实现要求 Java 15+，
#     而 Android 的 Ed25519 要 API 33+ —— 用 ed25519 会一直报 Auth fail。

set -euo pipefail
cd "$(dirname "$0")"

HOST=""
SSH_PORT=""
SSH_USER=""
KEY_FILE=""
FINGERPRINT=""
TOKEN=""
OUT="build/dafeiyu-controller-mine.apk"

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="${2:-}"; shift 2 ;;
    --ssh-port) SSH_PORT="${2:-}"; shift 2 ;;
    --ssh-user) SSH_USER="${2:-}"; shift 2 ;;
    --key-file) KEY_FILE="${2:-}"; shift 2 ;;
    --fingerprint) FINGERPRINT="${2:-}"; shift 2 ;;
    --token) TOKEN="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

[ -n "$HOST" ]        || { echo "缺少 --host（服务器地址）" >&2; exit 2; }
[ -n "$SSH_PORT" ]    || { echo "缺少 --ssh-port" >&2; exit 2; }
[ -n "$SSH_USER" ]    || { echo "缺少 --ssh-user" >&2; exit 2; }
[ -n "$KEY_FILE" ]    || { echo "缺少 --key-file（RSA 私钥路径）" >&2; exit 2; }
[ -n "$TOKEN" ]       || { echo "缺少 --token（管理口令）" >&2; exit 2; }
[ -f "$KEY_FILE" ]    || { echo "找不到私钥文件：$KEY_FILE" >&2; exit 2; }

# 密钥必须是 RSA —— 见文件头说明，ed25519 在安卓上用不了。
if ! head -1 "$KEY_FILE" | grep -qE 'BEGIN (RSA |OPENSSH )?PRIVATE KEY'; then
  echo "私钥格式不认识：$(head -1 "$KEY_FILE")" >&2; exit 2
fi
if head -1 "$KEY_FILE" | grep -q "OPENSSH PRIVATE KEY"; then
  if ! ssh-keygen -y -f "$KEY_FILE" 2>/dev/null | head -1 | grep -q "^ssh-rsa"; then
    echo "❌ 这把密钥不是 RSA。安卓上的 JSch 用不了 ed25519（需要 Java 15+）。" >&2
    echo "   重新生成：ssh-keygen -t rsa -b 3072 -m PEM -f <路径> -N ''" >&2
    exit 2
  fi
fi

# ---------------------------------------------------------------- 账号预检
#
# 为什么必须有这一步（2026-09-18 的线上事故）：
#   v1.0.7 / v1.0.8 打包时 --ssh-user 传成了 `dafeiyu`，而服务器上
#   **真实账号是 `dafeiyu-app`**（受限账号建的时候带 -app 后缀）。
#   后果：App 一律连不上，报「服务器不接受这个 App 的密钥」——
#   这句话把用户和排查者都指向「密钥过期」，而真凶是**账号名打错了**。
#   密钥、指纹、口令、端口全都是对的，只有用户名错，所以所有「比对配置」
#   式的自检都发现不了：产物里确实有地址、有指纹、有口令、有私钥。
#
#   教训：**「字段都在」不等于「字段是对的」。** 唯一能证明账号对的，
#   是拿这把钥匙去服务器上真连一次。所以这里在构建前真连。
#
# 怎么判「对」：受限账号的 shell 是 nologin，登录成功后 sshd 会回
#   「This account is currently not available.」—— 这是**成功**的标志
#   （认证过了才轮到 shell 检查）。而账号不存在时 sshd 回
#   「Permission denied (publickey)」/ JSch 回 "Auth fail"。
#   两者要分开判，否则会把正常情况当失败。
if [ "${SKIP_USER_CHECK:-0}" != "1" ]; then
  if command -v ssh >/dev/null 2>&1; then
    echo "→ 预检 SSH 账号 $SSH_USER@$HOST:$SSH_PORT …"
    set +e
    CHECK_OUT="$(ssh -i "$KEY_FILE" \
        -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=15 -o IdentitiesOnly=yes \
        -o PreferredAuthentications=publickey -o LogLevel=ERROR \
        -p "$SSH_PORT" "$SSH_USER@$HOST" true 2>&1)"
    CHECK_RC=$?
    set -e
    case "$CHECK_OUT" in
      *"Permission denied"*|*"Auth fail"*|*"not exist"*)
        echo "❌ 服务器不认这个账号：$SSH_USER" >&2
        echo "   服务器原话：$CHECK_OUT" >&2
        echo >&2
        echo "   这几乎总是**账号名写错了**，不是密钥过期。" >&2
        echo "   去服务器上确认真实账号名（注意可能有 -app 之类的后缀）：" >&2
        echo "     awk -F: '\$1 ~ /dafeiyu/ {print \$1}' /etc/passwd" >&2
        echo "   然后把这个名字原样传给 --ssh-user。" >&2
        exit 2
        ;;
      *"This account is currently not available"*)
        echo "  ✓ 账号存在且密钥可用（shell 被限制成 nologin，符合预期）"
        ;;
      "")
        # 没有任何输出 + 退出码 0：认证通过且 shell 正常退出。
        echo "  ✓ 账号预检通过（连上了，且没有任何报错）"
        ;;
      *)
        # ★ 关键：**没连上**（超时/拒绝/域名解析不了）时绝不能当成通过。
        #
        # 这里踩过一个真坑，必须留记录（2026-09-18）：
        #   原来这里是 `*) echo "✓ 账号预检通过"` —— 一个兜底「通过」。
        #   结果服务器端口被拒（Connection refused，ssh 退出码 255）时，
        #   预检照样打印「✓ 账号预检通过」，然后照样打包。
        #   也就是说：**这道用来防「连不上」的检查，在真的连不上时是绿的。**
        #   这和它要防的那次事故（v1.0.7 账号写错、自检全绿）是同一类错误 ——
        #   兜底分支永远不该是「通过」。
        #   实测证据：端口 10313 被拒时，旧版输出「✓ 账号预检通过」并产出了 APK。
        #
        # 判据改成 fail-closed：只有「明确的成功信号」才算通过，
        # 其余一切（包括看不懂的输出）都当失败。
        if [ "$CHECK_RC" -ne 0 ]; then
          echo "❌ 预检没能连上服务器 —— 这**不能**当作「账号没问题」。" >&2
          echo "   ssh 退出码：$CHECK_RC" >&2
          echo "   ssh 原话：$CHECK_OUT" >&2
          echo >&2
          echo "   常见原因：" >&2
          echo "     * 本机 IP 被服务器防火墙/防爆破（fail2ban）临时封了；" >&2
          echo "     * 端口填错、或服务器换了端口；" >&2
          echo "     * 网络不通。" >&2
          echo "   先确认这个端口现在**真的能连上**再打包 —— 否则你打出来的包" >&2
          echo "   和「账号写错」的包一样，用户装上一律连不上。" >&2
          exit 2
        fi
        echo "❌ 预检结果看不懂，按失败处理（不能把不确定当通过）。" >&2
        echo "   ssh 原话：$CHECK_OUT" >&2
        exit 2
        ;;
    esac
  else
    echo "警告：本机没有 ssh 命令，跳过账号预检。" >&2
    echo "      账号名写错会让用户一律连不上，且报错指向「密钥过期」——" >&2
    echo "      强烈建议在能跑 ssh 的机器上构建，或手工确认账号名。" >&2
  fi
else
  echo "警告：SKIP_USER_CHECK=1，跳过账号预检（仅用于离线复现构建）。" >&2
fi

# 指纹强烈建议填：不填就没法防中间人
if [ -z "$FINGERPRINT" ]; then
  echo "警告：没提供 --fingerprint，App 将不校验服务器身份。" >&2
  echo "      这意味着中间人可以假冒服务器，拿走隧道里的口令和 API Key。" >&2
  echo "      强烈建议填上（用 ssh-keyscan -p <端口> <地址> | ssh-keygen -lf - 取）。" >&2
else
  case "$FINGERPRINT" in
    SHA256:*) ;;
    *) echo "指纹格式应为 SHA256:... 开头" >&2; exit 2 ;;
  esac
fi

PRESET="app/src/com/dafeiyu/controller/Preset.java"
BACKUP="$(mktemp)"
cp "$PRESET" "$BACKUP"
# 无论成功失败都还原：这是本脚本存在的全部意义。
restore() {
  cp "$BACKUP" "$PRESET"
  rm -f "$BACKUP"
  echo "已还原 $PRESET（公开仓库保持零真实信息）"
}
# ★ 这里**故意不写** `trap restore EXIT`：
#   全文只能有**一个** EXIT trap（下面那个 cleanup_all），因为 bash 里
#   后写的会覆盖先写的。写两个 = 还原静默失效，理由见下。

# ★ 踩过的坑（2026-09-18，测试当场抓到 11 项失败）：
#   脚本后半段为了让构建日志可读，又写了一个 `trap 'rm -f $LOG' EXIT`，
#   它**覆盖**了上面的 restore —— 于是服务器地址、SSH 私钥、管理口令
#   全部留在 app/src/.../Preset.java 里，下一次 commit 就会推到公开仓库。
#   当时是测试先炸出来的，不是人看出来的。
#
#   教训：EXIT trap 只能有一个，谁在后面写谁赢。
#   所以这里**不再**加第二个 EXIT trap，改成一个统一的清理钩子：
#   后面所有需要在退出时清理的东西都登记到 EXTRA_CLEAN 里，
#   由唯一的 restore 顺带处理。这样新增清理逻辑不会再破坏还原。
EXTRA_CLEAN=""
cleanup_all() {
  restore
  # 逐个删登记过的临时文件（路径可能含空格，用 while read 而不是 for）
  if [ -n "$EXTRA_CLEAN" ]; then
    printf '%s\n' "$EXTRA_CLEAN" | while IFS= read -r f; do
      [ -n "$f" ] && rm -f "$f" 2>/dev/null
    done
  fi
}
trap cleanup_all EXIT

# 私钥内容要嵌进 Java 字符串字面量：转义反斜杠和引号。
# PEM 里一般只有 base64 和换行，但转义一下更稳妥。
KEY_ESCAPED="$(python3 - "$KEY_FILE" <<'PY'
import sys
raw = open(sys.argv[1], encoding="utf-8").read().strip()
# Java 字符串字面量里不能有裸换行，用 \n 转义
out = raw.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
print(out)
PY
)"

# 注入：写成一个含真实连接信息的 Preset.java
cat > "$PRESET" <<JAVA
package com.dafeiyu.controller;

/**
 * 定制版预设 —— 本文件由 build-preset.sh 在**本地构建时**临时生成，
 * 构建结束会立即还原为空模板。**不要提交这个版本。**
 *
 * 里面装了：服务器地址、SSH 端口、专用账号、专用账号的 RSA 私钥、
 * 服务器指纹、管理口令。
 *
 * 安全边界（写在这里免得后来人踩）：拿到这个 APK 的人就能管理
 * 服务器上的机器人实例。专用账号在服务器上被限制成「只能转发到
 * 管理端口」，所以损失面限于机器人管理，不涉及整台服务器。
 */
public final class Preset {

    private Preset() {
    }

    public static final boolean HAS_PRESET = true;

    public static final String HOST = "$HOST";

    public static final int SSH_PORT = $SSH_PORT;

    public static final String SSH_USER = "$SSH_USER";

    public static final String SSH_KEY =
            "$KEY_ESCAPED";

    public static final String HOST_FINGERPRINT = "$FINGERPRINT";

    public static final String MANAGER_TOKEN = "$TOKEN";

    public static final String HINT = "内部版：已内置服务器，点「连接」即可。";

    /** 预设是否完整可用。 */
    public static boolean usable() {
        return HAS_PRESET && HOST.length() > 0 && SSH_PORT > 0
                && SSH_USER.length() > 0 && SSH_KEY.length() > 0
                && MANAGER_TOKEN.length() > 0;
    }

    // 旧版字段（保留以免破坏既有脚本与测试）
    public static final String WEBUI_BASE = "";
    public static final String TOKEN_SALT = "";
    public static final String TOKEN_IV = "";
    public static final String TOKEN_CT = "";
}
JAVA

echo "→ 构建内部版 APK…"
# 两个环境变量的作用：
#   DSH_PRESET_BUILD=1 —— 告诉 build.sh「这是内部版」，跳过「不许含内置地址」的公开版闸门；
#   OUT_APK           —— 产物写到独立文件名，**绝不覆盖**公开版产物
#                        （否则定制包会躺在公开版的路径上，极易误发）。
mkdir -p "$(dirname "$OUT")"
# ★ 日志必须用 mktemp，不能用固定的 /tmp/preset-build.log。
#   踩过：固定路径下如果已存在一个**别人拥有**的同名文件，
#   重定向会在 build.sh 启动**之前**就失败（Permission denied），
#   而 `||` 分支会 tail 那个**上一次的旧日志** ——
#   于是屏幕上打的是「构建失败」+ 一份看起来成功的旧日志。
#   这比不报错更糟：既没构建，又给了你一个假的好消息。
BUILD_LOG="$(mktemp -t preset-build.XXXXXX.log)" || {
  echo "建不了临时日志文件，中止。" >&2; exit 1; }
# 登记到统一的清理钩子里（**不要**在这里写 trap ... EXIT，理由见上面 restore 附近）。
EXTRA_CLEAN="$EXTRA_CLEAN
$BUILD_LOG"
if ! KS_PASS="${KS_PASS:-dafeiyu2026}" DSH_PRESET_BUILD=1 OUT_APK="$OUT" \
     bash build.sh >"$BUILD_LOG" 2>&1; then
  echo "构建失败，日志尾部：" >&2
  tail -20 "$BUILD_LOG" >&2
  exit 1
fi

echo "→ 内部版产物：$OUT"
ls -la "$OUT"

# ---- 构建后自检 ----
fail=0
CHECK_DEX="$(mktemp)"
# ① 私钥必须真的进去了（否则装上连不上）
#
# 注意：classes.dex 在 zip 里是压缩的，`unzip -p | grep` 有时取不到，
# 会误报。直接在整个 APK 上 grep -a（原始字节搜索）才准 ——
# 一个「总是报警」的检查会训练人忽略警告，比没有检查更糟。
if ! grep -qa "BEGIN RSA PRIVATE KEY" "$OUT"; then
  echo "❌ 产物里没有私钥 —— 装上连不上服务器。" >&2
  fail=1
fi
# ② 管理口令必须在里面
if ! grep -qa "$TOKEN" "$OUT"; then
  echo "❌ 产物里没有管理口令 —— 装了也连不上。" >&2
  fail=1
fi
# ②b 主机地址必须在里面
if ! grep -qa "$HOST" "$OUT"; then
  echo "❌ 产物里没有服务器地址。" >&2
  fail=1
fi
# ③ 指纹必须在里面
if [ -n "$FINGERPRINT" ] && ! grep -qa "${FINGERPRINT#SHA256:}" "$OUT"; then
  echo "❌ 产物里没有服务器指纹。" >&2
  fail=1
fi
# ④ 公开版字段必须仍为空（防止把旧的加密 Token 路径带进来）
if grep -qa "TOKEN_CT = \"[A-Za-z0-9+/]" "$PRESET" 2>/dev/null; then
  echo "⚠ 检测到旧版 TOKEN_CT 有值（本次不应出现）"
fi
# ⑤ ★ 账号名必须真的进了产物，而且**必须精确匹配**。
#
# 这条是 2026-09-18 事故的直接补丁：当时产物里地址、指纹、口令、私钥
# 一应俱全，自检全绿，但账号名是 `dafeiyu` 而不是 `dafeiyu-app` ——
# 于是所有用户都连不上，报错还指向「密钥过期」。
#
# 为什么不用 grep -qa "$SSH_USER"：`dafeiyu` 是 `dafeiyu-app` 的前缀，
# 子串匹配在**错的那一边也会通过**（包里到处是 dafeiyu-astrbot 这类串）。
# 所以这里改成从 dex 里把 SSH_USER 常量**读出来比对**，而不是搜子串。
#
# ★★ 这道检查自己出过一次「假绿灯」，记在这里防止再犯（2026-09-18）：
#   第一版是用 `re.finditer(rb'[A-Za-z0-9][A-Za-z0-9_.-]{2,31}', d)` 在
#   **原始字节**上正则扫，然后要求候选里含 `$SSH_USER`。它看起来对，
#   实际是坏的：那个字符集**包含斜杠和点**，于是 dex 里成百上千个
#   `Lcom/dafeiyu/controller/Foo;` 会被切成 `...dafeiyu` 这样的片段，
#   让「dafeiyu」变成一个候选 —— 于是拿错账号（dafeiyu）打的包
#   自检**照样全绿**，正是这道检查要防的那次事故。
#   实测：故意用 --ssh-user dafeiyu 构建，旧版自检输出
#   「✓ 自检：SSH 账号 dafeiyu 在产物里」并留下了坏包。
#
#   修法（两层）：
#     ① 正经解析 dex 字符串表（ULEB128），不再在原始字节上乱切；
#     ② 账号名必须**整串等于**候选，且候选里不允许出现
#        `/ . $ : _` 这些「这是包名/类名/字段名，不是账号」的字符。
if unzip -p "$OUT" classes.dex > "$CHECK_DEX" 2>/dev/null && [ -s "$CHECK_DEX" ]; then
  ACTUAL_USER="$(python3 - "$CHECK_DEX" <<'PY' 2>/dev/null || true
import re, struct, sys

def read_uleb(d, o):
    r = 0; s = 0
    while True:
        b = d[o]; o += 1
        r |= (b & 0x7f) << s
        if not (b & 0x80):
            break
        s += 7
    return r, o

raw = open(sys.argv[1], "rb").read()
if raw[:4] != b"dex\n":
    raise SystemExit(0)
n, off = struct.unpack_from("<II", raw, 0x38)
# 账号名：全小写字母/数字/连字符。**刻意排除** / . $ _ : 空格 ——
# 带上它们就会把 Lcom/dafeiyu/controller 这类包名也当成候选，
# 而那正是让错账号蒙混过关的原因。
ACCT = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
cands = set()
for i in range(n):
    p = struct.unpack_from("<I", raw, off + 4 * i)[0]
    ln, p = read_uleb(raw, p)
    s = raw[p:p + ln].decode("utf-8", "replace")
    if ACCT.match(s):
        cands.add(s)
print(" ".join(sorted(cands)))
PY
)"
  case " $ACTUAL_USER " in
    *" $SSH_USER "*)
      echo "  ✓ 自检：产物里内置的账号 = $SSH_USER（与 --ssh-user 一致）"
      ;;
    *)
      echo "❌ 产物里的 SSH 账号对不上。" >&2
      echo "   期望：$SSH_USER" >&2
      echo "   产物里出现：${ACTUAL_USER:-（没找到任何候选）}" >&2
      echo "   账号名错会让用户一律连不上，且报错指向「密钥过期」——" >&2
      echo "   这正是 v1.0.7 / v1.0.8 的事故原因。" >&2
      fail=1
      ;;
  esac
else
  echo "❌ 解不出产物里的 classes.dex，无法核对账号名。" >&2
  fail=1
fi
rm -f "$CHECK_DEX"

# ★ 说清这道自检**能证明什么、不能证明什么** —— 这一点必须写死在这里，
#   因为上次的事故就是「自检全绿」给了人虚假的安全感：
#
#     它能证明：--ssh-user 传的那个值**真的进了产物**。
#               （防的是「Preset.java 被还原/构建用了旧文件」这类问题）
#     它不能证明：那个值**是对的**。
#               账号名写错时，产物里当然还是那个错名字，
#               自己跟自己比永远相等 —— 这是**同义反复**，
#               不是校验。上次 `dafeiyu` 的包自检全绿就是这个原因。
#
#   唯一能证明账号对的是**拿这把钥匙去服务器上真连一次**（上面的预检）。
#   所以跳过预检时，这里必须把话说得很重，不能让人以为「自检过了就没事」。
if [ "${SKIP_USER_CHECK:-0}" = "1" ]; then
  echo
  echo "⚠️  本次跳过了账号预检（SKIP_USER_CHECK=1）。" >&2
  echo "    上面的自检只证明「你传的账号进了产物」，**不能证明它是服务器上真实存在的账号**。" >&2
  echo "    账号名写错时用户会一律连不上，而报错会误导向「密钥过期」。" >&2
  echo "    这个包**不要发给别人**，除非你已经另行确认过账号名。" >&2
  echo
fi

[ "$fail" = "0" ] || { echo "自检未通过，已删除产物。" >&2; rm -f "$OUT"; exit 1; }
echo "✓ 自检：口令、指纹都在产物里"
echo "✓ 自检：服务器已内置（$HOST）"
echo
echo "提醒：这个 APK 能管理服务器上的机器人实例，只发给信得过的人。"

# ★★★ 最后一道防线：**立刻**在这里还原，而不是只等 EXIT trap。★★★
#
# 为什么不能只靠 trap：
#   bash 的 EXIT trap 只有**一个**，谁最后写谁赢。脚本前面注册了
#   cleanup_all（里面会调 restore），但只要有人在它**之后**又写一个
#   `trap ... EXIT`，还原就再也不会发生 —— 而屏幕上一切正常。
#
#   我本人就是这样把服务器地址、SSH 私钥、管理口令留在了
#   app/src/.../Preset.java 里（2026-09-18，测试报出 11 项失败才发现）。
#   变异测试确认：只加「统一的清理钩子」挡不住后面新加的 trap。
#
# 所以这里做**显式还原**：不依赖任何 trap 语义，执行到这就一定还原。
# 顺带把 EXIT trap 清掉，避免退出时再跑一次（跑两次无害，但没必要）。
restore
trap - EXIT
# 再核一次：源码树里绝不允许残留真实凭据。
# 只还原不核对是不够的 —— 万一路径变了、备份坏了，还原会「成功」
# 但内容还是脏的，而你不会知道。
if grep -qE 'BEGIN RSA PRIVATE KEY|HOST *= *"[0-9]|MANAGER_TOKEN *= *"[A-Za-z0-9_-]{20,}' "$PRESET" 2>/dev/null; then
  echo "❌ 严重：$PRESET 里仍残留真实凭据，还原失败！" >&2
  echo "   千万不要 commit，先手工把它清空。" >&2
  exit 4
fi
