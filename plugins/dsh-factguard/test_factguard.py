# test_factguard.py —— dsh-factguard 自身事实、口语触发与动态年龄回归测试
from datetime import date
import importlib.util
import json
import sys
import tempfile

path = sys.argv[1] if len(sys.argv) > 1 else "main.py"
spec = importlib.util.spec_from_file_location("factguard_tested", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

positive = [
    "你多大了", "小妹妹，你今年多大了？", "你今年几岁", "大肥鱼多大",
    "你年龄多大", "生日几号", "小妹妹到底几岁了", "你是谁",
    "你叫什么名字", "大肥鱼是男鱼吗", "你是女的吗", "你住哪里",
    "你是什么模型", "你是真人吗", "你有身体吗", "你长什么样",
    "你是群主吗", "你是不是管理员", "你有B站号吗", "你有主人吗",
    "你是谁的女儿", "你会什么", "你能干嘛",
]
negative = [
    "我今年多大了", "他几岁", "我妹妹生日", "今年群里多大变化",
    "群主是男的", "我是什么模型", "他是真人吗", "我有身体",
    "朝露是管理员吗", "群主住哪里", "我老婆是谁",
]
for text in positive:
    assert m._TRIGGER_RE.search(text), "自身问法漏判: %r" % text
for text in negative:
    assert not m._TRIGGER_RE.search(text), "非自身问法误判: %r" % text

assert m._age_on("2023-11-02", date(2026, 9, 10)) == 2
assert m._age_on("2023-11-02", date(2026, 11, 2)) == 3
assert m._age_on("bad", date(2026, 9, 10)) is None

m._facts = dict(m._DEFAULT_FACTS)
block = m.build_inject(date(2026, 9, 10))
required = [
    "本群名：大肥鱼", "当前周岁：2岁", "直接答「2岁，生日是2023-11-02」",
    "不是群主", "没有现实肉身", "没有主人、父母、子女、配偶或恋人",
    "没有个人B站号", "底层聊天接口可能更换", "能力问题以单独的封闭能力清单为准",
    # 2026-09-15 群友反馈「咋肥鱼把自己当男的了」，群主确认「我本来就是写成女的」。
    "自称：", "不要自称「鱼哥」", "我本来就没性别", "叫爸爸也没用",
    "性别口径：自己是女的", "我是女的啊",
]
for text in required:
    assert text in block, "事实卡缺少: %s\n%s" % (text, block)
# 别名那一行绝不能再说「鱼哥可以应」——旧文案就是这句让它理直气壮自称鱼哥的。
assert "不是自称" in block, "别名没写清「鱼哥是群友的叫法，不是自称」"
assert "可以应" not in block, "别名又放开了「鱼哥可以应」"
assert block.startswith("<self_facts>") and block.endswith("</self_facts>")
assert len(block) <= 1400, len(block)

# 自称/性别触发：这些短句 2026-09-15 真的在群里出现过，之前一条都没触发，
# 模型手里没有事实卡才会自称「鱼哥」、说自己「没性别」。
class _Ev:
    def __init__(self, at):
        self.is_at_or_wake_command = at

role_at = ["妈妈", "小鱼弟", "你是GG还是MM啊", "臭妈妈坏妈妈", "叫鱼哥"]
for text in role_at:
    assert m._triggered(_Ev(True), text), "被点名的角色称呼漏判: %r" % text
assert m._triggered(_Ev(True), "咋肥鱼把自己当男的"), "带名字的角色称呼漏判"
assert m._triggered(_Ev(False), "大肥鱼是男鱼吗"), "自身问法漏判"
assert not m._triggered(_Ev(False), "我妹妹生日"), "别人的亲属不该注入"
assert not m._triggered(_Ev(False), "群主是男的"), "说别人性别不该注入"
assert not m._triggered(_Ev(False), "妈妈叫我回家"), "没点名没叫名字不该注入"

# 旧版 JSON 只有三个字段时，新增事实必须由默认表补齐，不能因升级丢失。
old_path = m._FACTS_PATH
with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
    json.dump({"gender": "女", "birthday": "2023-11-02", "note": "旧备注"}, f, ensure_ascii=False)
    temp_path = f.name
m._FACTS_PATH = temp_path
loaded = m.load_facts()
m._FACTS_PATH = old_path
assert loaded["name"] == "大肥鱼"
assert "管理员" in loaded["group_role"]
assert loaded["note"] == "旧备注"

print("ALL PASS: %d positive, %d negative, expanded self facts" % (len(positive), len(negative)))
