"""dsh-awareness 集成测试：用假事件跑「采集 → 撤回还原 → 懒加载注入」全链路。

需要 astrbot 运行时，必须在 astrbot 容器内跑（py3.12）：

    sudo docker cp plugins/dsh-awareness astrbot:/tmp/aw
    sudo docker exec astrbot python /tmp/aw/test_awareness_integration.py

重点验证用户最关心的那一件事：**撤回的内容能不能被记住**——
平台只给 message_id，必须靠本地缓存把正文还原出来。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# env 必须在 import main 之前设好：DB 路径是模块级常量。
_TEST_DB = "/tmp/test_awareness.db"
os.environ["DSH_AWARENESS_DB"] = _TEST_DB
os.environ["DSH_AWARENESS_GROUPS"] = "476573490"
os.environ["DSH_AWARENESS_OWNER"] = "2774067216"
os.environ["DSH_AWARENESS_KEEP_MIN"] = "120"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import main as aw  # noqa: E402
    from astrbot.core.platform.message_type import MessageType  # noqa: E402
except BaseException as exc:  # pragma: no cover
    print("IMPORT_FAIL: %r" % (exc,))
    raise SystemExit(2)

BOT = "3752949717"
GID = "476573490"


def comp(name, **kw):
    """造一个类名为 Plain/Image/At... 的假组件（classify 只看类名）。"""
    obj = type(name, (), {})()
    for key, value in kw.items():
        setattr(obj, key, value)
    return obj


class FakeMsg:
    def __init__(self, comps, mid="", raw=None, gid=GID):
        self.message = comps
        self.message_id = mid
        self.raw_message = raw
        # 真实 AstrBotMessage 带 group_id，插件按它判断群归属。
        self.group_id = gid


class FakeEvent:
    def __init__(self, comps=None, uid="1296432570", name="区", mid="", raw=None,
                 gid=GID, text=""):
        self.message_obj = FakeMsg(comps or [], mid, raw, gid)
        self._uid, self._name, self._gid, self._text = uid, name, gid, text

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return self._name

    def get_self_id(self):
        return BOT

    def get_message_type(self):
        return MessageType.GROUP_MESSAGE

    def get_platform_name(self):
        return "aiocqhttp"

    def get_message_str(self):
        return self._text


class FakeReq:
    def __init__(self):
        self.extra_user_content_parts = []


def fresh():
    if os.path.exists(_TEST_DB):
        os.remove(_TEST_DB)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(_TEST_DB + suffix):
            os.remove(_TEST_DB + suffix)
    aw.init_db()
    model = aw.Main.__new__(aw.Main)
    model._last_cleanup = 0.0
    return model


def test_record_and_recall():
    model = fresh()
    # 1) 群友发了图配文 —— 入环并留下 message_id，供撤回时回查。
    model._record_event(
        FakeEvent(comps=[comp("Image"), comp("Plain", text="看我新买的鱼缸")],
                  mid="msg-1001", text="看我新买的鱼缸"), GID)
    # 2) 纯图片（无正文）—— kind 落到 image。
    model._record_event(FakeEvent(comps=[comp("Image")], uid="3552366388",
                                  name="陌玄兔", mid="msg-1002"), GID)
    # 3) 机器人自己的消息不入环。
    model._record_event(FakeEvent(comps=[comp("Plain", text="我自己说的话")],
                                  uid=BOT, name="大肥鱼", mid="msg-1003"), GID)

    rows = model._load(GID, time.time() - 600)
    assert len(rows) == 2, [(r["kind"], r["text"]) for r in rows]
    assert rows[0]["text"] == "看我新买的鱼缸", rows[0]
    assert rows[0]["extra"].get("media") == ["image"], rows[0]
    assert rows[1]["kind"] == "image" and rows[1]["text"] == "", rows[1]

    # 4) 撤回那条图配文：平台只给 message_id，正文必须被还原出来。
    model._record_notice(
        {"post_type": "notice", "notice_type": "group_recall",
         "group_id": GID, "message_id": "msg-1001",
         "user_id": "1296432570", "operator_id": "1296432570"}, GID)
    rows = model._load(GID, time.time() - 600)
    recall = [r for r in rows if r["kind"] == "recall"]
    assert len(recall) == 1, rows
    assert recall[0]["text"] == "看我新买的鱼缸", recall[0]
    assert recall[0]["extra"].get("orig_name") == "区", recall[0]

    # 5) 撤回一条本地没缓存的消息：照实说没留存，不编内容。
    model._record_notice(
        {"post_type": "notice", "notice_type": "group_recall",
         "group_id": GID, "message_id": "msg-does-not-exist",
         "user_id": "1418045381", "operator_id": "1418045381"}, GID)
    rows = model._load(GID, time.time() - 600)
    lost = [r for r in rows if r["kind"] == "recall"][-1]
    assert lost["text"] == "", lost

    # 6) 重复 message_id 不重复入库。
    before = len(rows)
    model._record_event(FakeEvent(comps=[comp("Plain", text="重复")], mid="msg-1001"), GID)
    assert len(model._load(GID, time.time() - 600)) == before
    print("  record/recall ok")


def test_index_and_detail_injection():
    model = fresh()
    for i in range(6):
        model._record_event(
            FakeEvent(comps=[comp("Plain", text="第%d句闲聊" % i)],
                      uid="1296432570", name="区", mid="m-%d" % i,
                      text="第%d句闲聊" % i), GID)
    model._record_event(FakeEvent(comps=[comp("Image")], uid="3552366388",
                                  name="陌玄兔", mid="m-img"), GID)
    model._record_notice(
        {"post_type": "notice", "notice_type": "group_recall", "group_id": GID,
         "message_id": "m-3", "user_id": "1296432570", "operator_id": "1296432570"}, GID)

    # a) 日常闲聊：只注入薄索引，不展开明细（这就是「认知太多又不行」的解法）。
    req = FakeReq()
    asyncio.run(model.inject(
        FakeEvent(comps=[comp("Plain", text="今天天气不错")], text="今天天气不错"), req))
    assert len(req.extra_user_content_parts) == 1, len(req.extra_user_content_parts)
    index_text = req.extra_user_content_parts[0].text
    assert index_text.startswith("<group_awareness>"), index_text
    assert "撤回1" in index_text, index_text
    assert "第3句闲聊" not in index_text, index_text

    # b) 被 @ 问「刚才撤回了什么」：索引 + 明细一起给，且撤回正文在里面。
    req = FakeReq()
    asyncio.run(model.inject(
        FakeEvent(comps=[comp("At", qq=BOT), comp("Plain", text="刚才撤回了什么")],
                  text="刚才撤回了什么"), req))
    joined = "\n".join(p.text for p in req.extra_user_content_parts)
    assert "<group_awareness_detail>" in joined, joined
    assert "[撤回]" in joined and "第3句闲聊" in joined, joined

    # c) 群主说话也展开明细。
    req = FakeReq()
    asyncio.run(model.inject(
        FakeEvent(comps=[comp("Plain", text="在吗")], uid="2774067216",
                  name="难谓言", text="在吗"), req))
    assert len(req.extra_user_content_parts) == 2, len(req.extra_user_content_parts)

    # d) 别的群不注入。
    req = FakeReq()
    asyncio.run(model.inject(
        FakeEvent(comps=[comp("Plain", text="刚才")], gid="999", text="刚才"), req))
    assert req.extra_user_content_parts == [], req.extra_user_content_parts
    print("  injection ok")


def test_filter():
    f = aw.AwarenessFilter()
    # 本群消息放行。
    assert f.filter(FakeEvent(comps=[comp("Plain", text="hi")]), None) is True
    # 撤回通知放行（message_str 为空也要能走到 handler）。
    assert f.filter(FakeEvent(raw={"post_type": "notice",
                                   "notice_type": "group_recall"}), None) is True
    # 别的群拦掉。
    assert f.filter(FakeEvent(gid="123456"), None) is False
    # webchat 平台拦掉。
    ev = FakeEvent()
    ev.get_platform_name = lambda: "webchat"
    assert f.filter(ev, None) is False
    print("  filter ok")


def test_sensitive_not_stored():
    model = fresh()
    model._record_event(FakeEvent(
        comps=[comp("Plain", text="我的 api_key = sk-abcdef123456")],
        mid="s-1", text="我的 api_key = sk-abcdef123456"), GID)
    assert model._load(GID, time.time() - 600) == [], "敏感内容不该落库"
    print("  sensitive ok")


def main():
    print("dsh-awareness 集成测试（%s）" % sys.version.split()[0])
    test_record_and_recall()
    test_index_and_detail_injection()
    test_filter()
    test_sensitive_not_stored()
    print("AWARENESS_INTEGRATION_TEST_OK")


if __name__ == "__main__":
    main()
