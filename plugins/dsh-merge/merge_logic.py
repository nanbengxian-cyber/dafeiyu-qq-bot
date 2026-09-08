# -*- coding: utf-8 -*-
"""dsh-merge —— 空艾特回应的纯逻辑（不依赖 astrbot，可单测）。

限流目标「不能回复太多」：
  1. 每 uid 冷却：同一个人刚被回过（UID_CD 秒内）就不再回，防单人对刷。
  2. 每群窗口上限：WIN 秒内最多回 CAP 条，防空艾特风暴刷屏。
  3. 概率响应：每次被裸 @ 只有 RATE 概率回，本来就是「偶尔回」。
"""

BARE_LINES = [
    "？",
    "叫我干嘛",
    "咋了",
    "有话直说",
    "放",
    "说",
    "别光点不吱声",
    "嗯？",
]


def bare_allowed(now, gid, uid, hist, last, uid_cd, group_cap, win):
    """限流判定。

    参数：
      now     当前时间戳
      gid / uid  群号 / 说话人
      hist    gid 下之前裸艾特的时间戳列表（未过滤）
      last    该 uid 上次回应时间戳（0 = 从没回过）
      uid_cd / group_cap / win  冷却秒 / 窗口上限 / 窗口秒
    返回：(allowed, new_hist, reason)
      allowed=False 时不回（reason 说明原因：uid_cd / group_cap）
      new_hist 是过滤掉窗口外旧记录后的列表，供写回。
    """
    if now - last < uid_cd:
        return False, [t for t in hist if now - t <= win], "uid_cd"
    h = [t for t in hist if now - t <= win]
    if len(h) >= group_cap:
        return False, h, "group_cap"
    return True, h, "ok"


def bare_choose(rand01, rate):
    """按概率挑一句；没中返回 None。

    rand01 是 [0,1) 的随机数（测试可注入），rate 是回应概率。
    命中时从 BARE_LINES 里随机取一句。
    """
    if rand01 > rate:
        return None
    import random as _r

    return _r.choice(BARE_LINES)
