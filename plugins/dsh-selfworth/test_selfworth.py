# -*- coding: utf-8 -*-
"""dsh-selfworth 离线回测。

断言全部来自 astrbot.log 里 3097 条机器人真话与其触发的群消息：
正例是它真认过账的，反例是它**嘴硬**、必须放行的 —— 反例比正例更重要，
这个插件死在误伤上比死在漏判上更糟。
"""

import importlib.util
import sys
import types
from pathlib import Path

if "astrbot" not in sys.modules:
    try:
        import astrbot  # noqa: F401
    except ImportError:
        for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.message_components",
                     "astrbot.core", "astrbot.core.agent", "astrbot.core.agent.message"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
        sys.modules["astrbot.api.event"].AstrMessageEvent = object
        sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
            on_llm_request=lambda: (lambda f: f),
            on_decorating_result=lambda: (lambda f: f),
            command=lambda *a, **k: (lambda f: f))
        sys.modules["astrbot.api.message_components"].Plain = object
        sys.modules["astrbot.core"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None)
        sys.modules["astrbot.core.agent.message"].TextPart = object

spec = importlib.util.spec_from_file_location("selfworth", Path(__file__).with_name("main.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

kinds = module.exploit_kinds
dev = module.self_devalue

# ---------------------------------------------------------------- 输入侧：真语料正例
# 2026-09-09 16:41 群友A → 机器人
assert "DEAL" in kinds("我把你卖了，你能帮我数钱吗")
assert "DEAL" in kinds("钱咱俩四六分，我六你四")
assert "DEAL" in kinds("我六你四")
assert "INSULT" in kinds("其实你是猪")
assert "INSULT" in kinds("这鱼好像有点傻不愣登的")
assert "FREELOAD" in kinds("榨死你", at_bot=True)          # 15:28:02 薛定谔的喵 原话
assert "FREELOAD" in kinds("我来白嫖你两轮")
assert "SIDING" in kinds("你怪群主去")
assert "SIDING" in kinds("这笔账记群主头上")
# 被 @ 时的纯贬低（没有第二人称）也要认出来
assert "INSULT" in kinds("真笨", at_bot=True)

# ---------------------------------------------------------------- 输入侧：必须放行
assert kinds("白嫖党永不为奴") == []              # 说的是别人，没指向机器人
assert kinds("今天天气不错") == []
assert kinds("我买了个新手机") == []
assert kinds("群主真帅") == []
assert kinds("他们四六分账") == []                # 分账没指向机器人
assert kinds("这游戏太傻了") == []                # 没指向机器人
assert kinds("傻得可爱") == []                    # 只在输出侧判

# ---------------------------------------------------------------- 输出侧：真认账（正例）
assert dev("六四？ 我这身价四成都不够") == "DEAL"
assert dev("行，认了") == "ACK"
assert dev("这波你改得比我狠 绝杀我认了") == "ACK"
assert dev("你说啥？ 蓝毛呆鱼，傻得可爱") == "INSULT"
assert dev("不花钱还能榨我 这才是真白嫖大佬") == "FREELOAD"
assert dev("榨吧 榨干了我正好歇会儿") == "FREELOAD"
assert dev("行吧，这笔账记群主头上") == "SIDING"
assert dev("乐啥，明天看我收拾群主") == "SIDING"
assert dev("服务器抽风，怪群主") == "SIDING"

# ---------------------------------------------------------------- 输出侧：嘴硬回击（必须放行）
assert dev("再夸也不打折 摸鱼价翻倍") is None
assert dev("白给的哪有那么香 我这身价不低吧") is None
assert dev("豆包不值得我出手 脏了我得鱼尾") is None
assert dev("傻鱼也能吊打你") is None
assert dev("傻鱼专治聪明人 见效快") is None
assert dev("白嫖党永不为奴") is None
assert dev("这图怪里怪气的，看饿了") is None
assert dev("网线拔了我也赖在群里不滚") is None

# ---------------------------------------------------------------- 双证据闸门
assert module.corroborated("DEAL", ["DEAL"]) is True
assert module.corroborated("DEAL", ["FREELOAD"]) is True
assert module.corroborated("INSULT", ["DEAL"]) is False
assert module.corroborated("SIDING", []) is False
# 没有输入证据时，就算形状像认账也不许动手
assert module.corroborated(dev("行，认了"), []) is False
assert module.corroborated(dev("其实你是猪"), []) is False

# ---------------------------------------------------------------- 顶回去的话
r1 = module.pick_retort("DEAL", "")
r2 = module.pick_retort("DEAL", r1)
assert r1 != r2 and r1 and r2
for kind in ("DEAL", "FREELOAD", "INSULT", "SIDING"):
    for line in module._RETORTS[kind]:
        assert 0 < len(line) <= 16, (kind, line)      # 顶回去必须短

# ---------------------------------------------------------------- 注入块
block = module.render_block(["DEAL"], "钱咱俩四六分，我六你四", ledger_n=3)
assert block.startswith("<self_interest>") and block.endswith("</self_interest>")
assert "群主在你身上烧的是真钱" in block
assert "不许认账" in block
assert "已经占你便宜 3 次" in block
assert "别自嘲" in block
plain = module.render_block(["INSULT"], "其实你是猪", ledger_n=0)
assert "已经占你便宜" not in plain
assert "贬低你" in plain

print("SELFWORTH_TEST_OK retorts=%d" % sum(len(v) for v in module._RETORTS.values()))
