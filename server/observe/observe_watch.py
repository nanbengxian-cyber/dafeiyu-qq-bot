# -*- coding: utf-8 -*-
"""QQ 机器人回复质量观察窗（v1）。

定时（cron 每 5 分钟）扫描 astrbot 日志窗口，检测四类问题：
  R1 同人双回复/工具收尾艾特错位（本次"新表情包来了"式错误）
  R2 主模型中转 429 限流
  R3 主动回复失败（Traceback）
  R4 识图失败拖慢（vischain 档全挂）
  R5 napcat 掉线（Login Error / 二维码循环；只告警，不自动重启——扫码须人工）

有错就改：
  R1 复发（24h 内 >=3 次）-> 自动给人格追加强化约束（幂等）+ 重启 + 邮件
  R2~R4 -> 记录 + 邮件告警（外部服务问题，无法自动修）

状态存 /opt/qqbot/observe/state.json，告警带冷却防刷屏。
"""
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

BASE = "/opt/qqbot/observe"
LOG = "/opt/qqbot/astrbot/data/logs/astrbot.log"
STATE = os.path.join(BASE, "state.json")
ISSUES = os.path.join(BASE, "issues.json")
NOTIFY = "/opt/qqbot/notify_mail.py"
PATCH = "/opt/qqbot/observe/patch_observe.py"
WINDOW_MIN = 10          # 看最近 10 分钟
PAIR_WINDOW_S = 25       # 工具收尾双回复时间窗（拆句/分段秒级，正常回复>25s）
TOOLCALL = re.compile(r"使用工具：|Agent 使用工具|Tool `[a-z_]+` Result|工具调用生图|工具调用语音|工具调用视频")
ALERT_COOLDOWN_S = 1800  # 同类问题告警冷却 30 分钟

# [R5 2026-09-14] napcat 掉线探针。2026-09-14 07:33 掉线 3.5h 无告警的补丁。
# 现场校准（踩坑记录）：
#   ① Login Error 是间歇性的：掉线后并不是每分钟都报，12 分钟窗可能 0 次。
#      窗口必须放宽到 30 分钟。
#   ② docker logs 需要 root：observe timer 以 root 跑没问题，但手工验证
#      时必须 sudo，否则拿到空输出会误判「探针坏了」。
#   ③ 判据用双证据：Login Error >=1 **且** 二维码出现（只有掉线等扫码才会
#      打二维码）。单看任何一个都会抖——Error 可能只是重连抖动，二维码
#      刷一次也可能是扫码瞬间的正常打印。
R5_LOGIN_ERR_RE = re.compile(r"Login Error|登录超时|token 失效")
R5_QRCODE_RE = re.compile(r"二维码|qrcode|扫码")
R5_WINDOW_MIN = 30
R1_AUTOFIX_THRESHOLD = 3  # 24h 内 R1 次数达到即自动修

# [fix:r1-restart-loop-v1 2026-09-13] 「补丁在但仍复发」时允许的重启次数上限。
#
# 原实现：`elif patch_applied and now - last_patch > 7200: 重启`。
# 意图是「补丁打过了问题还在，那就重启确认一下」。但补丁已在位时重启
# **不会改变任何一行代码**，R1 自然也不会因此消失 —— 于是这条分支成了永动机：
# 2026-09-13 回查 journalctl，连续 15 次 autofix=True，间隔稳定在
# 2h00m~2h05m（= 7200s 冷却 + 5 分钟一次的 timer 粒度），12 次/天，
# 而 24h 内 R1 照样发生 15 次 —— 一次都没被它治好。
# 每次重启的净效果只有掉线：qq_watchdog 日志里整齐对应着
# online -> degraded -> online，收发通道断 10~21 秒，群里就是「打到一半不理人」。
# 所以：每代补丁最多给 R1_RETRY_MAX 次「确认加载」重启，之后只告警不重启。
R1_RETRY_MAX = int(os.environ.get("OBSERVE_R1_RETRY_MAX", "1"))
R1_RETRY_COOLDOWN_S = 7200  # 那次重试的冷却，与原 7200 秒语义一致

ANSI = re.compile(r"\x1b\[[0-9;]*m")
TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\.\d{3}\]")
SEND = re.compile(r"Prepare to send - (.*?): (.*)$")
MENTION = re.compile(r"\[mention\] at=True 用了工具\(([^,]+)")
RETRY429 = re.compile(r"Request failed with retryable error; retrying \(\d+/\d+\): Error code: 429|当前访问量过大")
FAILED = re.compile(r"主动回复失败")
VISFAIL = re.compile(r"vischain\] .*档全挂|vischain\] .*识图放弃")


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_marker": None, "r1_count": [], "alerts": {}}


def save_state(st):
    os.makedirs(BASE, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def tail_log(n=2000):
    try:
        out = subprocess.run(["tail", "-n", str(n), LOG],
                             capture_output=True, text=True).stdout
        return out.splitlines()
    except Exception as e:
        print("tail fail:", e)
        return []


def send_mail(subject, body):
    try:
        subprocess.Popen(
            ["/usr/bin/python3", NOTIFY, subject, "-"],
            stdin=subprocess.PIPE,
        ).communicate(body.encode("utf-8"), timeout=10)
        print("mail sent:", subject)
    except Exception as e:
        print("mail fail:", e)


def restart_astrbot():
    """重启 AstrBot 容器。

    单独包一层是为了能被单测替换掉 —— 决策逻辑必须能在不碰生产的前提下验证。
    """
    subprocess.run(["docker", "restart", "astrbot"], capture_output=True)


def patch_fp():
    """补丁脚本自身的指纹（mtime+size）。脚本被改过 = 新一代补丁。

    只看 patch_applied 这一个布尔量的话，补丁脚本将来升级了也永远走不到
    「首次应用」分支 —— 因为那个标志一旦为真就再没被清过。用指纹判断换代，
    新补丁才能重新拿到一次「应用后重启」。
    """
    try:
        s = os.stat(PATCH)
        return "%d-%d" % (int(s.st_mtime), s.st_size)
    except OSError:
        return "missing"


def r1_autofix(st, changed, fp, now=None):
    """R1 复发的自动修复决策。

    纯状态机：只改 st 并返回 (fixed, report_line)。所有 docker 交互都走
    restart_astrbot()，所以可以在容器外直接跑单测。

    changed: 本次补丁脚本是否真的改写了插件文件（False = 补丁早已在位）。
    """
    now = time.time() if now is None else now
    if st.get("patch_fp") != fp:
        seen_before = "patch_fp" in st
        st["patch_fp"] = fp
        # 老 state 没有 patch_fp：那种情况下过去的重启早就用滥了（实测 15 次），
        # 迁移时直接记成「额度已用完」，免得升级当天又白重启一次。
        st["patch_retry_used"] = 0 if seen_before else R1_RETRY_MAX

    if changed:
        # 本次真的改了文件 -> 必须重启才能加载新代码。这一次重启是必要的。
        st["patch_applied"] = True
        st["patch_applied_at"] = now
        st["patch_last"] = now
        st["patch_retry_used"] = 0
        restart_astrbot()
        return True, "AUTO-FIX: 插件补丁已应用 + 容器已重启"

    if st.get("patch_retry_used", 0) < R1_RETRY_MAX and \
            now - st.get("patch_applied_at", 0) > R1_RETRY_COOLDOWN_S:
        # 补丁已在位但仍复发：只给 R1_RETRY_MAX 次「确认加载」重启。
        st["patch_applied"] = True
        st["patch_retry_used"] = st.get("patch_retry_used", 0) + 1
        st["patch_last"] = now
        restart_astrbot()
        return True, ("AUTO-FIX(2nd): 补丁已存在仍复发，重启确认加载"
                      "（第 %d/%d 次，之后不再重启）"
                      % (st["patch_retry_used"], R1_RETRY_MAX))

    st["patch_applied"] = True
    return False, ("提示: 插件补丁已应用仍检测到 R1，需人工诊断"
                   "（重启额度 %d/%d 已用完，已停止重复重启）"
                   % (st.get("patch_retry_used", 0), R1_RETRY_MAX))


def r5_napcat_probe(st):
    """R5：napcat 掉线检测（Login Error 循环 / 二维码等待）。

    只读 napcat 容器日志。返回 (report_line or None, evidence_dict)。
    决策是纯函数式的：证据 -> 结论，方便单测（把日志行直接喂正则即可）。
    """
    try:
        out = subprocess.run(
            ["docker", "logs", "napcat", "--since", f"{R5_WINDOW_MIN}m"],
            capture_output=True, text=True, timeout=30,
        ).stderr or ""
        # napcat 日志主要走 stderr；stdout 可能带二维码 banner，一并合并
        out += subprocess.run(
            ["docker", "logs", "napcat", "--since", f"{R5_WINDOW_MIN}m"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception as e:
        return None, {"probe_error": str(e)}
    lines = out.splitlines()[-400:]
    now = time.time()
    err_ts = []
    qr = False
    for ln in lines:
        if R5_LOGIN_ERR_RE.search(ln):
            err_ts.append(ln)
        if R5_QRCODE_RE.search(ln):
            qr = True
    n_err = len(err_ts)
    st.setdefault("r5", {})
    if n_err >= 1 and qr:
        first = err_ts[0][:80] if err_ts else ""
        return (
            f"R5 napcat 掉线：近30分钟 Login Error x{n_err}，二维码={qr} —— 机器人不在线，需人工扫码",
            {"login_err": n_err, "qrcode": qr, "first": first},
        )
    return None, {"login_err": n_err, "qrcode": qr}


def main():
    lines = tail_log()
    now = datetime.now()
    cutoff = now - timedelta(minutes=WINDOW_MIN)
    sends = []          # (dt, receiver, text) —— Prepare to send 的昵称是被回复对象
    tool_ts = []        # (dt, 工具名)：两条回复之间出现媒体工具调用 => 工具收尾双回复
    r1_events = []
    n429 = 0
    nfailed = 0
    nfail_real = 0
    nvisfail = 0
    prev6 = []
    for ln in lines:
        ln = ANSI.sub("", ln)
        # R3 噪音过滤：decide 判「不说话」/ selfguard 停传播 之后框架仍尝试发空消息
        # 导致的「主动回复失败」是预期静默，不是真错误（避免告警疲劳）。
        # decide 静默序列：decide stopped -> Prepare to send(空) -> selfguard stopped
        #   -> Traceback -> 主动回复失败，stop 标记在失败行前 4 行内。
        if FAILED.search(ln):
            nfailed += 1
            if not any("stopped event propagation" in p for p in prev6):
                nfail_real += 1
        prev6 = (prev6 + [ln])[-6:]
        m = TS.search(ln)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if dt < cutoff:
            continue
        s = SEND.search(ln)
        if s:
            receiver, text = s.group(1), s.group(2).strip()
            sends.append((dt, receiver, text))
            continue
        if RETRY429.search(ln):
            n429 += 1
        if VISFAIL.search(ln):
            nvisfail += 1
        # 媒体工具调用锚点（mention 补丁后媒体工具不再打 at=True 日志，
        # 改用插件/agent 的调用日志当证据）
        if TOOLCALL.search(ln):
            tool_ts.append((dt, ln[:40]))

    # ---- R1: 给同一人的两条回复间隔很短，且两条之间有媒体工具调用 ----
    #   = 工具收尾双回复（"新表情包来了"式）。仅凭间隔会误伤拆句/分段
    #   （秒级）和同人多次正常回复（>25s），所以必须两边同时成立：
    #   间隔 <= PAIR_WINDOW_S(25s) 且 中间确有工具调用，或第二条带 [At:（错位铁证）。
    # 空文本 Prepare to send 是 decide/selfguard 停传播后的噪音（根本没发出），
    # 参与配对会误报（10:46 事件就是空文本+正常回复被配成 at错位）。先滤掉。
    real_sends = [(dt, r, t) for dt, r, t in sends if t.strip()]
    by_receiver = defaultdict(list)
    for dt, receiver, text in real_sends:
        by_receiver[receiver].append((dt, text))
    for receiver, items in by_receiver.items():
        items.sort()
        for i in range(1, len(items)):
            gap = (items[i][0] - items[i - 1][0]).total_seconds()
            if gap > PAIR_WINDOW_S:
                continue
            first_dt, second = items[i - 1][0], items[i][1]
            between = [t for t, _ in tool_ts if first_dt < t < items[i][0]]
            second_at = "[At:" in second
            if not (between or second_at):
                continue
            r1_events.append({
                "sender": receiver,
                "first": items[i - 1][1][:60],
                "second": second[:60],
                "gap_s": round(gap, 1),
                "tool_between": bool(between),
                "ts": items[i][0].strftime("%Y-%m-%d %H:%M:%S"),
                "kind": "at错位" if second_at else "工具收尾双回复",
            })

    st = load_state()
    # R1 24h 滚动计数
    day_ago = time.time() - 86400
    st["r1_count"] = [t for t in st.get("r1_count", []) if t > day_ago]
    fresh = [e for e in r1_events if e["ts"] > (st.get("last_r1_ts") or "")]
    for e in fresh:
        st["r1_count"].append(time.time())
        st["last_r1_ts"] = e["ts"]

    issues = {"time": now_iso(), "r1": r1_events, "n429": n429,
              "nfailed": nfailed, "nfail_real": nfail_real,
              "nvisfail": nvisfail, "n_sends": len(sends)}
    prev = {}
    try:
        with open(ISSUES, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        pass
    with open(ISSUES, "w", encoding="utf-8") as f:
        json.dump(issues, f, ensure_ascii=False, indent=1)

    alerts = st.setdefault("alerts", {})
    now_t = time.time()
    report = []
    if r1_events:
        report.append(f"R1 疑似工具收尾艾特错位 x{len(r1_events)}: " +
                      "; ".join(f"{e['sender']}「{e['second']}」" for e in r1_events[:3]))
    if n429:
        report.append(f"R2 主模型中转 429 x{n429}")
    if nfail_real:
        report.append(f"R3 主动回复失败(真) x{nfail_real}")
    elif nfailed:
        report.append(f"R3 主动回复失败 x{nfailed}(多为decide预期静默)")
    if nvisfail:
        report.append(f"R4 识图失败 x{nvisfail}")
    # ---- R5: napcat 掉线（最高优先级：人不在线一切白搭）----
    r5_line, r5_ev = r5_napcat_probe(st)
    if r5_line:
        report.append(r5_line)
        st["r5"]["last_hit"] = now_iso()

    # ---- 自动修复：R1 复发 -> 插件补丁（幂等）+ 重启 ----
    # 模型层约束（人格）实测拦不住"回甲时顺手接乙的茬"（C1 情景两轮 FAIL），
    # 真正的落点是插件层：mention 媒体工具收尾豁免 @ + imagegen 收尾文案。
    # 决策逻辑全部在 r1_autofix() 里（可单测），这里只负责跑补丁脚本和收结果。
    fixed = False
    if len(st["r1_count"]) >= R1_AUTOFIX_THRESHOLD:
        out = subprocess.run([sys.executable, PATCH], capture_output=True, text=True)
        changed = "已打过补丁" not in out.stdout
        fixed, line = r1_autofix(st, changed, patch_fp())
        report.append(line)

    # ---- 告警（冷却 + 去重）----
    key = "|".join(report) if report else ""
    if report:
        last = alerts.get("last_alert_t", 0)
        if key and (now_t - last > ALERT_COOLDOWN_S):
            body = "观察窗 %s 发现 %s\n\n" % (now_iso(), WINDOW_MIN) + "\n".join(report)
            body += f"\n\n24h内R1累计: {len(st['r1_count'])} 次"
            if fixed:
                body += "\n\n已执行自动修复：人格强化约束已追加并重启容器。"
            send_mail("【机器人观察窗】发现回复问题", body)
            alerts["last_alert_t"] = now_t
            alerts["last_body"] = body[:500]

    save_state(st)
    print(f"[{now_iso()}] sends={len(sends)} R1={len(r1_events)} 429={n429} "
          f"fail={nfailed}(real {nfail_real}) visfail={nvisfail} autofix={fixed}")


if __name__ == "__main__":
    main()
