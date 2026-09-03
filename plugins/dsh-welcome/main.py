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
        return (text or "").strip(), []
    cleaned = STICKER_RE.sub("", text or "").strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    paths = []
    for t in tags:
        p = _resolve_sticker(t)
        if p:
            paths.append(p)
        else:
            logger.debug("[welcome] 未知贴纸名，仅剥除标记: %r", t)
    return cleaned, paths


def _raw_get(raw, key, default=None):
    """notice 的 raw_message 可能是 dict 也可能是对象，统一取值。"""
    if raw is None:
        return default
    if hasattr(raw, "get"):
        try:
            return raw.get(key, default)
        except BaseException:
            pass
    return getattr(raw, key, default)


class GroupIncreaseFilter(CustomFilter):
    """只放行 OneBot group_increase（有人进群）通知。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        raw = getattr(event.message_obj, "raw_message", None)
        return (
            _raw_get(raw, "post_type") == "notice"
            and _raw_get(raw, "notice_type") == "group_increase"
        )


def _prune() -> None:
    """清掉过期的去重记录，别让字典无限长。"""
    now = time.time()
    for k in [k for k, ts in _welcomed.items() if now - ts > DEDUP_TTL * 2]:
        _welcomed.pop(k, None)


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[welcome] 已加载：开关=%s 去重=%.0fs 冷却=%.0fs 延迟=%.0f~%.0fs 限定群=%s",
            "开" if WELCOME_ENABLED else "关",
            DEDUP_TTL,
            GROUP_COOLDOWN,
            DELAY_MIN,
            DELAY_MAX,
            ONLY_GROUPS or "全部",
        )

    async def _member_name(self, event: AstrMessageEvent, gid: str, uid: str) -> str:
        """尽量取到新成员的群名片/昵称；取不到返回空串。"""
        bot = getattr(event, "bot", None)
        if bot is None:
            return ""
        try:
            # 同 imgctx：多连接时不传 self_id 会 ApiNotAvailable。
            # 入群欢迎是 notice 事件，同样已离开 websocket 上下文。
            _sid = str(getattr(event.message_obj, "self_id", "") or "")
            _kw = {"self_id": int(_sid)} if _sid.isdigit() else {}
            info = await bot.get_group_member_info(
                group_id=int(gid), user_id=int(uid), no_cache=True, **_kw
            )
            return (info.get("card") or info.get("nickname") or "").strip()
        except BaseException as e:
            logger.debug("[welcome] 取昵称失败 uid=%s: %s", uid, e)
            return ""

    async def _persona_prompt(self) -> str | None:
        """取群人格的 system_prompt。取不到返回 None（LLM 就用裸提示词）。"""
        try:
            target = self.context.get_config()["provider_settings"].get(
                "default_personality"
            )
            if not target:
                return None
            # get_personas 是协程，返回 Persona(SQLModel) 列表
            for p in await self.context.get_db().get_personas() or []:
                if getattr(p, "persona_id", None) == target:
                    return getattr(p, "system_prompt", None)
        except BaseException as e:
            logger.debug("[welcome] 取人格失败，用裸提示词: %s", e)
        return None

    async def _gen_text(self, event: AstrMessageEvent, name: str) -> str:
        """让 LLM 用群人格写一句欢迎；失败则回落到预置短句。"""
        who = f"，名字叫「{name}」" if name else ""
        try:
            # get_current_chat_provider_id 需要 umo（会话来源），
            # 而且它是**协程**，必须 await。
            # 之前漏了 await：拿到的是 coroutine 对象，它是真值，
            # `if not provider_id` 拦不住，一路传进 llm_generate 变成
            # 「Provider <coroutine object ...> not found」，
            # 于是欢迎词从上线起就一直走 FALLBACKS 兜底短句，LLM 一次都没成功。
            # 异常被下面的 except 吞成一行 debug 日志，所以一直没人发现。
            provider_id = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
            if not provider_id:
                raise RuntimeError("没有可用的 chat provider")

            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=GEN_PROMPT.format(who=who),
                system_prompt=await self._persona_prompt(),
            )
            text = (resp.completion_text or "").strip()
            # 模型偶尔会加引号或多行，只取第一行并去掉包裹引号
            text = text.splitlines()[0].strip().strip('"').strip("“”").strip()
            if text:
                return text
            raise ValueError("LLM 返回空")
        except BaseException as e:
            logger.warning("[welcome] LLM 生成失败，用兜底句: %s", e)
            return random.choice(FALLBACKS)

    @filter.custom_filter(GroupIncreaseFilter)
    async def on_member_join(self, event: AstrMessageEvent):
        if not WELCOME_ENABLED:
            return
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            gid = str(_raw_get(raw, "group_id", "") or event.get_group_id() or "")
            uid = str(_raw_get(raw, "user_id", "") or "")
            self_id = str(_raw_get(raw, "self_id", "") or event.get_self_id() or "")

            if not gid or not uid:
                return
            if uid == self_id:
                logger.info("[welcome] 是机器人自己被拉进群 %s，不欢迎自己", gid)
                return
            if ONLY_GROUPS and gid not in ONLY_GROUPS:
                logger.debug("[welcome] 群 %s 不在限定范围，跳过", gid)
                return

            now = time.time()
            _prune()

            key = (gid, uid)
            prev = _welcomed.get(key)
            if prev is not None and now - prev < DEDUP_TTL:
                logger.info("[welcome] %s 在 %.0fs 前已欢迎过，跳过重放", uid, now - prev)
                return

            last = _group_last.get(gid)
            if last is not None and now - last < GROUP_COOLDOWN:
                logger.info(
                    "[welcome] 群 %s 冷却中（%.0fs 前刚欢迎过），跳过以免刷屏",
                    gid,
                    now - last,
                )
                _welcomed[key] = now
                return

            # 先占位，防止同一事件并发重入
            _welcomed[key] = now
            _group_last[gid] = now

            name = await self._member_name(event, gid, uid)
            raw_text = await self._gen_text(event, name)
            # llm_generate 不走 on_llm_response 管道，dsh-sticker 的钩子
            # 不会触发，必须自己剥掉 [贴纸:x] 并补发 GIF
            text, stickers = _split_stickers(raw_text)
            if not text:
                text = random.choice(FALLBACKS)

            # 像真人一样迟疑一下再说话，也顺便躲开「入群提示和欢迎同一秒」的机械感
            await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

            await event.send(
                MessageChain(chain=[At(qq=uid, name=name or uid), Plain(" " + text)])
            )
            # 贴纸单独发一条（图文混在一条里会被丢弃）
            for p in stickers[:1]:
                try:
                    await event.send(
                        MessageChain(chain=[Image.fromFileSystem(p)])
                    )
                except BaseException as e:
                    logger.warning("[welcome] 贴纸发送失败 %s: %s", p, e)
            logger.info(
                "[welcome] 已欢迎 群=%s 新成员=%s(%s): %s%s",
                gid,
                name or "?",
                uid,
                text,
                f" +{len(stickers[:1])}张贴纸" if stickers else "",
            )
        except BaseException as e:
            logger.error("[welcome] 处理入群事件失败: %s", e)
        finally:
            # 通知类事件没有正常回复内容，明确终止传播，
            # 免得后面的阶段拿空消息去问 LLM
            event.stop_event()

    @filter.command("欢迎测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """/欢迎测试 —— 用自己当新人，试一次欢迎词（不影响真实去重记录）。"""
        name = event.get_sender_name() or ""
        raw_text = await self._gen_text(event, name)
        text, stickers = _split_stickers(raw_text)
        tail = f"（另附 {len(stickers)} 张贴纸）" if stickers else ""
        yield event.plain_result(f"[欢迎词预览] {text or '（空）'}{tail}")

    @filter.command("欢迎状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """/欢迎状态 —— 查看欢迎功能配置与最近记录。"""
        gid = event.get_group_id() or "?"
        last = _group_last.get(gid)
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
