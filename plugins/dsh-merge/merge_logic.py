# -*- coding: utf-8 -*-
"""dsh-merge 的纯逻辑（不依赖 AstrBot，可单测）。

除了空艾特限流，这里还负责在艾特风暴停下后重新读取、整理真实群聊上下文。
整合回复不再把每个问题列成待办逐项回答，而是先看清对话最后落在哪条主线上。
"""

import re
import sqlite3


_SPACE_RE = re.compile(r"\s+")


def read_recent_context(gid, limit, db_path):
    """从 dsh-memory 的 buffer/archive 读取最新真实消息，按时间正序返回。

    两张表有重叠，先归并再去重；数据库不可用时返回空列表，让调用方使用
    本次艾特窗口里自己保存的消息兜底。
    """
    rows = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=2)
    except sqlite3.Error:
        return rows
    try:
        for table in ("buffer", "archive"):
            try:
                rows.extend(con.execute(
                    "SELECT user_id, name, ts, text FROM %s "
                    "WHERE group_id=? ORDER BY ts DESC LIMIT ?" % table,
                    (gid, max(1, int(limit)) * 2),
                ).fetchall())
            except sqlite3.Error:
                pass
    finally:
        con.close()

    merged = {}
    for uid, name, ts, text in rows:
        clean = _SPACE_RE.sub(" ", str(text or "")).strip()
        if not clean:
            continue
        key = (str(uid), clean)
        rec = (str(uid), str(name or uid), float(ts), clean)
        if key not in merged or rec[2] > merged[key][2]:
            merged[key] = rec
    return sorted(merged.values(), key=lambda row: row[2])[-max(1, int(limit)):]


def combine_context(context_rows, mention_rows, limit=24):
    """合并数据库上下文和当前艾特窗口，并标记哪些内容在喊机器人。

    同一人重复发送完全相同的文本视为催促噪声，只保留最新一份；这正是避免
    最终回复呈现为“逐条堆砌”的第一道结构性约束。
    """
    merged = {}
    for targeted, rows in ((False, context_rows), (True, mention_rows)):
        for uid, name, ts, text in rows:
            clean = _SPACE_RE.sub(" ", str(text or "")).strip()
            if not clean:
                continue
            key = (str(uid), clean)
            old = merged.get(key)
            rec = [str(uid), str(name or uid), float(ts), clean, targeted]
            if old is None:
                merged[key] = rec
            else:
                if rec[2] >= old[2]:
                    old[1], old[2], old[3] = rec[1], rec[2], rec[3]
                old[4] = bool(old[4] or targeted)
    rows = sorted(merged.values(), key=lambda row: row[2])
    return [tuple(row) for row in rows[-max(1, int(limit)):]]


def build_context_prompt(rows, max_msg=120):
    """把安静后的上下文变成“选主线自然接话”提示，而非逐项答题清单。"""
    lines = []
    for _uid, name, _ts, text, targeted in rows:
        mark = "〔这一波在喊你〕" if targeted else ""
        lines.append("%s：%s%s" % (name, text[:max_msg], mark))
    transcript = "\n".join(lines) or "（上下文暂时读取不到）"
    return (
        "刚才群里连续有人喊你，你已经先停下来等他们说完。下面是截至现在、"
        "安静下来以后重新读取的最新群聊上下文（按时间顺序）：\n\n%s\n\n"
        "先理解整段对话最后落在什么话题、后面的消息如何修正前面的意思，再像"
        "真人重新跟上聊天一样，只发一条自然回复。重点规则：\n"
        "1. 这不是待办清单，不要逐人、逐句回答，也不要把每条被艾特内容都塞进答案；\n"
        "2. 优先接最新且仍在继续的核心话头；重复催促、玩梗噪声、已被后文带过"
        "或互相冲突的旧话可以不回应；\n"
        "3. 若话题互不相干，宁可只选最值得接的一条，也不要用“另外、还有、至于”"
        "把几段答案硬拼起来；\n"
        "4. 不要@、不要点名，不要复述聊天记录，不要说你在整合/扫描上下文；"
        "保持大肥鱼平时简短自然的群聊语气。只输出最终要发的一条消息。"
    ) % transcript

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
