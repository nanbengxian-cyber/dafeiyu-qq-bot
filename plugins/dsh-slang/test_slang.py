# -*- coding: utf-8 -*-
"""dsh-slang 情景回归（2026-09-07）。

覆盖黑话学习流水线的 12 个情景：
  1. watch 只收白名单群普通群消息（私聊/命令/机器人自己/纯噪声 不进缓冲）
  2. 攒够 MIN_MSG 触发提取 -> 候选入库 candidate+count=1+证据
  3. 同一词再现 -> count 递增、证据追加去重
  4. 拉丁词词边界：ds 不命中 friends（注入匹配）
  5. 提取 prompt 防注入：<script> 被转义、明说语料不可信
  6. 新候选自动考究 -> meaning/example 写入、status 仍 candidate
  7. 考究不确定 -> meaning="不确定"，确认后也不注入
  8. 注入：confirmed+释义命中 -> req.extra_user_content_parts 追加
  9. 影子模式：不追加、shadow_hits+1、shadow 记录落盘
 10. 注入排序：次数高优先，最多 INJECT_MAX 条
 11. 拒绝词不重复入库；/黑话确认 /黑话拒绝 /黑话备注 生效并持久化
 12. 噪声过滤：纯标点/URL/全常见字不进词库；单字词条首次不建库

跑法：python3 test_slang.py（宿主可跑，无需 astrbot 环境）
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
from collections import deque

# ---------------------------------------------------------------- 桩
for n in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.message_components",
          "astrbot.api.star", "astrbot.core", "astrbot.core.platform",
          "astrbot.core.platform.message_type", "astrbot.core.agent",
          "astrbot.core.agent.message"):
    sys.modules.setdefault(n, types.ModuleType(n))
sys.modules["astrbot.api.event"].AstrMessageEvent = object
sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
    command=lambda *a, **k: (lambda f: f),
    on_llm_request=lambda: (lambda f: f),
    platform_adapter_type=lambda *a, **k: (lambda f: f),
    PlatformAdapterType=types.SimpleNamespace(ALL="ALL"))


class StarStub:
    """astrbot.api.star.Star：no-op 构造，让测试能真正走 Main.__init__。"""
    def __init__(self, *a, **k):
        pass


sys.modules["astrbot.api.star"].Star = StarStub
sys.modules["astrbot.api.star"].Context = object
sys.modules["astrbot.core.platform.message_type"].MessageType = types.SimpleNamespace(
    GROUP_MESSAGE="GROUP")


class TextPartStub:
    def __init__(self, text=None, **k):
        self.text = text


sys.modules["astrbot.core.agent.message"].TextPart = TextPartStub
sys.modules["astrbot.core"].logger = types.SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None,
    error=lambda *a, **k: None, debug=lambda *a, **k: None)

sp = importlib.util.spec_from_file_location("m", "main.py")
m = importlib.util.module_from_spec(sp)
sp.loader.exec_module(m)

# 测试用：状态文件与阈值调小，保持默认值本身不动
_TMP = tempfile.mkdtemp(prefix="dsh-slang-test-")
m._STATE = os.path.join(_TMP, "slang.json")
m.MIN_MSG = 3
m.COOLDOWN = 0
m.SAMPLE = 30
m.INJECT_MAX = 4
m.RESEARCH = True
m.RESEARCH_BATCH = 3

_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def reset():
    m._loaded = False
    m._entries, m._shadow, m._meta = [], [], {}
    m._buffers.clear()
    m._extract_pending.clear()
    m._last_extract.clear()
    m._single_seen.clear()
    m._state_lock = asyncio.Lock()
    m._auto_log.clear()
    m._started_auto = False
    m._stat.update(seen=0, extracted=0, researched=0, injected=0,
                   inject_hits=0, shadow_hits=0, confirmed=0, rejected=0, skip=0)
    if os.path.exists(m._STATE):
        os.remove(m._STATE)
    return m._STATE


def _plugin(llm_responses=None):
    plugin = object.__new__(m.Main)
    plugin.context = types.SimpleNamespace(
        send_message=lambda sess, chain: None,
        llm_generate=lambda **kw: None,
        get_current_chat_provider_id=lambda gid: "p1")
    plugin._lock = asyncio.Lock()
    queue = deque(llm_responses or [])

    async def fake_generate(star_ctx, gid, prompt, system=""):
        return queue.popleft() if queue else None

    m._generate = fake_generate
    return plugin


class FakeEvent:
    def __init__(self, gid, uid, name, text, wake=True, msg_type="GROUP"):
        self.message_str = text
        self.message_obj = types.SimpleNamespace(timestamp=1234567890.0)
        self._gid, self._uid, self._name = gid, uid, name
        self._type = msg_type
        self._self = "botqq"
        self.is_at_or_wake_command = wake
    def get_message_type(self):
        return self._type
    def get_group_id(self):
        return self._gid
    def get_sender_id(self):
        return self._uid
    def get_sender_name(self):
        return self._name
    def get_self_id(self):
        return self._self
    def get_extra(self, k):
        return None
    def plain_result(self, text):
        return text


def feed_watch(ev, llm_responses=None):
    plugin = _plugin(llm_responses)
    _LOOP.run_until_complete(plugin.watch(ev))
    _LOOP.run_until_complete(asyncio.sleep(0.05))  # 让 ensure_future 的提取/考究任务跑完


def feed_inject(ev, shadow_override=None):
    plugin = _plugin()
    old = m.SHADOW
    if shadow_override is not None:
        m.SHADOW = shadow_override
    req = types.SimpleNamespace(extra_user_content_parts=[])
    _LOOP.run_until_complete(plugin.inject(ev, req))
    m.SHADOW = old
    return req


def add_confirmed(word, meaning="群内黑话，需要语境才能懂", count=1):
    """直接入库一个已确认词条（模拟 LLM 考究 + 群主确认的最终态）。"""
    m._ensure_state()
    e, _ = m._upsert(m._entries, word, {"uid": "u1", "name": "A", "text": word, "ts": 1.0})
    e["meaning"] = meaning
    e["count"] = count
    e["status"] = m.CONFIRMED
    return e


def run_cmd(name, ev):
    """命令是 async generator：驱动它收集 yield 的结果。"""
    plugin = _plugin()
    method = getattr(plugin, name)
    out = []

    async def _drain():
        async for x in method(ev):
            out.append(x)

    _LOOP.run_until_complete(_drain())
    return out


# ================================================================ 情景测试
def t1_watch_filter():
    """情景1：watch 只收白名单群普通群消息（私聊/命令/自己/纯噪声不进缓冲）。"""
    reset()
    gid = "476573490"
    feed_watch(FakeEvent(gid, "u1", "A", "今天又出bug了"))
    feed_watch(FakeEvent(gid, "u2", "B", "私聊不该进", msg_type="PRIVATE"))
    feed_watch(FakeEvent(gid, "u3", "C", "/黑话候选"))
    feed_watch(FakeEvent(gid, "u3", "C", "黑话确认 神了"))  # waking_check 剥掉 / 后的形态
    feed_watch(FakeEvent("999999", "u4", "D", "别的群不该进"))
    feed_watch(FakeEvent(gid, "botqq", "大肥鱼", "我自己说的话"))
    feed_watch(FakeEvent(gid, "u5", "E", "。。。。。"))
    buf = m._buffers.get(gid)
    assert buf is not None and len(buf) == 1, "只该收第一条，实际 %d" % (len(buf) if buf else -1)
    assert buf[0][2] == "今天又出bug了"
    assert m._stat["seen"] == 1
    print("✓ 情景1 watch 过滤：只收白名单群普通消息")


def t2_extract_trigger():
    """情景2：攒够 MIN_MSG 触发提取 -> 候选入库 candidate+count=1+证据；
    幻觉候选（语料里没出现过的词）被硬性拦截。"""
    reset()
    gid = "476573490"
    resp = json.dumps([{"content": "人机", "source_id": "2"},
                       {"content": "神了", "source_id": "3"},
                       {"content": "虚空词", "source_id": "9"}])  # 语料里没有 -> 幻觉
    feed_watch(FakeEvent(gid, "u1", "A", "这段子太典了"))
    feed_watch(FakeEvent(gid, "u2", "B", "你就是个神了"))
    feed_watch(FakeEvent(gid, "u3", "C", "这人机发言哈哈哈哈"), [resp])
    assert m._stat["extracted"] == 1, "第3条应触发一次提取"
    assert m._stat["researched"] == 0, "没给考究响应，不应考究"
    got = {e["content"]: e for e in m._entries}
    assert "人机" in got and "神了" in got, "候选应入库，实际 %s" % list(got)
    assert "虚空词" not in got, "幻觉候选必须被拦截"
    assert m._stat["skip"] >= 1, "幻觉候选应计入 skip"
    assert got["人机"]["status"] == m.CANDIDATE
    assert got["人机"]["count"] == 1
    assert got["人机"]["evidence"][0]["text"] == "这人机发言哈哈哈哈", "证据应指向含词的最近消息"
    assert got["神了"]["evidence"][0]["text"] == "你就是个神了"
    print("✓ 情景2 提取触发：3条触发，候选入库带证据，幻觉候选被拦")


def t3_repeat_count():
    """情景3：同一词再现 -> count 递增、证据追加去重。"""
    reset()
    gid = "476573490"
    r1 = json.dumps([{"content": "神了", "source_id": "1"}])
    r2 = json.dumps([{"content": "神了", "source_id": "1"}])
    feed_watch(FakeEvent(gid, "u1", "A", "神了"))
    feed_watch(FakeEvent(gid, "u2", "B", "也神了"))
    feed_watch(FakeEvent(gid, "u3", "C", "真神了"), [r1])
    feed_watch(FakeEvent(gid, "u4", "D", "又神了"), [r2])
    e = next(x for x in m._entries if x["content"] == "神了")
    assert e["count"] == 2, "两次提取到同一词应 count=2，实际 %d" % e["count"]
    texts = [ev["text"] for ev in e["evidence"]]
    assert texts[0] == "真神了" and "又神了" in texts, "证据按时间序收集：%s" % texts
    # 证据按 (text,name) 去重：同一句话再进不重复
    before = len(e["evidence"])
    _m, _ = m._upsert(m._entries, "神了", {"uid": "u4", "name": "D", "text": "又神了", "ts": 1.0})
    assert len(e["evidence"]) == before, "同一证据不应重复追加"
    print("✓ 情景3 重复词：count 递增、证据去重")


def t4_latin_boundary():
    """情景4：拉丁词词边界：ds 不命中 friends、命中「就ds喽」。"""
    reset()
    add_confirmed("ds", "DeepSeek 简称")
    hits = m.matched(m._entries, "用friends聊天有点卡")
    assert hits == [], "friends 不该命中 ds：%r" % [e["content"] for e in hits]
    hits = m.matched(m._entries, "那就ds喽")
    assert [e["content"] for e in hits] == ["ds"], "「就ds喽」应命中 ds"
    print("✓ 情景4 词边界：ds 不误中 friends")


def t5_prompt_escape():
    """情景5：提取 prompt 防注入：<script> 转义、明说语料不可信。"""
    msgs = [("u1", "A", '<script>alert("pwn")</script> 神了', 1.0)]
    p = m.build_extraction_prompt(msgs)
    assert "<script>" not in p, "原始标签不该进 prompt（防注入）"
    assert "&lt;script&gt;" in p, "应被转义"
    assert "不可信" in p, "应明说语料不可信"
    print("✓ 情景5 prompt 防注入：转义 + 语料不可信声明")


def t6_research():
    """情景6：新候选自动考究 -> meaning/example 写入、status 仍 candidate。"""
    reset()
    gid = "476573490"
    r_extract = json.dumps([{"content": "鼠鼠", "source_id": "1"}])
    r_research = json.dumps({"content": "鼠鼠",
                             "meaning": "三角洲里穿便宜装备闷头捡东西的玩家，自嘲",
                             "example": "我就是个鼠鼠", "confirmed": True})
    feed_watch(FakeEvent(gid, "u1", "A", "鼠鼠这词啥意思"))
    feed_watch(FakeEvent(gid, "u2", "B", "笑死鼠鼠"))
    feed_watch(FakeEvent(gid, "u3", "C", "真鼠鼠了"), [r_extract, r_research])
    e = next(x for x in m._entries if x["content"] == "鼠鼠")
    assert e["meaning"] == "三角洲里穿便宜装备闷头捡东西的玩家，自嘲", e["meaning"]
    assert e["example"] == "我就是个鼠鼠"
    assert e["status"] == m.CANDIDATE, "考究不转正，等群主确认"
    assert m._stat["researched"] == 1
    print("✓ 情景6 考究：含义/例句写入，状态仍候选")


def t7_uncertain():
    """情景7：考究不确定 -> meaning=不确定，确认后也不注入。"""
    reset()
    m._ensure_state()
    e, _ = m._upsert(m._entries, "某词", {"uid": "u1", "name": "A", "text": "某词", "ts": 1.0})
    info = m.parse_research(json.dumps({"content": "某词", "meaning": "", "confirmed": False}))
    e["meaning"] = info["meaning"] or "不确定"
    assert e["meaning"] == "不确定"
    e["status"] = m.CONFIRMED  # 群主误确认也不行
    req = feed_inject(FakeEvent("476573490", "u9", "Z", "今天某词了吗"))
    assert req.extra_user_content_parts == [], "含义不确定的词条即使确认也不注入"
    print("✓ 情景7 不确定：空含义确认后仍不注入")


def t8_inject_live():
    """情景8：confirmed+释义命中 -> req.extra_user_content_parts 追加。"""
    reset()
    add_confirmed("神了", "表示惊叹/离谱")
    req = feed_inject(FakeEvent("476573490", "u1", "A", "今天这波真神了"), shadow_override=False)
    assert len(req.extra_user_content_parts) == 1, "应注入 1 块"
    block = req.extra_user_content_parts[0].text
    assert "神了" in block and "惊叹" in block, block
    assert m._stat["injected"] == 1
    print("✓ 情景8 正式注入：命中确认词条 -> 上下文追加")


def t9_shadow():
    """情景9：影子模式：不追加、shadow_hits+1、shadow 记录落盘。"""
    reset()
    add_confirmed("神了", "表示惊叹/离谱")
    req = feed_inject(FakeEvent("476573490", "u1", "A", "今天这波真神了"), shadow_override=True)
    assert req.extra_user_content_parts == [], "影子模式绝不改上下文"
    assert m._stat["shadow_hits"] == 1 and m._stat["injected"] == 0
    assert m._shadow and m._shadow[-1]["terms"] == ["神了"], m._shadow
    # 落盘节流默认 30s，测试里第一次必然落盘
    data = json.load(open(m._STATE, encoding="utf-8"))
    assert data["shadow"] and data["shadow"][-1]["terms"] == ["神了"], "shadow 应持久化"
    print("✓ 情景9 影子模式：只记录不注入、shadow 落盘")


def t10_inject_order():
    """情景10：注入排序：次数高优先，最多 INJECT_MAX 条。"""
    reset()
    add_confirmed("甲词", "释义甲", count=5)
    add_confirmed("乙词", "释义乙", count=1)
    for i in range(5):
        add_confirmed("低频词%d" % i, "释义%d" % i, count=1)
    req = feed_inject(FakeEvent("476573490", "u1", "A",
                                "甲词乙词低频词0低频词1低频词2低频词3低频词4"),
                      shadow_override=False)
    block = req.extra_user_content_parts[0].text
    assert block.index("甲词") < block.index("乙词"), "次数高应排前"
    assert m.matched([e for e in m._entries if e["status"] == m.CONFIRMED],
                     "甲词乙词低频词0低频词1低频词2低频词3低频词4")[:1][0]["content"] == "甲词"
    hits = m.matched(m._entries, "甲词乙词低频词0低频词1低频词2低频词3低频词4")
    assert len(hits) == m.INJECT_MAX, "最多注入 %d 条，实际 %d" % (m.INJECT_MAX, len(hits))
    # 预算：超长释义/例句时 render 自动退化成只给释义并裁到 BUDGET 内
    e_long = add_confirmed("长词", "释义" * 300, count=9)
    e_long["example"] = "例" * 200
    block2 = m.render(m.matched([e for e in m._entries if e["status"] == m.CONFIRMED],
                                "长词 甲词 乙词"))
    assert len(block2) <= m.BUDGET + 2, "render 必须压到预算内，实际 %d 字" % len(block2)
    assert "长词" in block2 and "｜例" not in block2, "超预算应退化只给释义"
    print("✓ 情景10 注入排序：次数优先 + INJECT_MAX 上限 + 预算兜底")


def t11_commands_and_persist():
    """情景11：拒绝词不重复入库；/黑话确认 /黑话拒绝 /黑话备注 生效并持久化。"""
    reset()
    m._ensure_state()
    e, _ = m._upsert(m._entries, "乐子", {"uid": "u1", "name": "A", "text": "乐子", "ts": 1.0})
    # 拒绝
    out = run_cmd("reject", FakeEvent("476573490", "2774067216", "群主", "/黑话拒绝 乐子"))
    assert "已拒绝" in out[0], out
    # 已拒绝的词再提取不重新入库、count 不涨
    before = e["count"]
    got, created = m._upsert(m._entries, "乐子", {"uid": "u2", "name": "B", "text": "乐子", "ts": 2.0})
    assert got is None and e["count"] == before, "拒绝词不重复入库"
    # 确认
    e2, _ = m._upsert(m._entries, "人机", {"uid": "u1", "name": "A", "text": "人机", "ts": 1.0})
    out = run_cmd("confirm", FakeEvent("476573490", "2774067216", "群主", "/黑话确认 人机"))
    assert "已确认「人机」" in out[0], out
    assert e2["status"] == m.CONFIRMED
    # 备注
    out = run_cmd("remark", FakeEvent("476573490", "2774067216", "群主", "/黑话备注 人机 说别人像机器人"))
    assert "已备注" in out[0] and e2["meaning"] == "说别人像机器人", out
    # 持久化往返
    entries, shadow, meta = m.load_state(m._STATE)
    got2 = {x["content"]: x for x in entries}
    assert got2["人机"]["status"] == m.CONFIRMED
    assert got2["人机"]["meaning"] == "说别人像机器人"
    assert got2["乐子"]["status"] == m.REJECTED
    # 非群主调用命令无效
    out = run_cmd("confirm", FakeEvent("476573490", "12345", "路人", "/黑话确认 人机"))
    assert out == [], "非群主命令应无效"
    print("✓ 情景11 命令与持久化：确认/拒绝/备注 + 落盘往返 + 权限")


def t12_noise():
    """情景12：噪声过滤：纯标点/URL/全常见字不进词库；单字词条首次不建库。"""
    reset()
    m._ensure_state()
    for bad in ["。。。", "https://example.com/x", "什么", "但是", "666", "1.2.3"]:
        got, created = m._upsert(m._entries, bad, {"uid": "u1", "name": "A", "text": bad, "ts": 1.0})
        assert got is None and created is False, "「%s」不该入库" % bad
    # 单字首次不建
    got, created = m._upsert(m._entries, "肝", {"uid": "u1", "name": "A", "text": "肝", "ts": 1.0})
    assert created is False
    # 再次出现 -> 建库
    got, created = m._upsert(m._entries, "肝", {"uid": "u2", "name": "B", "text": "肝", "ts": 2.0})
    assert created is True and got["count"] == 1, "单字第二次出现应建库 count=1"
    # 短拼音缩写保留（yyds/xswl）
    got, created = m._upsert(m._entries, "yyds", {"uid": "u1", "name": "A", "text": "yyds", "ts": 1.0})
    assert created is True, "yyds 该保留"
    print("✓ 情景12 噪声过滤：纯噪声不进库、单字二次建库、缩写保留")


def t13_restart_persist():
    """情景13：重启后状态加载——提取不清空历史词库、冷却时间从 meta 恢复并生效。"""
    reset()
    gid = "476573490"
    m._ensure_state()
    e, _ = m._upsert(m._entries, "老词", {"uid": "u1", "name": "A", "text": "老词", "ts": 1.0})
    e["meaning"] = "早就确认的老词"
    e["status"] = m.CONFIRMED
    m._last_extract[gid] = time.time()  # 真实的最近提取时间
    m._meta["lastExtract"] = dict(m._last_extract)
    m.save_state(m._entries, m._shadow, m._meta)
    # ---- 模拟重启：模块状态清空，但 slang.json 还在 ----
    m._loaded = False
    m._entries, m._shadow, m._meta = [], [], {}
    m._last_extract.clear()
    m._buffers.clear()
    m._extract_pending.clear()
    # 重启后直接跑提取（历史词库必须先加载，否则整体覆写冲掉）
    r = json.dumps([{"content": "新词", "source_id": "1"}])
    feed_watch(FakeEvent(gid, "u1", "A", "说新词"))
    feed_watch(FakeEvent(gid, "u2", "B", "也说新词"))
    feed_watch(FakeEvent(gid, "u3", "C", "都说新词"), [r])
    contents = {x["content"] for x in m._entries}
    assert "老词" in contents, "重启后提取不得冲掉历史词库：%s" % contents
    assert "新词" in contents, "新候选应入库"
    old = next(x for x in m._entries if x["content"] == "老词")
    assert old["status"] == m.CONFIRMED and old["meaning"] == "早就确认的老词"
    # ---- 冷却从 meta 恢复：把上次提取时间拨到未来，重启后不应再触发 ----
    reset()
    m._ensure_state()
    e2, _ = m._upsert(m._entries, "旧词", {"uid": "u1", "name": "A", "text": "旧词", "ts": 1.0})
    e2["count"] = 3
    m._last_extract[gid] = time.time() + 100000.0  # 未来时间 = 冷却未过
    m._meta["lastExtract"] = dict(m._last_extract)
    m.save_state(m._entries, m._shadow, m._meta)
    m._loaded = False
    m._entries, m._shadow, m._meta = [], [], {}
    m._last_extract.clear()
    m._buffers.clear()
    m._extract_pending.clear()
    feed_watch(FakeEvent(gid, "u1", "A", "说个词"))
    feed_watch(FakeEvent(gid, "u2", "B", "也说个词"))
    feed_watch(FakeEvent(gid, "u3", "C", "都说个词"), [r])
    assert m._last_extract.get(gid) == time.time() + 100000.0 or \
           abs(m._last_extract.get(gid, 0) - (time.time() + 100000.0)) < 5, \
        "冷却时间应从 meta 恢复：%s" % m._last_extract.get(gid)
    assert m._stat["extracted"] == 0, "冷却未过不应触发提取"
    assert all(x["content"] != "新词" for x in m._entries), "冷却未过不应新增候选"
    print("✓ 情景13 重启持久化：历史词库保留 + 冷却恢复并生效")


def t14_auto_review():
    """情景14：自动审核 —— 8h 定时让 AI 审候选，通过/拒绝/再等等落库落盘。"""
    reset()
    m.AUTO = True
    m.GROUPS = {"476573490"}
    m._ensure_state()
    # 三个候选，createdAt 拨老（过 AUTO_MIN_AGE 才审）
    def old_entry(word, meaning, count):
        e, _ = m._upsert(m._entries, word,
                         {"uid": "u1", "name": "A", "text": word, "ts": 1.0})
        e["meaning"] = meaning
        e["count"] = count
        e["createdAt"] = "2026-01-01T00:00:00"
        return e
    old_entry("顶流", "", 5)            # 泛词 -> reject
    old_entry("绝绝子", "不确定", 8)      # 真黑话 -> approve 并给释义
    old_entry("流麻", "不确定", 3)        # 拿不准 -> defer
    review = json.dumps([
        {"content": "绝绝子", "decision": "approve",
         "meaning": "形容非常好、绝了", "reason": "网络用语，含义明确"},
        {"content": "顶流", "decision": "reject", "reason": "普通泛词"},
        {"content": "流麻", "decision": "defer", "reason": "证据不足"},
    ])
    plugin = _plugin([review])
    _LOOP.run_until_complete(m._run_auto_review(plugin.context))
    got = {e["content"]: e for e in m._entries}
    assert got["绝绝子"]["status"] == m.CONFIRMED, "应通过并转正"
    assert got["绝绝子"]["meaning"] == "形容非常好、绝了", "应采用审核给的新释义"
    assert got["顶流"]["status"] == m.REJECTED, "泛词应拒绝"
    assert got["流麻"]["status"] == m.CANDIDATE, "拿不准应再等等"
    assert got["流麻"]["reviewCount"] == 1
    assert m._stat["confirmed"] == 1 and m._stat["rejected"] == 1
    assert float(m._meta.get("lastAutoReview") or 0) > 0, "上次审核时间应落盘"
    assert len(m._auto_log) == 3
    assert m._auto_log[0]["decision"] == "approve"
    print("✓ 情景14 自动审核：通过/拒绝/再等等 + 落盘 + 决策日志")


def t15_auto_review_gates():
    """情景15：自动审核的门——太新的不审、approve 无释义不当转正、defer 达上限自动拒。"""
    reset()
    m.AUTO = True
    m.GROUPS = {"476573490"}
    # ---- 太新的候选不进本轮（年龄 < AUTO_MIN_AGE），不发 LLM、不动 meta ----
    m._ensure_state()
    e, _ = m._upsert(m._entries, "新词", {"uid": "u1", "name": "A", "text": "新词", "ts": 1.0})
    e["count"] = 2
    plugin = _plugin([json.dumps([{"content": "新词", "decision": "approve", "meaning": "x"}])])
    _LOOP.run_until_complete(m._run_auto_review(plugin.context))
    assert e["status"] == m.CANDIDATE, "太新的候选不应被审"
    assert not m._meta.get("lastAutoReview"), "整批都是新词时应早退不落盘"
    assert m._entries[0]["reviewCount"] == 0
    # ---- approve 但没给释义 -> 不当转正（当 defer 处理）----
    e2, _ = m._upsert(m._entries, "秒词", {"uid": "u1", "name": "A", "text": "秒词", "ts": 1.0})
    e2["count"] = 2
    e2["createdAt"] = "2026-01-01T00:00:00"
    e3, _ = m._upsert(m._entries, "模糊词", {"uid": "u1", "name": "A", "text": "模糊词", "ts": 1.0})
    e3["count"] = 1
    e3["meaning"] = "不确定"
    e3["createdAt"] = "2026-01-01T00:00:00"
    e3["reviewCount"] = m.AUTO_MAX_DEFER - 1  # 已经再等等两次，这次再 defer 就满上限
    review = json.dumps([
        {"content": "秒词", "decision": "approve", "meaning": "", "reason": ""},
        {"content": "模糊词", "decision": "defer", "reason": "拿不准"},
    ])
    plugin = _plugin([review])
    _LOOP.run_until_complete(m._run_auto_review(plugin.context))
    assert e2["status"] == m.CANDIDATE, "approve 无释义不得转正"
    assert e2["reviewCount"] == 1
    assert e3["status"] == m.REJECTED, "defer 达上限应自动拒绝"
    assert e3["reviewCount"] == m.AUTO_MAX_DEFER
    assert m._stat["confirmed"] == 0
    assert any("defer 达上限" in a["reason"] for a in m._auto_log)
    # ---- _auto_due：8h 到点判定 ----
    now = 100000.0
    assert m._auto_due(now, 0) is True, "从未审过应到点"
    assert m._auto_due(now, now - m.AUTO_INTERVAL + 1) is False, "没满 8h 不到点"
    assert m._auto_due(now, now - m.AUTO_INTERVAL - 1) is True, "满 8h 到点"
    print("✓ 情景15 自动审核门：新词不审 + 无释义不转正 + defer 上限自动拒 + 到点判定")


def t16_init_starts_auto_loop():
    """情景16：真正走构造函数——AUTO 开时应正常加载并调度循环任务（且不重复）。"""
    reset()
    m.AUTO = True
    m.ENABLED = True
    ctx = types.SimpleNamespace()

    async def _ctor():
        return m.Main(ctx)

    plugin = _LOOP.run_until_complete(_ctor())
    assert plugin.context is ctx
    assert m._started_auto is True, "__init__ 应通过 call_soon 启动自动审核循环"
    _LOOP.run_until_complete(asyncio.sleep(0))
    assert m._started_auto is True, "不应重复启动"
    # watch 兜底路径也应无害（已启动则不重复）
    feed_watch(FakeEvent("476573490", "u1", "A", "普通消息"))
    assert m._started_auto is True
    print("✓ 情景16 构造函数：AUTO 开时正常加载并启动循环（不重复）")


# ================================================================ 主流程
def main():
    tests = [t1_watch_filter, t2_extract_trigger, t3_repeat_count, t4_latin_boundary,
             t5_prompt_escape, t6_research, t7_uncertain, t8_inject_live,
             t9_shadow, t10_inject_order, t11_commands_and_persist, t12_noise,
             t13_restart_persist, t14_auto_review, t15_auto_review_gates,
             t16_init_starts_auto_loop]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print("✗ %s 失败：%s" % (t.__name__, e))
        except BaseException as e:
            failed += 1
            print("✗ %s 异常：%r" % (t.__name__, e))
    if failed:
        print("共 %d 个情景，失败 %d 个" % (len(tests), failed))
        sys.exit(1)
    print("全部通过：%d 个情景" % len(tests))


if __name__ == "__main__":
    main()
