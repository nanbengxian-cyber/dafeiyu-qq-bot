# -*- coding: utf-8 -*-
"""dsh-scene 的离线单测。stub 掉 astrbot，所以容器内外都能跑。

跑法：
    python3 test_scene.py
    docker exec astrbot python3 /AstrBot/data/plugins/dsh-scene/test_scene.py
"""

import importlib.util
import os
import sys
import types

# ---- stub astrbot（跟 dsh-drift / dsh-glossary 同一套写法）
for n in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
          "astrbot.core", "astrbot.core.agent", "astrbot.core.agent.message"):
    sys.modules.setdefault(n, types.ModuleType(n))
sys.modules["astrbot.api"].logger = types.SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None,
    error=lambda *a, **k: None, debug=lambda *a, **k: None)
sys.modules["astrbot.api.star"].Star = object
sys.modules["astrbot.api.star"].Context = object
sys.modules["astrbot.api.event"].AstrMessageEvent = object
sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
    on_llm_request=lambda *a, **k: (lambda f: f),
    on_decorating_result=lambda *a, **k: (lambda f: f),
    command=lambda *a, **k: (lambda f: f))


class _TP:
    def __init__(self, text=""):
        self.text = text


sys.modules["astrbot.core.agent.message"].TextPart = _TP

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("scene", os.path.join(HERE, "main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# ================================================================ 时段
# 阈值来自本群真语料（见 main.py 文件头的小时分布），不是拍的。

assert m.SLEEP_FROM == 3 and m.SLEEP_TO == 9, (m.SLEEP_FROM, m.SLEEP_TO)

for h in (3, 4, 5, 6, 7, 8):
    assert m.in_sleep(h), h
    assert m.period_of(h) == "sleep", h
for h in (9, 10, 12, 15, 20, 2):
    assert not m.in_sleep(h), h
for h in (21, 22, 23, 0, 1):
    assert m.period_of(h) == "peak", h
# 02:00 实测 44 条，比睡觉时段多得多、又远不到高峰，必须落「平时」
assert m.period_of(2) == "normal"
assert m.period_of(9) == "normal"     # 群主点头的是「3 点到 9 点」，9 点算醒
assert m.period_of(15) == "normal"    # 15:00 有 186 条的小高峰，但仍不是「最热闹」

# 跨午夜的写法也得对（万一以后改成 22~6）
_a, _b = m.SLEEP_FROM, m.SLEEP_TO
m.SLEEP_FROM, m.SLEEP_TO = 22, 6
assert m.in_sleep(23) and m.in_sleep(0) and m.in_sleep(5)
assert not m.in_sleep(6) and not m.in_sleep(12) and not m.in_sleep(21)
m.SLEEP_FROM, m.SLEEP_TO = _a, _b

# FROM == TO 视为不启用，别把一整天都判成睡觉
m.SLEEP_FROM = m.SLEEP_TO = 5
assert not m.in_sleep(5) and not m.in_sleep(12)
m.SLEEP_FROM, m.SLEEP_TO = _a, _b

# 关掉总开关就永远不是睡觉时段
_on = m.SLEEP_ON
m.SLEEP_ON = False
assert not m.in_sleep(4) and m.period_of(4) == "normal"
m.SLEEP_ON = _on

# ================================================================ 时段那一行
sleep_line = m.period_line(4)
assert "犯困" in sleep_line and "慢半拍" in sleep_line, sleep_line
assert "%" not in sleep_line.replace("0.6%", ""), "百分号没转义好：" + sleep_line
peak_line = m.period_line(22)
assert "最热闹" in peak_line, peak_line
assert "犯困" not in peak_line
normal_line = m.period_line(14)
assert "不冷不热" in normal_line and "犯困" not in normal_line

# ================================================================ 渲染
b = m.render("神人乐子群", 98, 22, "")
assert b.startswith("<scene>") and b.rstrip().endswith("</scene>")
assert "神人乐子群" in b and "98 人" in b
assert "暗区突围" in b and "三角洲行动" in b
assert "最热闹" in b
assert "正在跟你说话" not in b            # 普通群友不加这一行

b_admin = m.render("神人乐子群", 98, 22, "admin")
assert "这个人是群里的管理员" in b_admin
b_owner = m.render("神人乐子群", 98, 22, "owner")
assert "是群主" in b_owner

b_sleep = m.render("神人乐子群", 98, 4, "")
assert "犯困" in b_sleep and "最热闹" not in b_sleep

# 群名还没查到时：不许输出「群：」那行，但块本身仍然成立（背景+时段有用）
b_noname = m.render("", 0, 22, "")
assert "群：" not in b_noname and "暗区突围" in b_noname

# 人数为 0 时不输出「0 人」
b_nocount = m.render("神人乐子群", 0, 14, "")
assert "神人乐子群" in b_nocount and "0 人" not in b_nocount

# ================================================================ 预算
# 预算是硬上限。宁可少注入，也不许超。
for budget in (80, 120, 200, 300, 400, 1000):
    bb = m.render("神人乐子群", 98, 4, "admin", budget=budget)
    body = bb[len(m.HEADER):-len(m.FOOTER)] if bb else ""
    assert len(body.strip()) <= budget, (budget, len(body))
# 预算极小时可以为空，但绝不能抛异常
assert m.render("神人乐子群", 98, 4, "admin", budget=1) == "" or True

# 正常情况下的块长要在可接受范围（别悄悄膨胀）
full = m.render("神人乐子群", 98, 4, "admin")
assert 200 <= len(full) <= 700, len(full)

# ================================================================ 标签形状
# dsh-ctxclean 靠 ^\s*<([a-z][a-z0-9_]*)> 结构性地清历史里的陈旧块。
# 标签必须是纯小写，否则清不掉，就会像当年那样在历史里堆几百份。
import re
tag = re.match(r"^<([a-z][a-z0-9_]*)>", m.HEADER)
assert tag and tag.group(1) == "scene", m.HEADER[:40]
assert m.FOOTER == "</scene>"

# ================================================================ 群名不许写死
# 群主原话：「名字很多时候都是管理员改的」。实测旧文档写的「AAA精神病院病友交流群」
# 已经变成「神人乐子群」。所以代码里一个群名都不许出现。
src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
body_src = src.split('"""', 2)[2] if src.count('"""') >= 2 else src   # 跳过文件头注释
assert "神人乐子群" not in body_src, "群名被写死进代码了"
assert "AAA精神病院" not in body_src, "旧群名被写死进代码了"

# 作息旋钮不许写死在两处：dsh-decide 读的是同一批 DSH_SLEEP_* env
for k in ("DSH_SLEEP", "DSH_SLEEP_FROM", "DSH_SLEEP_TO"):
    assert k in src, k

# 群名单默认只作用于主群（跟其它插件一致：语料群必须全程静音）
assert m.GROUPS == {"100000001"}, m.GROUPS

print("SCENE_TEST_OK 睡觉=%d~%d点 高峰=%s 背景=%d字 块长=%d 预算=%d"
      % (m.SLEEP_FROM, m.SLEEP_TO, sorted(m.PEAK_HOURS), len(m.ABOUT), len(full), m.BUDGET))
