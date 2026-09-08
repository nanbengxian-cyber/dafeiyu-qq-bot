# -*- coding: utf-8 -*-
"""dsh-video「做视频」意图判据的回归测试。

跑法：docker exec astrbot python3 /AstrBot/data/plugins/dsh-video/test_video.py

插件依赖真 astrbot 包，不能整体 import，所以用 AST 把正则常量和纯函数抠出来 exec。
常量按「顶层赋值 + 全大写命名」整批抓，不列清单 —— 这些正则彼此引用，
列清单必漏（dsh-memory 上踩过两次 NameError）。

判据的两个方向都要守：
  MUST_FIRE     真请求必须还能出片（否则功能白瞎）
  MUST_NOT_FIRE 叙述/议论/搜索绝不能出片（出片要 4 分钟、要钱、还会发进群）
"""

import ast
import io
import os
import re
import sys
import time

P = os.environ.get("DSH_VID_MAIN", "/AstrBot/data/plugins/dsh-video/main.py")
SRC = io.open(P, encoding="utf-8").read()

NAME_OK = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
WANT_FN = ("_derive_prompt", "_imperative_gain")
chunks = []
for node in ast.parse(SRC).body:
    if isinstance(node, ast.Assign):
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if names and all(NAME_OK.match(n) for n in names):
            seg = ast.get_source_segment(SRC, node)
            if seg:
                chunks.append(seg)
    elif isinstance(node, ast.FunctionDef) and node.name in WANT_FN:
        chunks.append(ast.get_source_segment(SRC, node))

ns = {"re": re, "os": os, "time": time}
for _ in range(8):                      # 正则互相拼接，要多趟才收敛
    rest = []
    for c in chunks:
        try:
            exec(compile(c, "<x>", "exec"), ns)
        except Exception:
            rest.append(c)
    if not rest:
        break
    chunks = rest

for want in ("VIDEO_MAKE_RE", "VIDEO_WATCH_RE", "IMG_NOUN_RE", "COMMENT_HEAD_RE",
             "STRICT_MAKE", "MIN_PROMPT", *WANT_FN):
    assert want in ns, f"没抠到 {want} —— 少一层就等于没测那一层"

MAKE = ns["VIDEO_MAKE_RE"]
WATCH = ns["VIDEO_WATCH_RE"]
IMGN = ns["IMG_NOUN_RE"]
COMMENT = ns["COMMENT_HEAD_RE"]
derive = ns["_derive_prompt"]
gain = ns["_imperative_gain"]

fails = []


def decide(text, strict=True):
    """复刻 auto_video 兜底路径（leaked=False 那条分支）的完整判定。

    返回 (会不会出片, 原因, 画面描述)。改 main.py 的判定顺序时这里要同步。
    """
    if not MAKE.search(text):
        return False, "没有做视频的意思", ""
    if WATCH.search(text):
        return False, "是看视频", ""
    if IMGN.search(text):
        return False, "要的是图", ""
    p = derive(text)
    if len(p) < ns.get("MIN_PROMPT", 3):
        return False, "推不出画面描述", p
    if strict:
        g = gain(text, p)
        if g < 2:
            return False, "不是祈使句", p
        if COMMENT.search(p):
            return False, "推出来是议论", p
    return True, "出片", p


# ---------------------------------------------------------------- 必须出片
MUST_FIRE = [
    # 真语料
    ("做一个视频用日语说杂鱼重复三次", "用日语说杂鱼重复三次"),
    ("给我生成一个阳光明媚的视频", "阳光明媚"),
    # 常见说法，宾语长度不同（历史上掐字数漏过 7 字宾语）
    ("做个小鲸鱼甩尾巴的动画", None),
    ("来段海浪拍岸的视频", None),
    ("帮我做个猫娘跳舞的视频", None),
    ("大肥鱼，给我整段下雨的短片", None),
    ("生成一个赛博朋克城市夜景的视频吧", None),
    ("做个视频，一条鲸鱼在海里翻身", "一条鲸鱼在海里翻身"),
]

# ---------------------------------------------------------------- 绝不能出片
MUST_NOT_FIRE = [
    # ↓ 三条真语料误判，这次事故的核心
    ("唉，要是再来几次这种视频，那我的群也不用那么冷了", "不是祈使句"),
    ("生成视频好像要时间吧", "推出来是议论"),
    ("去给我找一个玉足视频", "没有做视频的意思"),
    ("视频生成啊，大哥", "不是祈使句"),
    # ↓ 叙述自己的视频（群主 11:13 真发过）
    ("我去，我的视频快4000播放了", None),
    ("我昨天发的视频播放量涨了", None),
    ("他做的那个视频挺好看的", None),
    # ↓ 要「取来已有的」而不是做新的：动词是找/搜/发/转，不是创建
    ("帮我搜一下这个B站视频，介绍里面的详细信息 https://b23.tv/F8TdUKB", None),
    ("帮我查看这个视频，并且告诉他的内容 https://b23.tv/oqIQQx0", None),
    ("给我发个视频看看", None),
    ("谁有那个视频发一下", None),
    # ↓ 要的是图，视频只是修饰语
    ("生成一个和我发的表情包差不多的视频", "要的是图"),
    ("画个视频封面", "要的是图"),
    # ↓ 在议论出片这件事
    ("生成视频应该很贵吧", None),
    ("做视频可能要等挺久", None),
    # ↓ 只有视频词，没有创建动词
    ("这视频啥意思", None),
    ("视频挺清楚的", None),
]

print("=== 必须出片 ===")
for text, want_prompt in MUST_FIRE:
    ok, why, p = decide(text)
    mark = "✓" if ok else "✗"
    print(f"  {mark} {text[:34]:36s} → {why}｜画面={p[:26]}")
    if not ok:
        fails.append(f"该出片却没出：{text}（{why}）")
    elif want_prompt is not None and p != want_prompt:
        fails.append(f"画面描述不对：{text} → 得到「{p}」期望「{want_prompt}」")

print("\n=== 绝不能出片 ===")
for text, want_why in MUST_NOT_FIRE:
    ok, why, p = decide(text)
    mark = "✗" if ok else "✓"
    print(f"  {mark} {text[:34]:36s} → {why}")
    if ok:
        fails.append(f"不该出片却出了：{text}（画面={p}）")
    elif want_why and why != want_why:
        fails.append(f"拦下的理由变了：{text} → 「{why}」期望「{want_why}」"
                     f"（判据顺序变了会让日志说不清原因）")

# ---------------------------------------------------------------- 结构断言
print("\n=== 结构 ===")

# 「给我/帮我」必须是可选前缀而不是与创建动词并列。
# 并列时「给我 + 任何动词 + 视频」都命中，这是「去给我找一个玉足视频」的根因。
src_make = re.search(r"VIDEO_MAKE_RE = re\.compile\((.*?)\n\)", SRC, re.S)
assert src_make, "找不到 VIDEO_MAKE_RE 源码"
body = src_make.group(1)
if re.search(r"\|\s*给我\s*\|\s*帮我", body):
    fails.append("VIDEO_MAKE_RE 又把「给我|帮我」并进创建动词表了")
print("  ✓ 「给我/帮我」不在创建动词表里")

if "(?:给我|帮我|替我)?" not in body:
    fails.append("VIDEO_MAKE_RE 里没有「给我/帮我」的可选前缀，正常请求会漏")
print("  ✓ 「给我/帮我」作为可选前缀保留（不漏正常请求）")

# 一键关必须真的能退回旧行为
off_fires = [t for t, _ in MUST_NOT_FIRE if decide(t, strict=False)[0]]
if not off_fires:
    fails.append("关掉 STRICT_MAKE 之后行为没变化 —— 说明新判据根本没生效")
print(f"  ✓ DSH_VID_STRICT_MAKE=0 能退回旧行为（旧行为会误出片 {len(off_fires)} 条）")

# 兜底路径必须还带着 leaked 豁免（模型自己调了工具就不受这些否决约束）
if "if STRICT_MAKE and not leaked:" not in SRC:
    fails.append("兜底里的 leaked 豁免没了：模型自己发起的调用会被误拦")
print("  ✓ 模型自己发起的调用（leaked）不受这三层否决约束")

print()
if fails:
    for f in fails:
        print("  ✗", f)
    sys.exit(f"VIDEO_TEST_FAIL {len(fails)} 项")
print(f"VIDEO_TEST_OK 该出片 {len(MUST_FIRE)} 条 / 不该出片 {len(MUST_NOT_FIRE)} 条")
