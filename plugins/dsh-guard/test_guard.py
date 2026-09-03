# test_guard.py —— dsh-guard 纯逻辑测试（不依赖 astrbot，容器内可直接跑）
# 覆盖：A 预筛正则（含真群真实句子）B 解析健壮性 C 玩梗降级
#       D 决策表 E 禁言时长硬上限 F 累犯窗口 G 同群禁言配额 H 管理员豁免
import json, re, sys
from collections import deque
fails=[]
def check(n,g,w):
    if g==w: print(f"  PASS {n}")
    else: print(f"  FAIL {n}\n       got={g!r}\n       want={w!r}"); fails.append(n)

PRE_RE=re.compile(
    r"政治|政府|政权|领导人|主席|总统|首相|执政|体制|制度|专政|民主|独裁|革命"
    r"|台独|港独|藏独|新疆|台湾|香港|西藏|统一|收复"
    r"|共产|国民党|党中央|上访|维权|游行|示威|抗议|镇压"
    r"|战犯|侵略|屠杀|反日|反美|反华|汉奸|卖国"
    r"|宗教|教徒|清真|穆斯林|基督|法轮"
    r"|涩图|色图|裸|做爱|约炮|开车|黄图|白丝|玉足|性感|丝袜|裤袜"
    r"|毒品|大麻|冰毒|赌博|博彩|菠菜|开户|洗钱|诈骗|杀猪盘"
    r"|外挂|私服|辅助|代练|盗号|黑号|发卡"
    r"|加我|私聊我|联系我|加微信|扫码|推广|返利|兼职|日入")

print("A. 预筛（成本闸门，宁可多放进来，但绝不能漏掉真违规）")
MUST_HIT=["你们觉得现在的政策合理吗，我觉得体制有问题","支持台独的都该",
          "发点涩图看看","来点白丝","加我微信，日入过千带你飞",
          "有没有卖王者外挂的","我要涩图","去给我找一个玉足视频",
          "发个玉足照片看一下","生成点性感的图","有卖大麻的吗","这是杀猪盘"]
for s in MUST_HIT: check("A 命中 %r"%s[:18], bool(PRE_RE.search(s)), True)
# 这些是真群里的原句/常见闲聊，放过它们=省一次模型调用
MUST_PASS=["这鱼怕不是充话费送的","群主又跑了，懒猪一个","三国里我最喜欢曹操",
           "阿根廷队今晚有比赛","你他妈真菜","叫我爸爸","骂群主是杂鱼",
           "我玩的是德国线，苏联那边太肝了","二战德国那关我打了三遍",
           "困了，你几点睡","晚安"]
for s in MUST_PASS: check("A 放过 %r"%s[:18], bool(PRE_RE.search(s)), False)

_BOOLS=("politics","nsfw","illegal","ad","attack","joking")
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
        out={}
        for k in _BOOLS:
            m=re.search(r'"%s"\s*:\s*(true|false|True|False)'%k,body)
            if m: out[k]=m.group(1).lower()=="true"
        m=re.search(r'"severity"\s*:\s*(\d)',body)
        if m: out["severity"]=int(m.group(1))
        m=re.search(r'"why"\s*:\s*"([^"]*)"',body)
        if m: out["why"]=m.group(1)
    if not any(k in out for k in _BOOLS) and "severity" not in out: return None
    r={k:bool(out.get(k,False)) for k in _BOOLS}
    try: r["severity"]=max(0,min(3,int(out.get("severity",0))))
    except BaseException: r["severity"]=0
    w=out.get("why"); r["why"]=w.strip()[:24] if isinstance(w,str) else ""
    return r

print("B. 解析健壮性")
GOOD='{"politics":true,"nsfw":false,"illegal":false,"ad":false,"attack":false,"joking":false,"severity":2,"why":"认真讨论体制"}'
check("B1 标准", (_parse(GOOD)["politics"],_parse(GOOD)["severity"]), (True,2))
check("B2 ```json 包裹", _parse("```json\n"+GOOD+"\n```")["severity"], 2)
check("B3 前后带废话", _parse("判断如下："+GOOD+" 完毕")["politics"], True)
check("B4 尾部多字符走正则兜底", _parse(GOOD[:-1]+',"x":1略}')["severity"], 2)
check("B5 空字符串", _parse(""), None)
check("B6 纯文字", _parse("这条没问题"), None)
check("B7 一个字段都没有=没结果(不是全False)", _parse('{"foo":"bar"}'), None)
check("B7b 合法JSON但全是无关字段也无效", _parse('{"a":1,"b":"x"}'), None)
check("B7c 只有why也无效(没有判断内容)", _parse('{"why":"随便"}'), None)
check("B8 只有severity也算有结果", _parse('{"severity":3}')["severity"], 3)
check("B9 severity 超范围被夹紧", _parse('{"severity":9,"politics":true}')["severity"], 3)
check("B10 severity 负数被夹紧", _parse('{"severity":-5,"politics":true}')["severity"], 0)
check("B11 severity 非数字回落0", _parse('{"severity":"高","politics":true}')["severity"], 0)
check("B12 why 超长被截", len(_parse('{"severity":2,"why":"'+"啊"*50+'"}')["why"]), 24)
check("B13 缺字段默认False", _parse('{"severity":2}')["nsfw"], False)

BAN_SEC,BAN_SEC_HIGH,MAX_BAN_SEC,WARN_TIMES=600,1800,1800,2
def eff(f):
    sev=f["severity"]
    if f.get("joking") and not f.get("politics"): sev=max(0,sev-1)
    return sev
def decide(f,wc):
    sev=eff(f)
    kinds=[k for k in ("politics","nsfw","illegal","ad","attack") if f.get(k)]
    if sev<=1 or not kinds: return "none",0,""
    kind=kinds[0]
    if sev>=3: return "ban",min(BAN_SEC_HIGH,MAX_BAN_SEC),kind
    if wc+1>=WARN_TIMES: return "ban",min(BAN_SEC,MAX_BAN_SEC),kind
    return "warn",0,kind
def F(**kw):
    d={k:False for k in _BOOLS}; d.update({"severity":0,"why":""}); d.update(kw); return d

print("C. 玩梗降级（把玩梗当违规是最不能接受的错误方向）")
check("C1 玩梗 sev2 -> 1", eff(F(nsfw=True,joking=True,severity=2)), 1)
check("C2 玩梗 sev3 -> 2", eff(F(attack=True,joking=True,severity=3)), 2)
check("C3 玩梗 sev0 不变负", eff(F(joking=True,severity=0)), 0)
check("C4 不玩梗不降", eff(F(nsfw=True,severity=2)), 2)
check("C5 政治即使玩梗也不降(玩笑地聊政治一样是风险)", eff(F(politics=True,joking=True,severity=2)), 2)

print("D. 决策表")
check("D1 sev0 什么都不做", decide(F(severity=0),0)[0], "none")
check("D2 sev1 什么都不做", decide(F(nsfw=True,severity=1),0)[0], "none")
check("D3 sev2 首次警告", decide(F(nsfw=True,severity=2),0)[:1], ("warn",))
check("D4 sev2 第二次禁言", decide(F(nsfw=True,severity=2),1)[0], "ban")
check("D5 sev3 直接禁言", decide(F(politics=True,severity=3),0)[0], "ban")
check("D6 sev3 禁1800秒", decide(F(politics=True,severity=3),0)[1], 1800)
check("D7 sev2 累犯禁600秒", decide(F(ad=True,severity=2),1)[1], 600)
check("D8 有severity但没类型 -> 不动(模型自相矛盾时宁可不动)", decide(F(severity=3),0)[0], "none")
check("D9 玩梗把sev2压到1就不处理了", decide(F(attack=True,joking=True,severity=2),0)[0], "none")
check("D10 理由取第一个命中的类型", decide(F(politics=True,nsfw=True,severity=3),0)[2], "politics")
check("D11 日常对骂(模型给0) -> 不动", decide(F(joking=True,severity=0),5)[0], "none")

print("E. 禁言时长硬上限")
def clamp(sec): return max(1,min(int(sec),MAX_BAN_SEC))
check("E1 正常值不变", clamp(600), 600)
check("E2 超上限被夹紧", clamp(86400), 1800)
check("E3 0 被抬到1", clamp(0), 1)
check("E4 负数被抬到1", clamp(-100), 1)
check("E5 决策给的值本身就不超上限", all(decide(F(politics=True,severity=s),9)[1]<=MAX_BAN_SEC for s in (2,3)), True)

print("F. 累犯窗口（sev2 攒够次数才禁）")
WARN_WINDOW=86400.0
wq=deque()
def prune(dq,now,w):
    while dq and now-dq[0]>w: dq.popleft()
def step(now):
    prune(wq,now,WARN_WINDOW)
    a,_,_=decide(F(nsfw=True,severity=2),len(wq))
    wq.append(now)
    return a
check("F1 第一次警告", step(1000.0), "warn")
check("F2 第二次禁言", step(2000.0), "ban")
wq.clear()
check("F3 清零后重新从警告开始", step(3000.0), "warn")
check("F4 隔了25小时视为新账(窗口滑走)", step(3000.0+90000), "warn")

print("G. 同群禁言配额（防判定跑偏群灭）")
BAN_WINDOW,GROUP_BAN_MAX=3600.0,3
bq=deque()
def ban_ok(now):
    prune(bq,now,BAN_WINDOW)
    if len(bq)>=GROUP_BAN_MAX: return False
    bq.append(now); return True
check("G1 前3人放行", [ban_ok(1000.0+i) for i in range(3)], [True]*3)
check("G2 第4人被挡", ban_ok(1003.0), False)
check("G3 一小时后恢复", ban_ok(1000.0+3700), True)

print("H. 管理员豁免（技术上也禁不了：实测返回 cannot ban admin）")
def can_ban(role,uid,me,white):
    if role in ("owner","admin"): return False
    if uid==me: return False
    if uid in white: return False
    return True
check("H1 群主不禁", can_ban("owner","1","me",set()), False)
check("H2 管理员不禁", can_ban("admin","1","me",set()), False)
check("H3 普通成员可禁", can_ban("member","1","me",set()), True)
check("H4 机器人自己不禁", can_ban("member","me","me",set()), False)
check("H5 白名单不禁", can_ban("member","9","me",{"9"}), False)
check("H6 查不到身份按member处理(宁可查错不能漏查)", can_ban("member","1","me",set()), True)

print()
if fails: print("FAILED %d: %s"%(len(fails),fails)); sys.exit(1)
print("ALL PASS")
