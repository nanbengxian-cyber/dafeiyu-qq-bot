# -*- coding: utf-8 -*-
"""大肥鱼群动态观察器（记分卡版）。

与 observe_watch.py 互补：那个盯的是「回复质量故障」（R1~R4，含自动修复），
这个盯的是「群动态与说话质量」，只读不改生产，产出可比的记分卡：

  P1 被@未回      群友 @ 机器人后 60s 内完全没有回应的条数/占比
  P2 群主被忽略   群主消息后 120s 内无回应（含未点名但明显在跟机器人说话）
  P3 参与度       机器人发言占群消息比例（过高=抢话，过低=透明）
  P4 自我重复     机器人自身近 3h 内复用的短句次数（同一句 ≥3 次）
  P5 刷屏         机器人 60s 内连发 ≥3 条的次数
  P6 功能失败     语音审核拦截/超时、主动回复失败、Traceback 次数
  P7 冷却提示刷屏 「慢点，还有 N 秒冷却」类机械提示次数

输出：/opt/qqbot/observe/dynamics.json（最新）+ dynamics_history.jsonl（滚动）
     + 追加人类可读记分卡到 /opt/qqbot/observe/dynamics.log
有超阈值项时发邮件（带冷却），阈值见 THRESHOLDS。
"""
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta

BASE = "/opt/qqbot/observe"
LOG = "/opt/qqbot/astrbot/data/logs/astrbot.log"
OUT = os.path.join(BASE, "dynamics.json")
HIST = os.path.join(BASE, "dynamics_history.jsonl")
TXT = os.path.join(BASE, "dynamics.log")
NOTIFY = "/opt/qqbot/notify_mail.py"

WINDOW_H = float(os.environ.get("DYN_WINDOW_H", "3"))
BOT_QQ = os.environ.get("DYN_BOT_QQ", "3752949717")
OWNER_QQ = os.environ.get("DYN_OWNER_QQ", "2774067216")
ALERT_COOLDOWN_S = 3600

TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d{3}\]")
IN_RE = re.compile(r"\[default\] \[default\(aiocqhttp\)\] (.+?)/(\d+): (.*)$")
OUT_RE = re.compile(r"Prepare to send - (.+?)/(\d+): (.*)$")

MECH_HINT = re.compile(r"慢点，还有 \d+ 秒冷却|这次语音没确认发出去|发不出声音")
# 指令回执类输出（/权限、/情绪状态…）不是「聊天措辞重复」，统计重复率时排除
COMMAND_OUT = re.compile(r"^(你是群主|我这边和你的互动档位|用法：|我记住的你|已删除|复读 |违规看守|自主系统)")
VOICE_BLOCK = re.compile(r"\[voice\] 审核未通过不发语音|\[voice\] 审核超时|\[voice\] 审核异常")
# 机器人输出里的结构化标记（框架与插件塞的），不是它自己的措辞。
# 引用标记来自 dsh-quote：`[引用消息] 正文`。
_MARKER = re.compile(
    r"\[(?:引用消息|引用图片|图片|语音|表情|视频|文件|At:\d+)\]"
)

FAILED = re.compile(r"主动回复失败")
TB = re.compile(r"\[ERRO\].*Traceback|Traceback \(most recent call last\)")
# decide/selfguard 主动停传播后框架仍尝试发空消息导致的「主动回复失败」是预期静默
STOPPED = "stopped event propagation"

THRESHOLDS = {
    "p1_mention_miss": 6,       # 条
    "p2_owner_ignored": 1,      # 条（群主被点名却没回即告警）
    "p3_share_max": 0.45,       # 参与度上限
    "p4_repeat_max": 4,         # 同一短句出现次数
    "p5_burst": 2,              # 次（60s 内连发 >=BURST_N 次）
    "p6_fails": 4,              # 次
    "p7_mech": 6,               # 条
}
BURST_N = 5


def read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except (PermissionError, OSError):
        try:
            out = subprocess.run(["sudo", "cat", path], capture_output=True, text=True)
            return out.stdout.splitlines()
        except Exception:
            return []


def parse(lines, since):
    ev = []
    recent = []
    for ln in lines:
        m = TS.match(ln)
        if not m:
            recent = (recent + [ln])[-6:]
            continue
        try:
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            recent = (recent + [ln])[-6:]
            continue
        rest = ln[m.end():]
        mo = OUT_RE.search(rest)
        if mo:
            ev.append({"t": t, "k": "bot", "who": mo.group(1),
                       "qq": mo.group(2), "text": mo.group(3).strip()})
            recent = (recent + [ln])[-6:]
            continue
        mi = IN_RE.search(rest)
        if mi:
            ev.append({"t": t, "k": "user", "who": mi.group(1),
                       "qq": mi.group(2), "text": mi.group(3).strip()})
            recent = (recent + [ln])[-6:]
            continue
        if VOICE_BLOCK.search(ln) or TB.search(ln) or (
                FAILED.search(ln) and not any(STOPPED in p for p in recent)):
            if t >= since:
                ev.append({"t": t, "k": "err", "who": "log", "qq": "",
                           "text": ln.strip()[:200]})
        recent = (recent + [ln])[-6:]
    ev.sort(key=lambda x: (x["t"], 0 if x["k"] == "user" else 1))
    return ev


def logical_replies(bots, gap_s=6.0):
    """把 6 秒内连续的 Prepare to send 合并成「一次逻辑回复」。

    dsh-human 会把一句拆成多条（"好家伙" / "我还有专属墙了"），框架对每条
    分段各打一行日志；不合并的话参与度和刷屏率都会虚高。
    """
    out = []
    for b in bots:
        if out and (b["t"] - out[-1]["t_end"]).total_seconds() <= gap_s:
            out[-1]["t_end"] = b["t"]
            out[-1]["parts"].append(b["text"])
            continue
        out.append({"t_start": b["t"], "t_end": b["t"], "who": b["who"],
                    "qq": b["qq"], "parts": [b["text"]]})
    for o in out:
        o["text"] = " ".join(p for p in o["parts"] if p).strip()
    return out


def score(ev, hours, raw_lines=None):
    users = [e for e in ev if e["k"] == "user" and e["qq"] != BOT_QQ]
    bots = [e for e in ev if e["k"] == "bot" and e["text"]]
    errs = [e for e in ev if e["k"] == "err"]
    replies = [r for r in logical_replies(bots) if r["text"]]

    # P1 被@未回
    p1 = []
    mentions = 0
    for i, e in enumerate(ev):
        if e["k"] != "user" or e["qq"] == BOT_QQ or BOT_QQ not in e["text"]:
            continue
        mentions += 1
        t0 = e["t"]
        hit = any(x["k"] == "bot" and 0 <= (x["t"] - t0).total_seconds() <= 60
                  for x in ev[i + 1:])
        if not hit:
            p1.append({"t": e["t"].strftime("%H:%M:%S"), "who": e["who"],
                       "text": e["text"][:80]})

    # P2 群主被忽略：群主「点名机器人」却没回应才算。
    # 群主平时话很多（一晚上上百条），按「所有消息都要回」算会把正常的
    # 不抢话（dsh-clarify 的设计目标）误报成缺陷，所以只看点名：
    # 带 @机器人 / 出现「大肥鱼」「肥鱼」/ 带问号且出现「你」。
    name_hit = re.compile(r"大肥鱼|肥鱼|" + BOT_QQ)
    p2 = []
    for i, e in enumerate(ev):
        if e["k"] != "user" or e["qq"] != OWNER_QQ:
            continue
        t = e["text"]
        called = (BOT_QQ in t) or bool(name_hit.search(t)) or ("你" in t and "？" in t) \
            or ("你" in t and "?" in t)
        if not called:
            continue
        t0 = e["t"]
        hit = any(x["k"] == "bot" and 0 <= (x["t"] - t0).total_seconds() <= 120
                  for x in ev[i + 1:])
        if not hit:
            p2.append({"t": t0.strftime("%H:%M:%S"), "text": t[:80]})

    # P3 参与度（按逻辑回复算，避免 dsh-human 拆句虚高）
    share = (len(replies) / float(len(users))) if users else 0.0

    # P4 自我重复：机器人自身措辞的 4 字片段频次。
    # 用「整句精确匹配」会漏掉真正的复读——同一句口头禅往往套在不同前缀里
    # （如「爪子拿开」出现在「又来，爪子拿开」「不许扣 爪子拿开」里）。
    # 所以改成 4 字滑窗，且要求出现在 >=3 条**不同回复**里，避免单条长回复刷高。
    gram_replies = Counter()
    for b in bots:
        # 先摘掉结构化标记再看措辞：`[引用消息]` 是真的会被念进 4 字滑窗的
        # （日志里 328 条），不摘的话「引用消息」永远是最高频假 gram，
        # 把真正的复读（爪子拿开 / 图没看着）挤下去。
        s = _MARKER.sub("", b["text"])
        s = re.sub(r"[^\u4e00-\u9fff]", "", s)
        if MECH_HINT.search(b["text"]) or COMMAND_OUT.search(b["text"].strip()):
            continue
        for g in set(s[i:i + 4] for i in range(len(s) - 3)):
            gram_replies[g] += 1
    p4 = [{"text": k, "n": v} for k, v in gram_replies.most_common(10) if v >= 3]

    # P5 刷屏：60s 内机器人连发 >=BURST_N 次逻辑回复。
    # 阈值取 5：群里 8 条/分钟的常态下，一分钟回 3~4 次是正常参与
    # （实测 5h 里 3~4 次的簇有 59 个），只有 >=5 次才算压过群友刷屏。
    p5 = []
    bts = [r["t_start"] for r in replies]
    i = 0
    while i < len(bts):
        j = i
        while j + 1 < len(bts) and (bts[j + 1] - bts[i]).total_seconds() <= 60:
            j += 1
        if j - i + 1 >= BURST_N:
            p5.append({"start": bts[i].strftime("%H:%M:%S"), "n": j - i + 1})
        i = j + 1

    # P6 功能失败
    p6 = Counter()
    for e in errs:
        if VOICE_BLOCK.search(e["text"]):
            p6["voice审核拦截/超时"] += 1
        elif FAILED.search(e["text"]):
            p6["主动回复失败"] += 1
        elif TB.search(e["text"]):
            p6["Traceback"] += 1

    # P7 机械提示刷屏
    p7 = [b for b in bots if MECH_HINT.search(b["text"])]

    return {
        "window_h": hours,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_user_msg": len(users),
        "n_bot_msg": len(bots),
        "n_bot_reply": len(replies),
        "n_mention": mentions,
        "p1_mention_miss": p1,
        "p2_owner_ignored": p2,
        "p3_share": round(share, 3),
        "p4_repeat": p4,
        "p5_burst": p5,
        "p6_fails": dict(p6),
        "p6_total": sum(p6.values()),
        "p7_mech": len(p7),
        "p7_samples": [b["text"][:30] for b in p7[:5]],
        "top_talkers": Counter(u["who"] for u in users).most_common(8),
    }


def alerts(sc, prev):
    out = []
    if len(sc["p1_mention_miss"]) >= THRESHOLDS["p1_mention_miss"]:
        out.append("P1 被@未回 %d 条" % len(sc["p1_mention_miss"]))
    if len(sc["p2_owner_ignored"]) >= THRESHOLDS["p2_owner_ignored"]:
        out.append("P2 群主被忽略 %d 条：%s" % (
            len(sc["p2_owner_ignored"]),
            "; ".join(x["text"][:30] for x in sc["p2_owner_ignored"][:3])))
    if sc["p3_share"] >= THRESHOLDS["p3_share_max"]:
        out.append("P3 参与度过高 %.0f%%" % (sc["p3_share"] * 100))
    for r in sc["p4_repeat"]:
        if r["n"] >= THRESHOLDS["p4_repeat_max"]:
            out.append("P4 自我重复「%s」x%d" % (r["text"][:20], r["n"]))
    if len(sc["p5_burst"]) >= THRESHOLDS["p5_burst"]:
        out.append("P5 刷屏 %d 次" % len(sc["p5_burst"]))
    if sc["p6_total"] >= THRESHOLDS["p6_fails"]:
        out.append("P6 功能失败 %d 次 %s" % (sc["p6_total"], sc["p6_fails"]))
    if sc["p7_mech"] >= THRESHOLDS["p7_mech"]:
        out.append("P7 机械提示 %d 条" % sc["p7_mech"])
    return out


def render(sc, al):
    return (
        "[%s] 窗口%.0fh 群友%d 机器人%d条/%d次 参与度%.0f%% 被@%d未回%d 群主点名未回%d "
        "重复%d 刷屏%d 功能失败%d 机械提示%d%s" % (
            sc["generated"], sc["window_h"], sc["n_user_msg"], sc["n_bot_msg"],
            sc["n_bot_reply"],
            sc["p3_share"] * 100, sc["n_mention"], len(sc["p1_mention_miss"]),
            len(sc["p2_owner_ignored"]), len(sc["p4_repeat"]), len(sc["p5_burst"]),
            sc["p6_total"], sc["p7_mech"],
            ("  ⚠ " + " | ".join(al)) if al else "")
    )


def main():
    os.makedirs(BASE, exist_ok=True)
    since = datetime.now() - timedelta(hours=WINDOW_H)
    ev = parse(read_lines(LOG), since)
    sc = score(ev, WINDOW_H)
    al = alerts(sc, None)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(sc, f, ensure_ascii=False, indent=1)
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps(sc, ensure_ascii=False) + "\n")
    line = render(sc, al)
    with open(TXT, "a", encoding="utf-8") as f:
        f.write(line + "\n")

    if al:
        keyf = os.path.join(BASE, "dynamics_alert.json")
        last, lastkey = 0, ""
        try:
            with open(keyf, encoding="utf-8") as f:
                d = json.load(f)
                last, lastkey = d.get("t", 0), d.get("key", "")
        except Exception:
            pass
        key = "|".join(al)
        if key != lastkey or time.time() - last > ALERT_COOLDOWN_S:
            body = line + "\n\n" + "\n".join("- " + a for a in al)
            body += "\n\n明细：/opt/qqbot/observe/dynamics.json"
            try:
                subprocess.Popen(["/usr/bin/python3", NOTIFY, "【群动态记分卡】需关注", "-"],
                                 stdin=subprocess.PIPE).communicate(body.encode("utf-8"), timeout=10)
            except Exception as e:
                print("mail fail:", e)
            with open(keyf, "w", encoding="utf-8") as f:
                json.dump({"t": time.time(), "key": key}, f, ensure_ascii=False)

    print(line)


if __name__ == "__main__":
    main()
