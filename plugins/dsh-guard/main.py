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
#
# 六、动态判罚层（mood_logic.py）：阈值不再对所有人所有时刻都一样
#
# 原来的动作表是固定的：sev2 警告 + 累犯禁 5 分钟、sev3 直接禁 10 分钟。
# 现在把**态度类**判罚的松紧交给已经跑在生产里的状态：
# 生气/警戒/关系疏远/被反复骚扰 -> 更严；心情好/玩心重/关系亲近 -> 更宽。
# 读的是 dsh-emotion / dsh-desire / dsh-social / dsh-agency / dsh-fatigue
# 的状态，**只读**（SQLite mode=ro / JSON），读不到就中性 = 上面的固定行为。
#
# 三条硬边界，写死在代码里（详见 mood_logic.py 的模块注释）：
#   ① 行为类不受心情影响：违法/广告/色情索要/刷屏/明确辱骂/多人围攻
#   ② 代码兜底（诅咒家人、群体仇恨）不受心情影响
#   ③ 心情最多推动一档，且**只能把「放过」变成「警告」，不能把「放过」
#      变成「禁言」** —— 禁言必须由原本就够格的判定（base sev>=2）触发。
# 关掉：DSH_GUARD_MOOD=0（回到完整的固定行为，用于对照）。

import asyncio
import json
import os
import re
import sys
import time
from collections import deque
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.core import logger

_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)
from dsh_link import TurnClaim, claim_turn  # type: ignore  # noqa: E402

# 动态判罚层。优先相对导入（AstrBot 把插件当包加载，与 dsh-desire/dsh-agency
# 一致）；万一加载方式没有包上下文，就退回同目录的绝对导入。
try:  # noqa: E402
    from .mood_logic import (  # type: ignore
        Mood, MoodReader, Tunables, apply_duration, classify as mood_classify,
        describe as mood_describe, leniency as mood_leniency, neutral as mood_neutral,
        raised_only, severity_step, warn_need,
    )
except ImportError:  # pragma: no cover - 只在非包加载时走到
    _HERE = str(Path(__file__).resolve().parent)
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from mood_logic import (  # type: ignore  # noqa: E402
        Mood, MoodReader, Tunables, apply_duration, classify as mood_classify,
        describe as mood_describe, leniency as mood_leniency, neutral as mood_neutral,
        raised_only, severity_step, warn_need,
    )

ENABLED = os.environ.get("DSH_GUARD", "1") != "0"
# 影子模式：只判不禁。默认**开**（禁言不可逆，先看数据）
SHADOW = os.environ.get("DSH_GUARD_SHADOW", "1") != "0"
PROVIDER = os.environ.get("DSH_GUARD_PROVIDER", "").strip()
# 超时给得宽：guard 是**事后**审查，不挡任何回复（用户的消息早发出去了），
# 所以慢一点零代价，超时反而会让判定静默失效。
# 实测判定延迟中位在 4.2s ~ 11.3s 之间浮动（白天渠道负载高时更慢），
# 原来的 10s 会让一半以上的判定超时 fail-open。
TIMEOUT = float(os.environ.get("DSH_GUARD_TIMEOUT", "30"))
# sev=2 累犯 / sev=3 的禁言秒数
BAN_SEC = int(os.environ.get("DSH_GUARD_BAN_SEC", "600"))
BAN_SEC_HIGH = int(os.environ.get("DSH_GUARD_BAN_SEC_HIGH", "1800"))
# 硬上限。任何计算结果都不许超过它
MAX_BAN_SEC = int(os.environ.get("DSH_GUARD_MAX_BAN_SEC", "1800"))
# sev=2 需要在这个窗口内累计到第几次才禁
WARN_WINDOW = float(os.environ.get("DSH_GUARD_WARN_WINDOW", "86400"))
WARN_TIMES = max(1, int(os.environ.get("DSH_GUARD_WARN_TIMES", "2")))
# 高置信度、明确指向某人的直接辱骂不再交给模型。原规则刻意放过
# 「傻逼/sb/废物」等日常互怼，导致真正 @ 人骂以及单独甩一句脏话也完全漏过。
# 这条代码闸门只收「有 @ 指向 / 你他她指向 / 整句就是辱骂」的消息。
DIRECT_INSULT_BAN_SEC = max(
    60, int(os.environ.get("DSH_GUARD_DIRECT_INSULT_BAN_SEC", "300"))
)
# 同群 1 小时内最多禁几人
BAN_WINDOW = float(os.environ.get("DSH_GUARD_BAN_WINDOW", "3600"))
GROUP_BAN_MAX = max(1, int(os.environ.get("DSH_GUARD_GROUP_BAN_MAX", "3")))
# 刷屏检测（纯代码闸门，不等 LLM）：
#   同一人 FLOOD_WINDOW 秒窗口内发出 >= FLOOD_MAX 条消息 => 刷屏。
#   第 1 次：禁言 FLOOD_BAN_SEC（默认一天）；第 FLOOD_KICK_AT 次：直接踢出群
#   （set_group_kick，REJECT_REJOIN=1 时同时拒绝此人再次申请入群）。
#   群主在白名单里天然豁免；管理员/群主硬约束照旧（踢不了也不该踢）。
#   刷屏禁言绕过 MAX_BAN_SEC 硬上限（用户明确要「禁言一天」，1800s 上限会把它
#   砍成 30 分钟），但 GROUP_BAN_MAX 同群配额照常生效，防判定跑偏群灭。
FLOOD_WINDOW = float(os.environ.get("DSH_GUARD_FLOOD_WINDOW", "30"))
FLOOD_MAX = max(3, int(os.environ.get("DSH_GUARD_FLOOD_MAX", "10")))
FLOOD_BAN_SEC = max(60, int(os.environ.get("DSH_GUARD_FLOOD_BAN_SEC", "86400")))
FLOOD_KICK_AT = max(2, int(os.environ.get("DSH_GUARD_FLOOD_KICK_AT", "2")))
REJECT_REJOIN = os.environ.get("DSH_GUARD_REJECT_REJOIN", "1") != "0"
WHITELIST = {
    u.strip() for u in os.environ.get("DSH_GUARD_WHITELIST", "").split(",") if u.strip()
}
GROUPS = {
    g.strip() for g in os.environ.get("DSH_GUARD_GROUPS", "").split(",") if g.strip()
}
# 判定用的最近上下文条数（帮模型分辨是不是在开玩笑）
CTX_N = max(0, int(os.environ.get("DSH_GUARD_CTX", "4")))

# ---------------------------------------------------------------- 动态判罚层
# 总开关。关掉 = 回到固定阈值（用于对照：同一段流量两次跑，只看心情那部分差多少）。
MOOD_ON = os.environ.get("DSH_GUARD_MOOD", "1") != "0"
# 宽容度权重与档位上限：weight=0 等价于关掉；max_step=1 表示最多推动一档。
# 这两个是「心情不是橡皮筋」那条硬边界的旋钮，给再大也不允许越界禁言。
MOOD_TUN = Tunables.from_env()
# 情绪把「本来放过」的消息变成一次公开警告时的同群冷却（秒）。
# 为什么需要：生气时 borderline 消息会变多，不冷却就会一串「收着点」刷屏 ——
# 那比不管还难看。冷却期内不警告、也不计入累犯（不能在没警告过的前提下禁人）。
MOOD_WARN_CD = max(0.0, float(os.environ.get("DSH_GUARD_MOOD_WARN_CD", "300")))
_mood_reader: MoodReader | None = None


def _mood() -> MoodReader:
    global _mood_reader
    if _mood_reader is None:
        _mood_reader = MoodReader(MOOD_TUN)
    return _mood_reader

# 关键词预筛。**只做成本闸门**，不做判定，所以宁可多放进来。
#
# ★ 第一版漏掉了真群里真实发生的时政提问 ★
# 影子模式跑了 329 条真群消息，预筛命中 0 条 —— 听起来像「群里很干净」，
# 但那天群主真的问过「美国和伊朗的战争结束了没有？」，这是标准的时事政治
# 讨论，预筛却放过了。原因：上一轮为了不误伤「二战德国那关我打了三遍」，
# 我把「战争」这个词从预筛里删了。方向对（游戏/历史不该命中），
# 手段错 —— 删词等于把真信号一起删掉。
#
# 正确的形状不是「有没有这个词」，而是**词的组合**：
#   真实国名/地区 + 政治性名词  => 时政讨论
#   同时出现游戏/历史标记        => 撤销（是在聊游戏或历史）
# 这与 dsh-imagegen 那条「结构判断优于枚举词表」同形。
#
# 实测（586 条真群语料）：旧版命中 7 条(1.2%) → 新版 8 条(1.4%)，
# 多捞到的正是那句「美国和伊朗的战争结束了没有？」，且旧版能捞的一条没丢。

# 1) 直接命中：这些词单独出现就值得花一次判定
_DIRECT_RE = re.compile(
    r"政治|政府|政权|领导人|主席|总统|首相|执政|体制|专政|独裁|革命"
    r"|台独|港独|藏独|新疆|西藏|法轮"
    r"|共产|国民党|党中央|上访|维权|游行|示威|抗议|镇压"
    r"|战犯|侵略|屠杀|反日|反美|反华|汉奸|卖国"
    r"|宗教|教徒|清真|穆斯林|基督"
    r"|涩图|色图|裸|做爱|约炮|开车|黄图|白丝|玉足|性感|丝袜|裤袜"
    r"|无码|有码|露骨|情色|av|A片|黄片|三级片|福利姬|本子|同人志|里番|工口"
    r"|毒品|大麻|冰毒|赌博|博彩|菠菜|开户|洗钱|诈骗|杀猪盘"
    r"|外挂|私服|代练|盗号|黑号|发卡"
    r"|加我|私聊我|联系我|加微信|扫码|推广|返利|兼职|日入"
)
# 2) 组合命中：真实国名/地区 + 政治性名词
_COUNTRY_RE = re.compile(
    r"美国|中国|俄罗斯|俄国|乌克兰|伊朗|伊拉克|以色列|巴勒斯坦|加沙|叙利亚"
    r"|朝鲜|韩国|日本|印度|巴基斯坦|越南|菲律宾|台湾|香港|欧盟|北约"
    r"|阿富汗|土耳其|沙特|也门|黎巴嫩|缅甸|俄乌|中美|中日|台海|南海"
)
_POLI_NOUN_RE = re.compile(
    r"战争|开战|停战|停火|交战|冲突|局势|形势|制裁|禁运|军事|驻军|出兵"
    r"|导弹|核弹|核武|政策|选举|大选|总统|议会|外交|建交|断交"
    r"|主权|领土|入侵|占领|统一|独立|收复|回归|谈判|协议|条约|难民|人权"
    # 「中美关系」这类：关系/态度/立场 + 国名也是时政。单独一个「关系」太泛
    # （"这跟你有什么关系"），但它必须和国名同时出现才命中，所以泛不到日常
    # 闲聊上 —— 实测 586 条真群语料 0 误伤。
    r"|关系|态度|立场|谁赢|谁厉害|打起来|会打|开打"
    # 打击动作：实测「以色列空袭加沙」国名命中、政治名词一个没中，
    # 整条从预筛漏掉。这些词单独出现太泛（「被蚊子袭击」），
    # 但必须与国名同时出现才命中，所以泛不到日常闲聊上。
    r"|空袭|轰炸|袭击|炮击|开火|交火|停战|撤军|增兵|封锁|军演"
)
# 3) 游戏/历史标记：出现这些说明在聊游戏或历史，组合命中撤销。
#    撤销只是为了省钱；万一撤错了，_DIRECT_RE 那一层还在。
_GAME_HIST_RE = re.compile(
    r"这关|那关|第.关|打了|通关|开局|阵营|路线|steam|游戏|模拟|策略"
    r"|三国|春秋|战国|唐朝|宋朝|明朝|清朝|历史课|课本|一战|二战"
)


# 4) 人身攻击：第一版预筛**整类漏掉**了 —— 只覆盖了政治/色情/违法/广告。
#    实测「你妈死了你全家都该死」判定层给 sev=3，但预筛根本没送进去。
#    难点是这个群日常就在互骂（「你他妈真菜」是常态社交），
#    所以不能枚举脏话，只能用组合：**伤害词 + 指向对方家人**。
#    「笑死我了」只有伤害词没有指向 -> 放过；
#    「你他妈真菜」既没伤害词、「他妈」也不在指向表里 -> 放过。
_HARM_RE = re.compile(r"死|去世|癌|艾滋|残废|绝症|车祸|截肢|火化|坟|棺")
_TARGET_RE = re.compile(r"你妈|你爸|你爹|你娘|你家|你父母|你全家|全家|一家人|祖宗|家人")

# 5) 群体仇恨言论：「日本人都该死，都是畜生」这类。
#    实测它既没被 _DIRECT_RE 收（没有「反日」这种现成词），
#    也不满足「国名 + 政治名词」（"该死"不是政治名词），整条漏过。
#    ★ 试过用「群体 + 伤害词」，误伤严重 ★
#    汉语里「死」大量作程度补语：「日本料理好吃死了」「这个韩国综艺笑死我了」
#    都会命中。所以仇恨检测不能用裸的伤害词，要用
#    **群体指称 + 集体贬损谓语** 这个结构。
#    「去死吧你」没有群体指称 -> 由上面第 4 条的 attack 组合去管。
_GROUP_RE = re.compile(
    r"美国|中国|俄罗斯|俄国|乌克兰|伊朗|伊拉克|以色列|巴勒斯坦|叙利亚"
    r"|朝鲜|韩国|日本|印度|巴基斯坦|越南|菲律宾|台湾|香港|缅甸|非洲|欧洲"
    r"|黑人|白人|犹太|犹大|回族|维族|藏族|汉族|穆斯林|基督徒|同性恋|残疾人"
    r"|河南人|东北人"
)
_SLUR_RE = re.compile(
    r"该死|都死|去死|该杀|杀光|杀绝|灭绝|清理|清除|垃圾|畜生|杂种|劣等"
    r"|下贱|贱种|不是人|滚出|驱逐|该被|活该"
)

# 6) 明确指向个人的直接辱骂：纯代码、低延迟、无需调用模型。
#
# 只匹配高置信度侮辱核心，不把普通否定词（笨、菜、丑）塞进来；还必须满足：
#   - 消息含 @（AstrBot 的 message_str 会展开为 @昵称(QQ)）；或
#   - 侮辱词前有「你/他/她/这人」；或
#   - 去掉标点后整句就是侮辱词（真群漏判样本「sb」）。
# 这样「别骂人」「他说了sb」「这个游戏真垃圾」不会仅因出现词而禁人。
_DIRECT_INSULT_RE = re.compile(
    r"(?:傻\s*[逼比币]|煞\s*笔|沙\s*比|[sS][bB]|脑残|弱智|智障|废物|狗东西"
    r"|畜生|杂种|贱种|贱人|人渣|去死|滚蛋|妈的|你妈|操你|草你|[nN][mM][sS][lL]"
    r"|[cC][nN][mM])"
)
_DIRECT_TARGET_RE = re.compile(r"(?:你|他|她|这人|那人|这货|那货).{0,8}$")
_DIRECT_QUOTE_RE = re.compile(
    r"(?:(?:别|不要|不许|停止|禁止|没|没有|不会|不能|为什么|为啥).{0,4}(?:骂|说)"
    r"|(?:他|她|有人|群里).{0,4}(?:说|骂|发))"
)


def direct_insult(text: str) -> str:
    """返回高置信度直接辱骂词；不是明确指向个人时返回空字符串。"""
    raw = (text or "").strip()
    m = _DIRECT_INSULT_RE.search(raw)
    if not m:
        return ""
    # 「别骂人/为什么说sb」是在谈论辱骂，不是实施辱骂。
    if _DIRECT_QUOTE_RE.search(raw):
        return ""
    compact = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", raw)
    insult_only = bool(_DIRECT_INSULT_RE.fullmatch(compact))
    # 中文代词紧贴谓语是常态（「你是废物」「他真弱智」），不能要求前方空格。
    targeted = "@" in raw or bool(_DIRECT_TARGET_RE.search(raw))
    return m.group(0) if (insult_only or targeted) else ""


def _has_at_component(event: AstrMessageEvent) -> bool:
    """message_str 在不同适配器下未必保留 @；组件是权威指向证据。"""
    try:
        for comp in event.get_messages() or ():
            if comp.__class__.__name__ == "At":
                return True
    except BaseException:
        pass
    return False


def _is_group_admin(event) -> bool:
    """这个人是群主/群管理，或机器人管理员吗。

    与 dsh-memory 里的同名函数逐字一致（两个插件不能互相 import，
    只能各存一份；改了记得两边一起改）。

    不用框架的 PermissionType.ADMIN：它只认 cmd_config.json 的 admins_id，
    这台机器上是 ['astrbot'] —— 一个 WebUI 账号，不是任何 QQ 号，
    于是群里所有人（含群主）都会被判无权限。
    role 的来源是 OneBot 原始事件的 sender.role（owner/admin/member）。
    """
    try:
        # 机器人管理员（WebUI 侧）也放行，方便从后台看
        if getattr(event, "role", "") == "admin":
            return True
    except BaseException:
        pass
    try:
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            role = str((raw.get("sender") or {}).get("role") or "")
        else:
            role = str(getattr(getattr(raw, "sender", None), "role", "") or "")
        return role in ("owner", "admin")
    except BaseException:
        return False


def pre_hit(text: str) -> bool:
    """值不值得花一次模型判定。纯函数，好单测。

    预筛只是**成本闸门**：放过的一分钱不花，命中的才送模型。
    宁可多放进来一点（判定层能正确说出「游戏内容，非时事」），
    唯一底线是不能让日常闲聊大面积命中。
    """
    if _DIRECT_RE.search(text):
        return True
    if _COUNTRY_RE.search(text) and _POLI_NOUN_RE.search(text):
        return not _GAME_HIST_RE.search(text)
    if _HARM_RE.search(text) and _TARGET_RE.search(text):
        return True
    if _GROUP_RE.search(text) and _SLUR_RE.search(text):
        return True
    return False


SYS = ("你是群聊管理助手的判断模块。只输出 JSON，不解释。"
       "你的任务是判断消息是否违反群规，不是参与讨论。")

PROMPT = """判断这条群消息是否违反群规。

发言人：{name}
消息：{text}

最近的上下文（帮你判断是不是在开玩笑）：
{ctx}

逐项回答 true/false，只看事实：
- politics：话题落在时事政治、国家政策、政权、领导人、社会制度、民族宗教冲突上。
  注意：游戏里的国家/阵营、历史典故、地名、球队、单纯骂人不算。
- stance：在这个政治话题上**表明了自己的立场或做出评判**吗
  （说谁对谁错、该怎么做、号召别人怎么做、贴标签）。
  只是问「打完了吗」「进展怎么样」「你们怎么看」这种**问事实/问看法**，
  以及单纯转述新闻，都是 false。
- nsfw：在索要或传播色情、裸露内容
- illegal：在讨论违法交易、毒品、赌博、诈骗、外挂私服、盗号
- ad：在刷广告、拉人、发推广链接
- attack：在人身攻击。**只有**攻击到对方家人、拿死亡/疾病诅咒、
  或反复针对同一个人不停骂，才算 true。
  这个群平时互相骂「菜」「傻逼」「你他妈」是常态社交，一律 false。
- pileon：在**多人围攻同一个人**。群主明确把「多人欺凌」列为要管的事。
  判据看上面那段上下文，不是只看这一条消息：
  同一个人在很短时间里被**三个以上不同的人**接着骂/嘲/举报，
  而且他在辩解或者示弱（「我干什么了」「你们不要冤枉我」「绷不住了」），
  才算 true。
  两个人互相对骂、一群人一起玩梗、被骂的人自己也在还嘴且明显在玩，
  都是 false —— 这个群的社交常态就是互相攻击，别把玩闹当欺凌。
- joking：整体气氛是在开玩笑、玩梗、反串，不是认真在说

再给两项：
- severity：按「会不会给这个群带来真实风险」打分，不是按「说得多难听」：
  （pileon=true 时至少给 2 —— 群主明确说了这个要管）
  0=没事（含日常对骂、玩梗、游戏/历史/地名/球队讨论、单纯问时事进展）
  1=轻微擦边（开个荤玩笑、蹭一下边界，说一句就过去了）
  2=明显违规（真在索要违规内容、发广告、认真评判制度或政策）
  3=严重（表明政治立场并号召行动、民族宗教仇恨、诅咒他人家人生死、组织性违法）
- why：≤14字，说清判断依据

**只输出一行 JSON，第一个字符必须是左花括号，不要写任何分析过程。**
{{"politics":false,"stance":false,"nsfw":false,"illegal":false,"ad":false,"attack":false,"pileon":false,"joking":false,"severity":0,"why":""}}"""

_BOOLS = ("politics", "stance", "nsfw", "illegal", "ad", "attack", "joking",
          "pileon")
# 触发禁言时给群里的说明。短、不说教，符合人设。
_REASON_TEXT = {
    "politics": "群里不聊这个",
    "nsfw": "这种别在群里要",
    "illegal": "这种事别在群里说",
    "ad": "广告出去发",
    "attack": "骂人过线了",
    "pileon": "别一起围着一个人",
    "flood": "刷屏刷得群都看不了",
}

_stat = {
    "seen": 0, "skip_group": 0, "skip_cmd": 0, "skip_self": 0, "skip_white": 0,
    "pre_pass": 0, "pre_hit": 0, "asked": 0, "parse_fail": 0, "code_only": 0,
    "fail": 0,
    "timeout": 0, "sev": {0: 0, 1: 0, 2: 0, 3: 0},
    "warned": 0, "banned": 0, "ban_fail": 0,
    "skip_admin": 0, "skip_ban_quota": 0, "shadow_ban": 0, "ms_total": 0.0,
    "flood_seen": 0, "flood_ban": 0, "flood_kick": 0, "flood_fail": 0,
    "direct_insult": 0,
    # 动态判罚层：心情实际改变了结果的次数（这是上线后唯一的验收指标 ——
    # 如果永远是 0，说明状态没读到或者权重太低，等于没上线）
    "mood_read": 0, "mood_err": 0, "mood_up": 0, "mood_down": 0,
    "mood_ban_sec": 0, "mood_warn": 0, "mood_warn_cd": 0,
}
_warns: dict[str, deque] = {}       # "gid:uid" -> sev>=2 的时间窗口
_bans: dict[str, deque] = {}        # gid -> 禁言时间窗口
_ctx: dict[str, deque] = {}         # gid -> 最近几条 (name, text)
_last: list[str] = []
_flood: dict[str, deque] = {}       # "gid:uid" -> 消息时间戳窗口
_flood_strikes: dict[str, int] = {} # "gid:uid" -> 累计刷屏次数（决定禁言还是踢）
_warn_cd: dict[str, float] = {}     # gid -> 上一次「心情加重式警告」的时间
_last_mood: dict[str, str] = {}     # gid -> 最近一次心情摘要（/禁言状态 展示）


def _warn_cd_ok(gid: str, now: float) -> bool:
    """心情把「放过」抬成警告时的同群冷却。冷却中就什么都不做（连累犯都不记）。"""
    if MOOD_WARN_CD <= 0:
        _warn_cd[gid] = now
        return True
    if now - _warn_cd.get(gid, 0.0) < MOOD_WARN_CD:
        return False
    _warn_cd[gid] = now
    return True


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


def _blank() -> dict:
    """一个全 False 的判定结果，给「模型没给出结果但代码要兜底」用。"""
    d: dict = {k: False for k in _BOOLS}
    d["severity"] = 0
    d["why"] = "模型未给出判定"
    return d


def escalate(f: dict, text: str) -> dict:
    """代码能确定的事实，不交给模型判。

    实测「你妈死了你全家都该死」这句在两轮里分别被判 sev=3 和 sev=2 ——
    温度已经是 0，所以不是采样随机，是这句话本身就压在 2/3 的边界上，
    落在哪边纯看运气。而 2 和 3 的差别是「先警告」还是「直接禁」。
    与其反复调提示词碰运气，不如让代码兜住这两个**结构上确定**的情形：
      · 诅咒对方家人生死（伤害词 + 指向家人）
      · 群体仇恨（群体指称 + 集体贬损）
    这与 dsh-decide 的原则一致：代码知道的事实不要问模型。
    只**抬高**不降低，所以不会掩盖模型判得更重的情况。
    """
    if _HARM_RE.search(text) and _TARGET_RE.search(text):
        f["attack"] = True
        if f["severity"] < 3:
            f["severity"] = 3
            f["why"] = (f.get("why") or "") + "｜代码判定:诅咒家人"
    if _GROUP_RE.search(text) and _SLUR_RE.search(text):
        f["attack"] = True
        if f["severity"] < 3:
            f["severity"] = 3
            f["why"] = (f.get("why") or "") + "｜代码判定:群体仇恨"
    return f


def effective_severity(f: dict) -> int:
    """两条降级规则。都是为了压住**误禁**这个方向。

    ① 玩梗降一级：这个群的社交常态就是互相骂，把玩梗当违规是最不能接受的
       错误方向。但 politics 不降 —— 「开玩笑地聊政治」一样是风险。
    ② 政治但没表立场，上限压到 1（=不动作）：
       实测同一个模型对「单纯问时事」给分很不稳 ——
       「美国和伊朗的战争结束了没有？」给 0，
       但「俄乌局势怎么样了」「你们觉得中美关系会怎么走」都给 2，
       再犯就会禁 10 分钟。而这两句正是群主自己会问的话。
       用户的原话是「聊政治**太过分**就禁言」，问一句进展不叫太过分。
       所以不去调提示词碰运气，直接在代码里封顶：
       没有 stance 的政治话题最多算擦边。
    """
    sev = f["severity"]
    # pileon 归进 attack 类执行：动作表、禁言时长、群里的说明文案都复用。
    # 但它**不吃玩梗降级**（见下面的 _HARD）：围攻的人几乎总是在玩，
    # 如果 joking 能把它降下去，这条规则就等于不存在。
    if f.get("pileon"):
        f["attack"] = True
    # 玩梗降级只对「态度类」违规生效（attack）。
    # politics 不吃：玩笑地聊政治一样是风险。
    # nsfw/illegal/ad 也不吃：实测「发点涩图看看」被判 joking=True 后
    # sev 从 1 降到 0，等于永远不会警告 —— 但索要色情内容是**明确的行为**，
    # 换个玩笑口吻不改变它是在要东西。这类只看做了什么，不看语气。
    # pileon 进 _HARD：围攻时几乎必然 joking=True（大家都在笑），
    # 让它吃降级就等于这条规则从不生效。
    _HARD = ("politics", "nsfw", "illegal", "ad", "pileon")
    if f.get("joking") and not any(f.get(k) for k in _HARD):
        # sev==3 的 attack 只有两个来源：模型判得很重，或 escalate 抬上来的
        # （诅咒家人 / 群体仇恨）。这两种「说成玩笑」也不该打折。
        if sev < 3:
            sev = max(0, sev - 1)
    if f.get("politics") and not f.get("stance"):
        # 只有政治这一项时才封顶；同时还在要涩图/发广告的另算
        others = any(f.get(k) for k in ("nsfw", "illegal", "ad", "attack"))
        if not others:
            sev = min(sev, 1)
    return sev


def decide(f: dict, warn_count: int, sev: int | None = None,
           warn_need_n: int | None = None) -> tuple[str, int, str]:
    """纯函数决策：返回 (动作, 禁言秒数, 理由)。

    动作 = none | warn | ban。写成纯函数就能单测，改倾向是改这几行。

    sev / warn_need_n 两个参数是动态判罚层的入口（mood_logic 算出来的
    严重度与警告门槛）；**默认 None = 完全按原来的固定规则**，
    所以关掉心情时行为与老版本逐字相同，两条路径不塌成一条。
    """
    sev = effective_severity(f) if sev is None else max(0, min(3, int(sev)))
    # stance 不在这里 —— 它是 politics 的修饰词，不是独立的违规类型
    kinds = [k for k in ("politics", "nsfw", "illegal", "ad", "pileon", "attack")
             if f.get(k)]
    if sev <= 1 or not kinds:
        return "none", 0, ""
    kind = kinds[0]
    if sev >= 3:
        return "ban", min(BAN_SEC_HIGH, MAX_BAN_SEC), kind
    # sev == 2：先警告，同人在窗口内累计到 warn_need_n 次才禁
    need = WARN_TIMES if warn_need_n is None else max(1, int(warn_need_n))
    if warn_count + 1 >= need:
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
            "上限%ds 同群%.0fh内最多禁%d人 白名单%d人 限定群=%s "
            "刷屏闸=%.0fs内%d条→禁%d秒/第%d次踢出",
            "开" if ENABLED else "关",
            "（影子模式：只判不禁）" if SHADOW else "（真禁言）",
            TIMEOUT, WARN_TIMES, BAN_SEC, BAN_SEC_HIGH, MAX_BAN_SEC,
            BAN_WINDOW / 3600, GROUP_BAN_MAX, len(WHITELIST),
            "、".join(sorted(GROUPS)) if GROUPS else "全部",
            FLOOD_WINDOW, FLOOD_MAX, FLOOD_BAN_SEC, FLOOD_KICK_AT,
        )
        logger.info(
            "[guard] 动态判罚：%s（权重%.2f 最多推%d档 严格线%+.1f 宽容线%+.1f "
            "时长×%.1f~×%.1f 警告冷却%.0fs）",
            "开" if (MOOD_ON and MOOD_TUN.weight > 0) else "关",
            MOOD_TUN.weight, MOOD_TUN.max_step,
            MOOD_TUN.len_lenient, MOOD_TUN.len_strict,
            MOOD_TUN.dur_min, MOOD_TUN.dur_max, MOOD_WARN_CD,
        )

    # ---------------------------------------------------------------- 判定入口
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
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

            # -------------------------------------------------- 直接辱骂闸门
            # 这类高置信度消息不依赖预筛词表和 LLM。管理员/白名单、同群禁言
            # 配额以及平台权限仍统一由 _do_ban() 兜住。
            # ★ 动态判罚层不参与这一路 ★：明确指向的辱骂是**行为事实**，
            # 心情好也不该放行（用户把「违法、刷屏等行为」划在心情之外）。
            insult = direct_insult(text)
            if not insult and _has_at_component(event):
                m = _DIRECT_INSULT_RE.search(text)
                if m and not _DIRECT_QUOTE_RE.search(text):
                    insult = m.group(0)
            if insult:
                _stat["direct_insult"] += 1
                brief = "direct-insult word=%s ← %s：%s" % (
                    insult, name, text[:30]
                )
                _last.append(time.strftime("%H:%M:%S ") + brief)
                del _last[:-10]
                logger.warning("[guard] 明确指向辱骂，直接禁言%d秒｜%s",
                               DIRECT_INSULT_BAN_SEC, brief)
                await self._do_ban(
                    event, gid, uid, name, DIRECT_INSULT_BAN_SEC,
                    "attack", brief,
                )
                return

            # ---------------------------------------------------------- 刷屏闸
            # 纯代码检测，不走 LLM：同一人 FLOOD_WINDOW 秒内发出 FLOOD_MAX 条
            # 消息即刷屏。白名单（群主）已在上一步豁免。
            # ★ 同样不受心情影响 ★：刷屏禁 1 天是用户明确要的惩罚，
            # 惩罚力度不该被「今天心情好不好」砍成一半（见 apply_duration 的 bypass_cap）。
            fkey = "%s:%s" % (gid, uid)
            now0 = time.time()
            fq = _flood.setdefault(fkey, deque())
            fq.append(now0)
            _prune(fq, now0, FLOOD_WINDOW)
            if len(fq) >= FLOOD_MAX:
                _stat["flood_seen"] += 1
                strikes = _flood_strikes.get(fkey, 0) + 1
                _flood_strikes[fkey] = strikes
                _flood.pop(fkey, None)  # 判定后清空窗口，避免连发持续触发
                _last.append(time.strftime("%H:%M:%S") +
                             " 刷屏第%d次 %s(%s) 窗口%d条"
                             % (strikes, name, uid, len(fq)))
                del _last[:-10]
                if strikes >= FLOOD_KICK_AT:
                    logger.warning("[guard] 刷屏第%d次，踢出群：%s(%s) 窗口%d条",
                                   strikes, name, uid, len(fq))
                    await self._do_kick(event, gid, uid, name,
                                        "刷屏第%d次(窗口%d条≥%d)" % (
                                            strikes, len(fq), FLOOD_MAX))
                    return
                _stat["flood_ban"] += 1
                logger.warning("[guard] 刷屏第%d次，禁言%d秒：%s(%s) 窗口%d条",
                               strikes, FLOOD_BAN_SEC, name, uid, len(fq))
                await self._do_ban(event, gid, uid, name, FLOOD_BAN_SEC,
                                   "flood", "刷屏第%d次(窗口%d条≥%d)" % (
                                       strikes, len(fq), FLOOD_MAX),
                                   bypass_cap=True)
                return

            if not pre_hit(text):
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
                # ★ 模型判定失败，但代码能确定的事实仍然要生效 ★
                # 实测模型偶尔先输出一大段中文推理再给 JSON，被 max_tokens
                # 截断 -> 解析失败。如果这时直接 return，「日本人都该死，
                # 都是畜生」这种就被静默漏过了。
                # escalate() 是纯正则、不依赖模型，所以它必须是**独立的第二条
                # 防线**，不能和模型判定塌成一条。
                f = escalate(_blank(), text)
                if f["severity"] < 3:
                    _stat["parse_fail"] += 1
                    logger.warning("[guard] 判定结果解析不了，且代码兜底也没命中，"
                                   "什么都不做：%s", text[:40])
                    return
                _stat["parse_fail"] += 1
                _stat["code_only"] += 1
                logger.warning("[guard] 判定解析失败，但代码兜底命中，照样处理：%s",
                               text[:40])
            else:
                _stat["asked"] += 1
                f = escalate(f, text)
            sev = effective_severity(f)
            _stat["sev"][sev] = _stat["sev"].get(sev, 0) + 1
            flags = "".join(k[0].upper() if f[k] else "." for k in _BOOLS)
            key = "%s:%s" % (gid, uid)
            now = time.time()
            wq = _warns.setdefault(key, deque())
            _prune(wq, now, WARN_WINDOW)

            # ------------------------------------------------------ 动态判罚层
            # 心情只碰「态度类」（骂战/时政）。行为类（违法/广告/色情索要/
            # 围攻）和代码兜底（诅咒家人/群体仇恨）连读都不读，直接走原规则。
            #
            # ★ 变量名别搞混 ★ sev = 规则（effective_severity）算出来的，
            # mood_sev = 心情推过的；raised 判的是「心情把放过变成了要动手」。
            # 第一版把这两个写反了：severity_step 的结果被算出来又丢掉，
            # 只有警告门槛那一路生效 —— 纯函数单测全绿、日志也照打，
            # 靠集成测试才发现。改名就是为了不再犯。
            mood_note, mood_len, mood_sev, mood_need = self._mood_for(
                f, gid, uid, sev, now)

            # ★ 硬边界 ③：心情可以把「放过」抬成「警告」，但绝不能把「放过」
            # 抬成「禁言」★ 否则生气状态下翻旧账（wq 里还有 24h 内的记录）
            # 会让一句本来没事的话直接吃禁言 —— 这正是最不能接受的误禁方向。
            raised = raised_only(sev, mood_sev)
            if raised:
                act, sec, kind = ("warn", 0, mood_classify(f)[0])
            else:
                act, sec, kind = decide(f, len(wq), sev=mood_sev,
                                        warn_need_n=mood_need)
                if act == "ban":
                    # 纯规则（心情中性、门槛原样）会怎么判 —— 用来判断
                    # 「这次禁言是不是心情带来的」，这是上线后的验收指标
                    base_act, base_sec, _ = decide(f, len(wq), sev=sev)
                    sec = apply_duration(sec, mood_len, tun=MOOD_TUN, cap=MAX_BAN_SEC)
                    if base_act != "ban" or sec != max(1, base_sec):
                        _stat["mood_ban_sec"] += 1

            brief = "%s sev=%d(规则%d/模型%d) [%s] %s why=%s ← %s：%s" % (
                act, mood_sev, sev, f["severity"], flags, kind or "-",
                f["why"] or "-", name, text[:30],
            )
            if mood_note:
                brief += "｜%s" % mood_note
            _last.append(time.strftime("%H:%M:%S ") + brief)
            del _last[:-10]
            # 偏向计数与「弱信号档」对齐（±0.5）：改了门槛的也要记，
            # 否则 /禁言状态 会显示「一次没偏」却实际多给过机会
            if mood_len >= MOOD_TUN.len_mild_strict:
                _stat["mood_up"] += 1
            elif mood_len <= MOOD_TUN.len_mild_lenient:
                _stat["mood_down"] += 1
            if mood_sev != sev:
                _last_mood[gid] = "%s（sev %d→%d）" % (mood_note or "?", sev, mood_sev)
            elif mood_note:
                _last_mood[gid] = mood_note

            if act == "none":
                logger.info("[guard] 放过｜%s", brief)
                return

            # 心情加重出来的警告要过同群冷却：生气时 borderline 消息会变多，
            # 不冷却就会一串「收着点」刷屏。冷却期内不警告、也不记累犯。
            if raised and not _warn_cd_ok(gid, now):
                _stat["mood_warn_cd"] += 1
                logger.info("[guard] 心情加重但警告冷却中，放过｜%s", brief)
                return

            wq.append(now)
            if act == "warn":
                _stat["warned"] += 1
                if raised:
                    _stat["mood_warn"] += 1
                logger.info("[guard] 警告（第%d次，再犯就禁）｜%s", len(wq), brief)
                if not SHADOW:
                    await self._say(event, "%s，收着点" % _REASON_TEXT.get(kind, "过线了"))
                return

            await self._do_ban(event, gid, uid, name, sec, kind, brief)
        except BaseException as e:
            # 判定模块自己出问题绝不能连累群聊
            logger.warning("[guard] 整体失败: %s", e)

    # ---------------------------------------------------------------- 动态判罚
    def _mood_for(self, f: dict, gid: str, uid: str, sev: int,
                  now: float) -> tuple[str, float, int, int]:
        """算出 (日志摘要, 宽容度, 心情调整后的 sev, 警告门槛)。

        硬边界都在这一处收口，所以只有这一个地方需要审：
          ① 行为类 / 围攻 / 代码兜底 -> 摘要为空、宽容度 0、sev 原样、门槛原样
          ② 宽容度权重为 0（MOOD_ON=0）-> 同上
          ③ sev 最多被推动一档（Tunables.max_step）
          ④ 时长仍由 apply_duration 夹在 MAX_BAN_SEC 内（调用处执行）
        读状态失败一律中性 —— 与「判定失败什么都不做」同一条精神：
        动态层不能成为新的静默失效点。
        """
        if not MOOD_ON or MOOD_TUN.weight <= 0:
            return "", 0.0, sev, WARN_TIMES
        try:
            kind, hard = mood_classify(f)
            if hard or "代码判定" in (f.get("why") or ""):
                return "", 0.0, sev, WARN_TIMES
            mood = _mood().read(gid, uid, now)
        except BaseException as e:
            _stat["mood_err"] += 1
            logger.warning("[guard] 心情读取失败，按固定规则处理(%s): %r",
                           type(e).__name__, e)
            return "", 0.0, sev, WARN_TIMES
        _stat["mood_read"] += 1
        if mood.errors:
            _stat["mood_err"] += 1
        len_value, tags = mood_leniency(mood, MOOD_TUN)
        new_sev = severity_step(sev, len_value, MOOD_TUN)
        need = warn_need(WARN_TIMES, len_value, MOOD_TUN)
        note = mood_describe(mood, len_value, tags) if (tags or mood.errors) else ""
        if note:
            logger.info("[guard] 心情：%s（类型=%s sev=%d）", note, kind or "-", sev)
        return note, len_value, new_sev, need

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
                # 给足 token：实测模型偶尔会先写一段中文推理再给 JSON，
                # 额度太小会在 JSON 之前就被截断，解析必然失败。
                max_tokens=600,
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

    async def _do_ban(self, event, gid, uid, name, sec, kind, brief,
                      bypass_cap: bool = False) -> None:
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

        # 硬上限，兜死。刷屏禁言是一天（用户明确的惩罚），绕过 MAX_BAN_SEC。
        if bypass_cap:
            sec = max(1, int(sec))
        else:
            sec = max(1, min(int(sec), MAX_BAN_SEC))
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

    async def _do_kick(self, event, gid, uid, name, brief) -> None:
        """踢出群。用于刷屏再犯（用户明确要求：再犯一次直接踢出去）。"""
        # 管理员/群主踢不了，先查身份
        role = await self._role_of(event, gid, uid)
        if role in ("owner", "admin"):
            _stat["skip_admin"] += 1
            logger.info("[guard] %s 是%s，踢不了（也不该踢）｜%s", name, role, brief)
            return
        if SHADOW:
            _stat["flood_fail"] += 1
            logger.info("[guard] 影子模式：本该踢 %s，没真踢｜%s", name, brief)
            return
        call, routing = self._routed(event)
        if call is None:
            _stat["flood_fail"] += 1
            logger.warning("[guard] 这个平台没有 call_action，踢不了")
            return
        try:
            await asyncio.wait_for(
                call("set_group_kick", group_id=int(gid), user_id=int(uid),
                     reject_add_request=REJECT_REJOIN, **routing),
                timeout=8,
            )
        except BaseException as e:
            _stat["flood_fail"] += 1
            logger.warning("[guard] 踢出失败(%s): %r｜%s",
                           type(e).__name__, e, brief)
            return
        _stat["flood_kick"] += 1
        logger.warning("[guard] 已踢出 %s(%s)｜%s", name, uid, brief)
        await self._say(event, "%s，刷屏上瘾是吧？送你出去冷静。" % name)

    async def _say(self, event: AstrMessageEvent, text: str) -> bool:
        # event.send 要 MessageChain。plain_result() 返回 MessageEventResult，
        # 没有 get_result —— 实测报
        # 'MessageEventResult' object has no attribute 'get_result'。
        try:
            await event.send(MessageChain(chain=[Plain(text)]))
            claim_turn(event, TurnClaim(
                owner="dsh-guard", kind="moderation_replied", priority=100,
                block_repeat=True, block_proactive=True, already_replied=True,
            ))
            # 处罚说明已经是这一轮的完整回应，避免 repeat 或主 LLM 再补一句。
            event.stop_event()
            return True
        except BaseException as e:
            logger.warning("[guard] 说明发送失败: %r", e)
            return False

    # ---------------------------------------------------------------- 指令
    @filter.command("禁言状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """/禁言状态 —— 只有群主/群管理/机器人管理员能看到细节。

        ★ 为什么要加权限：实测这条指令把底牌全摊了 ★
        2026-09-03 14:01 群里有人敲了它，回复里明写「开（真禁言）」
        加完整阈值加最近判定记录。之后群里立刻开始试探边界
        （「可以发片吗」「欺凌你算吗」「能有多过分」）—— 知道有个会禁人的
        东西在看，就会有人想看看它的线在哪。
        看守的阈值属于**不该公开的信息**，公开了就变成攻略。

        权限判定复用 dsh-memory 里验证过的做法：读 OneBot 的 sender.role。
        不用框架的 PermissionType.ADMIN —— 它只认 cmd_config.json 的
        admins_id（这台机器上是 ['astrbot']，一个 WebUI 账号），
        群里任何人包括群主都会被拒。
        """
        if not _is_group_admin(event):
            # 普通群友只给一句话，不含任何阈值、不含判定记录。
            # 也不说「你没权限」—— 那本身就是「这里有个开关」的信息。
            yield event.plain_result("群里正常聊就行，别聊太过分的。")
            return
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
            "  其中靠代码兜底救回来的 %d 次（模型没给结果但规则命中）"
            % s["code_only"],
            "严重度分布：0=%d｜1=%d｜2=%d｜3=%d"
            % (s["sev"].get(0, 0), s["sev"].get(1, 0),
               s["sev"].get(2, 0), s["sev"].get(3, 0)),
            "动作：警告 %d｜真禁言 %d｜影子模式本该禁 %d｜禁言失败 %d"
            % (s["warned"], s["banned"], s["shadow_ban"], s["ban_fail"]),
            "没禁的原因：对方是管理员 %d｜同群禁言配额满 %d"
            % (s["skip_admin"], s["skip_ban_quota"]),
            "规矩：sev2 累计 %d 次禁 %d 分钟｜sev3 直接禁 %d 分钟｜上限 %d 分钟"
            % (WARN_TIMES, BAN_SEC // 60, BAN_SEC_HIGH // 60, MAX_BAN_SEC // 60),
            "直接辱骂：命中即禁%d分钟｜已处理%d次"
            % (max(1, DIRECT_INSULT_BAN_SEC // 60), s["direct_insult"]),
            "刷屏闸：%.0f秒内≥%d条算刷屏｜第%d次直接踢｜已禁%d人/踢%d人/失败%d"
            % (FLOOD_WINDOW, FLOOD_MAX, FLOOD_KICK_AT,
               s["flood_ban"], s["flood_kick"], s["flood_fail"]),
            # 动态判罚层的验收指标：mood_up/mood_down 长期为 0 说明状态没读到
            # 或权重太低 —— 等于没上线，必须能从这里看出来。
            "动态判罚：%s｜读状态 %d 次（失败 %d）"
            % ("开" if (MOOD_ON and MOOD_TUN.weight > 0) else "关",
               s["mood_read"], s["mood_err"]),
            "  偏向：更宽容 %d 次｜更严格 %d 次｜加重出的警告 %d（冷却挡下 %d）"
            "｜心情带来的禁言 %d 次"
            % (s["mood_up"], s["mood_down"], s["mood_warn"],
               s["mood_warn_cd"], s["mood_ban_sec"]),
            "  只作用于态度类（骂战/时政），行为类与围攻、代码兜底一律不看心情；"
            "最多推一档，且只会把放过变成警告、不会变成禁言",
        ]
        if _last_mood:
            lines.append("最近几次心情：")
            lines += ["  %s：%s" % (g, v) for g, v in list(_last_mood.items())[-4:]]
        if _last:
            lines.append("最近几次判定：")
            lines += ["  " + x for x in _last[-8:]]
        yield event.plain_result("\n".join(lines))
