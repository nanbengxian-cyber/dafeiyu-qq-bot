# -*- coding: utf-8 -*-
"""observe_watch 的 R1 自动修复决策单测（不需要 docker，可在宿主机跑）。

背景（2026-09-13 事故）：
  `observe_watch.py` 里有一条「补丁在但仍复发（>2h 冷却），重启重试一次」的分支。
  但补丁已应用时重启**不改任何代码**，R1 不会因此消失 —— 这条分支于是每 7200 秒
  准时重启一次 AstrBot，实测连续 15 次、间隔 2h00m~2h05m（12 次/天），
  而 24h 内 R1 照样 15 次：一次都没治好，净效果只有「收发通道断 10~21 秒」。

本测试盯住：
  ① 补丁**首次应用**必须重启（这次是必要的，不能一起砍掉）；
  ② 补丁早就在位时，重启次数被 R1_RETRY_MAX 封顶，不再无限循环；
  ③ 从老 state（没有 patch_fp）迁移过来时不再白重启一次；
  ④ 补丁脚本换代后重试额度重置，新补丁仍能拿到「应用后重启」；
  ⑤ 全流程中 docker 一次都不许被真的调用（restart_astrbot 被替换成计数器）。

运行：python3 server/observe/test_r1_autofix.py
"""
import importlib.util
import os
import sys
from pathlib import Path

MOD = Path(__file__).with_name("observe_watch.py")
spec = importlib.util.spec_from_file_location("observe_watch", MOD)
ow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ow)

CHECKS = []
RESTARTS = []


def fake_restart():
    RESTARTS.append(1)


ow.restart_astrbot = fake_restart
ow.R1_RETRY_MAX = 1
ow.R1_RETRY_COOLDOWN_S = 7200


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond)))
    print("%-56s %s %s" % (name, "PASS" if cond else "FAIL", detail if not cond else ""))


def run_ticks(st, changed, fp, n, start=1_700_000_000.0, step=300):
    """模拟 timer 每 5 分钟跑一次，返回这期间重启了几次。"""
    before = len(RESTARTS)
    for i in range(n):
        ow.r1_autofix(st, changed, fp, now=start + i * step)
    return len(RESTARTS) - before


def main():
    print("R1_RETRY_MAX =", ow.R1_RETRY_MAX, " 冷却 =", ow.R1_RETRY_COOLDOWN_S, "s\n")

    # ---------- ① 补丁首次应用：必须重启 ----------
    RESTARTS.clear()
    st = {"patch_fp": "fp-A"}
    fixed, line = ow.r1_autofix(st, changed=True, fp="fp-A", now=1_700_000_000.0)
    check("首次应用补丁 -> 重启", fixed and len(RESTARTS) == 1,
          "fixed=%s restarts=%d" % (fixed, len(RESTARTS)))
    check("首次应用后额度被清零", st["patch_retry_used"] == 0, repr(st.get("patch_retry_used")))
    check("首次应用后记下 applied_at", st.get("patch_applied_at") is not None)

    # ---------- ② 补丁已在位、R1 持续复发：次数必须封顶 ----------
    RESTARTS.clear()
    st = {"patch_fp": "fp-A", "patch_applied": True, "patch_applied_at": 1_700_000_000.0}
    n = run_ticks(st, changed=False, fp="fp-A", n=288)   # 24 小时 × 每 5 分钟
    check("24h 内重启次数 <= R1_RETRY_MAX", n <= ow.R1_RETRY_MAX, "实测重启 %d 次" % n)
    check("且确实用掉了那 1 次确认重启", n == 1, "实测重启 %d 次" % n)

    # 再跑 30 天，仍然不许再重启（老代码这里会重启 360 次）
    n2 = run_ticks(st, changed=False, fp="fp-A", n=288 * 30)
    check("再跑 30 天重启次数为 0", n2 == 0, "实测重启 %d 次" % n2)
    check("告警文案点名额度已用完", "已停止重复重启" in ow.r1_autofix(
        st, False, "fp-A", now=1_700_000_000.0 + 86400 * 40)[1])

    # ---------- ③ 老 state 迁移：不再白重启一次 ----------
    RESTARTS.clear()
    legacy = {"patch_applied": True, "patch_last": 1_700_000_000.0,
              "r1_count": [1, 2, 3], "alerts": {}}
    n3 = run_ticks(legacy, changed=False, fp="fp-A", n=288 * 7)
    check("老 state 迁移后 7 天零重启", n3 == 0, "实测重启 %d 次" % n3)
    check("迁移时额度直接记满", legacy["patch_retry_used"] == ow.R1_RETRY_MAX,
          repr(legacy.get("patch_retry_used")))
    check("迁移后仍标注补丁已应用", legacy.get("patch_applied") is True)

    # ---------- ④ 补丁脚本换代：额度重置，新补丁仍能重启 ----------
    RESTARTS.clear()
    st2 = dict(legacy)
    fixed4, _ = ow.r1_autofix(st2, changed=True, fp="fp-B", now=1_700_000_000.0 + 86400 * 8)
    check("换代后新补丁应用 -> 重启", fixed4 and len(RESTARTS) == 1,
          "fixed=%s restarts=%d" % (fixed4, len(RESTARTS)))
    check("换代后额度重置为 0", st2["patch_retry_used"] == 0, repr(st2.get("patch_retry_used")))
    n4 = run_ticks(st2, changed=False, fp="fp-B", n=288 * 7, start=1_700_000_000.0 + 86400 * 9)
    check("换代后新的一代也只有 1 次确认重启", n4 == 1, "实测重启 %d 次" % n4)

    # ---------- ⑤ 对照：老逻辑在同一场景下的重启次数 ----------
    def old_restarts(days):
        """老逻辑的等价实现，只为对照，不参与生产。"""
        last = 0.0
        applied = True
        cnt = 0
        for i in range(int(days * 288)):
            now = 1_700_000_000.0 + i * 300
            if applied and now - last > 7200:
                last = now
                cnt += 1
        return cnt

    old = old_restarts(30)
    # 稳态对照：补丁早已在位、R1 持续复发，跑满 30 天
    st_cmp = {"patch_fp": "fp-C", "patch_applied": True,
              "patch_applied_at": 1_700_000_000.0, "patch_retry_used": 0}
    new = run_ticks(st_cmp, changed=False, fp="fp-C", n=288 * 30)
    print()
    print("对照：同样「补丁在位且 R1 持续复发」30 天")
    print("   老逻辑重启 %d 次（约每天 %.1f 次，每次掉线 10~21s）" % (old, old / 30.0))
    print("   新逻辑重启 %d 次" % new)
    check("新逻辑把 30 天的 %d 次重启压到 <= %d 次" % (old, ow.R1_RETRY_MAX),
          old > 1 and new <= ow.R1_RETRY_MAX)

    print()
    bad = [n for n, ok in CHECKS if not ok]
    print("R1_AUTOFIX_TEST_%s（%d/%d）" % ("FAIL" if bad else "OK",
                                          len(CHECKS) - len(bad), len(CHECKS)))
    for n in bad:
        print("  FAILED:", n)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
