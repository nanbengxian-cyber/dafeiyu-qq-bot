# -*- coding: utf-8 -*-
"""读写 imagegen.env —— 保留注释、顺序和格式。

为什么不用现成的 dotenv 库：这个文件不是纯数据，它有 55 行注释，
里面记着每个变量为什么设成这个值、踩过什么坑。用 dotenv 读成 dict 再写回去
会把注释全部抹掉 —— 那些注释比变量本身值钱。

所以这里做的是**行级编辑**：只替换目标变量所在的那一行，其他字节原样保留。
改一个变量，diff 就只有一行。

格式约定（实测 /opt/qqbot/imagegen.env 134 行全部符合）：
  · `KEY=VALUE`，等号两边无空格
  · 值不带引号（全文 0 处引号）
  · `#` 开头是注释，空行是分节
  · 无重复变量

Docker 的 env_file 解析比这更宽松（允许 `export`、允许引号），
但这个文件是我们自己维护的，收紧格式换来的是可预测的写入行为。
遇到不符合约定的行，读的时候跳过、写的时候报错，而不是猜。
"""

import hashlib
import os
import re

# 变量名：大写字母、数字、下划线。这是我们自己的命名规范（全部 DSH_ 前缀），
# 收紧到这个范围能让「这一行是不是赋值」的判断没有歧义。
LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


class EnvError(Exception):
    """env 文件本身有问题（不存在、格式不认、变量不存在）。"""


def read(path):
    """读成 {变量名: 字符串值}。注释、空行、认不出的行一律跳过。

    值不做任何类型转换 —— 类型是 spec 的事，这一层只管字节。
    """
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                m = LINE_RE.match(line.rstrip("\n"))
                if m:
                    out[m.group(1)] = m.group(2)
    except FileNotFoundError:
        raise EnvError("找不到 env 文件：%s" % path) from None
    except OSError as exc:
        raise EnvError("读不了 env 文件：%s" % exc) from exc
    return out


def fingerprint(path):
    """整个文件的 md5。写之前核对一次，防止覆盖别人（人工 vi、别的脚本）的改动。

    读不到就回 None —— 调用方要把 None 当成「核对失败」而不是「没变化」。
    """
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return None


def _format(value):
    """把 Python 值写成 env 行里的字符串。

    bool 写成 1/0 而不是 True/False：插件代码里的判断是
    `os.environ.get("X", "1") in ("1", "true", "yes")`，写 True 会被当成假。
    这个坑很隐蔽 —— 变量看着设了，功能却是关的。
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        # 0.25 要写 0.25，不要 0.25000000000000006；整数型不带 .0
        if value == int(value):
            return str(int(value))
        return repr(round(value, 6)).rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value)


def validate_value(text):
    """env 值里不许有换行 —— 一行一个变量是这个格式的全部前提。

    也挡住 `\\n` 注入：如果放过去，一个值就能伪造出任意多行赋值，
    等于绕过白名单改任何变量。
    """
    if "\n" in text or "\r" in text:
        raise EnvError("值里不能有换行")
    if "\x00" in text:
        raise EnvError("值里不能有空字符")
    return text


def write(path, updates, expect_fingerprint=None, allow_add=False,
          section="# ---- 控制台托管（下面这些是手机上加的，可以照常手改）----"):
    """行级替换。返回真正改动了的 {变量: (旧值, 新值)}。

    默认要求每个变量**必须已经在文件里存在**：不存在就报错。理由是这个文件的分节注释
    （`# ---- dsh-voice ----`）标着变量归属，盲目追加到末尾会让文件慢慢烂掉。

    allow_add=True 时允许追加，但只追加到文件末尾一个**固定的托管小节**里，
    这一节由控制台维护、位置可预测。这是为「248 个变量里有 120 个线上没设、
    只走代码默认值」准备的 —— 那些恰恰是最需要能在手机上调的（现在改它们只能改代码）。
    新增值写成 (var, None) 的形式回报，调用方据此区分「改了」和「第一次设」。

    expect_fingerprint 不为 None 时先核对，不一致抛 EnvError（调用方转 409）。
    """
    if not updates:
        return {}

    current = fingerprint(path)
    if expect_fingerprint is not None and current != expect_fingerprint:
        raise EnvError("env 文件在这期间被别处改了，本次没写")

    formatted = {}
    for key, value in updates.items():
        if not LINE_RE.match(key + "="):
            raise EnvError("变量名不合法：%r" % key)
        formatted[key] = validate_value(_format(value))

    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise EnvError("读不了 env 文件：%s" % exc) from exc

    seen = set()
    changed = {}
    for i, line in enumerate(lines):
        m = LINE_RE.match(line.rstrip("\n"))
        if not m:
            continue
        key = m.group(1)
        if key not in formatted:
            continue
        seen.add(key)
        old = m.group(2)
        new = formatted[key]
        if old == new:
            continue
        # 保留原来的行尾（最后一行可能没有 \n）
        tail = "\n" if line.endswith("\n") else ""
        lines[i] = "%s=%s%s" % (key, new, tail)
        changed[key] = (old, new)

    missing = sorted(set(formatted) - seen)
    if missing and not allow_add:
        raise EnvError("env 文件里没有这些变量，拒绝新增：%s" % ", ".join(missing))

    if missing:
        # 追加到托管小节。小节不存在就先建 —— 只建一次，之后都往里加。
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        idx = _section_index(lines, section)
        if idx is None:
            lines.append("\n")
            lines.append(section + "\n")
            idx = len(lines)
        for key in missing:
            lines.insert(idx, "%s=%s\n" % (key, formatted[key]))
            idx += 1
            changed[key] = (None, formatted[key])

    if not changed:
        return {}

    # 原子替换：先写临时文件再 rename。直接就地改写的话，
    # 万一写一半进程被杀，env 文件就烂了 —— 而 astrbot 下次重启会读它。
    tmp = path + ".console.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        # 保持原文件的权限（imagegen.env 是 600，里面有 API key）
        try:
            st = os.stat(path)
            os.chmod(tmp, st.st_mode & 0o7777)
        except OSError:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise EnvError("写不了 env 文件：%s" % exc) from exc

    return changed


def _section_index(lines, section):
    """托管小节的插入位置（小节标题的下一行）。没有这个小节回 None。"""
    want = section.strip()
    for i, line in enumerate(lines):
        if line.strip() == want:
            return i + 1
    return None
