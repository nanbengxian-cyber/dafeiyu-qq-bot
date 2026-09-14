# -*- coding: utf-8 -*-
"""动态配置（旋钮）的解析、校验与写回。

仓库里的 deploy/console-config.json 决定控制台显示哪些配置项；控制台不写死控件，
只认类型。遇到不认识的类型**跳过而不是报错**，这样以后加新控件不会让老 EXE 崩。

env 文本按行编辑，保留注释、空行与顺序 —— 配置文件的注释比变量本身值钱。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
MAX_STR_LEN = 200
MANAGED_SECTION = "# ---- 控制台托管（下面这些是控制台加的，可以照常手改）----"
KINDS = ("bool", "int", "float", "str", "enum")
TRUE_WORDS = ("1", "true", "yes", "on", "是", "开")
FALSE_WORDS = ("0", "false", "no", "off", "否", "关")


class KnobError(Exception):
    """配置项取值不合法；消息可直接展示。"""


@dataclass(frozen=True)
class Knob:
    key: str
    label: str
    group: str = "常规"
    kind: str = "str"
    default: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    options: Tuple[str, ...] = ()
    hint: str = ""
    restart: bool = True
    secret: bool = False

    @property
    def widget(self) -> str:
        if self.secret:
            return "password"
        if self.kind == "bool":
            return "check"
        if self.kind == "enum":
            return "combo"
        return "entry"


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_knobs(raw: Any) -> List[Knob]:
    """把仓库下发的 JSON 解析成旋钮列表；非法项跳过，保证向前兼容。"""
    if isinstance(raw, dict):
        items = raw.get("knobs")
    else:
        items = raw
    if not isinstance(items, list):
        return []
    out: List[Knob] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not LINE_RE.match(key + "=") or key in seen:
            continue
        kind = str(item.get("type") or "str").strip().lower()
        if kind not in KINDS:
            continue
        options = item.get("options")
        if kind == "enum":
            if not isinstance(options, list) or not options:
                continue
            options = tuple(str(opt) for opt in options)
        else:
            options = ()
        seen.add(key)
        out.append(
            Knob(
                key=key,
                label=str(item.get("label") or key),
                group=str(item.get("group") or "常规"),
                kind=kind,
                default=str(item.get("default", "")),
                minimum=_as_float(item.get("min")),
                maximum=_as_float(item.get("max")),
                step=_as_float(item.get("step")),
                options=options,
                hint=str(item.get("hint") or ""),
                restart=bool(item.get("restart_required", True)),
                secret=bool(item.get("secret", False)),
            )
        )
    return out


def group_order(knobs: List[Knob]) -> List[str]:
    order: List[str] = []
    for knob in knobs:
        if knob.group not in order:
            order.append(knob.group)
    return order


def env_values(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        match = LINE_RE.match(line)
        if match:
            out[match.group(1)] = match.group(2)
    return out


def display_value(knob: Knob, raw: Optional[str]) -> str:
    """给界面显示的值。

    注意区分两种情况：变量**不在文件里**（raw is None）用默认值；
    变量在文件里但写成空值（raw == ""）必须原样显示成空 —— 插件里
    `os.environ.get("X", "1")` 拿到的是空串而不是默认值，很多开关
    `"" in ("1","true")` 判定为**关**。把空值显示成默认值会误导用户。
    """
    if raw is None:
        return knob.default
    return raw


def is_enabled(knob: Knob, raw: Optional[str]) -> bool:
    value = display_value(knob, raw).strip().lower()
    if value in TRUE_WORDS:
        return True
    if value in FALSE_WORDS:
        return False
    return bool(value)


def coerce_value(knob: Knob, value: Any) -> str:
    """把界面输入校验并格式化成 env 行里的字符串。"""
    if knob.kind == "bool":
        if isinstance(value, bool):
            return "1" if value else "0"
        text = str(value).strip().lower()
        if text in TRUE_WORDS:
            return "1"
        if text in FALSE_WORDS:
            return "0"
        raise KnobError("%s只能选开或关。" % knob.label)

    if knob.kind == "enum":
        text = str(value).strip()
        if text not in knob.options:
            raise KnobError("%s只能从预设里选。" % knob.label)
        return text

    if knob.kind in ("int", "float"):
        text = str(value).strip()
        if text == "":
            raise KnobError("%s不能留空。" % knob.label)
        try:
            number = float(text)
        except ValueError:
            raise KnobError("%s必须是数字。" % knob.label) from None
        if knob.kind == "int" and number != int(number):
            raise KnobError("%s必须是整数。" % knob.label)
        if knob.minimum is not None and number < knob.minimum:
            raise KnobError("%s不能小于 %s。" % (knob.label, _fmt_num(knob.minimum)))
        if knob.maximum is not None and number > knob.maximum:
            raise KnobError("%s不能大于 %s。" % (knob.label, _fmt_num(knob.maximum)))
        return _fmt_num(number) if knob.kind == "float" else str(int(number))

    text = str(value)
    if "\n" in text or "\r" in text or "\x00" in text:
        raise KnobError("%s不能包含换行或空字符。" % knob.label)
    text = text.strip()
    if len(text) > MAX_STR_LEN:
        raise KnobError("%s太长（最多 %d 个字符）。" % (knob.label, MAX_STR_LEN))
    if knob.options and text not in knob.options:
        raise KnobError("%s只能从预设里选。" % knob.label)
    return text


def _fmt_num(number: float) -> str:
    if number == int(number):
        return str(int(number))
    return repr(round(number, 6)).rstrip("0").rstrip(".")


def apply_env_text(text: str, updates: Dict[str, str],
                   section: str = MANAGED_SECTION) -> Tuple[str, Dict[str, Tuple[Optional[str], str]]]:
    """行级替换；不存在的变量追加到固定的托管小节。返回 (新文本, 实际改动)。"""
    for key, value in updates.items():
        if not LINE_RE.match(key + "="):
            raise KnobError("变量名不合法：%s" % key)
        if "\n" in value or "\r" in value or "\x00" in value:
            raise KnobError("值里不能有换行或空字符。")

    lines = (text or "").splitlines(keepends=True)
    seen = set()
    changed: Dict[str, Tuple[Optional[str], str]] = {}
    for index, line in enumerate(lines):
        match = LINE_RE.match(line.rstrip("\n"))
        if not match:
            continue
        key = match.group(1)
        if key not in updates:
            continue
        seen.add(key)
        old = match.group(2)
        new = updates[key]
        if old == new:
            continue
        tail = "\n" if line.endswith("\n") else ""
        lines[index] = "%s=%s%s" % (key, new, tail)
        changed[key] = (old, new)

    missing = sorted(set(updates) - seen)
    if missing:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        index = _section_index(lines, section)
        if index is None:
            lines.append("\n")
            lines.append(section + "\n")
            index = len(lines)
        for key in missing:
            lines.insert(index, "%s=%s\n" % (key, updates[key]))
            index += 1
            changed[key] = (None, updates[key])

    return "".join(lines), changed


def _section_index(lines: List[str], section: str) -> Optional[int]:
    want = section.strip()
    for index, line in enumerate(lines):
        if line.strip() == want:
            return index + 1
    return None


def secret_state(knob: Knob, raw: Optional[str]) -> str:
    """敏感项只报「已设置 / 未设置」，绝不回显真实值。"""
    if not knob.secret:
        return display_value(knob, raw)
    return "已设置" if (raw is not None and raw != "") else "未设置"
