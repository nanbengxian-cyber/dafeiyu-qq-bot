#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量识图链条的「白花时间」—— 判断慢档降权(SLOW_TTL)有没有真的省下延迟。

口径（务必和 docs 里那条记录一致）：
  · 只统计 `[vischain]` 日志行；
  · 成功 = 「X 一次过（Ns）」「降级到 X 才成功（第a档第b次，Ns）」
           「X 图片转 JPEG 后成功（Ns）」，注意括号是**全角**的；
  · 白花 = 「X 第n次失败(Ns, ...)」里那些耗时，即花掉却没换来结果的墙钟；
  · 关键指标 = 白花秒数 / 成功次数，就是「每张图平均多等了几秒」。

用法：python3 vischain_waste.py [--since "2026-09-12 23:57"] [--lines 70000]
"""
import argparse
import collections
import re
import statistics
import subprocess

LOG = "/opt/qqbot/astrbot/data/logs/astrbot.log"
TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d{3}\]")


def collect(since=None, lines=70000):
    out = subprocess.run(["sudo", "tail", "-n", str(lines), LOG],
                         capture_output=True, text=True).stdout.split("\n")
    ok = collections.defaultdict(list)
    fail = collections.defaultdict(list)
    for l in out:
        if "[vischain]" not in l:
            continue
        if since:
            m = TS.match(l)
            if not m or m.group(1) < since:
                continue
        b = l.split("[vischain] ", 1)[-1]
        f = re.match(r"(\S+) 第\d+次失败\(([\d.]+)s", b)
        if f:
            fail[f.group(1)].append(float(f.group(2)))
            continue
        s = re.match(r"(\S+) 一次过（([\d.]+)s）", b)
        if s:
            ok[s.group(1)].append(float(s.group(2)))
            continue
        d = re.match(r"降级到 (\S+) 才成功（第\d+档第\d+次，([\d.]+)s）", b)
        if d:
            ok[d.group(1)].append(float(d.group(2)))
            continue
        j = re.match(r"(\S+) 图片转 JPEG 后成功（([\d.]+)s）", b)
        if j:
            ok[j.group(1)].append(float(j.group(2)))
    return ok, fail


def report(ok, fail, title):
    n_ok = sum(len(v) for v in ok.values())
    waste = sum(x for v in fail.values() for x in v)
    n_fail = sum(len(v) for v in fail.values())
    print("== %s ==" % title)
    if not n_ok:
        print("  还没有成功样本")
        return None
    print("  成功 %d 次｜失败重试 %d 次｜白花 %.0f 秒" % (n_ok, n_fail, waste))
    print("  **每次成功平均白花 %.1f 秒**" % (waste / n_ok))
    allok = [x for v in ok.values() for x in v]
    print("  成功耗时 P50 %.1fs / P90 %.1fs" % (
        statistics.median(allok), sorted(allok)[int(len(allok) * 0.9)]))
    for pid in sorted(set(list(ok) + list(fail))):
        o, f = ok.get(pid, []), fail.get(pid, [])
        w = sum(f)
        print("    %-42s 成功%4d 失败%4d 白花%6.0fs (%.1fs/成功)"
              % (pid, len(o), len(f), w, w / max(1, len(o))))
    return waste / n_ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    ap.add_argument("--lines", type=int, default=70000)
    a = ap.parse_args()
    ok, fail = collect(a.since, a.lines)
    report(ok, fail, "自 %s 起" % (a.since or "日志开头"))
