# test_decide.py —— dsh-decide 纯逻辑测试（不依赖 astrbot，容器内可直接跑）
# 覆盖：A 解析健壮性（含正则兜底）B veto-only 决策表 C 注入块形状
#       D 明确要东西的白名单 E 沉默连击保护 F 熔断 G avoid 越权过滤
import json, re, sys
fails=[]
def check(n,g,w):
    if g==w: print(f"  PASS {n}")
    else: print(f"  FAIL {n}\n       got={g!r}\n       want={w!r}"); fails.append(n)

_BOOLS=("arrange","venting","stop","ack","banter","open","about_bot")
_STRS=("topic","to","tone","avoid")
_CAP_RE=re.compile(r"别(真)?(画|发|生成|搜|查|做图|出图|语音|视频|唱|放)|不要(画|发|生成|搜|查|放)|别调用|别用工具")
_ASK_RE=re.compile(r"(画|生成|做|发|来|整|录|唱)(一|几|多)?(张|个|段|条|首|遍|次)?"
                   r"(图|照|壁纸|表情|视频|语音|音频|歌)"
                   r"|(语音|视频|图|壁纸)(说|念|读|来|发)"
                   r"|发(个|条|段)?(语音|视频|图)"
                   r"|(搜|查)(一下|下|搜)"
                   r"|搜索|百度|谷歌|google"
                   r"|画(个|张|一|几)|唱(首|个|一)"
                   r"|(再|多)(画|发|生成|来|做)(几|一)?(张|个|遍|次|条)")
_NOT_ASK_RE=re.compile(r"好像|大概|应该|估计|是不是|要时间|花时间|挺慢|很慢"
                   r"|不了|不能|没法|做不到|生成不了|画不了"
                   r"|我(要|去|来|自己|想)(录|画|搜|查|生成|做|发)"
                   r"|吗？$|吧$|吧？$")
def is_ask(t): return bool(_ASK_RE.search(t)) and not bool(_NOT_ASK_RE.search(t))
fallback=[0]
def _parse(raw):
    s=(raw or "").strip()
    if not s: return None
    s=re.sub(r"^```[a-zA-Z]*\s*|\s*```$","",s).strip()
    i,j=s.find("{"),s.rfind("}")
    body=s[i:j+1] if (i>=0 and j>i) else s
    out=None
    try:
        o=json.loads(body)
        if isinstance(o,dict): out=o
    except BaseException: out=None
    if out is None:
        fallback[0]+=1; out={}
        for k in _BOOLS:
            m=re.search(r'"%s"\s*:\s*(true|false|True|False)'%k, body)
            if m: out[k]=m.group(1).lower()=="true"
        for k in _STRS:
            m=re.search(r'"%s"\s*:\s*"([^"]*)"'%k, body)
            if m: out[k]=m.group(1)
    if not any(k in out for k in _BOOLS): return None
    r={k:bool(out.get(k,False)) for k in _BOOLS}
    for k in _STRS:
        v=out.get(k); r[k]=v.strip()[:20] if isinstance(v,str) else ""
    if r["avoid"] and _CAP_RE.search(r["avoid"]):
        r["avoid_dropped"]=r["avoid"]; r["avoid"]=""
    return r
def verdict(f):
    if f["stop"]: return "沉默","有人叫别插话"
    if f["venting"]: return "沉默","有人在诉苦"
    if f["arrange"] and not f["banter"]: return "沉默","两人在谈具体安排"
    if f["ack"] and not f["open"]: return "沉默","只是一句应答"
    pos=[k for k in ("banter","open","about_bot") if f[k]]
    return "回话","可接（%s）"%(",".join(pos) if pos else "无否决信号")
def F(**kw):
    d={k:False for k in _BOOLS}; d.update({k:"" for k in _STRS}); d.update(kw); return d

print("A0. PROMPT 模板可 format（guard 在这里栽过一次：裸 { 让每次判定都失败）")
import os as _os
_mp = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "main.py")
if _os.path.exists(_mp):
    _src = open(_mp, encoding="utf-8").read()
    _i = _src.index('PROMPT = """'); _j = _src.index('"""', _i + 12) + 3
    _ns = {}; exec(_src[_i:_j], {}, _ns)
    try:
        _out = _ns["PROMPT"].format(transcript="阿强：草")
        check("A0a PROMPT.format 不抛异常", True, True)
        check("A0b 占位符被替换", "阿强：草" in _out, True)
        check("A0c JSON 示例的花括号活下来", '{"arrange"' in _out, True)
        check("A0d 没有残留占位符", bool(re.search(r"\{transcript\}", _out)), False)
    except BaseException as _e:
        check("A0a PROMPT.format 不抛异常  <<%s: %s>>" % (type(_e).__name__, _e), False, True)
else:
    print("  SKIP A0（同目录没有 main.py）")

print("A. 解析健壮性")
GOOD='{"arrange":false,"venting":false,"stop":false,"ack":false,"banter":true,"open":false,"about_bot":false,"topic":"白猫来历","to":"群友A对群友D","tone":"轻松","avoid":"别太认真"}'
check("A1 标准 JSON", _parse(GOOD)["banter"], True)
check("A1b 字符串字段", _parse(GOOD)["topic"], "白猫来历")
check("A2 ```json 包裹", _parse("```json\n"+GOOD+"\n```")["banter"], True)
check("A3 前后带废话", _parse("我看到的是："+GOOD+" 完毕")["banter"], True)
b0=fallback[0]
check("A4 尾部多字符(实测遇到的『略』)", _parse(GOOD[:-1]+',"x":1略}')["banter"], True)
check("A4b 走了正则兜底", fallback[0]>b0, True)
check("A5 True/False 大写", _parse('{"banter":True,"open":False}')["banter"], True)
check("A6 空字符串", _parse(""), None)
check("A7 纯文字无 JSON", _parse("他们在闲聊"), None)
check("A8 完全没有布尔字段=没结果(不是全False)", _parse('{"topic":"x"}'), None)
check("A8b 合法 JSON 但只有字符串字段也无效", _parse('{"topic":"x","tone":"y","avoid":"z"}'), None)
check("A9 缺字段默认 False", _parse('{"banter":true}')["venting"], False)
check("A10 缺字符串默认空", _parse('{"banter":true}')["topic"], "")
check("A11 超长字段截到20", len(_parse('{"banter":true,"topic":"'+"啊"*50+'"}')["topic"]), 20)
check("A12 数组包对象也能捞", _parse('[{"banter":true}]')["banter"], True)
check("A13 非字符串的 topic 归空", _parse('{"banter":true,"topic":123}')["topic"], "")

print("B. veto-only 决策表")
check("B1 什么都没感知到 -> 回话(v4 这里判错3个)", verdict(F())[0], "回话")
check("B2 玩梗 -> 回话", verdict(F(banter=True))[0], "回话")
check("B3 话头 -> 回话", verdict(F(open=True))[0], "回话")
check("B4 提到它 -> 回话", verdict(F(about_bot=True))[0], "回话")
check("B5 叫别插话 -> 沉默", verdict(F(stop=True))[0], "沉默")
check("B6 诉苦 -> 沉默", verdict(F(venting=True))[0], "沉默")
check("B7 谈安排 -> 沉默", verdict(F(arrange=True))[0], "沉默")
check("B8 谈安排但在玩梗 -> 回话", verdict(F(arrange=True,banter=True))[0], "回话")
check("B9 纯应答 -> 沉默", verdict(F(ack=True))[0], "沉默")
check("B10 应答但是个话头 -> 回话", verdict(F(ack=True,open=True))[0], "回话")
check("B11 stop 压过 banter(硬否决)", verdict(F(stop=True,banter=True))[0], "沉默")
check("B12 venting 压过 banter", verdict(F(venting=True,banter=True))[0], "沉默")
check("B13 理由可读", verdict(F(venting=True))[1], "有人在诉苦")
check("B14 无信号时理由标注清楚", verdict(F())[1], "回话（无否决信号）".replace("回话（","可接（"))

print("C. 注入块形状")
def _render(f):
    lines=["<judgement>","开口前你已经看过一眼群里的情况了，这是你自己的判断："]
    for k,lab in (("topic","· 他们在聊：%s"),("to","· 这句是：%s"),("tone","· 你该用的态度：%s"),("avoid","· 分寸上别：%s")):
        if f.get(k): lines.append(lab%f[k])
    if f.get("venting"): lines.append("· 有人心里不痛快，别抖机灵。")
    if f.get("arrange"): lines.append("· 他们在说正事，你只是路过搭一句，别接管话题。")
    lines.append("· 只接你真能接的那一点，一句话说完。")
    lines.append("按这个判断说话，但**不要**把上面任何一条读出来、复述或提到。")
    lines.append("</judgement>")
    return "\n".join(lines)
blk=_render(_parse(GOOD))
check("C1 小写标签(ctxclean _TAG_RE 认得)", bool(re.match(r"^\s*<([a-z][a-z0-9_]*)>", blk)), True)
check("C2 有闭合", blk.endswith("</judgement>"), True)
check("C3 含禁止复述", all(x in blk for x in ("不要","读出来","复述")), True)
check("C4 不含成句台词", any(x in blk for x in ["你应该说","回复：","例如"]), False)
bare=_render(F())
check("C5 空感知不产生空行", "\n\n" in bare, False)
check("C5b 空感知不留空值项", bool(re.search(r"·[^\n]*：\s*$", bare, re.M)), False)
check("C6 长度可控(<400字)", len(blk)<400, True)
check("C7 诉苦时多一句提醒", "别抖机灵" in _render(F(venting=True)), True)
check("C8 谈正事时多一句提醒", "别接管话题" in _render(F(arrange=True)), True)

print("D. 明确要东西的白名单（这些绝不能被路由拦掉）")
# 全部取自真群 archive 里的真实句子 + 两轮实测校准出的边界
for s in ["谁给我画一张懒人群主","发个语音说群主是懒猪","来张图","画个鱼",
          "生成一个视频","搜索这个页面","查一下这个","帮我搜搜",
          "语音说句话","整张壁纸","做个表情","唱首歌",
          "你能参考这个，再画几张吗",          # 量词在动词后：画几张
          "你用日语发语音说一句杂鱼重复三次",
          "帮我搜索里面的内容并且说出来",
          "再画几张"]:
    check("D 收 %r"%s, is_ask(s), True)
for s in ["你们都几点睡","这鱼今天怎么这么安静","群主又跑了","哪来的白猫",
          "在弄了，等下重启","嗯","今天被领导骂了一顿",
          # 下面四条是实测误伤过的：推测句 / 否定句 / 第一人称自述 / 指令名
          "生成视频好像要时间吧","你想什么呢？生成不了那么复杂的",
          "这波神了，不行，我要录个视频","我去搜一下","我自己画得了"]:
    check("D 不误伤 %r"%s, is_ask(s), False)

print("E. 沉默连击保护")
MAX_STREAK=6; streak={}
def gate(gid):
    n=streak.get(gid,0)+1
    if n>MAX_STREAK: streak[gid]=0; return "放行"
    streak[gid]=n; return "沉默"
seq=[gate("g") for _ in range(15)]
check("E1 前6次沉默", seq[:6], ["沉默"]*6)
check("E2 第7次强制放行", seq[6], "放行")
check("E3 放行后重新计数", seq[7:13], ["沉默"]*6)
check("E4 不会永久哑掉", "放行" in seq[6:], True)

print("F. 熔断")
FAIL_MAX=3
class B:
    def __init__(s): s.run=0; s.until=0.0
    def fail(s,now=1000.0):
        s.run+=1
        if s.run>=FAIL_MAX: s.until=now+600; s.run=0; return "熔断"
        return "继续"
    def ok(s): s.run=0
b=B()
check("F1 前2次不熔断", [b.fail() for _ in range(2)], ["继续","继续"])
check("F2 第3次熔断", b.fail(), "熔断")
b2=B(); b2.fail(); b2.ok()
check("F3 成功一次清零", b2.run, 0)
check("F4 清零后要再攒满", [b2.fail() for _ in range(3)][-1], "熔断")

print("G. avoid 越权过滤（v1 实测吐出过 '别真画'）")
for bad in ["别真画","不要画","别发语音","别搜","不要生成","别调用","别用工具","别放视频"]:
    j=_parse('{"banter":true,"avoid":"%s"}'%bad)
    check("G 丢掉 %r"%bad, (j["avoid"], j.get("avoid_dropped")), ("", bad))
for good in ["别太认真","别说教","别抢话题","别接话茬","别硬杠或显摆"]:
    j=_parse('{"banter":true,"avoid":"%s"}'%good)
    check("G 保留 %r"%good, j["avoid"], good)

print("H. 刚说过就闭嘴（MIN_GAP）")
MIN_GAP=60.0
def gap_gate(last,now): return "沉默" if now-last<MIN_GAP else "判断"
check("H1 10秒前说过 -> 闭嘴", gap_gate(1000,1010), "沉默")
check("H2 59秒 -> 闭嘴", gap_gate(1000,1059), "沉默")
check("H3 60秒 -> 走判断", gap_gate(1000,1060), "判断")
check("H4 从没说过(last=0) -> 走判断", gap_gate(0,1e9), "判断")

print()
if fails: print(f"FAILED {len(fails)}: {fails}"); sys.exit(1)
print("ALL PASS")
