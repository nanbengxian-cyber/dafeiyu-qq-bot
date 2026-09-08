# test_fwd.py —— dsh-fwd 纯逻辑测试。
#
# 直接 import main（容器里 astrbot 可裸 import，已实测），因此测的是**真代码**
# 而不是副本 —— dsh-acl 那次教训：测试里抄一份逻辑，改 main 时容易漏改。
#
# 覆盖：
#   A 节点形状归一（内联事件 / OneBot node / CQ 字符串 / 垃圾输入）
#   B 段渲染（文本 face 图 视频 语音 文件 卡片 at）
#   C 嵌套展开与深度上限
#   D 保头保尾
#   E summary 混合策略
#   F 媒体挑选（只给留下来的行花钱）
#   G 整块渲染（身份被抹的提示、字数上限、省略标记）
#   H 真实 97 条样本回归

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M  # noqa: E402

fails = []


def check(n, got, want):
    if got == want:
        print(f"  PASS {n}")
    else:
        print(f"  FAIL {n}\n       got={got!r}\n       want={want!r}")
        fails.append(n)


def ok(n, cond, why=""):
    if cond:
        print(f"  PASS {n}")
    else:
        print(f"  FAIL {n} {why}")
        fails.append(n)


def seg(t, **d):
    return {"type": t, "data": d}


def ev(who, uid, ts, segs, card=""):
    """内联在 forward.data.content 里的形状：完整消息事件。"""
    return {
        "self_id": 100000002, "user_id": uid, "time": ts,
        "message_id": 1, "message_seq": 1, "real_id": 1, "real_seq": "1",
        "message_type": "group", "post_type": "message", "message_format": "array",
        "sender": {"user_id": uid, "nickname": who, "card": card},
        "message": segs, "raw_message": "", "font": 14, "sub_type": "normal",
    }


print("== A 节点形状归一 ==")
check("A1 内联事件取昵称",
      M.node_view(ev("群友E", 111, 1788449587, [seg("text", text="在吗")]))[:3],
      ("群友E", "111", 1788449587))
check("A2 群名片优先于昵称",
      M.node_view(ev("原昵称", 111, 0, [], card="群里的名"))[0], "群里的名")
check("A3 OneBot node 段",
      M.node_view(seg("node", nickname="小明", user_id=222,
                      content=[seg("text", text="hi")]))[:2], ("小明", "222"))
check("A4 CQ 字符串当纯文本",
      M.node_view({"nickname": "A", "message": "纯文本"})[3],
      [{"type": "text", "data": {"text": "纯文本"}}])
check("A5 垃圾输入不炸", M.node_view("不是字典"), ("", "", 0, []))
check("A6 没有昵称回落某人", M.node_view({"message": []})[0], "某人")

print("== B 段渲染 ==")


def one(segs):
    recs = M.flatten([ev("A", 1, 0, segs)])
    return M.render_parts(recs[0]) if recs else ""


check("B1 纯文本", one([seg("text", text="你好")]), "你好")
check("B2 已知表情", one([seg("face", id="182")]), "[表情:笑哭]")
check("B3 未知表情不猜", one([seg("face", id="99999")]), "[表情]")
check("B4 图片带可用 summary",
      one([seg("image", summary="[/笑哭]", file="a.jpg")]), "[表情:笑哭]")
check("B5 图片 summary 没信息量",
      one([seg("image", summary="[动画表情]", file="a.jpg")]), "[图片]")
check("B6 视频带大小",
      one([seg("video", file="v.mp4", file_size="3633027")]), "[视频 3.5MB]")
check("B7 语音", one([seg("record", file_size="10812")]), "[语音 11KB]")
check("B8 文件", one([seg("file", file="报告.pdf")]), "[文件 报告.pdf]")
check("B9 卡片取 prompt",
      one([seg("json", data='{"ver":"1.0","prompt":"[QQ小程序]鸣潮演示"}')]),
      "[卡片：[QQ小程序]鸣潮演示]")
check("B10 at", one([seg("at", qq="123", name="大肥鱼")]), "@大肥鱼")
check("B11 混排",
      one([seg("text", text="看这个"), seg("image", summary="[/狗头]", file="b.jpg"),
           seg("text", text="笑死")]),
      "看这个 [表情:狗头] 笑死")
check("B12 单行超长截断",
      one([seg("text", text="字" * 400)]).endswith("…"), True)
ok("B13 单行截断到 LINE_MAX",
   len(one([seg("text", text="字" * 400)])) == M.LINE_MAX + 1)
check("B14 未知段被忽略",
      one([seg("poke", type="1"), seg("text", text="戳")]), "戳")

print("== C 嵌套展开 ==")
inner = [ev("内层甲", 9, 100, [seg("text", text="里面第一句")]),
         ev("内层乙", 8, 101, [seg("text", text="里面第二句")])]
outer = [ev("外层", 7, 50, [seg("text", text="看这个"),
                            seg("forward", id="INNER", content=inner)])]
recs = M.flatten(outer)
check("C1 展开后条数", len(recs), 3)
check("C2 层数标注", [r["depth"] for r in recs], [0, 1, 1])
check("C3 外层留下入口提示",
      M.render_parts(recs[0]), "看这个 [又一段聊天记录，共 2 条，内容接在下面]")
check("C4 内层正文", M.render_parts(recs[1]), "里面第一句")

_l3 = [ev("L3", 4, 0, [seg("text", text="第四层")])]
_l2 = [ev("L2", 3, 0, [seg("forward", id="C", content=_l3)])]
_l1 = [ev("L1", 2, 0, [seg("forward", id="B", content=_l2)])]
deep = [ev("L0", 1, 0, [seg("forward", id="A", content=_l1)])]
r1 = M.flatten(deep, max_depth=1)
check("C5 深度上限=1 只到 L1", [r["who"] for r in r1], ["L0", "L1"])
ok("C6 超深处留说明",
   "层数太深" in M.render_parts(r1[-1]), M.render_parts(r1[-1]))
r3 = M.flatten(deep, max_depth=3)
check("C7 深度上限=3", [r["who"] for r in r3], ["L0", "L1", "L2", "L3"])
check("C8 空 content 视为无法展开",
      "层数太深" in M.render_parts(
          M.flatten([ev("X", 1, 0, [seg("forward", id="E", content=[])])])[0]),
      True)

big = [ev("A", 1, 0, [seg("text", text=str(i))]) for i in range(50)]
check("C9 节点总数上限", len(M.flatten(big, budget=[10])), 10)

print("== D 保头保尾 ==")
r = [ev("A", 1, 0, [seg("text", text=str(i))]) for i in range(100)]
recs = M.flatten(r)
kept, om, hc = M.select_records(recs, 14, 8)
check("D1 选中条数", len(kept), 22)
check("D2 省略条数", om, 78)
check("D3 头段条数", hc, 14)
check("D4 头是前 14 条", M.render_parts(kept[0]), "0")
check("D5 尾是后 8 条", M.render_parts(kept[-1]), "99")
check("D6 尾段起点", M.render_parts(kept[14]), "92")
kept2, om2, hc2 = M.select_records(recs[:20], 14, 8)
check("D7 不超阈值不省略", (len(kept2), om2, hc2), (20, 0, 20))
kept3, om3, hc3 = M.select_records(recs[:22], 14, 8)
check("D8 刚好等于阈值不省略", (len(kept3), om3), (22, 0))
kept4, om4, _ = M.select_records(recs[:23], 14, 8)
check("D9 超一条就省略", (len(kept4), om4), (22, 1))

print("== E summary 混合策略 ==")
check("E1 表情包名可用", M.summary_text("[/笑哭]"), "笑哭")
check("E2 动画表情没信息量", M.summary_text("[动画表情]"), "")
check("E3 图片没信息量", M.summary_text("[图片]"), "")
check("E4 空串", M.summary_text(None), "")
check("E5 自定义文字保留", M.summary_text("[战场截图]"), "战场截图")
check("E6 大小写英文占位", M.summary_text("[Image]"), "")

print("== F 媒体挑选 ==")
recs = M.flatten([
    ev("A", 1, 0, [seg("image", summary="[/笑哭]", file="has_summary.jpg",
                       url="http://x/1")]),
    ev("B", 2, 0, [seg("image", summary="[图片]", file="need1.jpg", url="http://x/2")]),
    ev("C", 3, 0, [seg("image", summary="", file="need2.jpg", url="http://x/3")]),
    ev("D", 4, 0, [seg("image", summary="", file="nourl.jpg")]),
    ev("E", 5, 0, [seg("video", file="v.mp4", url="http://x/v", file_size="100")]),
])
picked = M.pick_media(recs, max_images=5, max_videos=1)
check("F1 有 summary 的不花钱",
      [p["key"] for p in picked], ["need1.jpg", "need2.jpg", "v.mp4"])
check("F2 图片张数上限",
      [p["key"] for p in M.pick_media(recs, max_images=1, max_videos=0)],
      ["need1.jpg"])
check("F3 视频可单独关",
      [p for p in M.pick_media(recs, max_images=0, max_videos=0)], [])
kept, om, hc = M.select_records(recs, 1, 1)
check("F4 只给留下来的行花钱",
      [p["key"] for p in M.pick_media(kept, max_images=5, max_videos=1)], ["v.mp4"])

print("== G 整块渲染 ==")
two = M.flatten([ev("甲", 1, 1788449400, [seg("text", text="第一句")]),
                 ev("乙", 2, 1788449500, [seg("text", text="第二句")])])
blk = M.render_block(two, 2, 0, 2)
ok("G1 开头说明条数", "共 2 条" in blk, blk[:80])
ok("G2 两个人不加身份提示", "隐去了发言人身份" not in blk)
ok("G3 正文有名字", "甲: 第一句" in blk, blk)
ok("G4 提醒图片是转述", "方括号里是转述" in blk)

same = M.flatten([ev("QQ用户", 100000020, 1788449400 + i,
                     [seg("text", text="第%d句" % i)]) for i in range(6)])
blk2 = M.render_block(same, 6, 0, 6)
ok("G5 同一身份要提示可能是两个人", "隐去了发言人身份" in blk2, blk2[:200])

long_recs = M.flatten([ev("A", 1, 1788449400, [seg("text", text="话" * 50)])
                       for _ in range(60)])
kept, om, hc = M.select_records(long_recs, 14, 8)
blk3 = M.render_block(kept, 60, om, hc, max_chars=600)
ok("G6 字数上限生效", len(blk3) <= 800, len(blk3))
ok("G7 超上限有说明", "篇幅有限" in blk3)

kept4, om4, hc4 = M.select_records(long_recs, 3, 2)
blk4 = M.render_block(kept4, 60, om4, hc4, max_chars=100000)
ok("G8 省略标记出现", "（中间省略 55 条）" in blk4, blk4[:400])
lines = [x for x in blk4.split("\n") if x.startswith("……")]
check("G9 省略标记只有一个", len(lines), 1)
idx = blk4.split("\n").index([x for x in blk4.split("\n") if x.startswith("……")][0])
check("G10 省略标记在第 3 条之后", idx, 4)

nested = M.flatten([ev("外", 7, 1788449400, [
    seg("text", text="看这个"),
    seg("forward", id="I", content=[ev("内", 9, 1788449410, [seg("text", text="里面")])])])])
blk5 = M.render_block(nested, 2, 0, 2)
ok("G11 嵌套行有缩进", "\n　" in blk5, repr(blk5))

print("== H 真实样本回归（97 条私聊记录）==")
SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fwd_real.json")
if not os.path.exists(SAMPLE):
    print("  SKIP 样本文件不在，跳过 H 组")
else:
    d = json.load(open(SAMPLE, encoding="utf-8"))
    nodes = d["data"]["messages"]
    check("H1 样本条数", len(nodes), 97)
    recs = M.flatten(nodes)
    check("H2 全部解析出来", len(recs), 97)
    kept, om, hc = M.select_records(recs)
    check("H3 保头保尾后条数", len(kept), M.HEAD_N + M.TAIL_N)
    check("H4 省略条数", om, 97 - M.HEAD_N - M.TAIL_N)
    blk = M.render_block(kept, len(recs), om, hc)
    ok("H5 正文不超上限", len(blk) <= M.MAX_CHARS + 200, len(blk))
    ok("H6 第一句还原", "回赞" in blk, blk[:200])
    ok("H7 最后一句在里面", recs[-1]["who"] in blk)
    ok("H8 识别出身份被抹", "隐去了发言人身份" in blk, blk[:300])
    ok("H9 表情渲染成名字", "[表情:" in blk, "没有表情行")
    picked = M.pick_media(kept)
    ok("H10 媒体挑选不炸", isinstance(picked, list), picked)
    # 样本里唯一那张图带 summary "[/笑哭]"，属于零成本路径
    allm = M.pick_media(recs, max_images=9, max_videos=9)
    check("H11 唯一的图有 summary 不需花钱", allm, [])
    print("  ---- 渲染结果预览（前 700 字）----")
    for ln in blk[:700].split("\n"):
        print("  | " + ln)

print()
if fails:
    print("FAILED %d 项: %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("全部通过")
