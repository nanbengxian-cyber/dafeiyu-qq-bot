# -*- coding: utf-8 -*-
"""dsh-merge 纯逻辑回归测试；宿主机或 AstrBot 容器均可直接运行。"""

import os
import sqlite3
import tempfile

from merge_logic import build_context_prompt, combine_context, read_recent_context


def test_read_recent_context():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        for table in ("buffer", "archive"):
            con.execute(
                "CREATE TABLE %s (user_id TEXT, name TEXT, ts REAL, text TEXT, group_id TEXT)" % table
            )
        con.execute("INSERT INTO archive VALUES (?,?,?,?,?)", ("1", "甲", 1, "早先的话", "g"))
        con.execute("INSERT INTO archive VALUES (?,?,?,?,?)", ("2", "乙", 2, "  新  话题  ", "g"))
        # buffer/archive 的同一消息只留一份，并保留较新的时间。
        con.execute("INSERT INTO buffer VALUES (?,?,?,?,?)", ("2", "乙", 3, "新 话题", "g"))
        con.commit()
        rows = read_recent_context("g", 10, path)
        assert rows == [("1", "甲", 1.0, "早先的话"), ("2", "乙", 3.0, "新 话题")], rows
    finally:
        os.unlink(path)


def test_combine_and_prompt():
    context = [
        ("1", "甲", 1, "旧问题"),
        ("2", "乙", 2, "现在聊猫"),
        ("3", "丙", 3, "猫粮怎么选"),
    ]
    mentions = [
        ("1", "甲", 1, "旧问题"),
        ("3", "丙", 3, "猫粮怎么选"),
        ("3", "丙", 4, "猫粮怎么选"),
    ]
    rows = combine_context(context, mentions, 24)
    assert len(rows) == 3, rows
    assert rows[-1][3:] == ("猫粮怎么选", True), rows[-1]
    prompt = build_context_prompt(rows)
    assert "这不是待办清单" in prompt
    assert "宁可只选最值得接的一条" in prompt
    assert "〔这一波在喊你〕" in prompt
    assert "连问" not in prompt
    assert "另外说" not in prompt


def test_context_limit_keeps_latest():
    context = [(str(i), str(i), i, "消息%d" % i) for i in range(30)]
    rows = combine_context(context, [], 8)
    assert [row[3] for row in rows] == ["消息%d" % i for i in range(22, 30)]


if __name__ == "__main__":
    test_read_recent_context()
    test_combine_and_prompt()
    test_context_limit_keeps_latest()
    print("dsh-merge tests passed")
