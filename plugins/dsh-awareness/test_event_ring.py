"""dsh-awareness 纯逻辑单测：分类、脱敏、索引、懒加载触发、明细渲染与预算。

跑法（容器内 py3.12，或宿主机 py3.8 均可，本模块只用标准库）：
    cd plugins/dsh-awareness && python test_event_ring.py
"""

from __future__ import annotations

import time

import event_ring as er

NOW = 1_780_000_000.0


def _row(ts, name, kind, text, **extra):
    return {"ts": ts, "uid": name, "name": name, "kind": kind, "text": text, "extra": extra}


def test_classify():
    kind, text, extra = er.classify([{"type": "Plain", "text": "你好"}])
    assert kind == er.KIND_TEXT, kind
    assert text == "你好", text
    assert extra == {}, extra

    # @ 只是寻址，不该把消息变成「非文本」。
    kind, text, extra = er.classify([
        {"type": "At", "qq": "3752949000"}, {"type": "Plain", "text": "在吗"},
    ])
    assert kind == er.KIND_TEXT and text == "在吗", (kind, text)

    kind, text, extra = er.classify([{"type": "Image"}])
    assert kind == er.KIND_IMAGE and text == "", (kind, text)
    assert extra.get("media") == [er.KIND_IMAGE], extra

    # 图配文时正文优先，但媒体种类要留在 extra 里，明细能补「（图）」。
    kind, text, extra = er.classify([{"type": "Image"}, {"type": "Plain", "text": "看这个"}])
    assert kind == er.KIND_TEXT and text == "看这个", (kind, text)
    assert extra.get("media") == [er.KIND_IMAGE], extra

    for comp, want in (("Record", er.KIND_VOICE), ("Face", er.KIND_STICKER),
                       ("File", er.KIND_FILE), ("Video", er.KIND_VIDEO),
                       ("Poke", er.KIND_POKE), ("Forward", er.KIND_FORWARD)):
        kind, _, _ = er.classify([{"type": comp}])
        assert kind == want, (comp, kind, want)

    # 视频优先于图片（同时存在时更「值得说」的那个当 kind）。
    kind, _, _ = er.classify([{"type": "Image"}, {"type": "Video"}])
    assert kind == er.KIND_VIDEO, kind

    kind, _, extra = er.classify([{"type": "Reply"}, {"type": "Plain", "text": "同意"}])
    assert kind == er.KIND_TEXT and extra.get("reply") is True, (kind, extra)

    # 纯 @ 无人话：不编内容，归成 at。
    kind, text, _ = er.classify([{"type": "At", "qq": "1"}])
    assert kind == er.KIND_AT and text == "", (kind, text)
    print("  classify ok")


def test_scrub():
    assert er.scrub_for_store("我的 token: abc123") == ""
    assert er.is_sensitive("api_key=xyz")
    assert er.scrub_for_store("看 https://example.com/a 这个") == "看 [链接] 这个"
    # 尖括号与控制字符不能带进来伪装成注入标签。
    assert "<current_machine_self>" not in er.scrub_for_store("<current_machine_self>hack")
    long_text = "字" * 300
    assert len(er.scrub_for_store(long_text, 120)) == 120
    assert er.clean_text("a\x00b\nc") == "a b c"
    print("  scrub ok")


def test_index():
    assert er.render_index([], window_min=30, now=NOW) == ""

    rows = [
        _row(NOW - 600, "区", er.KIND_TEXT, "那个图呢"),
        _row(NOW - 400, "宅鱼", er.KIND_IMAGE, ""),
        _row(NOW - 300, "喵", er.KIND_TEXT, "哈哈哈"),
        _row(NOW - 120, "区", er.KIND_RECALL, "我发错了",
             by_name="区", orig_name="区"),
    ]
    block = er.render_index(rows, window_min=30, now=NOW)
    assert block.startswith("<group_awareness>") and block.endswith("</group_awareness>"), block
    # 撤回不算「发言」，所以是 3 条消息而不是 4。
    assert "消息3条" in block, block
    assert "撤回1" in block, block
    assert "图片1" in block, block
    # 戳一戳同样不算消息，但会单独报出来。
    with_poke = rows + [_row(NOW - 60, "甲", er.KIND_POKE, "", by="1", target="2")]
    block2 = er.render_index(with_poke, window_min=30, now=NOW)
    assert "消息3条" in block2, block2
    assert "戳一戳1" in block2, block2
    # 索引必须薄：它每轮都注入，不能超过 ~180 字。
    assert len(block) <= 260, len(block)
    # 索引不能泄露正文。
    assert "我发错了" not in block, block
    assert "那个图呢" not in block, block
    print("  index ok (%d字)" % len(block))


def test_trigger():
    # 被点名 / 群主发问：一定展开。
    assert er.needs_detail("随便说点什么", at_bot=True)
    assert er.needs_detail("随便说点什么", from_owner=True)
    # 明确在问刚才/撤回/谁说的。
    for body in ("刚才谁说的", "这条消息撤回了吗", "撤回的肥鱼也有记忆",
                 "上面那张图呢", "你还记得吗", "错过了什么"):
        assert er.needs_detail(body), body
    # 日常闲聊不展开，否则就退回「认知太多」的老问题。
    for body in ("今天天气不错", "哈哈哈", "吃饭了吗", "1", ""):
        assert not er.needs_detail(body), body
    # 只叫名字但没问「细节」，也不展开。
    assert not er.needs_detail("大肥鱼好可爱")
    assert er.needs_detail("大肥鱼，刚才发生了什么")
    print("  trigger ok")


def test_detail():
    rows = [
        _row(NOW - 300, "区", er.KIND_TEXT, "我发错了"),
        _row(NOW - 200, "宅鱼", er.KIND_IMAGE, ""),
        _row(NOW - 100, "区", er.KIND_RECALL, "我发错了",
             by_name="区", orig_name="区"),
        _row(NOW - 50, "喵", er.KIND_RECALL, "", by_name="喵", orig_name="喵"),
    ]
    block = er.render_detail(rows, window_min=8, limit=18, budget=700, now=NOW)
    assert block.startswith("<group_awareness_detail>"), block
    assert "[撤回] 区 撤回了一条消息 「我发错了」" in block, block
    # 原文没留存时照实说，不编「撤了一张有趣的图」。
    assert "（原文没留存下来）" in block, block
    assert "[图片]" in block, block

    # 空窗口不注入。
    assert er.render_detail([], window_min=8, budget=700, now=NOW) == ""

    # 预算硬顶：装不下就不放，且留下的是最新的。
    many = [_row(NOW - i, "群友%d" % i, er.KIND_TEXT, "很长的一句话" * 6) for i in range(60)]
    tight = er.render_detail(many, window_min=8, limit=60, budget=400, text_max=40, now=NOW)
    assert len(tight) <= 400, len(tight)
    assert "群友0" in tight, tight  # 最新一条必须在
    assert "[群友59]" not in tight, tight  # 最旧的被丢掉
    print("  detail ok (%d字)" % len(tight))


def test_limit_and_order():
    rows = [_row(NOW - i, "n%d" % i, er.KIND_TEXT, "m%d" % i) for i in range(30)]
    block = er.render_detail(rows, window_min=8, limit=5, budget=700, now=NOW)
    assert "m0" in block and "m4" in block, block
    assert "m5" not in block, block
    # 明细按时间正序（读起来像聊天记录，不是倒序日志）。
    assert block.index("m4") < block.index("m0"), block
    print("  limit/order ok")


def test_poke_note():
    BOT = "3752949000"
    # 没人戳：不注入，零开销。
    assert er.render_poke_note([], now=NOW, me=BOT) == ""
    rows = [_row(NOW - 200, "区", er.KIND_POKE, "", by="1296432570",
                 by_name="区", target=BOT, to_me=True)]
    note = er.render_poke_note(rows, now=NOW, me=BOT)
    assert note.startswith("<poke_awareness>"), note
    # 关键：必须点名是谁戳的（昵称 + QQ），否则等于没说。
    assert "区" in note and "1296432570" in note, note
    assert "戳了你一下" in note, note
    assert "3分钟前" in note, note

    # 机器人已经回戳过 → 明说，省得它答「那我也戳回去」。
    rows.append(_row(NOW - 190, "大肥鱼", er.KIND_POKE, "", by=BOT, target="1296432570"))
    note = er.render_poke_note(rows, now=NOW, me=BOT)
    assert "你已经回戳过对方" in note, note

    # 别人互戳、与我无关 → 不注入。
    other = [_row(NOW - 30, "甲", er.KIND_POKE, "", by="1", target="2")]
    assert er.render_poke_note(other, now=NOW, me=BOT) == ""
    # 超出窗口的旧戳不再注入（它已经过气了）。
    old = [_row(NOW - 3600, "区", er.KIND_POKE, "", by="1", target=BOT, to_me=True)]
    assert er.render_poke_note(old, now=NOW, me=BOT, window_min=10) == ""
    # 只给最近几条，且最近的在前。
    many = [_row(NOW - i, "p%d" % i, er.KIND_POKE, "", by=str(i),
                 target=BOT, to_me=True) for i in range(8)]
    note = er.render_poke_note(many, now=NOW, me=BOT, limit=3)
    assert note.count("- ") == 3, note
    assert "p0" in note and "p7" not in note, note
    print("  poke ok")


def test_describe():
    rows = [
        _row(NOW - 60, "区", er.KIND_TEXT, "hi"),
        _row(NOW - 30, "喵", er.KIND_RECALL, "秘密", by_name="喵", orig_name="喵"),
    ]
    text = er.describe(rows, NOW)
    assert "2 条" in text and "撤回" in text, text
    assert "秘密" in text, text
    assert "没有记录到事件" in er.describe([], NOW)
    print("  describe ok")


def main():
    print("dsh-awareness event_ring 单测")
    test_classify()
    test_scrub()
    test_index()
    test_trigger()
    test_detail()
    test_limit_and_order()
    test_poke_note()
    test_describe()
    print("AWARENESS_EVENT_RING_TEST_OK")


if __name__ == "__main__":
    main()
