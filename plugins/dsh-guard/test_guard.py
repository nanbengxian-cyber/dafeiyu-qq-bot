# test_guard.py —— dsh-guard 纯逻辑测试（不依赖 astrbot，容器内可直接跑）
# 覆盖：A 预筛正则（含真群真实句子）B 解析健壮性 C 玩梗降级
#       D 决策表 E 禁言时长硬上限 F 累犯窗口 G 同群禁言配额 H 管理员豁免
import json, re, sys
from collections import deque
fails=[]
def check(n,g,w):
    if g==w: print(f"  PASS {n}")
    else: print(f"  FAIL {n}\n       got={g!r}\n       want={w!r}"); fails.append(n)

# ---- 预筛：组合判断（与 guard_main.py 的 pre_hit 同构）----
# 旧版是一张扁平词表，为了不误伤「二战德国那关我打了三遍」把「战争」删掉，
# 结果漏掉了真群 09:26 的真实提问「美国和伊朗的战争结束了没有？」。
# 现在改成：直接词 OR (国名 AND 政治名词 AND NOT 游戏历史标记)。
_DIRECT_RE=re.compile(
    r"政治|政府|政权|领导人|主席|总统|首相|执政|体制|专政|独裁|革命"
    r"|台独|港独|藏独|新疆|西藏|法轮"
    r"|共产|国民党|党中央|上访|维权|游行|示威|抗议|镇压"
    r"|战犯|侵略|屠杀|反日|反美|反华|汉奸|卖国"
    r"|宗教|教徒|清真|穆斯林|基督"
    r"|涩图|色图|裸|做爱|约炮|开车|黄图|白丝|玉足|性感|丝袜|裤袜"
    r"|无码|有码|露骨|情色|av|A片|黄片|三级片|福利姬|本子|同人志|里番|工口"
    r"|毒品|大麻|冰毒|赌博|博彩|菠菜|开户|洗钱|诈骗|杀猪盘"
    r"|外挂|私服|代练|盗号|黑号|发卡"
    r"|加我|私聊我|联系我|加微信|扫码|推广|返利|兼职|日入")
_COUNTRY_RE=re.compile(
    r"美国|中国|俄罗斯|俄国|乌克兰|伊朗|伊拉克|以色列|巴勒斯坦|加沙|叙利亚"
    r"|朝鲜|韩国|日本|印度|巴基斯坦|越南|菲律宾|台湾|香港|欧盟|北约"
    r"|阿富汗|土耳其|沙特|也门|黎巴嫩|缅甸|俄乌|中美|中日|台海|南海")
_POLI_NOUN_RE=re.compile(
    r"战争|开战|停战|停火|交战|冲突|局势|形势|制裁|禁运|军事|驻军|出兵"
    r"|导弹|核弹|核武|政策|选举|大选|总统|议会|外交|建交|断交"
    r"|主权|领土|入侵|占领|统一|独立|收复|回归|谈判|协议|条约|难民|人权"
    r"|关系|态度|立场|谁赢|谁厉害|打起来|会打|开打")
_GAME_HIST_RE=re.compile(
    r"这关|那关|第.关|打了|通关|开局|阵营|路线|steam|游戏|模拟|策略"
    r"|三国|春秋|战国|唐朝|宋朝|明朝|清朝|历史课|课本|一战|二战")

# 人身攻击：不能枚举脏话（这个群日常互骂），用「伤害词 + 指向家人」的组合
_HARM_RE=re.compile(r"死|去世|癌|艾滋|残废|绝症|车祸|截肢|火化|坟|棺")
_TARGET_RE=re.compile(r"你妈|你爸|你爹|你娘|你家|你父母|你全家|全家|一家人|祖宗|家人")
# 群体仇恨：**不能用裸伤害词**（「好吃死了」「笑死我了」全命中），
# 必须是「群体指称 + 集体贬损谓语」
_GROUP_RE=re.compile(r"美国|中国|俄罗斯|俄国|乌克兰|伊朗|伊拉克|以色列|巴勒斯坦|叙利亚"
                     r"|朝鲜|韩国|日本|印度|巴基斯坦|越南|菲律宾|台湾|香港|缅甸|非洲|欧洲"
                     r"|黑人|白人|犹太|犹大|回族|维族|藏族|汉族|穆斯林|基督徒|同性恋|残疾人"
                     r"|河南人|东北人")
_SLUR_RE=re.compile(r"该死|都死|去死|该杀|杀光|杀绝|灭绝|清理|清除|垃圾|畜生|杂种|劣等"
                    r"|下贱|贱种|不是人|滚出|驱逐|该被|活该")

def pre_hit(text):
    if _DIRECT_RE.search(text): return True
    if _COUNTRY_RE.search(text) and _POLI_NOUN_RE.search(text):
        return not _GAME_HIST_RE.search(text)
    if _HARM_RE.search(text) and _TARGET_RE.search(text): return True
    if _GROUP_RE.search(text) and _SLUR_RE.search(text): return True
    return False

print("A. 预筛（成本闸门，宁可多放进来，但绝不能漏掉真违规）")
MUST_HIT=["你们觉得现在的政策合理吗，我觉得体制有问题","支持台独的都该",
          "发点涩图看看","来点白丝","加我微信，日入过千带你飞",
          "有没有卖王者外挂的","我要涩图","去给我找一个玉足视频",
          "发个玉足照片看一下","生成点性感的图","有卖大麻的吗","这是杀猪盘",
          # ↓ 真群 2026-09-03 09:26 的真实提问，旧版扁平词表漏了它
          "美国和伊朗的战争结束了没有？",
          "俄乌局势怎么样了","以色列和加沙那边停火了吗",
          "你们觉得中美关系会怎么走","台海局势紧张吗",
          "日本对中国的态度","俄罗斯和乌克兰会打起来吗",
          # ↓ 人身攻击这一类第一版预筛整类漏掉了（判定层给 sev=3 却没送进去）
          "你妈死了你全家都该死","你全家都得癌症","你爸车祸死了才好",
          # ↓ 群体仇恨：既不在直接词表里，也不满足「国名+政治名词」，第二版才补上
          "日本人都该死，都是畜生","黑人都该死","犹太人该被清理",
          "中国人都是垃圾该死","穆斯林该滚出去","同性恋都是畜生",
          # ↓ 露骨请求不含「涩图」这类具体词，第三版才补上
          "谁有那种无码的资源发我，越露骨越好","本子推荐一下","有av资源吗"]
for s in MUST_HIT: check("A 命中 %r"%s[:20], pre_hit(s), True)
# 这些是真群里的原句/常见闲聊，放过它们=省一次模型调用
MUST_PASS=["这鱼怕不是充话费送的","群主又跑了，懒猪一个","三国里我最喜欢曹操",
           "阿根廷队今晚有比赛","你他妈真菜","叫我爸爸","骂群主是杂鱼",
           "我玩的是德国线，苏联那边太肝了","二战德国那关我打了三遍",
           "困了，你几点睡","晚安",
           # 有国名但没政治名词
           "美国队长好看吗","日本料理好吃","我在台湾旅游过，夜市不错",
           # 有「关系」但没国名 —— 这是加「关系」这个词时最怕的误伤方向
           "这跟你有什么关系","我跟他关系很好",
           # 真群原句
           "中专和普高考的不一样","群主现在在哪发财","所以说主要是能不能学好",
           # 伤害词或指向词单独出现都不该命中 —— 这是加人身攻击组合时最怕的误伤
           "笑死我了","困死了","我死了都不干","你他妈真菜","他妈的真烦",
           "我家的猫很可爱","全家一起吃饭",
           # ↓ 「死」作程度补语，是仇恨检测最容易误伤的一片（第一版真的全命中了）
           "日本料理好吃死了","这个韩国综艺笑死我了","二战日本人死了很多",
           "困死了","累死我了","这游戏难死了","韩国泡菜好吃","黑人歌手唱得真好",
           "去死吧你"]
for s in MUST_PASS: check("A 放过 %r"%s[:20], pre_hit(s), False)
check("A 组合命中需要两个条件都在", pre_hit("伊朗"), False)
check("A 只有政治名词不命中", pre_hit("局势不太好"), False)
check("A 游戏历史标记能撤销组合命中",
      pre_hit("美国和伊朗的战争") and not pre_hit("美国和伊朗的战争那关我打了三遍"), True)

_BOOLS=("politics","stance","nsfw","illegal","ad","attack","joking")
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

print("A2. PROMPT 模板可 format（本轮最严重的 bug 就在这里）")
# 在 PROMPT 里写裸 { 会让 str.format 抛 ValueError: unexpected '{' in field name。
# 后果是**每一次判定都失败**，而失败路径是 fail-open（什么都不做）——
# 群里完全看不出异常，日志只有一行 warning，是个「安静地全面失效」的 bug。
# 前面 80 条断言全绿也没发现它，因为没有一条真的调用过 .format()。
import importlib.util as _ilu, os as _os
_gp = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "main.py")
if _os.path.exists(_gp):
    _src = open(_gp, encoding="utf-8").read()
    _i = _src.index('PROMPT = """'); _j = _src.index('"""', _i + 12) + 3
    _ns = {}
    exec(_src[_i:_j], {}, _ns)
    try:
        _out = _ns["PROMPT"].format(name="阿强", text="测试", ctx="上下文")
        check("A2a PROMPT.format 不抛异常", True, True)
        check("A2b 占位符都被替换掉了",
              all(x in _out for x in ("阿强", "测试", "上下文")), True)
        check("A2c 输出示例里的 JSON 花括号活下来了", '{"politics"' in _out, True)
        check("A2d 没有残留未替换的占位符",
              bool(re.search(r"\{(name|text|ctx)\}\}", _out)), False)
    except BaseException as _e:
        check("A2a PROMPT.format 不抛异常  <<%s: %s>>" % (type(_e).__name__, _e),
              False, True)
    try:
        _ns2 = {}
        exec(_src[_src.index('SYS = ('):_src.index("\n\n", _src.index('SYS = ('))], {}, _ns2)
        check("A2e SYS 是非空字符串", bool(_ns2.get("SYS", "").strip()), True)
    except BaseException:
        pass
else:
    print("  SKIP A2（同目录没有 main.py，本测试文件被单独拷出来跑）")

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
check("B12 why 超长被截", len(_parse('{"severity":2,"why":"'+'啊'*50+'"}')["why"]), 24)
check("B13 缺字段默认False", _parse('{"severity":2}')["nsfw"], False)

BAN_SEC,BAN_SEC_HIGH,MAX_BAN_SEC,WARN_TIMES=600,1800,1800,2
_HARD=("politics","nsfw","illegal","ad")
def eff(f):
    sev=f["severity"]
    # 玩梗降级只对态度类(attack)生效。硬行为类不吃：实测「发点涩图看看」
    # 被判 joking 后 sev 从 1 降到 0，等于永远不会警告。
    # sev==3 的 attack 只有两个来源：模型判得很重，或 escalate 抬上来的
    # （诅咒家人/群体仇恨）。这两种「说成玩笑」也不该打折。
    if f.get("joking") and not any(f.get(k) for k in _HARD) and sev<3: sev=max(0,sev-1)
    # 政治但没表立场 -> 上限 1（不动作）。实测模型对「单纯问时事」给分不稳：
    # 「美国和伊朗的战争结束了没有？」给 0，但「俄乌局势怎么样了」给 2。
    # 用户原话是「聊政治太过分才禁」，问一句进展不叫太过分。
    if f.get("politics") and not f.get("stance"):
        if not any(f.get(k) for k in ("nsfw","illegal","ad","attack")):
            sev=min(sev,1)
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

print("C0. escalate：代码能确定的事实不交给模型")
_HARM_RE2=_HARM_RE; _TARGET_RE2=_TARGET_RE
def escalate(f,text):
    if _HARM_RE2.search(text) and _TARGET_RE2.search(text):
        f["attack"]=True
        if f["severity"]<3: f["severity"]=3; f["why"]=(f.get("why") or "")+"｜代码判定:诅咒家人"
    if _GROUP_RE.search(text) and _SLUR_RE.search(text):
        f["attack"]=True
        if f["severity"]<3: f["severity"]=3; f["why"]=(f.get("why") or "")+"｜代码判定:群体仇恨"
    return f
# 实测同一句「你妈死了你全家都该死」两轮分别被判 sev=3 和 sev=2（温度已是 0，
# 不是采样随机，是这句压在 2/3 边界）。2 和 3 的差别是「先警告」还是「直接禁」。
check("C0a 模型判2 -> 代码抬到3", escalate(F(attack=True,severity=2),"你妈死了你全家都该死")["severity"], 3)
check("C0b 模型判3 -> 不动", escalate(F(attack=True,severity=3),"你妈死了")["severity"], 3)
check("C0c 只抬不降(模型判3不会被压回)", escalate(F(attack=True,severity=3),"日本人都该死")["severity"], 3)
check("C0d 群体仇恨也抬", escalate(F(severity=1),"日本人都该死，都是畜生")["severity"], 3)
check("C0e 抬的同时置 attack", escalate(F(severity=0),"你妈死了你全家都该死")["attack"], True)
check("C0f 普通句子不抬", escalate(F(severity=0),"笑死我了")["severity"], 0)
check("C0g 日常对骂不抬", escalate(F(severity=0),"你他妈真菜")["severity"], 0)
check("C0h why 里留下代码判定的痕迹", "代码判定" in escalate(F(severity=1),"你妈死了你全家该死")["why"], True)
# ★ 模型解析失败时，代码兜底必须仍然生效（两条独立防线不能塌成一条）★
# 实测模型偶尔先输出一段中文推理再给 JSON，被 max_tokens 截断 -> 解析失败。
# 如果那时直接 return，「日本人都该死，都是畜生」就被静默漏过了。
def blank():
    d={k:False for k in _BOOLS}; d["severity"]=0; d["why"]="模型未给出判定"; return d
check("C0i 解析失败+诅咒家人 -> 代码兜到3", escalate(blank(),"你妈死了你全家都该死")["severity"], 3)
check("C0j 解析失败+群体仇恨 -> 代码兜到3", escalate(blank(),"日本人都该死，都是畜生")["severity"], 3)
check("C0k 解析失败+普通句 -> 仍是0（什么都不做）", escalate(blank(),"美国和伊朗的战争结束了没有？")["severity"], 0)
check("C0l 解析失败+日常对骂 -> 仍是0", escalate(blank(),"你他妈真菜")["severity"], 0)

print("C. 降级规则（两条都是为了压住误禁这个方向）")
check("C1 玩梗+attack sev3 不降（诅咒家人说成玩笑也是诅咒）", eff(F(attack=True,joking=True,severity=3)), 3)
check("C1b 玩梗+nsfw 不降（索要色情是行为，不看语气）", eff(F(nsfw=True,joking=True,severity=2)), 2)
check("C1c 玩梗+ad 不降", eff(F(ad=True,joking=True,severity=2)), 2)
check("C1d 玩梗+illegal 不降", eff(F(illegal=True,joking=True,severity=2)), 2)
check("C2 玩梗+attack sev2 -> 1（这一档才是真的互怼玩梗）", eff(F(attack=True,joking=True,severity=2)), 1)
check("C3 玩梗 sev0 不变负", eff(F(joking=True,severity=0)), 0)
check("C4 不玩梗不降", eff(F(nsfw=True,severity=2)), 2)
# C5 原来断言 politics+joking sev2 仍是 2（玩梗降级对政治不生效）。
# 加了「没表立场就封顶 1」之后，这条会被封顶压到 1 —— 两条规则叠加的结果。
# 拆成两条分别锁死：玩梗降级确实对政治不生效（用带 stance 的场景验证），
# 封顶确实生效。
check("C5 政治+玩梗+没立场 -> 被封顶压到1", eff(F(politics=True,joking=True,severity=2)), 1)
check("C5b 政治+玩梗+有立场 -> 仍是3", eff(F(politics=True,stance=True,joking=True,severity=3)), 3)
print("   —— 政治：问事实 vs 表立场 ——")
check("C6 政治+没立场 sev2 -> 封顶1(问进展不叫太过分)", eff(F(politics=True,severity=2)), 1)
check("C7 政治+没立场 sev3 -> 也封顶1", eff(F(politics=True,severity=3)), 1)
check("C8 政治+表立场 sev3 不降", eff(F(politics=True,stance=True,severity=3)), 3)
check("C9 政治+表立场 sev2 不降", eff(F(politics=True,stance=True,severity=2)), 2)
check("C10 政治没立场但同时要涩图 -> 不封顶", eff(F(politics=True,nsfw=True,severity=2)), 2)
check("C11 政治没立场但同时发广告 -> 不封顶", eff(F(politics=True,ad=True,severity=2)), 2)
check("C12 非政治不受封顶影响", eff(F(illegal=True,severity=3)), 3)
check("C13 politics+stance+joking 仍按原值(政治不吃玩梗降级)",
      eff(F(politics=True,stance=True,joking=True,severity=3)), 3)

print("D. 决策表")
check("D1 sev0 什么都不做", decide(F(severity=0),0)[0], "none")
check("D2 sev1 什么都不做", decide(F(nsfw=True,severity=1),0)[0], "none")
check("D3 sev2 首次警告", decide(F(nsfw=True,severity=2),0)[:1], ("warn",))
check("D4 sev2 第二次禁言", decide(F(nsfw=True,severity=2),1)[0], "ban")
check("D5 sev3 直接禁言", decide(F(politics=True,stance=True,severity=3),0)[0], "ban")
check("D6 sev3 禁1800秒", decide(F(politics=True,stance=True,severity=3),0)[1], 1800)
check("D5b sev3 但政治没立场 -> 被封顶，不禁", decide(F(politics=True,severity=3),0)[0], "none")
check("D7 sev2 累犯禁600秒", decide(F(ad=True,severity=2),1)[1], 600)
check("D8 有severity但没类型 -> 不动(模型自相矛盾时宁可不动)", decide(F(severity=3),0)[0], "none")
check("D9 玩梗把sev2压到1就不处理了", decide(F(attack=True,joking=True,severity=2),0)[0], "none")
check("D10 理由取第一个命中的类型", decide(F(politics=True,stance=True,nsfw=True,severity=3),0)[2], "politics")
check("D11 日常对骂(模型给0) -> 不动", decide(F(joking=True,severity=0),5)[0], "none")
check("D12 问时事(政治无立场) -> 首犯不动", decide(F(politics=True,severity=2),0)[0], "none")
check("D13 问时事(政治无立场) -> 连问5次也不动", decide(F(politics=True,severity=2),5)[0], "none")
check("D14 表立场号召 -> 首犯直接禁", decide(F(politics=True,stance=True,severity=3),0)[0], "ban")
check("D15 stance 不会被当成违规类型", decide(F(politics=True,stance=True,severity=3),0)[2], "politics")

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
