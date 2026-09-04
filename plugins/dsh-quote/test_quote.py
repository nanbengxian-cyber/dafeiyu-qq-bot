"""dsh-quote 纯逻辑测试：不导入 astrbot，靠 AST 摘出纯函数跑。

覆盖点全部来自真群里量到的实际问题：
  · 群里有真人把昵称改成「大肥鱼」，与机器人同名 —— 必须只认 QQ 号
  · 引用原文开头带上一轮的 @昵称(QQ)，是噪声不是内容
  · @ 的是别人时不能当成对自己说话
"""
import ast
import os
import re
import sys

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
tree = ast.parse(open(PATH, encoding="utf-8").read())
ns = {"re": re, "os": os}
WANT = {"_strip_at_prefix", "_chain_text", "_who", "build_block"}
for node in tree.body:
    if isinstance(node, ast.Assign):
        try:
            exec(compile(ast.Module([node], []), PATH, "exec"), ns)
        except (NameError, AttributeError, TypeError):
            pass
    elif isinstance(node, ast.FunctionDef) and node.name in WANT:
        exec(compile(ast.Module([node], []), PATH, "exec"), ns)

B = ns["build_block"]
S = ns["_strip_at_prefix"]
SELF = "3752949717"          # 机器人真实 QQ
FAKE = "1493202695"          # 群里改名叫「大肥鱼」的真人
OWNER = "2774067216"

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


print("A 组 @前缀清理")
check("A1 去掉 @昵称(QQ) 前缀",
      S("@羽玲(1415757441) 我真的怀疑是不是要倒闭了") == "我真的怀疑是不是要倒闭了")
# A2：`@难谓言对` 这种 At 与正文没有分隔的样本，昵称边界不可判，
# 保守地整条留下（下游由组件链精确剥离），绝不能贪婪吃掉正文「对」。
check("A2 无分隔的 @ 不贪婪切", S("@难谓言对") == "@难谓言对")
check("A3 连续多个 @ 都去掉",
      S("@a(1) @b(2) 正文") == "正文")
check("A4 正文里的 @ 不动", S("邮箱是 a@b.com") == "邮箱是 a@b.com")


class _At:
    def __init__(self, qq):
        self.qq = qq
        self.text = ""


class _Plain:
    def __init__(self, text):
        self.text = text
        self.qq = ""


C = ns["_chain_text"]
check("A5 组件链精确跳过 At",
      C([_At("2774067216"), _Plain("对")]) == "对")
check("A6 组件链拼接多段文本",
      C([_Plain("前"), _Plain("后")]) == "前 后")

print("B 组 身份判定（只认 QQ 号）")
own = B("难谓言", OWNER, "大肥鱼", SELF, "30块确实不贵", SELF)
check("B1 引用机器人自己：说是自己说的", "【你自己】" in own)
check("B2 引用机器人自己：提示顺着接", "在回应你" in own)

fake = B("安", "3859099931", "大肥鱼", FAKE, "教官真是太逊了", SELF)
check("B3 同名真人不算自己", "【不是你说的】" in fake)
check("B4 同名真人：带上真实 QQ 号", ("QQ %s" % FAKE) in fake)
check("B5 同名真人：说明是别人之间的对话", "他们之间的对话" in fake)
check("B6 同名真人：明说别当成自己做过的", "别把它当成你做过的事" in fake)

other = B("难谓言", OWNER, "知意", "2983764916", "我就补个作业加开个学", SELF)
check("B7 引用第三人不算自己", "【不是你说的】" in other)

unknown = B("难谓言", OWNER, "", "", "某句旧话", SELF)
check("B8 拿不到被引用者 QQ 时不猜", "分不清是谁说的" in unknown and "【你自己】" not in unknown)

print("C 组 @ 指向")
at_bot = B("难谓言", OWNER, "知意", "2983764916", "旧话", SELF, (SELF,), ("大肥鱼2号",))
check("C1 @了机器人：说是对你说的", "@ 了你" in at_bot)
at_other = B("难谓言", OWNER, "知意", "2983764916", "旧话", SELF, ("2983764916",), ("知意",))
check("C2 @了别人：明说不是你", "不是你" in at_other)
check("C3 @了别人：带上对方 QQ", "2983764916" in at_other)
no_at = B("难谓言", OWNER, "知意", "2983764916", "旧话", SELF)
check("C4 没有 @ 时不提 @", "@ 了你" not in no_at and "@ 的是" not in no_at)

print("D 组 块形状与边界")
check("D1 小写标签开头", own.startswith("<quoted_message>"))
check("D2 标签闭合", own.endswith("</quoted_message>"))
check("D3 明说引用内容是旧的", "是**旧的**" in own)
long_text = "字" * 400
capped = B("难谓言", OWNER, "知意", "2983764916", long_text, SELF)
check("D4 引用原文被截断", capped.count("字") <= 130)
empty = B("难谓言", OWNER, "知意", "2983764916", "", SELF)
check("D5 空引用有兜底文案", "（空消息）" in empty)
check("D6 不含台词式指令", "你应该说" not in own and "回复：" not in own)
selfless = B("难谓言", OWNER, "大肥鱼", SELF, "旧话", "")
check("D7 self_id 缺失时不认自己", "【你自己】" not in selfless)

print()
if fails:
    print("FAILED %d: %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("全部通过（A 组 6 + B 组 8 + C 组 4 + D 组 7 = 25 项）")
