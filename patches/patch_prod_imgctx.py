"""把 dsh-imgctx 的「说图时放宽回看 + 取不到图别否认能力」补丁打到线上版本上。

为什么不直接覆盖整份 main.py：镜像里的 main.py 还带着一份**没上线的 SSRF 加固
重构**（_is_public_ip / _PublicResolver / MAX_REDIRECTS / _INFLIGHT_OWNERS）。整
文件覆盖等于顺手把那份重构推上线，那不是这次要改的东西。所以这里只把本次的
几处改动按字面锚点打上去，线上其余代码原样不动。

用法：python3 patch_prod_imgctx.py <线上版.py> <输出.py>
"""

import sys

src, dst = sys.argv[1], sys.argv[2]
s = open(src, encoding="utf-8").read()
n = 0


def sub(old, new):
    global s, n
    assert s.count(old) == 1, "锚点命中 %d 次，拒绝打补丁：%r" % (s.count(old), old[:70])
    s = s.replace(old, new)
    n += 1


sub(
    'ASK_IMAGE_RE = re.compile(r"图|截图|表情|照片|画的|p的", re.IGNORECASE)\n'
    "# 第二种情形：图刚发出来没多久，大概率就是眼下在聊的那张。",
    'ASK_IMAGE_RE = re.compile(r"图|截图|表情|照片|画的|p的", re.IGNORECASE)\n'
    "# 「这一轮在说图」时往回多看几条（LOOKBACK 之外的加宽，只在说图时生效）。\n"
    "# 2026-09-15 实测翻车：群友 11:57:32 发图，12:00:24 才 @ 它问「图片里面的内容是\n"
    "# 什么意思」，中间隔了 6 条 —— LOOKBACK=4 够不着那张图，于是它没图可看，还被\n"
    "# clarify 判成「图片内容未知」，最后反问「图片里是什么意思？」并说「真看不见\n"
    "# 图没递到我这边」，群友当场不满（「为什么不吐槽一下图片里的内容」「你看不见\n"
    "# 吗」）。图片 URL 的 rkey 只活约 18 分钟，MAX_AGE=300s 已经在兜底，所以这里\n"
    "# 放宽到 ASK_LOOKBACK 条是安全的。\n"
    'ASK_LOOKBACK = int(os.environ.get("DSH_IMGCTX_ASK_LOOKBACK", "12"))\n'
    "# 第二种情形：图刚发出来没多久，大概率就是眼下在聊的那张。",
)

sub(
    "    include_current: bool = False,\n) -> list[dict]:",
    "    include_current: bool = False,\n"
    "    lookback: int | None = None,\n"
    ") -> list[dict]:",
)

sub(
    "    include_current=True 时连当前这条消息一起看 —— 只在框架转述失败\n"
    "    （动图）的场合才这样，正常情况下当前消息是框架的地盘，别抢。\n"
    '    """\n'
    "    now = time.time()",
    "    include_current=True 时连当前这条消息一起看 —— 只在框架转述失败\n"
    "    （动图）的场合才这样，正常情况下当前消息是框架的地盘，别抢。\n\n"
    "    lookback 覆盖默认的 LOOKBACK：这一轮在说图时由调用方放大，好让「发完图隔\n"
    "    几条再问」也能找到那张图。不传就用 LOOKBACK。\n"
    '    """\n'
    "    span = LOOKBACK if lookback is None else lookback\n"
    "    now = time.time()",
)

sub(
    "    # 从最新往旧走，只看 LOOKBACK 条\n"
    "    for msg in reversed(messages[-LOOKBACK:] if LOOKBACK > 0 else messages):",
    "    # 从最新往旧走，只看 span 条\n"
    "    for msg in reversed(messages[-span:] if span > 0 else messages):",
)

sub(
    '        _kw = {"self_id": int(_sid)} if _sid.isdigit() else {}\n'
    "        history = await bot.get_group_msg_history(\n"
    "            group_id=int(group_id), count=LOOKBACK + 2, **_kw\n"
    "        )",
    '        _kw = {"self_id": int(_sid)} if _sid.isdigit() else {}\n'
    "        # 这一轮在说图就多往回捞几条：有人发完图隔了几条才 @ 它问「图里是啥」，\n"
    "        # 只捞 LOOKBACK 条根本够不着那张图（见 ASK_LOOKBACK 的注释）。\n"
    "        span = max(LOOKBACK, ASK_LOOKBACK) if _turn_asks_about_image(req) else LOOKBACK\n"
    "        history = await bot.get_group_msg_history(\n"
    "            group_id=int(group_id), count=span + 2, **_kw\n"
    "        )",
)

sub(
    "        items = _pick_images(messages, self_id, cur_id, include_current=rescue)\n"
    "        if not items:\n"
    "            return\n"
    "        await self._emit(req, provider_id, items, rescue=rescue)",
    "        items = _pick_images(\n"
    "            messages, self_id, cur_id, include_current=rescue, lookback=span\n"
    "        )\n"
    "        if not items:\n"
    "            # 这一轮在问图，但回看到底还是没捞到（图太老、rkey 过期、被撤了）。\n"
    "            # 什么都不说的话，模型会开始解释自己的内部机制 —— 2026-09-15 它就是这么\n"
    "            # 说出「图我这轮没拿到内容」「真看不见 图没递到我这边」的，群友当场回\n"
    "            # 「你看不见吗」「为什么不吐槽一下图片里的内容」。这两种说法都是硬伤：\n"
    "            # 既是内部状态泄露，又把自己说成没有看图能力（其实能力是有的，只是这\n"
    "            # 张拿不到）。所以这里明确告诉它：照常接话，别提机制、别否认能力。\n"
    "            if _turn_asks_about_image(req):\n"
    "                try:\n"
    "                    req.extra_user_content_parts.append(\n"
    "                        TextPart(\n"
    "                            text=(\n"
    "                                \"（这轮没能取到群里那张图的内容：图太旧、链接已过期或\"\n"
    "                                \"已被撤回。就按现有信息正常接话，或者直说没跟上、让对方\"\n"
    "                                \"再发一次；**不要说自己看不见图／没有看图能力，也不要\"\n"
    "                                \"解释图片是怎么到你这儿的**。）\"\n"
    "                            )\n"
    "                        )\n"
    "                    )\n"
    '                    logger.info("[imgctx] 在说图但这轮取不到图，已注入「别否认看图能力」提示")\n'
    "                except Exception as exc:  # noqa: BLE001\n"
    '                    logger.warning("[imgctx] 兜底提示注入失败: %s", exc)\n'
    "            return\n"
    "        await self._emit(req, provider_id, items, rescue=rescue)",
)

sub(
    '            f"回看最近 {LOOKBACK} 条消息，每轮最多转述 {MAX_IMAGES} 张\\n"',
    '            f"回看最近 {LOOKBACK} 条消息（说图时放宽到 {ASK_LOOKBACK} 条），"\n'
    '            f"每轮最多转述 {MAX_IMAGES} 张\\n"',
)

open(dst, "w", encoding="utf-8").write(s)
print("已打 %d 处补丁 -> %s" % (n, dst))
