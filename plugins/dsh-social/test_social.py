# -*- coding: utf-8 -*-
"""dsh-social 纯逻辑与 SQLite 关系账本测试。

运行：docker exec astrbot python3 /AstrBot/data/plugins/dsh-social/test_social.py
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

# 插件本体只在运行期需要 AstrBot；这里用最小桩加载纯函数与 Store。
for name in (
    "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.core",
    "astrbot.core.agent", "astrbot.core.agent.message", "astrbot.core.platform",
    "astrbot.core.platform.message_type",
):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
sys.modules["astrbot.api.event"].AstrMessageEvent = object
sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
    platform_adapter_type=lambda *a, **k: (lambda f: f),
    on_llm_request=lambda *a, **k: (lambda f: f),
    command=lambda *a, **k: (lambda f: f),
    PlatformAdapterType=types.SimpleNamespace(ALL="all"),
)
sys.modules["astrbot.core"].logger = types.SimpleNamespace(
    info=lambda *a, **k: None, debug=lambda *a, **k: None, warning=lambda *a, **k: None)
sys.modules["astrbot.core.agent.message"].TextPart = type("TextPart", (), {"__init__": lambda self, text: setattr(self, "text", text)})
sys.modules["astrbot.core.platform.message_type"].MessageType = types.SimpleNamespace(GROUP_MESSAGE="group")

spec = importlib.util.spec_from_file_location("social", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

passed = failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name, detail)

# ----- 不揣测第三人称/群友关系
kind, affinity, trust, avoid, reason = m.classify_event("那些都是人机", directed=False)
check("第三人称泛称不扣分", kind == "" and affinity == 0, repr((kind, affinity)))
kind, affinity, trust, avoid, reason = m.classify_event("你们别吵了", directed=False)
check("群友互相冲突不扣分", kind == "" and affinity == 0, repr((kind, affinity)))
kind, affinity, trust, avoid, reason = m.classify_event("@大肥鱼 别插话了", directed=True)
check("明确边界仅小扣分", kind == "boundary" and affinity == -1 and avoid > 0, repr((kind, affinity, avoid)))
kind, affinity, trust, avoid, reason = m.classify_event("@大肥鱼 谢啦帮大忙", directed=True)
check("直接感谢微量加分", kind == "thanks" and affinity == 1 and trust == 1, repr((kind, affinity, trust)))
kind, affinity, trust, avoid, reason = m.classify_event("你个傻逼", directed=True)
check("直接辱骂才低幅扣分", kind == "direct_insult" and affinity == -2 and trust == -1, repr((kind, affinity, trust)))

# ----- 评分防刷与遗忘
check("单日正向上限", m.clamp_delta(4, 8, 10) == 2, str(m.clamp_delta(4, 8, 10)))
check("单日负向上限", m.clamp_delta(-4, -8, 10) == -2, str(m.clamp_delta(-4, -8, 10)))
check("正分会自然回落", 0 < m.decay_affinity(80, 30 * 86400, 30) < 80)
check("负分会自然回落", -80 < m.decay_affinity(-80, 30 * 86400, 30) < 0)

# ----- 档位/注入不能削弱明确请求，也不暴露分数或原话
avoid_rel = m.Relation(affinity=-80, trust=10)
check("负分是避让档", m.tier_of(avoid_rel) == "避让", m.tier_of(avoid_rel))
block = m.render_context(avoid_rel, is_direct_request=True)
check("避让仍要求完整回答", "必须直接完整回应" in block and "装死" in block, block)
check("注入不泄露分数或攻击原话", "-80" not in block and "傻逼" not in block, block)
known = m.Relation(affinity=60, trust=50)
check("高亲密高信任才亲近", m.tier_of(known) == "亲近", m.tier_of(known))
not_trusted = m.Relation(affinity=60, trust=20)
check("亲密不足信任降为熟人", m.tier_of(not_trusted) == "熟人", m.tier_of(not_trusted))

# ----- SQLite：每日封顶、退出即清事件且禁后续记录
async def database_checks():
    path = str(Path(tempfile.mkdtemp()) / "social.db")
    store = m.Store(path)
    try:
        # Store 使用启动时读取的 DSH_SOCIAL_DAILY_DELTA_CAP；此处用默认 ±10
        # 验证真实持久化路径也会把累计变化卡住，而不是在测试中篡改运行配置。
        _, first = await store.apply("g", "u", "test", 8, 0, 0, "测试")
        rel, second = await store.apply("g", "u", "test", 8, 0, 0, "测试")
        check("数据库单日累计封顶", first == 8 and second == 2 and rel.affinity <= 10, repr((first, second, rel)))
        await store.opt_out("g", "u")
        out = await store.relation("g", "u")
        check("退出后标记并清空", out.opted_out and out.affinity == 0, repr(out))
        after, delta = await store.apply("g", "u", "test", 2, 0, 0, "测试")
        check("退出后不再累积分数", after.opted_out and delta == 0, repr((after, delta)))
    finally:
        store.close()

asyncio.run(database_checks())
print("%d passed, %d failed" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
