# -*- coding: utf-8 -*-
# dsh-acl —— 指令权限门卫
#
# ===========================================================================
# 一、要解决什么
#
# 群里 93 人、16 个管理员。而这些指令的代价并不对等：
#   /reset /new      —— 清空**整个群共享**的会话历史（unique_session=false，
#                        全群一个会话），谁发都是全群丢记忆
#   /忘记群记忆      —— 删掉群共同记忆
#   /做视频          —— 一次约 4 分钟、真金白银，还占全局限流 75s
#   /音色            —— 改的是会话级音色，影响之后所有人听到的声音
#   /set /unset      —— 改会话变量
#
# 框架自带的 @filter.permission_type(ADMIN) 在这台机器上**等于没有**：
# 它只认 cmd_config.json 的 admins_id，而那里是 ['astrbot'] —— 一个 WebUI
# 账号，不是任何 QQ 号。所以群里任何人（包括群主）发管理员指令都只会收到
# 「权限不足」。dsh-memory 的 /忘记群记忆 已经因为这个原因自己判过群内身份。
#
# 与其让每个插件各判一遍，不如把授权集中到一处。
#
# ===========================================================================
# 二、为什么用 QQ 号而不是昵称
#
# 群里有重名（多个带「喵」的、多个单字名），而且群名片随时可改、@ 渲染出来的
# 名字也可能带零宽字符。昵称做身份凭证等于没有凭证。
# OneBot 事件里的 sender.user_id 是唯一不可伪造的标识，全部判定只用它。
#
# 群内身份（owner/admin/member）从 raw_message["sender"]["role"] 读，
# 这条路 dsh-memory 已经实测可用。取不到就按 member 处理 ——
# 宁可判严不可判松。
#
# ===========================================================================
# 三、为什么是「高优先级 + stop_event」而不是重复注册指令
#
# 试过的另一条路：在本插件里也 @filter.command("reset")。问题是框架有
# command_conflicts 机制，同名指令会被重命名或标冲突，行为不可预测。
#
# 现在的做法基于框架两个已确认的行为：
#   ① star_handlers_registry.append 按 -priority 排序，
#      waking_check 遍历这个有序表构造 activated_handlers；
#   ② StarRequestSubStage 遍历 activated_handlers 时
#      `if event.is_stopped(): break` —— 前面的 handler 停掉事件，
#      后面的（包括真正的指令 handler）根本不会执行。
# 所以挂一个 priority=1000 的 event_message_type handler，
# 自己按 CommandFilter 的同一套规则认指令，不放行就 stop_event。
#
# 副作用评估：本 handler 对每条消息都激活，于是 activated_handlers 恒非空、
# is_wake 恒为 True。这**不会**让机器人回复每条消息 —— ProcessStage 调
# LLM 的条件是 is_at_or_wake_command（只有被 @ / 带前缀 / 私聊才为真），
# 不是 is_wake。dsh-guard 早就在 GROUP_MESSAGE 上这么挂着，行为已验证。
#
# ===========================================================================
# 四、匹配规则必须和 CommandFilter 逐字一致
#
# 框架的判定是（command.py:199-204）：
#   message_str = re.sub(r"\s+", " ", event.get_message_str().strip())
#   命中 = message_str == cmd  或  message_str.startswith(cmd + " ")
#
# 两个关键点：
#   · message_str 在 waking_check 里**已经剥掉了前缀 `/`**，也剥掉了第一个
#     @机器人。所以 `/记忆状态` 和 `@小鲸鱼 记忆状态` 到这里都是 `记忆状态`，
#     一套规则同时覆盖两种发法。
#   · 那个空格是刚需：靠它区分 `记住我`（指令 记住我）和
#     `记住 我喜欢篮球`（指令 记住 + 参数）。少了空格约束，
#     `记住我` 会被 `记住` 抢走。
#
# 还必须检查 is_at_or_wake_command。否则群友在闲聊里说一句「我的档案」
# （没带斜杠、没 @），本来根本不会触发指令，却会被门卫拦下来回一句
# 「这条只有群主能用」—— 凭空制造噪音。
#
# ===========================================================================
# 五、指令级 ACL 挡不住什么（务必知道）
#
# /画图 /说话 /做视频 /搜 /看网页 /b站 这六条都有**自然语言路径**：
# 群友直接说「画个猫娘」「用语音说」「做个动画」，走的是 LLM 函数工具或
# 各插件的 on_llm_response 兜底钩子，完全不经过指令系统，本插件看不见。
#
# 所以把它们设成 owner 只是关掉了「明路」，省不了钱。真要限制花钱的能力，
# 得在 dsh-imagegen / dsh-voice / dsh-video 内部按 sender_id 判一次。
# 默认策略据此把这几条留给所有人 —— 半拉子限制比不限制更让人困惑。
#
# 旋钮：
#   DSH_ACL              1/0 总开关（默认 1）
#   DSH_ACL_OWNER        QQ 号，逗号分隔（默认 100000001）
#   DSH_ACL_OWNER_CMDS   追加 owner 级指令，逗号分隔
#   DSH_ACL_ADMIN_CMDS   追加 admin 级指令
#   DSH_ACL_ALL_CMDS     降级为所有人可用（优先级最高，用来放开默认限制）
#   DSH_ACL_SCOPE        group / private / both（默认 both）
#   DSH_ACL_QUIET        1 = 拦下时不出声（默认 0，会说一句）
#   DSH_ACL_COOLDOWN     同一人拒绝提示的冷却秒数（默认 30，防刷屏）
# 指令：/权限（看自己能用什么）

# [patch:acl-initiate-v1]
import os
import re
import time

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger

# ---------------------------------------------------------------- 配置

ENABLED = os.environ.get("DSH_ACL", "1") != "0"

# 群主的 QQ 号。默认值是本群群主，换群改这个 env 就行。
OWNERS = {
    u.strip()
    for u in os.environ.get("DSH_ACL_OWNER", "").split(",")
    if u.strip()
}

SCOPE = os.environ.get("DSH_ACL_SCOPE", "both").strip().lower()
QUIET = os.environ.get("DSH_ACL_QUIET", "0") != "0"
REFUSE_COOLDOWN = float(os.environ.get("DSH_ACL_COOLDOWN", "30"))

# ---------------------------------------------------------------- 默认策略
#
# owner  = 只有 DSH_ACL_OWNER 里的 QQ 号
# admin  = owner + 群主/群管理（按 OneBot sender.role）
# all    = 人人可用（不出现在下面两张表里的指令都是这一档）
#
# 分档依据只有一条：**这条指令的后果由谁承担**。
#   后果落在全群 → owner
#   只是诊断信息 → admin
#   后果只落在自己身上 → all

OWNER_CMDS = {
    # —— 破坏共享状态。unique_session=false，全群共用一个会话，
    #    任何人 /reset 都是把全群的上下文清掉。
    "reset",
    "new",
    "stop",
    "set",
    "unset",
    # —— 记忆管理。别人的档案、群共同记忆，不该谁都能翻能删。
    "忘记群记忆",
    "记忆状态",
    "群记忆",
    # —— 会话级全局状态。换了音色之后所有人听到的都变了。
    "音色",
    # —— 最贵的一条：一次约 4 分钟 + 全局限流 75s，一个人就能把全群堵住。
    "做视频",
    # —— 会触发一次 LLM 调用的测试指令。
    "欢迎测试",
    # 绕过时段/冷场/每日额度直接触发一次判定+一次开口，
    # 不锁的话等于把每日额度交给全群随便点。
    "主动开口测试",
}

ADMIN_CMDS = {
    # 状态查询：不改任何东西，但暴露渠道地址、模型名、配额、判定明细，
    # 属于运维信息，给到群管理这一层就够了。
    "画图状态",
    "语音状态",
    "视频状态",
    "识图状态",
    "图片上下文",
    "联网状态",
    "贴纸状态",
    "说话风格",
    "艾特模式",
    "上下文状态",
    "插话判断",
    "禁言状态",
    "防骗状态",
    "戳一戳状态",
    "欢迎状态",
    "聊天记录状态",
    "主动开口状态",
    # token 用量 = 花了多少钱。
    "stats",
}

# 明确留给所有人的（写出来是为了自文档化，实际逻辑是「不在上面两张表里」）。
#
# 特别说明 /忘记我：这是群友对自己数据的退出权，**刻意不设限**。
# 把它锁成 owner 只能用等于「你的资料只有群主能删」，比不设限更糟。
# /我的档案 /记住 /记住我 同理，作用域都只有自己。
PUBLIC_CMDS = {
    "我的档案",
    "记住",
    "记住我",
    "忘记我",
    "画图",
    "说话",
    "搜",
    "看网页",
    "b站",
    "help",
    "sid",
    "权限",
}


def _split_env(name: str) -> set:
    return {c.strip() for c in os.environ.get(name, "").split(",") if c.strip()}


# env 追加/覆盖。ALL 最后处理，所以它能把默认限制放开 ——
# 需要临时开一条给群友时不用改代码。
OWNER_CMDS |= _split_env("DSH_ACL_OWNER_CMDS")
ADMIN_CMDS |= _split_env("DSH_ACL_ADMIN_CMDS")
_FREE = _split_env("DSH_ACL_ALL_CMDS")
OWNER_CMDS -= _FREE
ADMIN_CMDS -= _FREE
PUBLIC_CMDS |= _FREE

# 被管的指令全集。按长度倒序匹配：万一将来出现 `记住` 和 `记住我` 这种
# 前缀关系，长的先试，避免短的抢掉。
_MANAGED = sorted(OWNER_CMDS | ADMIN_CMDS, key=len, reverse=True)

# 拦下时顺便告诉对方「你能用什么替代」。没有替代的就不写，
# 硬凑一句反而像敷衍。
_HINT = {
    "记忆状态": "想看我记住了你什么，发 /我的档案",
    "群记忆": "想看我记住了你什么，发 /我的档案",
    "忘记群记忆": "想删掉关于你自己的，发 /忘记我",
    "reset": "这会清掉全群的对话历史，不是只清你的",
    "new": "这会给全群开一个新会话",
    "做视频": "直接说「做个……的动画」也能触发，不占这条指令",
    "音色": "换了之后所有人听到的都变了，所以只有群主能改",
}

_stat = {
    "seen": 0,
    "matched": 0,
    "pass_owner": 0,
    "pass_admin": 0,
    "denied": 0,
    "quiet_denied": 0,
}
_last_refuse: dict[str, float] = {}
_recent: list[str] = []


# ---------------------------------------------------------------- 纯函数区
# 这一段不碰 event、不碰 astrbot，可以单测（见 test_acl.py）。


def norm(text: str) -> str:
    """按 CommandFilter 的同一套规则归一化消息正文。"""
    return re.sub(r"\s+", " ", (text or "").strip())


def match_command(text: str, managed=None) -> str:
    """认出这条消息在调用哪个被管指令；不是被管指令返回空串。

    规则与 CommandFilter 逐字一致：完全相等，或以「指令名 + 空格」开头。
    那个空格不能省 —— 少了它 `记住我` 会被 `记住` 抢走。
    """
    t = norm(text)
    if not t:
        return ""
    for cmd in managed if managed is not None else _MANAGED:
        if t == cmd or t.startswith(cmd + " "):
            return cmd
    return ""


def level_of(cmd: str, owner_cmds=None, admin_cmds=None) -> str:
    """这条指令属于哪一档。"""
    oc = OWNER_CMDS if owner_cmds is None else owner_cmds
    ac = ADMIN_CMDS if admin_cmds is None else admin_cmds
    if cmd in oc:
        return "owner"
    if cmd in ac:
        return "admin"
    return "all"


def allowed(level: str, uid: str, role: str, owners=None) -> bool:
    """够不够格。uid 是 QQ 号，role 是群内身份 owner/admin/member。

    群主的 QQ 号写在 OWNERS 里；同时也认群内 role == owner ——
    换群部署时忘了改 env 也不会把真群主锁在外面。
    """
    ow = OWNERS if owners is None else owners
    if level == "all":
        return True
    is_owner = uid in ow or role == "owner"
    if level == "owner":
        return is_owner
    if level == "admin":
        return is_owner or role == "admin"
    return True


# ---------------------------------------------------------------- 身份读取


def role_of(event) -> str:
    """群内身份：owner / admin / member。

    来源是 OneBot 原始事件的 sender.role。raw_message 是 aiocqhttp 的 Event,
    既支持下标也支持属性访问，但不同适配器形状不一样，所以整段包在 try 里。
    取不到按 member 处理 —— 宁可判严不可判松。
    """
    try:
        raw = getattr(event.message_obj, "raw_message", None)
        sender = None
        if raw is not None:
            try:
                sender = raw["sender"]
            except BaseException:
                sender = getattr(raw, "sender", None)
        role = str((sender or {}).get("role") or "").lower()
        if role in ("owner", "admin", "member"):
            return role
    except BaseException:
        pass
    return "member"


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[acl] 已加载：%s 群主=%s 作用域=%s owner级%d条 admin级%d条 %s",
            "开" if ENABLED else "关",
            "、".join(sorted(OWNERS)) or "未设置",
            SCOPE,
            len(OWNER_CMDS),
            len(ADMIN_CMDS),
            "（静默模式）" if QUIET else "",
        )
        if not OWNERS:
            logger.warning(
                "[acl] DSH_ACL_OWNER 为空！这时只认群内 role==owner，"
                "私聊里没有 role，owner 级指令谁都用不了。"
            )

    # ------------------------------------------------------------ 门卫
    #
    # priority=1000 让本 handler 排在所有指令 handler 前面。
    # 依据：star_handlers_registry.append 按 -priority 排序，
    # waking_check 遍历这个有序表，StarRequestSubStage 遇到
    # is_stopped() 就 break。
    #
    # 只挂 GROUP + PRIVATE，不用 ALL。差别在 OTHER_MESSAGE（notice 类事件，
    # 比如入群、戳一戳）：那些事件的 message_str 是空的，门卫本来就会立刻
    # return，但挂上去会让它们的 is_wake 变成 True，白白改变 dsh-welcome /
    # dsh-poke 所在链路的行为。能不碰就不碰。
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=1000,
    )
    async def gate(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            _stat["seen"] += 1

            # 只在真正的指令调用上生效。没被 @ 也没带前缀时，指令本来就不会
            # 触发（CommandFilter 第一行就查这个），门卫也别多话。
            if not getattr(event, "is_at_or_wake_command", False):
                return

            in_group = bool(event.get_group_id())
            if SCOPE == "group" and not in_group:
                return
            if SCOPE == "private" and in_group:
                return

            uid = str(event.get_sender_id() or "")
            me = str(event.get_self_id() or "")
            if not uid or uid == me:
                return

            cmd = match_command(event.get_message_str())
            if not cmd:
                return
            _stat["matched"] += 1

            role = role_of(event)
            level = level_of(cmd)
            if allowed(level, uid, role):
                _stat["pass_owner" if level == "owner" else "pass_admin"] += 1
                return

            # 不放行。先记账，再决定要不要出声，最后停掉事件。
            _stat["denied"] += 1
            name = event.get_sender_name() or uid
            _recent.append(
                "%s %s(%s)[%s] → /%s(%s级)"
                % (time.strftime("%H:%M:%S"), name, uid, role, cmd, level)
            )
            del _recent[:-10]
            logger.info(
                "[acl] 拦下 /%s（%s 级）｜%s(%s) 群内身份=%s", cmd, level, name, uid, role
            )

            if not QUIET:
                now = time.time()
                if now - _last_refuse.get(uid, 0.0) >= REFUSE_COOLDOWN:
                    _last_refuse[uid] = now
                    who = "群主" if level == "owner" else "群主和管理员"
                    text = "这条只有%s能用。" % who
                    hint = _HINT.get(cmd)
                    if hint:
                        text += hint
                    await self._say(event, text)
                else:
                    _stat["quiet_denied"] += 1

            # 必须在最后。stop_event 之后 StarRequestSubStage 会 break，
            # 真正的指令 handler 拿不到这次事件。
            event.stop_event()
        except BaseException as e:
            # 门卫自己出问题绝不能连累群聊。异常时 fail-open：
            # 不停事件，指令照常执行 —— 把人锁在门外比放进来更糟。
            logger.warning("[acl] 判定失败，本次放行: %r", e)

    @staticmethod
    async def _say(event: AstrMessageEvent, text: str) -> None:
        # event.send 要 MessageChain。plain_result() 返回 MessageEventResult，
        # 没有 get_result —— dsh-guard 已经栽过这一下。
        try:
            await event.send(MessageChain(chain=[Plain(text)]))
        except BaseException as e:
            logger.warning("[acl] 提示发送失败: %r", e)

    # ------------------------------------------------------------ 指令
    @filter.command("权限")
    async def cmd_perm(self, event: AstrMessageEvent):
        """/权限 —— 看看你能用哪些指令。"""
        uid = str(event.get_sender_id() or "")
        role = role_of(event)
        is_owner = uid in OWNERS or role == "owner"
        is_admin = is_owner or role == "admin"

        who = "群主" if is_owner else ("管理员" if is_admin else "群成员")
        lines = ["你是%s（QQ %s），可以用：" % (who, uid)]

        # 人人可用的那批：只列真实存在的，别把 env 里瞎填的名字也念出来。
        pub = sorted(PUBLIC_CMDS - OWNER_CMDS - ADMIN_CMDS)
        lines.append("　所有人：" + "、".join("/" + c for c in pub))
        if is_admin:
            lines.append("　管理员：" + "、".join("/" + c for c in sorted(ADMIN_CMDS)))
        if is_owner:
            lines.append("　群主：" + "、".join("/" + c for c in sorted(OWNER_CMDS)))
        if not is_admin:
            lines.append(
                "另外 %d 条状态查询要管理员、%d 条要群主。"
                % (len(ADMIN_CMDS), len(OWNER_CMDS))
            )
        lines.append("不用指令也行：直接说「画个猫」「用语音说」，发链接我会自己去看。")
        yield event.plain_result("\n".join(lines))

    @filter.command("权限状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """/权限状态 —— 门卫的运行统计（群主/管理员可见）。"""
        uid = str(event.get_sender_id() or "")
        role = role_of(event)
        if not (uid in OWNERS or role in ("owner", "admin")):
            yield event.plain_result("这条只有群主和管理员能用。想知道自己能用什么发 /权限")
            return
        s = _stat
        lines = [
            "指令门卫：%s%s" % ("开" if ENABLED else "关", "（静默）" if QUIET else ""),
            "群主 QQ：%s｜作用域：%s" % ("、".join(sorted(OWNERS)) or "未设置", SCOPE),
            "看过 %d 条消息，认出被管指令 %d 次" % (s["seen"], s["matched"]),
            "放行 %d 次（owner级 %d／admin级 %d）"
            % (s["pass_owner"] + s["pass_admin"], s["pass_owner"], s["pass_admin"]),
            "拦下 %d 次（其中 %d 次因冷却没出声）" % (s["denied"], s["quiet_denied"]),
            "分档：owner %d 条｜admin %d 条｜其余人人可用"
            % (len(OWNER_CMDS), len(ADMIN_CMDS)),
        ]
        if _recent:
            lines.append("最近几次拦下：")
            lines += ["　" + x for x in _recent[-6:]]
        yield event.plain_result("\n".join(lines))
