# -*- coding: utf-8 -*-
"""dsh-homophone —— 谐音匹配的纯逻辑（不依赖 astrbot，可单测）。"""


def matched_names(text, name_map):
    """在 text 里找「被喊」命中：本名 + 谐音变体。

    name_map: {规范名: [变体1, 变体2, ...]}（变体不含本名）。
    命中顺序：先本名，再逐个变体。每个规范名至多记一个命中。
    返回 [(规范名, 命中的写法), ...]；命中的写法=本名或某个变体。
    """
    out = []
    for canon in name_map:
        if canon and canon in text:
            out.append((canon, canon))
            continue
        for v in name_map[canon]:
            if v and v in text:
                out.append((canon, v))
                break
    return out


def matched_puns(text, puns):
    """在 text 里找命中的谐音梗条目。

    puns: [{ "text": ..., "mean": ... }, ...]，按词库顺序。
    返回命中的条目列表（同一词只算首条）。
    """
    seen = set()
    out = []
    for p in puns:
        t = p.get("text")
        if t and t not in seen and t in text:
            out.append(p)
            seen.add(t)
    return out
