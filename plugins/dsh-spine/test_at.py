# -*- coding: utf-8 -*-
"""dsh-spine @ 对照表测试。

核心断言用的是**生产日志里那对真实失败语料**：

    群友C: [At:3752949000] 😘😘😘
    大肥鱼:      这仨表情是给谁的

跑法（必须在 astrbot 容器里，要 import astrbot + 读 dsh_memory.db）：
  sudo docker cp 本文件 astrbot:/tmp/sp/ && sudo docker exec astrbot python3 /tmp/sp/test_at.py
"""

import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from loguru import logger as _lg          # noqa: E402

_LGR = type(_lg)
_ADD = _LGR.add
_LGR.add = lambda self, *a, **k: 0       # 别把 sink 挂到生产日志上
try:
    import main as S
finally:
    _LGR.add = _ADD
    _lg.remove()

ME = "3752949000"        # 机器人大肥鱼
OWNER = "2774000001"     # 群主难谓言
CAT = "3172949971"       # 群友C

fail = []


def ck(c, m):
    if not c:
        fail.append(m)


print("=== 1. 那对真实失败语料 ===")
b = S.at_legend(["[At:3752949000] 😘😘😘"], ME, {})
print("   注入块: %s" % b)
ck(b, "有 @ 自己却没生成对照表")
ck("＝**@你**" in b, "没有点明 [At:3752949000] 就是它自己：%r" % b)
ck(ME in b, "对照表里没有自己的号码")
ck("别读成" in b and "别问" in b, "缺「别问这是给谁的」那句行为约束")
ck("群友C" not in b, "把当前说话人写进对照表了（那是框架的活，会重复）")

print("\n=== 2. 别人的号码翻成名字 ===")
b = S.at_legend(["[At:%s] 在吗" % OWNER], ME, {OWNER: "难谓言"})
print("   %s" % b)
ck("@难谓言" in b, "已知名字没翻出来：%r" % b)
ck("＝**@你**" not in b, "没有 @ 自己却说了 @你：%r" % b)
b2 = S.at_legend(["[At:%s] 在吗" % OWNER], ME, {})
ck("QQ" + OWNER in b2, "名字查不到时应退化用号码：%r" % b2)

print("\n=== 3. 自己排第一 ===")
b = S.at_legend(["[At:%s] 甲" % CAT, "[At:%s] 乙" % OWNER, "[At:1] 丙".replace("1", ME)],
                ME, {})
first = b.split("；")[0]
ck(ME in first, "自己的号码没排第一：%r" % b[:90])
print("   第一条: %s" % b.split("。")[1][:70] if "。" in b else b[:70])

print("\n=== 4. 没有 @ 就不注入 ===")
for t in ["早上好", "", "普通一句话没有任何标记", "[图片] 看看这个"]:
    ck(S.at_legend([t], ME, {}) == "", "没有 @ 号码却注入了：%r → %r" % (t, S.at_legend([t], ME, {})))
print("   4 条无 @ 文本都不注入 ✓")

print("\n=== 5. 上限 6 个，多的只报数 ===")
many = " ".join("[At:%d]" % (700000000 + i) for i in range(10)) + "[At:%s]" % ME
b = S.at_legend([many], ME, {})
ck(b.count("＝@QQ") + b.count("＝**@你**") == 6, "没截到 6 条：%r" % b)
ck("另 5 个" in b, "超出部分没报数：%r" % b)
print("   %s" % b[-70:])

print("\n=== 6. _ctx_text 吃三种形状 ===")
ck(S._ctx_text("abc") == "abc", "str 形状")
ck(S._ctx_text({"role": "user", "content": "xyz"}) == "xyz", "dict 形状")
ck("a" in S._ctx_text([{"type": "text", "text": "a"}, "b"]), "list 形状")
ck(S._ctx_text(None) == "", "None 形状")
print("   str / dict / list / None 都能取文本 ✓")

print("\n=== 7. 插件装载 + 真跑一次 _inject_at（读成员表）===")


class FakeEv:
    def get_group_id(self):
        return "100000001"

    def get_sender_id(self):
        return CAT


class FakeReq:
    def __init__(self):
        self.prompt = "[At:%s] 😘😘😘" % ME
        self.contexts = [{"role": "user", "content": "[At:%s] 得修" % ME}]
        self.extra_user_content_parts = []


class FakeCtx:
    pass


try:
    plug = S.Main(FakeCtx())
    req = FakeReq()
    plug._inject_at(FakeEv(), req, "100000001", ME, "[At:%s] 😘😘😘" % ME)
    parts = req.extra_user_content_parts
    ck(len(parts) == 1, "没注入：%r" % parts)
    txt = parts[0] if isinstance(parts[0], str) else getattr(parts[0], "text", "")
    ck("＝**@你**" in txt, "真跑一次没点明是自己：%r" % txt)
    ck(plug._stat["at"] == 1, "计数没加：%r" % plug._stat)
    print("   %s" % txt)
    print("   成员表读到 %d 个名字（QQ→名字）" % len(plug._names.get("100000001") or {}))
except BaseException as exc:
    fail.append("装载/真跑异常：%r" % exc)

print("\n=== 8. 点名判定（真正的根因）===")
# 框架把「第一个 @它」从 message_str 里删掉了
# （aiocqhttp_platform_adapter.py:382-391），所以正文里扫不到 —— 只能看组件。


class At:
    def __init__(self, qq):
        self.qq = qq


class Plain:
    pass


class MsgObj:
    def __init__(self, comps, self_id=ME):
        self.message = comps
        self.self_id = self_id


class Ev:
    def __init__(self, comps, name="群友C"):
        self.message_obj = MsgObj(comps)
        self._n = name

    def get_sender_name(self):
        return self._n


# 就是那对失败语料的组件层形状：At(我) + Plain("😘😘😘")，正文里只剩表情
ev = Ev([At(ME), Plain()])
d, a_ = S.at_targets(ev)
ck(d and not a_, "被单独 @ 却没判出来：direct=%s all=%s" % (d, a_))
nt = S.mention_note(d, a_, "群友C")
print("   注入块: %s" % nt)
ck("点你" in nt, "没点明「这条是在点你」：%r" % nt)
ck("群友C" in nt, "没带上说话人：%r" % nt)
ck("给谁的" in nt, "没有针对「这是给谁的」那句行为约束：%r" % nt)
ck("抹掉" in nt, "没解释正文为什么是碎片：%r" % nt)

d, a_ = S.at_targets(Ev([At("3522559046"), Plain()]))
ck(not d and not a_, "@别人被误判成点它：%s %s" % (d, a_))
ck(S.mention_note(d, a_, "某人") == "", "@别人却要注入提示")
print("   @别人 → 不注入 ✓")

d, a_ = S.at_targets(Ev([At("all"), Plain()]))
ck(a_ and not d, "@全体成员没判出来：direct=%s all=%s" % (d, a_))
n2 = S.mention_note(d, a_, "群友C")
ck("全体" in n2 and "不是专门点你" in n2, "@全体的话术不对：%r" % n2)
print("   @全体 → %s" % n2[:52])

d, a_ = S.at_targets(Ev([Plain()]))
ck(not d and not a_ and S.mention_note(d, a_, "x") == "", "没有任何 @ 却注入了")
print("   没有任何 @ → 不注入 ✓")

print("\n=== 9. 端到端：那对失败语料真跑一次 ===")


class Req2:
    def __init__(self, prompt):
        self.prompt = prompt          # 框架删掉 @ 之后的样子
        self.contexts = []
        self.extra_user_content_parts = []


plug2 = S.Main(FakeCtx())
req2 = Req2("😘😘😘")                 # ← 正文里真的只有三个表情
plug2._inject_at(Ev([At(ME), Plain()]), req2, "100000001", ME, "😘😘😘")
ck(len(req2.extra_user_content_parts) == 1, "点名时没注入：%r" % req2.extra_user_content_parts)
t2 = req2.extra_user_content_parts[0]
t2 = t2 if isinstance(t2, str) else getattr(t2, "text", "")
ck("点你" in t2, "端到端注入块不对：%r" % t2)
print("   prompt=%r → 注入: %s" % ("😘😘😘", t2[:90]))

req3 = Req2("早上好啊")
plug2._inject_at(Ev([Plain()]), req3, "100000001", ME, "早上好啊")
ck(not req3.extra_user_content_parts, "普通消息也注入了：%r" % req3.extra_user_content_parts)
print("   普通消息 → 零注入 ✓")

print("\n" + "=" * 60)
if fail:
    print("AT_TEST_FAILED（%d 项）" % len(fail))
    for f in fail:
        print("  ✗ " + f)
    sys.exit(1)
print("AT_TEST_OK")
