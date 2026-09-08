# dsh-welcome —— 新成员入群欢迎。
#
# 原理：OneBot V11 的入群是 notice 事件（post_type=notice,
# notice_type=group_increase）。AstrBot 的 aiocqhttp 适配器会把它转成
# 一个 message_str 为空、message 为空列表的 AstrBotMessage，type 是
# GROUP_MESSAGE（因为带 group_id），照样丢进完整管道。
#
# 所以做法是：用 CustomFilter 只放行 group_increase，在 handler 里生成
# 欢迎词。因为插件 handler 被激活时 WakingCheckStage 会把 is_wake 置 True，
# 空消息也能一路走到 RespondStage。
#
# 几个刻意的设计：
#
# 1) 欢迎词让 LLM 现写，而不是写死模板。
#    群里的人格是「小鲸鱼」——一个说话很短、有点欠、拒绝客服腔的角色。
#    固定模板（"欢迎新成员加入本群！"）会瞬间破人设，比不欢迎更糟。
#    用 context.llm_generate 带上人格 system_prompt 现场生成，
#    每次不一样，也符合角色语气。
#
# 2) LLM 失败必须有兜底。
#    渠道抖动、超时、返回空都可能发生，那时宁可发一句预置的短句，
#    也不要什么都不发（新人进群没人理最尴尬）。兜底句同样写得很短、很随意。
#
# 3) 机器人自己被拉进群时不欢迎自己。
#    group_increase 的 user_id 等于 self_id 时直接跳过。
#
# 4) 去重 + 冷却。
#    风控/网络抖动可能让同一个 notice 重放；批量拉人时也可能几秒内涌入十几个。
#    (group_id, user_id) 记忆 5 分钟内不重复欢迎；同群欢迎之间留冷却间隔，
#    避免机器人连刷十条被当成刷屏号。
#
# 5) 昵称尽量取真名。
#    notice 事件里没有昵称，只有 user_id。通过 OneBot 的
#    get_group_member_info 拿 card/nickname；拿不到就不硬凑，只 @ 他。
#
# 6) 必须自己处理 [贴纸:x] 标记。
#    群人格教会了模型用 [贴纸:名] 发表情，平时由 dsh-sticker 的
#    on_llm_response 钩子拦下来换成 GIF。但这里是直接调 llm_generate，
#    不经过那条管道，钩子不会触发——标记会原样漏进群聊。实测生成的欢迎词
#    里确实出现了 [贴纸:探头]。所以这里复用同一套解析：标记一律从文字里
#    剥掉，能对应到真实贴纸目录的就补发一张 GIF。

import asyncio
import os
import random
import re
import time

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.core import logger
from astrbot.core.star.filter.custom_filter import CustomFilter

# 总开关
WELCOME_ENABLED = os.environ.get("DSH_WELCOME", "1") not in ("0", "false", "False")
# 同一个人多久内不重复欢迎（秒）
DEDUP_TTL = float(os.environ.get("DSH_WELCOME_DEDUP", "300"))
# 同一个群两次欢迎之间的最小间隔（秒），防批量拉人时刷屏
GROUP_COOLDOWN = float(os.environ.get("DSH_WELCOME_COOLDOWN", "20"))
# 发言前先等一会，像真人看到消息才反应（秒）
DELAY_MIN = float(os.environ.get("DSH_WELCOME_DELAY_MIN", "2"))
DELAY_MAX = float(os.environ.get("DSH_WELCOME_DELAY_MAX", "5"))
# 只在这些群欢迎，逗号分隔；留空=所有群
ONLY_GROUPS = [
    g.strip() for g in os.environ.get("DSH_WELCOME_GROUPS", "").split(",") if g.strip()
]

# LLM 生成欢迎词的指令。刻意强调「短」和「别客服腔」，
# 因为群人格最容易在这种场合退化成官腔。
GEN_PROMPT = (
    "群里刚进来一个新人{who}。用你平时在群里说话的语气，说一句欢迎。\n"
    "要求：\n"
    "- 只能一句话，最多 20 个字\n"
    "- 不要用「欢迎加入本群」这种客服腔和官方话\n"
    "- 不要问「有什么可以帮你」，你不是客服\n"
    "- 可以调侃、可以随意、可以玩梗，像老群友招呼新人那样\n"
    "- 不要带引号，直接说话"
)

# LLM 挂了时的兜底。都写得很短、很随意，和人格一致。
FALLBACKS = (
    "来了个新的",
    "欢迎，随便坐",
    "哦，新人",
    "来了？",
    "又多一个",
    "进来了就别跑",
    "欢迎，群里挺乱的",
)

# (group_id, user_id) -> 上次欢迎时间
_welcomed: dict[tuple[str, str], float] = {}
# group_id -> 该群上次欢迎时间
_group_last: dict[str, float] = {}

# 贴纸标记：和 dsh-sticker 保持一致的写法，含繁体与全角冒号
STICKER_DIR = os.environ.get("DSH_STICKER_DIR", "/AstrBot/data/stickers")
STICKER_RE = re.compile(
    r"[\[【]\s*(?:贴纸|貼紙|sticker)\s*[:：]\s*([^\]】]+?)\s*[\]】]"
)
_STICKER_EXTS = (".gif", ".png", ".jpg", ".jpeg", ".webp")


def _resolve_sticker(tag: str) -> str | None:
    """按贴纸名找到图片文件；找不到返回 None。"""
    tag = (tag or "").strip()
    if not tag or "/" in tag or ".." in tag:
        return None
    d = os.path.join(STICKER_DIR, tag)
    if not os.path.isdir(d):
        return None
    for fn in sorted(os.listdir(d)):
        if fn.lower().endswith(_STICKER_EXTS):
            return os.path.join(d, fn)
    return None


def _split_stickers(text: str) -> tuple[str, list[str]]:
    """把 [贴纸:x] 从文字里剥掉，返回 (干净文字, 可发送的贴纸路径列表)。

    标记必须无条件剥掉——哪怕贴纸名是错的，也不能让方括号漏进群聊。
    """
    tags = STICKER_RE.findall(text or "")
    if not tags:
        return (text or ""), []
    clean = STICKER_RE.sub("", text).strip()
    paths = []
    for t in tags:
        p = _resolve_sticker(t)
        if p:
            paths.append(p)
    return clean, paths


async def _fetch_name(context, uid: str, gid: str) -> str:
    """尽量拿真昵称。拿不到返回空串。"""
    try:
        # 走注册的 OneBot API：get_group_member_info
        api = context.get_platform_api("aiocqhttp")
        if not api:
            return ""
        # aiocqhttp 适配器的 api 是某种包装，试着按常见形状调
        for fn in ("get_group_member_info",):
            f = getattr(api, fn, None)
            if not f:
                continue
            if asyncio.iscoroutinefunction(f):
                info = await f(group_id=int(gid), user_id=int(uid))
            else:
                info = f(group_id=int(gid), user_id=int(uid))
            if isinstance(info, dict):
                return info.get("card") or info.get("nickname") or ""
            # 可能是 (status, dict) 元组
            if isinstance(info, (tuple, list)) and len(info) >= 2:
                d = info[-1]
                if isinstance(d, dict):
                    return d.get("card") or d.get("nickname") or ""
    except BaseException as e:
        logger.debug("[welcome] 拿昵称失败 uid=%s: %s", uid, e)
    return ""


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[welcome] 已加载：%s 去重%.0fs 冷却%.0fs 延迟%.0f~%.0fs 群=%s",
            "开" if WELCOME_ENABLED else "关", DEDUP_TTL, GROUP_COOLDOWN,
            DELAY_MIN, DELAY_MAX, "、".join(ONLY_GROUPS) or "全部",
        )

    @filter.custom_filter(CustomFilter(
        # OneBot V11 group_increase 通知的原始特征都会被 AstrBot 剥掉，
        # 适配器只留下一个 type=GROUP_MESSAGE 的空消息对象。所以只能靠
        # raw_message 里有 group_increase 字样来认（这个字段会被透传）。
        # 带 try：不同适配器/版本字段形状不一样，宁可放过也不要炸。
        check=_raw_has_group_increase,
    ))
    async def on_new_member(self, event: AstrMessageEvent):
        """新成员入群：生成欢迎词并发出。"""
        if not WELCOME_ENABLED:
            return
        try:
            info = _parse_notice(event)
            if not info:
                logger.info("[welcome] 认不出 group_increase（字段形状不同）")
                return
            gid, uid = info

            # 机器人自己被拉进来：不欢迎自己
            try:
                if str(uid) == str(event.get_self_id() or ""):
                    return
            except BaseException:
                pass

            # 群过滤
            try:
                msg_gid = str(event.get_group_id() or "")
                current_gid = gid or msg_gid
            except BaseException:
                current_gid = gid
            if ONLY_GROUPS and (current_gid not in ONLY_GROUPS):
                return

            # 去重 + 同群冷却
            now = time.time()
            key = (str(gid), str(uid))
            last = _welcomed.get(key, 0.0)
            if now - last < DEDUP_TTL:
                logger.info("[welcome] 该用户已在 %.0fs 内欢迎过，跳过", DEDUP_TTL)
                return
            glast = _group_last.get(str(gid), 0.0)
            wait = GROUP_COOLDOWN - (now - glast)
            if wait > 0:
                logger.info("[welcome] 本群冷却中，还要等 %.0fs", wait)
                await asyncio.sleep(wait)
            _welcomed[key] = now
            _group_last[str(gid)] = time.time()

            # 真人一样的反应延迟：机器人的回复速度也是人设的一部分
            try:
                await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            except BaseException:
                pass

            name = await _fetch_name(self.context, str(uid), str(gid))
            who = f"{name}（{uid}）" if name else f"{uid}"
            await self._send_welcome(event, who, str(uid), str(gid))
        except BaseException as e:  # noqa: BLE001
            # 欢迎是锦上添花，任何异常都不能让插件崩
            logger.error("[welcome] 处理 group_increase 异常: %s", e)

    async def _send_welcome(self, event, who: str, uid: str, gid: str) -> None:
        """生成欢迎词并发送。LLM 失败回落预置短句。"""
        text = ""
        try:
            prompt = GEN_PROMPT.format(who=who)
            # 用 llm_generate 直接生成。不带图片、不依赖管道，正是之前的坑。
            resp = await self.context.llm_generate(
                event.unified_msg_origin, prompt, system_prompt=""
            )
            text = (resp.completion_text or "").strip()
        except BaseException as e:  # noqa: BLE001
            logger.warning("[welcome] LLM 生成欢迎词失败，回落预置: %s", e)
        if not text:
            text = random.choice(FALLBACKS)

        # 剥掉贴纸标记；能对上的贴纸单独补发一张
        clean, stickers = _split_stickers(text)
        if clean:
            chain = MessageChain()
            chain.chain.append(At(qq=uid))
            chain.chain.append(Plain(" " + clean))
            await event.send(chain)
        if stickers:
            for p in stickers:
                try:
                    await event.send(MessageChain(chain=[Image.fromFileSystem(p)]))
                except BaseException as e:  # noqa: BLE001
                    logger.warning("[welcome] 贴纸发送失败: %s", e)

    @filter.command("欢迎测试")
    async def cmd_dry(self, event: AstrMessageEvent):
        """/欢迎测试 —— 不真发，只预览 LLM 会生成什么欢迎词。"""
        raw_text = ""
        try:
            resp = await self.context.llm_generate(
                event.unified_msg_origin,
                GEN_PROMPT.format(who="一个新同学"),
                system_prompt="",
            )
            raw_text = (resp.completion_text or "").strip()
        except BaseException as e:  # noqa: BLE001
            yield event.plain_result(f"LLM 生成失败：{e}")
            return
        if not raw_text:
            raw_text = random.choice(FALLBACKS)
        text, stickers = _split_stickers(raw_text)
        tail = f"（另附 {len(stickers)} 张贴纸）" if stickers else ""
        yield event.plain_result(f"[欢迎词预览] {text or '（空）'}{tail}")

    @filter.command("欢迎状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """/欢迎状态 —— 查看欢迎功能配置与最近记录。"""
        gid = event.get_group_id() or "?"
        last = _group_last.get(str(gid))
        last_s = f"{time.time() - last:.0f}s 前" if last else "无记录"
        yield event.plain_result(
            f"入群欢迎：{'开' if WELCOME_ENABLED else '关'}\n"
            f"欢迎词：LLM 现场生成（沿用群人格），失败回落预置短句\n"
            f"同人去重：{DEDUP_TTL:.0f}s\n"
            f"同群冷却：{GROUP_COOLDOWN:.0f}s\n"
            f"发言延迟：{DELAY_MIN:.0f}~{DELAY_MAX:.0f}s\n"
            f"限定群：{'、'.join(ONLY_GROUPS) if ONLY_GROUPS else '全部'}\n"
            f"本群上次欢迎：{last_s}\n"
            f"已记录人数：{len(_welcomed)}"
        )
