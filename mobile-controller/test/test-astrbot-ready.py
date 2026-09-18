# -*- coding: utf-8 -*-
"""astrbot_started_marker 就绪判据测试。

背景（2026-09-19 线上故障）：
  用户反映「怎么写配置都保存不进去」。线上日志四次 POST /instance/config 全 400，
  traceback 全是「这个机器人的聊天服务还没启动完」。
  根因：astrbot_started_marker 只查 docker logs --tail 80 里有没有
  "AstrBot started"。dfy 实例运行 24h、日志 10555 行，标记在第 509 行
  —— 早已被挤出最近 80 行，于是永远判「没启动完」，所有写入被拒。

修复：加兜底判据 —— 容器**正在运行且持续运行够久**（ASTRBOT_RUN_OK_SECONDS），
     那它必然早就启动完成了（启动失败容器会退出）。

本测试必须能区分：
  * 长期运行容器（标记被挤出）→ 应判已启动（修复前会误判 False）
  * 刚启动几秒的容器（标记还没出现）→ 应判未启动（不能误放行）
  * 容器不存在 / docker 不可用 → None（与旧行为一致）
"""
import importlib.util
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location("mgr", "server/dafeiyu-manager.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []
def ck(n, c, e=""):
    print("  %s %s%s" % ("PASS" if c else "FAIL", n, ("  <- " + str(e)) if not c and e != "" else ""))
    if not c:
        fails.append(n)


class FakeCompleted:
    def __init__(self, rc, out):
        self.returncode = rc
        self.stdout = out.encode("utf-8")


# 模拟 docker：按 argv 返回不同结果
def patch_docker(logs_out, inspect_out, logs_rc=0, inspect_rc=0,
                 no_docker=False):
    """把 subprocess.run 换成假实现。返回还原函数。"""
    real = subprocess.run
    def fake_run(argv, **kw):
        if no_docker or "docker" not in argv[0]:
            raise FileNotFoundError("no docker")
        if "logs" in argv:
            return FakeCompleted(logs_rc, logs_out)
        if "inspect" in argv:
            return FakeCompleted(inspect_rc, inspect_out)
        raise AssertionError("不认识的 argv: %r" % argv)
    subprocess.run = fake_run
    return lambda: setattr(subprocess, "run", real)


# 一个「运行很久」的启动时刻：把当前时刻往前推 24h
LONG_AGO = time.strftime("%Y-%m-%dT%H:%M:%S",
                         time.gmtime(time.time() - 24 * 3600))
JUST_NOW = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 5))

print("== 1) 标记还在最近 80 行 → 已启动（两种判据都该 True）==")
r = patch_docker(logs_out="...AstrBot started.\n", inspect_out="")
try:
    ck("有标记 → True", m.astrbot_started_marker("x") is True)
finally:
    r()

print("== 2) ★ 长期运行容器：标记被挤出（线上故障场景）→ 修复后必须 True ==")
r = patch_docker(logs_out="其他日志刷屏，没有标记", inspect_out="true|%s.123Z" % LONG_AGO)
try:
    ck("运行 24h 无标记 → True（修复前是 False）",
       m.astrbot_started_marker("dfy") is True)
finally:
    r()

print("== 3) 刚启动几秒、标记还没出现 → 未启动（不能误放行）==")
r = patch_docker(logs_out="启动日志…还没到 started", inspect_out="true|%s.123Z" % JUST_NOW)
try:
    ck("运行 5s 无标记 → False", m.astrbot_started_marker("x") is False)
finally:
    r()

print("== 4) 容器存在但没在跑（exited）→ 未启动 ==")
r = patch_docker(logs_out="旧日志无标记", inspect_out="false|%s.123Z" % LONG_AGO)
try:
    ck("exited 长期 → False", m.astrbot_started_marker("x") is False)
finally:
    r()

print("== 5) 容器不存在（logs 返回非 0）→ None（不误判没启动）==")
r = patch_docker(logs_out="", inspect_out="", logs_rc=1, inspect_rc=1)
try:
    ck("容器不存在 → None", m.astrbot_started_marker("x") is None)
finally:
    r()

print("== 6) 没有 docker 命令 → None ==")
r = patch_docker(logs_out="", inspect_out="", no_docker=True)
try:
    ck("docker 不可用 → None", m.astrbot_started_marker("x") is None)
finally:
    r()

print("== 7) inspect 解析：真实格式 true|2026-09-17T17:28:52.152884964Z ==")
r = patch_docker(logs_out="无标记", inspect_out="true|2026-09-17T17:28:52.152884964Z")
try:
    info = m._astrbot_life("x")
    ck("拿到 (running=True, epoch)", isinstance(info, tuple) and info[0] is True,
       info)
    ck("epoch 是数字且合理（2026-09 ≈ 1789e9）",
       isinstance(info[1], (int, float)) and 1.7e9 < info[1] < 1.9e9, info)
finally:
    r()

print("== 8) 兜底阈值：ASTRBOT_RUN_OK_SECONDS > READY_TIMEOUT（防止误伤短时启动）==")
ck("ASTRBOT_RUN_OK_SECONDS=90 且 > READY_TIMEOUT=40",
   getattr(m, "ASTRBOT_RUN_OK_SECONDS", 0) > getattr(m, "READY_TIMEOUT", 0),
   (getattr(m, "ASTRBOT_RUN_OK_SECONDS", None), getattr(m, "READY_TIMEOUT", None)))

print()
print("FAILURES: %d %s" % (len(fails), fails if fails else ""))
sys.exit(1 if fails else 0)