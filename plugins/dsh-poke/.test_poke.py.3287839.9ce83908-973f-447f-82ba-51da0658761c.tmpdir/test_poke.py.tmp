# test_poke.py —— dsh-poke 纯逻辑测试（不依赖 astrbot，容器内可直接跑）
# 覆盖：A raw 取值的三种形状 B 目标识别 C 普通/群主冷却 D 按群隔离限流 E 连击「烦了」判定
import sys, time
from collections import deque
fails=[]
def check(n,g,w):
    if g==w: print(f"  PASS {n}")
    else: print(f"  FAIL {n}\n       got={g!r}\n       want={w!r}"); fails.append(n)

def _rg(raw,key,default=None):
    if isinstance(raw,dict): return raw.get(key,default)
    try: return raw[key]
    except BaseException: pass
    return getattr(raw,key,default)

print("A. raw 取值三种形状（notice 的 raw 可能是 dict / Event / 对象）")
check("A1 dict", _rg({"post_type":"notice"},"post_type"), "notice")
check("A2 dict 缺键给默认", _rg({},"post_type","x"), "x")
class EvLike:           # 支持 [] 但不是 dict（aiocqhttp.Event 就是这样）
    def __init__(s,d): s.d=d
    def __getitem__(s,k): return s.d[k]
check("A3 支持[]的对象", _rg(EvLike({"sub_type":"poke"}),"sub_type"), "poke")
check("A4 []抛KeyError回落getattr", _rg(EvLike({}),"sub_type","d"), "d")
class AttrLike:
    post_type="notice"
check("A5 只有属性的对象", _rg(AttrLike(),"post_type"), "notice")
check("A6 都取不到给默认", _rg(object(),"nope","d"), "d")

print("B. 目标识别（戳的是不是我）")
class PokeComp:
    def __init__(s,t): s._t=t
    def target_id(s): return s._t
def target_of(comps, raw):
    for c in comps:
        fn=getattr(c,"target_id",None)
        if callable(fn):
            t=fn()
            if t: return str(t)
    t=_rg(raw,"target_id")
    return str(t) if t else None
check("B1 从组件读", target_of([PokeComp("123")],{}), "123")
check("B2 组件返回0时回落raw", target_of([PokeComp(None)],{"target_id":456}), "456")
check("B3 没组件也没raw", target_of([],{}), None)
check("B4 int 转成 str", target_of([PokeComp(789)],{}), "789")
ME="100000002"
def is_me(t): return bool(t) and t==ME
check("B5 戳我 -> 要回", is_me(target_of([PokeComp(ME)],{})), True)
check("B6 戳别人 -> 不回", is_me(target_of([PokeComp("2001")],{})), False)
check("B7 拿不到目标 -> 不回（宁可不动）", is_me(target_of([],{})), False)

print("C. 普通成员/群主冷却")
COOLDOWN=20.0; OWNER_COOLDOWN=3.0; OWNER="owner"
last={}
def cool_ok(gid,uid,now):
    cd=OWNER_COOLDOWN if uid==OWNER else COOLDOWN
    key=f"{gid}:{uid}"
    if now-last.get(key,0.0)<cd: return False
    last[key]=now; return True
check("C1 普通成员第一次放行", cool_ok("g","u",1000.0), True)
check("C2 普通成员5秒后被挡", cool_ok("g","u",1005.0), False)
check("C3 普通成员20秒后放行", cool_ok("g","u",1020.0), True)
check("C4 群主第一次放行", cool_ok("g",OWNER,2000.0), True)
check("C5 群主2秒后被挡", cool_ok("g",OWNER,2002.0), False)
check("C6 群主3秒后放行", cool_ok("g",OWNER,2003.0), True)
check("C7 换群不受影响", cool_ok("g2",OWNER,2003.1), True)

print("D. 按群隔离限流")
WINDOW=60.0; GROUP_MAX=6
gh={}
def prune(dq,now):
    while dq and now-dq[0]>WINDOW: dq.popleft()
def quota(gid,now):
    g=gh.setdefault(gid,deque()); prune(g,now)
    if len(g)>=GROUP_MAX: return False,"group"
    return True,""
def note(gid,now): gh.setdefault(gid,deque()).append(now)
t=1000.0
seq=[]
for i in range(7):
    ok,why=quota("A",t+i); seq.append("ok" if ok else why)
    if ok: note("A",t+i)
check("D1 同群前6次放行，第7次挡", seq, ["ok"]*6+["group"])
check("D2 A群满额不影响B群", quota("B",t+10)[0], True)
note("B",t+10)
check("D3 B群动作不改变A群限流", quota("A",t+11)[1], "group")
check("D4 窗口滑走后A群恢复", quota("A",t+200)[0], True)

print("E. 连击「烦了」判定")
rep={}
def bump(key): rep[key]=rep.get(key,0)+1; return rep[key]
def annoyed(key): return rep.get(key,0)>=2
check("E1 首次不烦", annoyed("k"), False)
bump("k")
check("E2 戳2下(计数1)还不烦", annoyed("k"), False)
bump("k")
check("E3 计数到2就烦了", annoyed("k"), True)
rep["k"]=0
check("E4 回戳后清零", annoyed("k"), False)

print("F. 短句库")
TALK=["干嘛","别戳","？","戳回去了","手别抖","再戳咬你","有事说事","戳我干啥"]
ANNOYED=["还戳？","戳够了没","手是不是闲","行吧你厉害","再戳我就装死"]
check("F1 一般短句都≤6字", max(len(x) for x in TALK)<=6, True)
check("F2 烦了短句都≤7字", max(len(x) for x in ANNOYED)<=7, True)
check("F3 没有客服腔", any(any(k in x for k in ("您","请问","为您","帮助")) for x in TALK+ANNOYED), False)
check("F4 两组不重复", set(TALK)&set(ANNOYED), set())

print()
if fails: print("FAILED %d: %s"%(len(fails),fails)); sys.exit(1)
print("ALL PASS")
