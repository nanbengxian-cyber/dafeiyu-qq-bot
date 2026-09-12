# -*- coding: utf-8 -*-
"""dsh-web 联想搜索纯逻辑回归测试。"""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "association_logic", Path(__file__).with_name("association_logic.py")
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def decision(text, **kwargs):
    return m.decide_associative_search(text, **kwargs)


# 强时效：不受探索抽样影响。
for text in (
    "DeepSeek最近有什么新消息",
    "这个游戏现在还值得玩吗",
    "Claude新模型发布了吗",
    "这个平台是不是已经倒闭了",
):
    ok, query, kind, why = decision(text, roll=0.99, rate=0.0)
    assert ok and query and kind == "fresh" and why == "时效信息", (text, decision(text))

# 探索联想：会适当发散，但保留概率闸门。
for text in (
    "哈基米这个梗哪来的",
    "听说OpenAI又整了个新东西",
    "这两款显卡哪个好",
    "MCP到底是干嘛的？",
):
    assert decision(text, roll=0.1, rate=0.5)[0], (text, decision(text, roll=0.1))
    assert decision(text, roll=0.9, rate=0.5)[3] == "探索抽样未中"

# 边界：不为日常感叹、无对象代词、办事请求或已有主路径乱搜。
for text in (
    "今天累死我了",
    "我去吃饭了",
    "这个是真的吗",
    "帮我改一下这段代码",
    "你觉得这也太离谱了吧",
    "好像也是",
    "真的假的直接踢",
    "你好像还很兴奋🤔",
    "那感觉跟贴吧直接放进去没区别",
    "我要把这两天都没更新的量都给他更过来",
):
    assert not decision(text, roll=0.0)[0], (text, decision(text, roll=0.0))
assert not decision("DeepSeek最近有什么新消息", explicit=True)[0]
assert not decision("DeepSeek最近有什么新消息", has_link=True)[0]
assert not decision("/联网状态", roll=0.0)[0]

# 查询清理：不要把 QQ 展示型 @ 和聊天填充词带给搜索引擎。
assert m.build_association_query("话说，哈基米这个梗哪来的？") == "哈基米这个梗哪来的"
assert m.build_association_query("@难谓言(2774067216) DeepSeek最近有什么更新？") == "DeepSeek最近有什么更新"

print("ASSOCIATION_LOGIC_TEST_OK")
