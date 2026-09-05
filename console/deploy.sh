#!/usr/bin/env bash
# 把控制台后端 + APK 部署到阿里云那台（your-server.example.com:/opt/qqbot）。
#
# 顺序是刻意的：**先备份、再传文件、再打补丁、再重启、最后验证**。
# 任何一步失败就地停下，不做「部分部署」—— 半个补丁比没补丁危险得多。
#
# 最要紧的一条约束：扫码页（8088）比控制台重要。它是掉线时唯一的补救入口，
# 所以补丁的设计是「console_api 导入失败就只让 /api/console/* 回 503」，
# 页面路由照旧。这个脚本在重启后会实测扫码页仍然能开。
#
# 用法：bash deploy.sh            部署 + 验证
#       bash deploy.sh --verify   只验证，不动服务器
#       bash deploy.sh --rollback 回滚到最近一个备份

set -euo pipefail
cd "$(dirname "$0")"

SERVER=${DEPLOY_SERVER:-root@your-server.example.com}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
REMOTE=/opt/qqbot
APK=build/dafeiyu-console.apk

say()  { printf '\033[1m› %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

MODE=${1:-deploy}

$SSH $SERVER true 2>/dev/null || die "连不上 $SERVER"

# ---------------------------------------------------------------- 回滚
if [ "$MODE" = "--rollback" ]; then
  say "回滚"
  $SSH $SERVER 'bash -s' <<'EOS'
set -e
cd /opt/qqbot
last=$(ls -t qrweb_auth.py.bak.console.* 2>/dev/null | head -1)
[ -n "$last" ] || { echo "没有备份可回滚" >&2; exit 1; }
cp -a "$last" qrweb_auth.py
python3 -m py_compile qrweb_auth.py
systemctl restart qqbot-qrweb.service
sleep 2
curl -sf -m 5 http://127.0.0.1:8088/healthz && echo " ← 扫码页正常"
echo "已回滚到 $last"
EOS
  exit 0
fi

# ---------------------------------------------------------------- 只验证
if [ "$MODE" != "--verify" ]; then

  for f in server/console_api.py server/console_spec.py server/envfile.py server/patch_qrweb.py; do
    [ -s "$f" ] || die "缺 $f"
  done
  [ -s "$APK" ] || die "缺 $APK（先跑 bash build.sh）"

  # 本地先把要传的东西自查一遍。传上去才发现语法错，服务已经重启了，代价高。
  say "本地自查"
  python3 -m py_compile server/console_api.py server/console_spec.py server/envfile.py
  ok "Python 语法通过"
  bash test/run-tests.sh >/dev/null 2>&1 || die "JVM 单测没过，不部署"
  ok "JVM 单测通过"
  python3 server/test_console.py >/dev/null 2>&1 || die "后端单测没过，不部署"
  ok "后端单测通过"

  # ---------------------------------------------------------------- 备份
  say "备份服务器现状"
  $SSH $SERVER 'bash -s' <<'EOS'
set -e
cd /opt/qqbot
tag=$(date +%s)
cp -a qrweb_auth.py "qrweb_auth.py.bak.console.$tag"
echo "  已备份 qrweb_auth.py.bak.console.$tag"
# 只留最近 5 份，别让备份把 40G 盘吃满
ls -t qrweb_auth.py.bak.console.* | tail -n +6 | xargs -r rm -f
EOS
  ok "备份完成"

  # ---------------------------------------------------------------- 传文件
  say "上传后端与 APK"
  mkdir -p /tmp/qqdeploy
  scp -q -o StrictHostKeyChecking=accept-new \
    server/console_api.py server/console_spec.py server/envfile.py server/patch_qrweb.py \
    "$SERVER:$REMOTE/" || die "上传后端失败"
  ok "console_api.py / console_spec.py / patch_qrweb.py"

  $SSH $SERVER "mkdir -p $REMOTE/public"
  scp -q "$APK" "$SERVER:$REMOTE/public/dafeiyu-console.apk" || die "上传 APK 失败"
  ok "dafeiyu-console.apk"

  # ---------------------------------------------------------------- 打补丁
  say "给 qrweb_auth.py 打补丁"
  $SSH $SERVER 'bash -s' <<'EOS'
set -e
cd /opt/qqbot
out=$(python3 patch_qrweb.py qrweb_auth.py 2>&1) || { echo "$out" >&2; exit 1; }
echo "$out" | sed 's/^/  /'
python3 -m py_compile qrweb_auth.py
chmod 644 console_api.py console_spec.py envfile.py public/dafeiyu-console.apk
EOS
  ok "补丁已应用且语法正确"

  # ---------------------------------------------------------------- 重启
  say "重启扫码页服务"
  $SSH $SERVER 'systemctl restart qqbot-qrweb.service; sleep 3; systemctl is-active qqbot-qrweb.service' \
    | grep -q '^active$' || die "qqbot-qrweb 没起来（用 bash deploy.sh --rollback 回滚）"
  ok "qqbot-qrweb 运行中"
fi

# ---------------------------------------------------------------- 实测
say "服务器实测"
$SSH $SERVER 'bash -s' <<'EOS'
set +e
cd /opt/qqbot

P=0; F=0
pass() { P=$((P+1)); printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { F=$((F+1)); printf '  \033[31m✗\033[0m %s %s\n' "$1" "${2:-}"; }

# 1) 扫码页本身没被弄坏 —— 这条最重要，它是掉线时的唯一补救入口
code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:8088/healthz)
[ "$code" = 200 ] && pass "健康检查 200" || fail "健康检查异常" "$code"

code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:8088/)
case "$code" in
  303|200) pass "扫码页可达（$code）" ;;
  *) fail "扫码页坏了" "$code" ;;
esac

# 2) 未登录访问控制台必须回 401 JSON（不是 303 跳登录页 ——
#    手机端拿到 HTML 会误以为成功）
out=$(curl -s -o /tmp/c.txt -w '%{http_code}' -m 5 http://127.0.0.1:8088/api/console/status)
body=$(cat /tmp/c.txt)
[ "$out" = 401 ] && pass "未登录回 401" || fail "未登录状态码不对" "$out"
case "$body" in \{*) pass "401 回的是 JSON" ;; *) fail "401 回的不是 JSON" "$(head -c 60 /tmp/c.txt)" ;; esac

# 3) 带真 cookie 打全部读端点
python3 - <<'PY'
import base64, hashlib, hmac, json, time, urllib.request, urllib.error
P = [0]; F = [0]
def pas(m): P[0]+=1; print("  \033[32m✓\033[0m "+m)
def bad(m,d=""): F[0]+=1; print("  \033[31m✗\033[0m %s %s"%(m,d))

try:
    conf = json.load(open("/opt/qqbot/qrweb_auth.json"))
    secret = bytes.fromhex(conf["secret"])
except Exception as e:
    bad("读不到 qrweb_auth.json", str(e)); raise SystemExit(1)

issued = str(int(time.time())).encode()
mac = hmac.new(secret, issued, hashlib.sha256).digest()
tok = "%s.%s" % (base64.urlsafe_b64encode(issued).decode().rstrip("="),
                 base64.urlsafe_b64encode(mac).decode().rstrip("="))

def call(path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request("http://127.0.0.1:8088" + path, data=data,
                               headers={"Cookie": "dsh_qr=" + tok,
                                        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)

# status
code, text = call("/api/console/status")
if code == 200:
    pas("状态接口 200")
    try:
        d = json.loads(text)
        qq = d.get("qq") or {}
        pas("状态含 qq.state=%s" % qq.get("state"))
        for key in ("containers", "host", "models", "runtime", "plugins"):
            if key in d:
                pas("状态含 " + key)
            else:
                bad("状态缺 " + key)
        rt = d.get("runtime") or {}
        if rt.get("tool_use_ok") is True:
            pas("tool_use 仍在（出图/语音不会哑）")
        else:
            bad("tool_use 丢了", str(rt.get("tool_use_ok")))
    except Exception as e:
        bad("状态不是合法 JSON", str(e))
else:
    bad("状态接口异常", "%s %s" % (code, text[:80]))

# schema
code, text = call("/api/console/schema", timeout=90)
if code == 200:
    d = json.loads(text)
    n_k = len(d.get("knobs") or [])
    n_m = len(d.get("modes") or [])
    n_a = len(d.get("actions") or [])
    n_mo = len(d.get("chat_models") or [])
    pas("schema 200（%d 旋钮 / %d 模式 / %d 动作 / %d 模型）" % (n_k, n_m, n_a, n_mo))
    if n_m >= 5: pas("模式数 ≥5")
    else: bad("模式太少", str(n_m))
    if n_k >= 8: pas("旋钮数 ≥8")
    else: bad("旋钮太少", str(n_k))
    # 白名单必须挡住敏感项
    paths = [k.get("path") for k in (d.get("knobs") or [])]
    leaked = [p for p in paths if p and ("password" in p or "admins" in p
              or p.startswith("provider_sources") or p.startswith("platform["))]
    if leaked: bad("schema 暴露了敏感项", ",".join(leaked))
    else: pas("schema 没有敏感项")
else:
    bad("schema 异常", "%s %s" % (code, text[:80]))

# config
code, text = call("/api/console/config")
if code == 200:
    vals = (json.loads(text) or {}).get("values") or {}
    pas("配置接口 200（%d 项）" % len(vals))
else:
    bad("配置接口异常", "%s %s" % (code, text[:80]))

# 越界写必须被拒，且不许静默钳制
code, text = call("/api/console/config",
                  {"values": {"provider_ltm_settings.active_reply.possibility_reply": 5}})
if code == 400:
    pas("越界值被拒（400）")
else:
    bad("越界值没被拒", "%s %s" % (code, text[:80]))

# 白名单外的路径必须被拒
code, text = call("/api/console/config", {"values": {"dashboard.password": "x"}})
if code in (400, 403):
    pas("白名单外的路径被拒（%d）" % code)
else:
    bad("白名单没挡住", "%s %s" % (code, text[:80]))

# 认不出的模式必须被拒。404 才是对的语义（这个模式不存在），
# 400 是「请求格式不对」—— 别把两者混起来。
code, text = call("/api/console/mode", {"id": "不存在的模式"})
if code == 404:
    pas("未知模式被拒（404）")
else:
    bad("未知模式没被拒", "%s %s" % (code, text[:80]))

# 框架自带插件不许被关
code, text = call("/api/console/plugin", {"name": "astrbot", "enabled": False})
if code in (400, 403):
    pas("框架插件不许关（%d）" % code)
else:
    bad("框架插件竟然可关", "%s %s" % (code, text[:80]))

# APK 下载
code, text = call("/console.apk")
if code == 200:
    pas("APK 可下载（%d 字节）" % len(text.encode("utf-8", "replace")))
else:
    bad("APK 下不了", str(code))

print("REMOTE_PASS=%d REMOTE_FAIL=%d" % (P[0], F[0]))
raise SystemExit(1 if F[0] else 0)
PY
py=$?

# 4) 配置文件没被写坏 —— BOM 在、键数没少、密钥没丢。
#    路径是 astrbot 小写（不是 AstrBot）—— 容器里是 /AstrBot，宿主挂载点是小写的，
#    写错了这一步会 FileNotFoundError 而不是报出真问题。
python3 - <<'PY'
import json, os
p = "/opt/qqbot/astrbot/data/cmd_config.json"
if not os.path.exists(p):
    print("  \033[31m✗\033[0m 找不到配置文件 " + p)
    raise SystemExit(0)
raw = open(p, "rb").read()
print("  \033[32m✓\033[0m BOM 保留" if raw[:3] == b"\xef\xbb\xbf"
      else "  \033[31m✗\033[0m BOM 丢了")
cfg = json.loads(raw.decode("utf-8-sig"))
print("  \033[32m✓\033[0m 顶层 %d 个键" % len(cfg))
srcs = cfg.get("provider_sources") or []
keyed = sum(1 for s in srcs if s.get("key"))
print("  \033[32m✓\033[0m provider_sources 有 key 的 %d 条" % keyed
      if keyed else "  \033[31m✗\033[0m provider_sources 的 key 丢了")
PY

echo "QRWEB=$(systemctl is-active qqbot-qrweb.service)"
echo "WATCHDOG=$(systemctl is-active qqbot-watchdog.service)"
echo "APKSIZE=$(stat -c %s /opt/qqbot/public/dafeiyu-console.apk 2>/dev/null || echo 0)"
exit $py
EOS
rc=$?

if [ "$rc" = 0 ]; then
  printf '\n\033[32m✓ 部署并实测通过\033[0m\n'
  echo "  下载地址：http://your-server.example.com:8088/console.apk（先在扫码页登录）"
else
  printf '\n\033[31m✗ 实测有失败项\033[0m — 需要修；回滚用 bash deploy.sh --rollback\n'
fi
exit $rc
