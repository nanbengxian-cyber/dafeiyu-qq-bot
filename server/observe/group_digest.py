#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大肥鱼群聊动态摘要器。

从 AstrBot 日志里抽出「群友消息 -> 机器人回复 -> 插件动作」的时间线，
用于持续观察群动态并定位可改进点。

用法：
  python3 group_digest.py                      # 最近 2 小时
  python3 group_digest.py --hours 6
  python3 group_digest.py --since 2026-09-12T20:00
  python3 group_digest.py --json out.json      # 额外导出结构化数据
  python3 group_digest.py --stats              # 只打印统计
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta

LOG_PATH = "/opt/qqbot/astrbot/data/logs/astrbot.log"
GROUP_ID = "100000001"
BOT_QQ = "3752949000"

TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.(\d{3})\]")
IN_RE = re.compile(r"\[default\] \[default\(aiocqhttp\)\] (.+?)/(\d+): (.*)$")
OUT_RE = re.compile(r"Prepare to send - (.+?)/(\d+): (.*)$")
PLUG_RE = re.compile(r"\[(\w[\w.-]*?):\d+\]: (.*)$")

# 值得记录成时间线节点的插件动作
NOISE_KEYS = (
    "[decide]", "[clarify]", "[guard]", "[selfguard]", "[acl]", "[fatigue]",
    "[initiate]", "[followup]", "[proactive]", "[mention]", "[human]", "[poke]",
    "[claimguard]", "[factguard]", "[leakguard]", "[joinguard]", "[armor]",
    "[desire]", "[emotion]", "[agency]", "[repeat]", "[steal]", "[imagegen]",
    "[spine]", "[scene]", "[listen]", "[noise]", "[drift]",
)


def read_lines(path):
    """优先直接读，权限不足时用 sudo。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
    except (PermissionError, OSError):
        proc = subprocess.run(
            ["sudo", "cat", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if proc.returncode != 0:
            sys.stderr.write("无法读取日志: %s\n" % proc.stderr.decode("utf-8", "replace"))
            sys.exit(1)
        data = proc.stdout.decode("utf-8", "replace")
    return data.splitlines()


def parse(lines, since_dt=None):
    events = []
    for line in lines:
        m = TS_RE.match(line)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        if since_dt and ts < since_dt:
            continue
        rest = line[m.end():]

        mm = OUT_RE.search(rest)
        if mm:
            events.append({
                "t": ts, "kind": "bot", "who": mm.group(1), "qq": mm.group(2),
                "text": mm.group(3).strip(),
            })
            continue

        mm = IN_RE.search(rest)
        if mm:
            events.append({
                "t": ts, "kind": "user", "who": mm.group(1), "qq": mm.group(2),
                "text": mm.group(3).strip(),
            })
            continue

        mm = PLUG_RE.search(rest)
        if mm:
            name, body = mm.group(1), mm.group(2).strip()
            if any(k in body for k in NOISE_KEYS) or name.startswith("dsh-"):
                events.append({
                    "t": ts, "kind": "plug", "who": name, "qq": "",
                    "text": body,
                })
    return events


def fmt_text(text):
    t = text if text else "(非文本/空)"
    t = t.replace("[At:", "@").replace("[表情:", "[表情:").replace("\n", " / ")
    if len(t) > 400:
        t = t[:400] + "…"
    return t


# 一条**空正文**的 `Prepare to send` 不是发出去的消息，只是一次「这轮不开口」
# 的决定：dsh-decide 判沉默、dsh-poke 回戳时都会走到 respond.stage，日志照样
# 打一行，紧接着必有一条 `stopped event propagation`。实测 688/688 都是如此，
# **内容为空 = 一条都没送到群里**。老版本把它渲染成 (非文本/空)，看着像机器人在
# 疯狂刷空消息，也把「机器人说了多少话」这个数直接灌水一倍多（142 vs 真实 31）。
# 这里如实标成「决定不说」，并从统计口径里剔出去。
PHANTOM_NOTE = "(决定不说 · 未发出)"


def render(events, show_plug=True):
    out = []
    last_user = None
    for ev in events:
        stamp = ev["t"].strftime("%H:%M:%S")
        if ev["kind"] == "user":
            tag = "★群主" if ev["qq"] == "2774000001" else ""
            if ev["qq"] == BOT_QQ:
                tag = "☆机器人自己"
            out.append("%s  %s(%s) %s: %s" % (stamp, ev["who"], ev["qq"], tag, fmt_text(ev["text"])))
            last_user = ev
        elif ev["kind"] == "bot":
            shown = fmt_text(ev["text"]) if ev["text"] else PHANTOM_NOTE
            out.append("%s      └─ 大肥鱼 → %s(%s): %s" % (stamp, ev["who"], ev["qq"], shown))
        else:
            if not show_plug:
                continue
            out.append("%s      · [%s] %s" % (stamp, ev["who"], fmt_text(ev["text"])))
    return "\n".join(out)


def stats(events):
    users = {}
    bots = 0
    replied = 0
    pending = None
    empty_bot = 0
    for ev in events:
        if ev["kind"] == "user":
            if ev["qq"] != BOT_QQ:
                users[ev["who"]] = users.get(ev["who"], 0) + 1
            pending = (ev["t"], ev["who"])
        elif ev["kind"] == "bot":
            bots += 1
            if not ev["text"]:
                empty_bot += 1
            if pending:
                replied += 1
                pending = None
    total_user = sum(users.values())
    real = bots - empty_bot
    lines = [
        "群友消息: %d 条 / 机器人**发出去**的回复: %d 条（另 %d 次决定不开口，没送到群里）"
        % (total_user, real, empty_bot),
        "开口率: %.0f%%（真实回复 / 群友消息）" % (100.0 * real / total_user if total_user else 0.0),
        "活跃群友: " + ", ".join(
            "%s×%d" % (k, v) for k, v in sorted(users.items(), key=lambda x: -x[1])[:15]
        ),
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--since", default="")
    ap.add_argument("--log", default=LOG_PATH)
    ap.add_argument("--json", default="")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--no-plug", action="store_true")
    args = ap.parse_args()

    if args.since:
        since_dt = datetime.strptime(args.since, "%Y-%m-%dT%H:%M")
    else:
        if args.hours <= 0:
            since_dt = None
        else:
            since_dt = datetime.now() - timedelta(hours=args.hours)

    events = parse(read_lines(args.log), since_dt)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [{**e, "t": e["t"].strftime("%Y-%m-%d %H:%M:%S")} for e in events],
                fh, ensure_ascii=False, indent=1,
            )
    if args.stats:
        print(stats(events))
        return
    print(render(events, show_plug=not args.no_plug))
    print()
    print("=== 统计 ===")
    print(stats(events))


if __name__ == "__main__":
    main()
