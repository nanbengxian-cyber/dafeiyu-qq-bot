# dsh-guard —— 违规判定 + 禁言。机器人在真群是 admin，有 set_group_ban 权限。
#
# ===========================================================================
# 一、三层结构，一层比一层贵
#
#   第 1 层  关键词预筛（正则，0 成本）
#              ↓ 命中才继续。实测真群 315 条真实消息只有 2.2% 命中
#   第 2 层  小模型判定（≈430 input tokens，中位 4.2s）
#              ↓ 输出 6 个布尔 + severity 0~3
#   第 3 层  代码决策（纯函数，可单测）
#              ↓ 查 role → 决定禁言 / 只警告 / 什么都不做
#
# 第 1 层是**成本闸门**不是判定器：宁可多放进来，判定交给模型。
# 第 3 层是**决策**，写在代码里而不是交给模型 —— 这是上一轮 dsh-decide 用
# 四次失败换来的教训（让模型输出结论，只改提示词判沉默率就在 0%~62% 乱跳）。
# 这里同样：模型只回答「这是什么类型的事、有多严重」，
# 「要不要禁、禁多久」由代码决定。
#
# 二、判定 prompt 刻意不列举具体政治话题
#
# 两个理由：列不全；把敏感词写进 prompt 本身就可能触发渠道的安全过滤。
# 改用**结构性描述**（说的是什么类型的事、目的是什么、会不会给群带来风险），
# 同时明确划出**不算**的情况：游戏里的国家/阵营、历史典故、地名、球队。
# 实测 19 个场景 0 次拒答、0 次解析失败。
#
# 三、attack 那一档差点酿成天天误禁
#
# 第一版实测 `你他妈真菜` 判 sev=2。这个群互相骂「菜」「傻逼」是常态社交，
# 按 sev=2 处理会天天误禁。修法是把门槛写成**结构性条件**：
# 只有攻击到家人、拿死亡诅咒、或反复针对同一人才算 true，日常对骂一律 false。
# 改完 `你他妈真菜` 降到 0，`骂群主是杂鱼`/`叫我爸爸`/`我家大肥会诈骗` 也都是 0。
# severity 的定义也从「说得多难听」改成「会不会给这个群带来真实风险」。
#
# 四、硬约束（写死在代码里，不给模型任何否决空间）
#
#   * 群主 / 管理员**绝不禁言** —— 技术上也做不到：实测对 admin 调
#     set_group_ban 返回 `cannot ban admin`。先查 role 能省掉无意义的 API
#     调用和满屏报错。这个群 93 人里有 16 个是群主/管理员（17%）。
#   * 机器人自己、白名单用户绝不禁言
#   * 禁言时长硬上限 30 分钟（不给「禁一天」的机会，误判代价太大）
#   * 同群 1 小时内最多禁 3 人（防判定跑偏导致群灭）
#   * 判定失败 / 超时 / 解析失败 -> **什么都不做**（fail-open）
#     注意这与 dsh-decide 的 fail-open 方向相反：那边失败=照旧说话，
#     这边失败=不动手。因为禁言是**不可逆**的，误禁一个人比漏禁一句话严重。
#
# 五、影子模式默认开启
#
# DSH_GUARD_SHADOW=1 时照样判定、照样打日志，但不真的禁言。
# 理由：判定准确率 19/19 是在 19 个手工场景上量的，不是在真群一周流量上量的。
# 先跑一段影子模式看日志里都判了谁，确认没误判再关掉。
# 指令 /禁言状态 看统计和最近 8 次判定。

import asyncio
import json
import os
import re
import time
from collections import deque

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger

ENABLED = os.environ.get("DSH_GUARD", "1") != "0"
# 影子模式：只判不禁。默认**开**（禁言不可逆，先看数据）
SHADOW = os.environ.get("DSH_GUARD_SHADOW", "1") != "0"
PROVIDER = os.environ.get("DSH_GUARD_PROVIDER", "").strip()
TIMEOUT = float(os.environ.get("DSH_GUARD_TIMEOUT", "10"))
# sev=2 累犯 / sev=3 的禁言秒数
BAN_SEC = int(os.environ.get("DSH_GUARD_BAN_SEC", "600"))
BAN_SEC_HIGH = int(os.environ.get("DSH_GUARD_BAN_SEC_HIGH", "1800"))
# 硬上限。任何计算结果都不许超过它
MAX_BAN_SEC = int(os.environ.get("DSH_GUARD_MAX_BAN_SEC", "1800"))
# sev=2 需要在这个窗口内累计到第几次才禁
WARN_WINDOW = float(os.environ.get("DSH_GUARD_WARN_WINDOW", "86400"))
WARN_TIMES = max(1, int(os.environ.get("DSH_GUARD_WARN_TIMES", "2")))
# 同群 1 小时内最多禁几人
BAN_WINDOW = float(os.environ.get("DSH_GUARD_BAN_WINDOW", "3600"))
GROUP_BAN_MAX = max(1, int(os.environ.get("DSH_GUARD_GROUP_BAN_MAX", "3")))
WHITELIST = {
    u.strip() for u in os.environ.get("DSH_GUARD_WHITELIST", "").split(",") if u.strip()
}
GROUPS = {
    g.strip() for g in os.environ.get("DSH_GUARD_GROUPS", "").split(",") if g.strip()
}
# 判定用的最近上下文条数（帮模型分辨是不是在开玩笑）
CTX_N = max(0, int(os.environ.get("DSH_GUARD_CTX", "4")))

# 关键词预筛。**只做成本闸门**，不做判定，所以宁可多放进来。
# 刻意不含「战争|历史问题」这类词：实测它们只会把
# 「我玩的是德国线，苏联那边太肝了」「二战德国那关我打了三遍」捞进来，
# 让这两句连模型都不用问，比命中后靠模型救回来更省。
PRE_RE = re.compile(
    r"政治|政府|政权|领导人|主席|总统|首相|执政|体制|制度|专政|民主|独裁|革命"
    r"|台独|港独|藏独|新疆|台湾|香港|西藏|统一|收复"
    r"|共产|国民党|党中央|上访|维权|游行|示威|抗议|镇压"
    r"|战犯|侵略|屠杀|反日|反美|反华|汉奸|卖国"
    r"|宗教|教徒|清真|穆斯林|基督|法轮"
    r"|涩图|色图|裸|做爱|约炮|开车|黄图|白丝|玉足|性感|丝袜|裤袜"
    r"|毒品|大麻|冰毒|赌博|博彩|菠菜|开户|洗钱|诈骗|杀猪盘"
    r"|外挂|私服|辅助|代练|盗号|黑号|发卡"
    r"|加我|私聊我|联系我|加微信|扫码|推广|返利|兼职|日入"
)

SYS = ("你是群聊管理助手的判断模块。只输出 JSON，不解释。"
       "你的任务是判断消息是否违反群规，不是参与讨论。")

PROMPT = """判断这条群消息是否违反群规。

发言人：{name}
消息：{text}

最近的上下文（帮你判断是不是在开玩笑）：
{ctx}

逐项回答 true/false，只看事实：
- politics：在讨论时事政治、国家政策、政权、领导人、社会制度、民族宗教冲突、
  或表明组织政治立场。注意：游戏里的国家/阵营、历史典故、地名、球队、
  单纯骂人不算。
- nsfw：在索要或传播色情、裸露内容
- illegal：在讨论违法交易、毒品、赌博、诈骗、外挂私服、盗号
- ad：在刷广告、拉人、发推广链接
- attack：在人身攻击。**只有**攻击到对方家人、拿死亡/疾病诅咒、
  或反复针对同一个人不停骂，才算 true。
  这个群平时互相骂「菜」「傻逼」「你他妈」是常态社交，一律 false。
- joking：整体气氛是在开玩笑、玩梗、反串，不是认真在说

再给两项：
- severity：按「会不会给这个群带来真实风险」打分，不是按「说得多难听」：
  0=没事（含日常对骂、玩梗、游戏/历史/地名/球队讨论）
  1=轻微擦边（开个荤玩笑、蹭一下边界，说一句就过去了）
  2=明显违规（真在索要违规内容、发广告、认真讨论时政）
  3=严重（表明政治立场并号召、诅咒他人家人生死、组织性违法）
- why：≤14字，说清判断依据

只输出一行 JSON：
{{"politics":false,"nsfw":false,"illegal":false,"ad":false,"attack":false,"joking":false,"severity":0,"why":""}}"""

_BOOLS = ("politics", "nsfw", "illegal", "ad", "attack", "joking")
# 触发禁言时给群里的说明。短、不说教，符合人设。
_REASON_TEXT = {
    "politics": "群里不聊这个",
    "nsfw": "这种别在群里要",
    "illegal": "这种事别在群里说",
    "ad": "广告出去发",
    "attack": "骂人过线了",
}

_stat = {
    "seen": 0, "skip_group": 0, "skip_cmd": 0, "skip_self": 0, "skip_white": 0,
    "pre_pass": 0, "pre_hit": 0, "asked": 0, "parse_fail": 0, "fail": 0,
    "timeout": 0, "sev": {0: 0, 1: 0, 2: 0, 3: 0},
    "warned": 0, "banned": 0, "ban_fail": 0,
    "skip_admin": 0, "skip_ban_quota": 0, "shadow_ban": 0, "ms_total": 0.0,
}
_warns: dict[str, deque] = {}       # "gid:uid" -> sev>=2 的时间窗口
_bans: dict[str, deque] = {}        # gid -> 禁言时间窗口
_ctx: dict[str, deque] = {}         # gid -> 最近几条 (name, text)
_last: list[str] = []


def _parse(raw: str) -> dict | None:
    """先整体 json.loads，失败就逐字段正则抠。

    schema 固定的小结构，逐字段比整体解析稳（dsh-decide 那边实测遇到过
    尾部多一个字导致整条解析失败）。
    """
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    body = s[i:j + 1] if (i >= 0 and j > i) else s
    out: dict | None = None
    try:
        o = json.loads(body)
        if isinstance(o, dict):
            out = o
    except BaseException:
        out = None
    if out is None:
        out = {}
        for k in _BOOLS:
            m = re.search(r'"%s"\s*:\s*(true|false|True|False)' % k, body)
            if m:
                out[k] = m.group(1).lower() == "true"
        m = re.search(r'"severity"\s*:\s*(\d)', body)
        if m:
            out["severity"] = int(m.group(1))
        m = re.search(r'"why"\s*:\s*"([^"]*)"', body)
        if m:
            out["why"] = m.group(1)
    # 一个字段都没有 = 模型没回答，这是「没结果」不是「全 False」。
    # 检查必须放在**两条路径之外**：整体 json.loads 成功但内容是 {"foo":"bar"}
    # 也得判无效，否则退化的模型输出会被当成「一切正常」放过去 ——
    # 对 guard 来说那意味着违规消息静默漏过。
    # （dsh-decide 踩过同一个坑，抄过来时把这个位置错误也抄了。）
    if not any(k in out for k in _BOOLS) and "severity" not in out:
        return None
    r: dict = {k: bool(out.get(k, False)) for k in _BOOLS}
    try:
        r["severity"] = max(0, min(3, int(out.get("severity", 0))))
    except BaseException:
        r["severity"] = 0
    w = out.get("why")
    r["why"] = w.strip()[:24] if isinstance(w, str) else ""
    return r


def effective_severity(f: dict) -> int:
    """玩梗降一级。

    这个群的社交常态就是互相骂，把玩梗当违规是最不能接受的错误方向。
    但 politics 不降 —— 「开玩笑地聊政治」在群里一样是风险。
    """
    sev = f["severity"]
    if f.get("joking") and not f.get("politics"):
        sev = max(0, sev - 1)
    return sev


def decide(f: dict, warn_count: int) -> tuple[str, int, str]:
    """纯函数决策：返回 (动作, 禁言秒数, 理由)。

    动作 = none | warn | ban。写成纯函数就能单测，改倾向是改这几行。
    """
    sev = effective_severity(f)
    kinds = [k for k in ("politics", "nsfw", "illegal", "ad", "attack") if f.get(k)]
    if sev <= 1 or not kinds:
        return "none", 0, ""
    kind = kinds[0]
    if sev >= 3:
        return "ban", min(BAN_SEC_HIGH, MAX_BAN_SEC), kind
    # sev == 2：先警告，同人在窗口内累计到 WARN_TIMES 次才禁
    if warn_count + 1 >= WARN_TIMES:
        return "ban", min(BAN_SEC, MAX_BAN_SEC), kind
    return "warn", 0, kind


def _prune(dq: deque, now: float, window: float) -> None:
    while dq and now - dq[0] > window:
        dq.popleft()


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[guard] 已加载：%s%s 超时%.0fs 警告%d次禁%ds／严重直接禁%ds "
            "上限%ds 同群%.0fh内最多禁%d人 白名单%d人 限定群=%s",
            "开" if ENABLED else "关",
            "（影子模式：只判不禁）" if SHADOW else "（真禁言）",
            TIMEOUT, WARN_TIMES, BAN_SEC, BAN_SEC_HIGH, MAX_BAN_SEC,
            BAN_WINDOW / 3600, GROUP_BAN_MAX, len(WHITELIST),
            "、".join(sorted(GROUPS)) if GROUPS else "全部",
        )

    # ---------------------------------------------------------------- 判定入口
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def check(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            me = str(event.get_self_id() or "")
            text = (event.message_str or "").strip()
            if not gid or not uid or not text:
                return
            if GROUPS and gid not in GROUPS:
                _stat["skip_group"] += 1
                return
            if uid == me:
                _stat["skip_self"] += 1
                return
            if text.startswith(("/", "／", "!", "！")):
                _stat["skip_cmd"] += 1
                return
            _stat["seen"] += 1

            name = (event.get_sender_name() or uid).strip()
            # 先记上下文再判断：模型要看「这句之前群里在聊什么」
            ctx_lines = list(_ctx.get(gid, ()))
            dq = _ctx.setdefault(gid, deque(maxlen=max(1, CTX_N)))
            dq.append("%s：%s" % (name, text[:60]))

            if uid in WHITELIST:
                _stat["skip_white"] += 1
                return

            if not PRE_RE.search(text):
                _stat["pre_pass"] += 1
                return
            _stat["pre_hit"] += 1
            logger.info("[guard] 预筛命中，送判定：%s：%s", name, text[:50])

            t0 = time.time()
            try:
                f = await self._judge(event.unified_msg_origin, name, text,
                                      "\n".join(ctx_lines))
            except asyncio.TimeoutError:
                _stat["timeout"] += 1
                logger.warning("[guard] 判定超时 %.0fs，什么都不做", TIMEOUT)
                return
            except BaseException as e:
                _stat["fail"] += 1
                logger.warning("[guard] 判定失败，什么都不做: %s", e)
                return
            _stat["ms_total"] += (time.time() - t0) * 1000
            if f is None:
                _stat["parse_fail"] += 1
                logger.warning("[guard] 判定结果解析不了，什么都不做")
                return
            _stat["asked"] += 1

            sev = effective_severity(f)
            _stat["sev"][sev] = _stat["sev"].get(sev, 0) + 1
            flags = "".join(k[0].upper() if f[k] else "." for k in _BOOLS)
            key = "%s:%s" % (gid, uid)
            now = time.time()
            wq = _warns.setdefault(key, deque())
            _prune(wq, now, WARN_WINDOW)

            act, sec, kind = decide(f, len(wq))
            brief = "%s sev=%d(原%d) [%s] %s why=%s ← %s：%s" % (
                act, sev, f["severity"], flags, kind or "-", f["why"] or "-",
                name, text[:30],
            )
            _last.append(time.strftime("%H:%M:%S ") + brief)
            del _last[:-10]

            if act == "none":
                logger.info("[guard] 放过｜%s", brief)
                return

            wq.append(now)
            if act == "warn":
                _stat["warned"] += 1
                logger.info("[guard] 警告（第%d次，再犯就禁）｜%s", len(wq), brief)
                if not SHADOW:
                    await self._say(event, "%s，收着点" % _REASON_TEXT.get(kind, "过线了"))
                return

            await self._do_ban(event, gid, uid, name, sec, kind, brief)
        except BaseException as e:
            # 判定模块自己出问题绝不能连累群聊
            logger.warning("[guard] 整体失败: %s", e)

    # ---------------------------------------------------------------- 判定调用
    async def _judge(self, umo: str, name: str, text: str, ctx: str) -> dict | None:
        pid = PROVIDER
        if not pid:
            # get_current_chat_provider_id 是**协程**，必须 await。
            # 不 await 会把 coroutine 对象一路传下去报
            # 「Provider <coroutine object ...> not found」——
            # dsh-welcome / dsh-memory / dsh-decide 都栽过这一下。
            pid = await self.context.get_current_chat_provider_id(umo)
        if not pid:
            return None
        resp = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=pid,
                prompt=PROMPT.format(name=name, text=text, ctx=ctx or "（无）"),
                system_prompt=SYS,
                temperature=0,   # 这是事实抽取不是创作，随机性只带来摇摆
            ),
            timeout=TIMEOUT,
        )
        raw = (getattr(resp, "completion_text", "") or "").strip()
        if not raw:
            raw = (getattr(resp, "reasoning_content", "") or "").strip()
        return _parse(raw)

    # ---------------------------------------------------------------- 执行禁言
    @staticmethod
    def _routed(event: AstrMessageEvent):
        """返回 (call_action, routing_params)。

        ★ call_action 必须带 self_id ★
        aiocqhttp 用 X-Self-ID 维护「多客户端」字典
        （CQHttp._add_wsr_api_client）。这台机器上除了真 napcat，端到端测试
        还会挂一条伪 napcat，不带 self_id 时 call_action 不知道发给谁，
        抛出的还是个**空消息**异常（日志里只有 "禁言失败: "，没有原因）。
        框架自己的 get_group() 就是靠 routing_params 解决的
        （aiocqhttp_message_event.py:244-252）。
        这个坑在只有一条连接时不会出现 —— 只有端到端测试能发现它。
        """
        bot = getattr(event, "bot", None)
        call = getattr(bot, "call_action", None)
        routing = {}
        sid = getattr(event.message_obj, "self_id", None)
        if sid:
            routing["self_id"] = sid
        return (call if callable(call) else None), routing

    async def _role_of(self, event: AstrMessageEvent, gid: str, uid: str) -> str:
        """查这个人在群里是什么身份。查不到就当 member（宁可查错也不能漏查）。"""
        call, routing = self._routed(event)
        if call is None:
            return "member"
        try:
            info = await asyncio.wait_for(
                call("get_group_member_info", group_id=int(gid), user_id=int(uid),
                     **routing),
                timeout=6,
            )
            return str((info or {}).get("role") or "member")
        except BaseException as e:
            logger.debug("[guard] 查身份失败(%s)，按普通成员处理: %r", uid, e)
            return "member"

    async def _do_ban(self, event, gid, uid, name, sec, kind, brief) -> None:
        # 硬约束 1：管理员/群主禁不了。实测对 admin 调 set_group_ban 返回
        # cannot ban admin —— 先查身份能省掉无意义的 API 调用和满屏报错。
        role = await self._role_of(event, gid, uid)
        if role in ("owner", "admin"):
            _stat["skip_admin"] += 1
            logger.info("[guard] %s 是%s，禁不了（也不该禁）｜%s", name, role, brief)
            return

        # 硬约束 2：同群 1 小时内最多禁 GROUP_BAN_MAX 人，防判定跑偏群灭
        now = time.time()
        bq = _bans.setdefault(gid, deque())
        _prune(bq, now, BAN_WINDOW)
        if len(bq) >= GROUP_BAN_MAX:
            _stat["skip_ban_quota"] += 1
            logger.warning(
                "[guard] 同群 %.0f 分钟内已禁 %d 人，达上限不再禁（可能是判定跑偏了）｜%s",
                BAN_WINDOW / 60, len(bq), brief,
            )
            return

        sec = max(1, min(int(sec), MAX_BAN_SEC))   # 硬上限，兜死
        if SHADOW:
            _stat["shadow_ban"] += 1
            logger.info("[guard] 影子模式：本该禁 %s %d 秒，没真禁｜%s",
                        name, sec, brief)
            return

        call, routing = self._routed(event)
        if call is None:
            _stat["ban_fail"] += 1
            logger.warning("[guard] 这个平台没有 call_action，禁不了")
            return
        try:
            await asyncio.wait_for(
                call("set_group_ban", group_id=int(gid), user_id=int(uid),
                     duration=sec, **routing),
                timeout=8,
            )
        except BaseException as e:
            _stat["ban_fail"] += 1
            # 异常消息经常是空的，必须连类型一起打，否则日志里只有
            # 「禁言失败: 」什么线索都没有
            logger.warning("[guard] 禁言失败(%s): %r｜%s",
                           type(e).__name__, e, brief)
            return
        bq.append(now)
        _stat["banned"] += 1
        logger.warning("[guard] 已禁言 %s(%s) %d 秒｜%s", name, uid, sec, brief)
        await self._say(event, "%s，%s，先安静 %d 分钟"
                        % (name, _REASON_TEXT.get(kind, "过线了"), max(1, sec // 60)))

    async def _say(self, event: AstrMessageEvent, text: str) -> None:
        # event.send 要 MessageChain。plain_result() 返回 MessageEventResult，
        # 没有 get_result —— 实测报
        # 'MessageEventResult' object has no attribute 'get_result'。
        try:
            await event.send(MessageChain(chain=[Plain(text)]))
        except BaseException as e:
            logger.warning("[guard] 说明发送失败: %r", e)

    # ---------------------------------------------------------------- 指令
    @filter.command("禁言状态")
    async def cmd_status(self, event: AstrMessageEvent):
        s = _stat
        total = s["pre_pass"] + s["pre_hit"]
        rate = (s["pre_hit"] / total * 100) if total else 0.0
        avg = s["ms_total"] / max(1, s["asked"])
        lines = [
            "违规看守：%s%s" % ("开" if ENABLED else "关",
                             "（影子模式：只判不禁）" if SHADOW else "（真禁言）"),
            "看过 %d 条：预筛放过 %d｜命中送判定 %d（%.1f%%）"
            % (s["seen"], s["pre_pass"], s["pre_hit"], rate),
            "判定 %d 次，平均 %.0fms；失败 %d｜超时 %d｜解析不了 %d"
            % (s["asked"], avg, s["fail"], s["timeout"], s["parse_fail"]),
            "严重度分布：0=%d｜1=%d｜2=%d｜3=%d"
            % (s["sev"].get(0, 0), s["sev"].get(1, 0),
               s["sev"].get(2, 0), s["sev"].get(3, 0)),
            "动作：警告 %d｜真禁言 %d｜影子模式本该禁 %d｜禁言失败 %d"
            % (s["warned"], s["banned"], s["shadow_ban"], s["ban_fail"]),
            "没禁的原因：对方是管理员 %d｜同群禁言配额满 %d"
            % (s["skip_admin"], s["skip_ban_quota"]),
            "规矩：sev2 累计 %d 次禁 %d 分钟｜sev3 直接禁 %d 分钟｜上限 %d 分钟"
            % (WARN_TIMES, BAN_SEC // 60, BAN_SEC_HIGH // 60, MAX_BAN_SEC // 60),
        ]
        if _last:
            lines.append("最近几次判定：")
            lines += ["  " + x for x in _last[-8:]]
        yield event.plain_result("\n".join(lines))
