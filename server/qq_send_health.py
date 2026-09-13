#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判定 NapCat 上行发送链路是否真的健康。

为什么需要这个：`get_status` 返回的 `online:true` **不能证明消息发得出去**。
2026-09-13 实证：QQ 会话失效后 NapCat 仍报 online=true / good=true，
但每条 `sendMsg` 都失败：

    15:24:32 [info] 发送 -> 群聊 [...] 张飞那事想了一下午…
    15:24:33 [error] EventChecker Failed: …NodeIKernelMsgService/sendMsg
        "result": 1006514, "errMsg": "网络连接异常!"

于是看守判 online、网页一直绿着、一封告警邮件都不发，人完全不知道
bot 已经一整天说不出话（用户 2026-09-13 报的正是这个）。

另一个坑：`发送 ->` 这行是**尝试发送**时打的，跟成功无关——它在 error 之前
约 0.2 秒出现。所以不能拿 `发送 ->` 的条数/时间当「活着」的证据（旧的
last_activity_age 就是这么被骗的：把失败的发送算成了活动）。

本脚本把每个 `发送 ->` 与紧随其后的 error 配对，得到：
    last_ok        最近一次**真的发出去**的发送时间（epoch）
    last_fail      最近一次发送失败的时间（epoch，强证据）
    last_fail_weak 最近一次媒体类失败（epoch，弱证据）
    last_recv      最近一次收到消息的时间（epoch）

判定：last_fail > last_ok ⇒ 上行坏了（uplink_broken）。

为什么不用 --since 限定窗口：docker 的容器日志随容器**重建**而重置，
所以「整份日志」天然等于「本次 napcat 生命周期」。用固定窗口反而会在
半死不重启超过窗口长度时出现假恢复。--window 仍保留，0 = 整份日志。

用法：
    python3 qq_send_health.py                 # JSON
    python3 qq_send_health.py --verdict       # ok / broken / unknown
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# docker logs --timestamps 的 RFC3339 前缀
TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s")
SEND = "发送 ->"
RECV = "接收 <-"
# 强证据：QQ 会话/网络层面的失败，文本消息也发不出去
FAIL_STRONG = ("1006514", "网络连接异常", "sendMsg")
# 弱证据：媒体上传失败，文本可能仍然正常
FAIL_WEAK = ("rich media transfer failed",)
# pending 发送超过这么久还没等到 error，就认为它成功了（error 最多滞后几百毫秒）
PENDING_TTL_S = 30


def parse_epoch(line):
    """从 docker --timestamps 前缀取 epoch；取不到返回 None。"""
    m = TS.match(line)
    if not m:
        return None
    raw = m.group(1)
    try:
        dt = datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


def container_started_at(container):
    """当前这次运行（docker restart 会更新）的启动时间，RFC3339；取不到回 None。

    为什么必须按「本次运行」切片：`docker restart` **不会**清空容器日志
    （只有 compose 重建会），所以重启后旧的失败记录还在。若把它们算进来，
    用户刚扫码登录成功、还没来得及发第一条消息时，会被判成「仍然发不出」
    —— 立刻误报一封假告警。
    """
    try:
        p = subprocess.run(
            ["docker", "inspect", container, "--format", "{{.State.StartedAt}}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        out = p.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return None
    if not TS.match(out + " "):
        return None
    return out


def collect(container, window_s):
    """取容器日志行。失败返回 (None, 原因)。"""
    cmd = ["docker", "logs", container, "--timestamps"]
    started = container_started_at(container)
    if started:
        # 只取本次运行之后的日志（含极小的时钟回退余量由 docker 自己处理）
        cmd[3:3] = ["--since", started]
    elif window_s > 0:
        cmd[3:3] = ["--since", "%ds" % window_s]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
    except FileNotFoundError:
        return None, "docker_not_found"
    except subprocess.TimeoutExpired:
        return None, "docker_timeout"
    except Exception as exc:
        return None, "docker_error: %s" % exc
    out = ANSI.sub("", p.stdout.decode("utf-8", "replace"))
    # 权限不足/容器不存在时 docker 会把错误写到 stdout（我们并了 stderr），
    # 那些行里不会有任何收发标记。必须显式识别，否则会静默返回全 0 —— 而
    # 全 0 在调用方看来是「健康」，正是最危险的假阴性。
    low = out.lower()
    for bad in ("permission denied", "no such container", "cannot connect to the docker daemon"):
        if bad in low:
            return None, bad.replace(" ", "_")
    return out.splitlines(), None


def analyse(lines):
    last_ok = 0
    last_fail = 0
    last_fail_weak = 0
    last_recv = 0
    n_ok = 0
    n_fail = 0
    n_fail_weak = 0
    # 每个发送尝试：(epoch, 是否已判定失败)
    pending_ts = None
    pending_failed = False

    def settle():
        nonlocal last_ok, n_ok
        if pending_ts is not None and not pending_failed:
            if pending_ts > last_ok:
                last_ok = pending_ts
            n_ok += 1

    for line in lines:
        ts = parse_epoch(line)
        # 超时的 pending 先结算，避免把很久以后的一次 error 算到它头上
        if pending_ts is not None and ts is not None and ts - pending_ts > PENDING_TTL_S:
            settle()
            pending_ts = None
            pending_failed = False

        if SEND in line:
            settle()
            pending_ts = ts if ts is not None else 0
            pending_failed = False
            continue

        if RECV in line and ts is not None:
            if ts > last_recv:
                last_recv = ts
            continue

        if pending_ts is None:
            continue

        # error 行本身可能没有正文（正文在下一行 JSON 里），所以两类标记都看。
        # 一次失败会命中多行（"发生错误 … sendMsg" + JSON 里的 1006514），
        # 用 pending_failed 做去重，保证「每次发送只计一次失败」。
        if any(k in line for k in FAIL_STRONG):
            if not pending_failed:
                n_fail += 1
            pending_failed = True
            if ts is not None and ts > last_fail:
                last_fail = ts
        elif any(k in line for k in FAIL_WEAK):
            if not pending_failed:
                n_fail_weak += 1
            pending_failed = True
            if ts is not None and ts > last_fail_weak:
                last_fail_weak = ts

    settle()
    return {
        "last_ok": last_ok,
        "last_fail": last_fail,
        "last_fail_weak": last_fail_weak,
        "last_recv": last_recv,
        "n_ok": n_ok,
        "n_fail": n_fail,
        "n_fail_weak": n_fail_weak,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=0, help="回看秒数，0=整份日志（默认）")
    ap.add_argument("--container", default="napcat")
    ap.add_argument("--verdict", action="store_true", help="只打印 ok / broken / unknown")
    args = ap.parse_args()

    lines, err = collect(args.container, args.window)
    if lines is None:
        if args.verdict:
            print("unknown")
        else:
            print(json.dumps({"error": err, "uplink_broken": None}))
        return 2

    res = analyse(lines)
    res["now"] = int(datetime.now(timezone.utc).timestamp())
    res["window"] = args.window
    # 上行是否坏了：最近一次发送是失败的（且确实有过失败）
    res["uplink_broken"] = bool(res["last_fail"] > 0 and res["last_fail"] > res["last_ok"])

    if args.verdict:
        print("broken" if res["uplink_broken"] else "ok")
    else:
        print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
