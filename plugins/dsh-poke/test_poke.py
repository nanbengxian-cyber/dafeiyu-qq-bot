# test_poke.py —— dsh-poke 纯逻辑测试（不依赖 astrbot，容器内可直接跑）
# 覆盖：A raw 取值的三种形状 B 目标识别 C 同人冷却 D 两级限流 E 连击「烦了」判定
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

print("C. 同人冷却")
COOLDOWN=20.0
last={}
def cool_ok(key,now):
    if now-last.get(key,0.0)<COOLDOWN: return False
    last[key]=now; return True
check("C1 第一次放行", cool_ok("g:u",1000.0), True)
check("C2 5秒后被挡", cool_ok("g:u",1005.0), False)
check("C3 19.9秒仍被挡", cool_ok("g:u",1019.9), False)
check("C4 20秒后放行", cool_ok("g:u",1020.0), True)
check("C5 换人不受影响", cool_ok("g:other",1020.1), True)
check("C6 换群不受影响", cool_ok("g2:u",1020.2), True)

print("D. 两级限流（同群 + 全局）")
WINDOW=60.0; GROUP_MAX=3; GLOBAL_MAX=6
gh={}; gl=deque()
def prune(dq,now):
    while dq and now-dq[0]>WINDOW: dq.popleft()
def quota(gid,now):
    g=gh.setdefault(gid,deque()); prune(g,now); prune(gl,now)
    if len(g)>=GROUP_MAX: return False,"group"
    if len(gl)>=GLOBAL_MAX: return False,"global"
    return True,""
def note(gid,now):
    gh.setdefault(gid,deque()).append(now); gl.append(now)
t=1000.0
seq=[]
for i in range(5):
    ok,why=quota("A",t+i); seq.append("ok" if ok else why)
    if ok: note("A",t+i)
check("D1 同群前3次放行，之后挡", seq, ["ok","ok","ok","group","group"])
# 全局：换群继续，直到全局 6 满
seq2=[]
for i,g in enumerate(["B","B","B","C"]):
    ok,why=quota(g,t+10+i); seq2.append("ok" if ok else why)
    if ok: note(g,t+10+i)
check("D2 换群仍受全局限制(已用3,再3满6)", seq2, ["ok","ok","ok","global"])
check("D3 窗口滑走后恢复", quota("A",t+200)[0], True)
gh2={}; gl2=deque()
check("D4 全新状态直接放行", quota("Z",t+300)[0], True)

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
