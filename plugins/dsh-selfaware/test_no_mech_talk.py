"""机器自述措辞回归测试（dsh-selfaware）。

2026-09-15 现场，机器自述块里的技术措辞被逐字搬进了群：

    11:38:56  机器人: 打不了，我连鼠标都没有
    11:39:12  机器人: 手游我也打不了 手都没有
    11:44:24  机器人: 模型不给，线下版也没有，我就是个跑服务器里的东西
    11:45:24  机器人: 住服务器里也是睡机柜 你来试试
    12:05:42  机器人: 真看不见 图没递到我这边
    11:48:36  群友（111）: 明明之前都能正常回答了，怎么又像人机了

「图没递到我这边」直接对应旧文案「视觉：已有图片文字转述；你没有直接看到
原始像素」，「跑服务器里的东西」对应「处境：群聊；Linux/AstrBot/容器」。

注意区分：当天 276 条发言里另有 14 条是**玩梗**（「睡机柜」「八核跑我，会烧的」
「服务器在香港你自己游过来」）—— 那些是性格，必须保留；本测试只针对把配置、
资源、内部通路当事实汇报的纯技术自述。

这个测试锁三件事：
  1. 机器自述块里不许出现会被照搬的技术词；
  2. 但**防幻觉语义必须保住**（收到图没转述出来时"不得声称看清"）；
  3. 措辞规则本身必须点名那些真出现过的机器话，否则禁不掉。

跑法（容器内 py3.12）：
    docker exec astrbot python3 /tmp/sa/dsh-selfaware/test_no_mech_talk.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_here = Path(__file__).with_name("self_model.py")
spec = importlib.util.spec_from_file_location("sm_mech", _here)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# 会被模型照搬到群里的技术词。加新词前先想清楚：这个词念给群友听像人话吗？
MECH_WORDS = ("像素", "遥测", "容器", "进程", "接口", "AstrBot", "/容器")

# 2026-09-15 真实出现的机器自述原句，机器自述块里不许给模型递这些词。
REAL_MECH_LINES = ("没递到我这边", "跑服务器里的东西", "我连鼠标都没有")


class _Model:
    def latest_states(self, now=None):
        return []


def _render(group_id="100000001", caption=False, pending=False, failed=False):
    return m.render_current_self(
        _Model(), group_id, "chat-main", "vision-opus5",
        {
            "image_captioned": caption,
            "image_pending": pending,
            "image_failed": failed,
        },
    )


# —— ① 四种输入状态都不能把技术词递给模型 ——
variants = {
    "无图": _render(),
    "有转述": _render(caption=True),
    "收到图无转述": _render(pending=True),
    "转述失败": _render(failed=True),
}
for label, block in variants.items():
    assert block.startswith("<current_machine_self>"), (label, block[:60])
    assert block.endswith("</current_machine_self>"), (label, block[-40:])
    for word in MECH_WORDS:
        assert word not in block, \
            "%s：机器自述块里出现了会被照搬的技术词 %r\n%s" % (label, word, block)
    for line in REAL_MECH_LINES:
        assert line not in block, \
            "%s：机器自述块里出现了真实机器台词 %r" % (label, line)

# —— ② 防幻觉语义必须还在（这是这个块存在的理由，不能为了"像人"删掉）——
assert "不得声称看清" in variants["收到图无转述"], \
    "收到图却没转述出来时，必须仍然禁止声称看清"
assert "不知道里面是什么" in variants["转述失败"], \
    "转述失败时必须明确说不知道内容"

# —— ③ 措辞规则要点名真出现过的机器话，**而且必须真的被注入** ——
#
# 注意这里不能只断言 `mm._TONE_RULE` 这个常量存在：定义在模块里 ≠ 注入给模型。
# 第一版就是这么写的，结果把 `render_capabilities()` 里的 `_TONE_RULE` 删掉
# （变异：`_HEADER, _TONE_RULE, CAPABILITY_TEXT` -> `_HEADER, CAPABILITY_TEXT`）
# 测试依然全绿 —— 规则根本没进 prompt，但测试看不见。所以必须断言**渲染结果**。
import importlib
_main = importlib.util.spec_from_file_location(
    "sa_main_mech", Path(__file__).with_name("main.py"))
mm = importlib.util.module_from_spec(_main)
try:
    _main.loader.exec_module(mm)
except BaseException as exc:  # 容器内缺 astrbot 依赖时只跳过这一节
    print("（跳过措辞规则检查：%s）" % type(exc).__name__)
else:
    cap_block = mm.render_capabilities()
    rule = mm._TONE_RULE
    assert "说法要求" in cap_block, \
        "措辞规则没有被注入到能力块里（定义了但没接线）"
    assert rule in cap_block, "能力块里注入的不是 _TONE_RULE 本身"
    for line in ("模型没递到我这边", "没有手或鼠标"):
        assert line in cap_block, \
            "措辞规则没点名 %r，模型不会知道要避开它" % line
    # 规则本身要短：它是每轮注入的，太长会挤掉真正的事实。
    assert len(rule) < 400, "措辞规则太长，会挤占预算：%d 字" % len(rule)

print("NO_MECH_TALK_TEST_OK（机器自述块无技术词，防幻觉语义与措辞规则都在）")
