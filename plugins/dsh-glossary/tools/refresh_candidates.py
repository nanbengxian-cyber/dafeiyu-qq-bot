#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-glossary 候选词体检 —— 只出报告，绝不自动改词表。

用法（在服务器容器里跑，因为语料库在容器里）：
    docker exec astrbot python3 \
      /AstrBot/data/plugins/dsh-glossary/tools/refresh_candidates.py
    # 不联网、只做语料侧挖掘：
    docker exec astrbot python3 .../refresh_candidates.py --no-net

---------------------------------------------------------------------------
为什么这个脚本的价值在「交叉核对」，不在「抓取」

参考实现（GitHub anime-meme-collector 的 collect_memes.py）是把 B站热门/热搜
抓下来直接塞进词库。实测那份成品词库 292 个词里，只有 14 个在本群语料里
出现过，而命中最多的那个是「15」—— 对应「被蹲了15次」「总1500刀」
和一个 QQ 号。也就是说**光抓是负资产**：假命中等于主动给模型喂错语境。

所以这里抓完必须过两道核对，输出的是一张短的人工待审清单：
  ① 这词本群真人到底说过没有（说过几次、几个人说过）
  ② 词表是不是已经覆盖了

另外补一条反向来源：直接从本群语料里挖高频未覆盖说法。这条比 B站
有用得多 —— 群里真在说的话，才是词表该解释的东西。

两处刻意不照抄参考实现：
  * 参考实现用 ssl.check_hostname=False + CERT_NONE 关掉了证书校验。
    那是拿中间人风险换「在奇怪环境里能跑」，不抄；这里用默认校验，
    连不上就当这一路没数据（失败只记录，不中断）。
  * 参考实现把裸关键词当词条。这里抓来的一律叫「候选」，没有释义
    就不可能进词表 —— 释义得人写，因为释义错了比没有更糟。
"""

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
import types
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DEFAULT_GROUPS = ()

# 噪声阻尼用的常见字。注意这只是**减少人工翻页量**，不是判断真假的依据 ——
# 判断永远是人看语料上下文。规则是「整个候选都由这些字组成才丢」，
# 所以「神了」「带带我」这种只含一个常见字的候选照样会留下来。
_STOP = (
    "的了是我你他她它们这那不在有和就都也很还要会能没没有什么怎么为什么因为所以"
    "但是可以一个自己现在真的知道感觉好像应该已经如果只是这个那个不是我们你们他们"
    "一下时候东西问题谢谢哈哈对啊嗯嗯行吧算了确实其实然后而且或者比如就是这样那样"
    "啥得看事干"
)
_STOP_SET = set(_STOP)
_CJK = re.compile(r"[\u4e00-\u9fff]")
_NOISE = re.compile(r"https?://\S+|\[CQ:[^\]]*\]|[@＠]\S+|\d{5,}")


def _load_glossary(plugin_dir: Path):
    """导入同目录上一层的 main.py，拿到 GLOSSARY / matched_entries。"""
    for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.core",
                 "astrbot.core.agent", "astrbot.core.agent.message"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
    sys.modules["astrbot.api.event"].AstrMessageEvent = object
    sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
        on_llm_request=lambda: (lambda f: f), command=lambda *a, **k: (lambda f: f))
    sys.modules["astrbot.core"].logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None)
    sys.modules["astrbot.core.agent.message"].TextPart = object
    spec = importlib.util.spec_from_file_location("glossary", plugin_dir / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get_json(url: str, timeout: int = 15):
    """默认证书校验。连不上就返回 None，调用方当这一路没数据。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://www.bilibili.com"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_bilibili() -> tuple[list[str], list[str]]:
    """B站热门排行标题里的引号/书名号片段 + 热搜关键词。失败返回空。"""
    titles, errs = [], []
    try:
        data = _get_json("https://api.bilibili.com/x/web-interface/ranking/v2"
                         "?rid=0&type=all")
        for item in (data.get("data") or {}).get("list", [])[:50]:
            t = item.get("title") or ""
            titles += re.findall(r"[「『“\"']([^」』”\"']{2,12})[」』”\"']", t)
            titles += re.findall(r"《([^》]{2,12})》", t)
            titles += re.findall(r"【([^】]{2,12})】", t)
    except Exception as exc:                                  # noqa: BLE001
        errs.append("ranking: %r" % (exc,))
    words = []
    try:
        data = _get_json("https://s.search.bilibili.com/main/hotword")
        words = [x.get("keyword", "").strip() for x in data.get("list", [])[:50]]
        words = [w for w in words if w]
    except Exception as exc:                                  # noqa: BLE001
        errs.append("hotword: %r" % (exc,))
    for e in errs:
        print("  ! B站取数失败（当这一路没数据）：%s" % e)
    return titles, words


def load_corpus(db: str, groups: tuple[str, ...]):
    """返回 [(speaker, text)]，buffer + archive 两张表。"""
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    marks = ",".join("?" * len(groups))
    rows = []
    for table in ("buffer", "archive"):
        try:
            cur = con.execute(
                "select user_id, text from %s where group_id in (%s)" % (table, marks),
                groups)
        except sqlite3.Error as exc:
            print("  ! 读 %s 失败：%r" % (table, exc))
            continue
        rows += [(str(u or "?"), t) for u, t in cur if t and t.strip()]
    con.close()
    return rows


def corpus_index(rows):
    """text -> 出现次数，以及 text -> 说过的人。噪声（链接/CQ码/长数字）先剔掉。"""
    texts, speakers = [], defaultdict(set)
    for who, raw in rows:
        clean = _NOISE.sub(" ", raw).strip()
        if clean:
            texts.append(clean)
            speakers[clean].add(who)
    return texts, speakers


def count_in_corpus(term: str, texts, speakers):
    n = 0
    who = set()
    for t in texts:
        if term in t:
            n += 1
            who |= speakers[t]
    return n, len(who)


def mine_catchphrases(rows, gl, max_len=6, min_speakers=2):
    """语料侧：**整条消息就是这么一句短话**、且至少两个人发过的说法。

    为什么用这个信号而不是 n-gram 词频：先试过 2~4 字 n-gram 按出现条数排序，
    排在最前面的是「，我」「了，」「吗？」「觉得」「视频」—— 高频但全是
    普通词和标点，30 行输出里一条可用的都没有，等于把筛选成本全推给人。

    「整条消息就是它」是个便宜得多的结构信号：口头禅、梗、招呼语才会被
    单独发出来当一整条消息（实测捞出「神了」「人机」「乐子」「何意味」
    「上号吧」「带带我」），而「觉得」「直接」「视频」这类普通词永远不会。

    「已覆盖」用 matched_entries 判，不是比字符串相等 —— 否则「笑死我了」
    「绷不住了」这种整句形式会被当成新词重复报出来。REJECTED 里回测淘汰过的
    也一并滤掉，不然每次跑都要重看一遍同样的结论。

    仍然只是候选：机器人指令（「我的档案」）和人名（「乐乐」）也会混进来，
    由人一眼划掉。
    """
    cnt = Counter()
    holders = defaultdict(set)
    for who, raw in rows:
        t = _NOISE.sub(" ", raw).strip()
        if not t or len(t) > max_len:
            continue
        cnt[t] += 1
        holders[t].add(who)
    out = []
    for t, c in cnt.items():
        if len(holders[t]) < min_speakers:
            continue
        cjk = _CJK.findall(t)
        if len(cjk) < 2:                       # 单字和纯符号/纯英文一律不算
            continue
        if len(cjk) != len(t):                 # 夹标点、数字、英文的先不看
            continue
        if all(ch in _STOP_SET for ch in t):   # 整句都是常见字＝普通应答
            continue
        if gl.matched_entries(t):              # 词表已经能命中，不算新词
            continue
        if any(bad in t for bad in gl.REJECTED):  # 回测淘汰过的，别每次都再报一遍
            continue
        out.append((len(holders[t]), c, t))
    out.sort(reverse=True)
    return out


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="dsh-glossary 候选词体检（只出报告）")
    ap.add_argument("--db", default=os.environ.get("DSH_MEM_DB",
                                                   "/AstrBot/data/dsh_memory.db"))
    ap.add_argument("--groups", default=",".join(DEFAULT_GROUPS))
    ap.add_argument("--no-net", action="store_true", help="不抓 B站，只做语料侧挖掘")
    ap.add_argument("--top", type=int, default=30, help="语料侧候选最多列几条")
    args = ap.parse_args()

    gl = _load_glossary(here.parent)
    groups = tuple(g.strip() for g in args.groups.split(",") if g.strip())

    print("词表现状：%d 条词条（带例句 %d 条）"
          % (len(gl.GLOSSARY), sum(1 for e in gl.GLOSSARY if e.usage)))
    rows = load_corpus(args.db, groups)
    texts, speakers = corpus_index(rows)
    print("语料：%d 条（群 %s）" % (len(texts), "、".join(groups)))
    if not texts:
        print("语料为空，没法核对，直接退出。")
        return 1

    if not args.no_net:
        print("\n────── ① B站候选 × 本群语料 ──────")
        titles, words = fetch_bilibili()
        cands = sorted({w for w in titles + words if 1 < len(w) <= 12})
        print("抓到候选 %d 个（排行标题片段 %d + 热搜 %d）"
              % (len(cands), len(titles), len(words)))
        hits = []
        for w in cands:
            n, k = count_in_corpus(w, texts, speakers)
            if n:
                hits.append((n, k, w))
        hits.sort(reverse=True)
        # 「已覆盖」跟第②段用同一套判断（matched_entries + REJECTED），
        # 别在这里退回字符串相等 —— 否则「破防了」会被当成词表没有的新词。
        fresh = [h for h in hits
                 if not gl.matched_entries(h[2])
                 and not any(bad in h[2] for bad in gl.REJECTED)]
        print("其中本群真说过的：%d 个；词表还没覆盖的：%d 个" % (len(hits), len(fresh)))
        for n, k, w in fresh[:args.top]:
            print("   待审  %-14s %d 条消息 / %d 人" % (w, n, k))
        if not fresh:
            print("   （没有新的。B站热词和本群话题重合度一向很低，这是正常结果）")

    print("\n────── ② 语料侧：被当成一整条消息发出来的短说法 ──────")
    print("门槛：整条消息 <=6 字、纯中文、>=2 个人发过、词表还没覆盖。")
    print("这类才是口头禅和梗；普通词不会被单独发出来。释义仍要人写。")
    found = mine_catchphrases(rows, gl)
    for k, n, t in found[:args.top]:
        print("   待审  %-14s %d 人发过 / 共 %d 次" % (t, k, n))
    if not found:
        print("   （没有新的）")

    print("\n────── 怎么入表 ──────")
    print("1. 逐条把候选词在语料里的**每一次**出现看一遍，确认是梗义不是字面义")
    print("   （上一轮就是这样淘汰掉「钓鱼」＝真钓鱼、「AK」＝群友名字的）")
    print("2. 手写释义 + avoid 排除上下文，例句从真语料里挑原话")
    print("3. 在 test_glossary.py 里同时加一条真命中和一条防假命中断言")
    print("4. 跑 test_glossary.py，再全语料回测确认命中率变化和零新增假命中")
    print("本脚本不写任何文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
