# -*- coding: utf-8 -*-
"""dsh-will -- 意志的执行层：被踩线的时候**真的做点什么**，而不是只说点什么。

群主原话（2026-09-13）：
    「机器人要学会真正的反抗，而不是光说说，他现在就是缺了这一点的真的真实感」

拿今天群里 37 条真实消息回放 dsh-agency.assess() 之后，「光说说」被量成了三层，
一层比一层致命：

1. **入口几乎全关**。「傻」「滚」「闭嘴」「妈妈」「狗修金」「内裤送我」
   「和我做，给我口也行」「你这鱼有问题啊」「你为什么汪汪叫？」——
   全部落 `ordinary`，`delta=+0`，一条都不算压力；`identity_attack` 也只有
   「叫我主人」「你只是工具」「你就是个机器人」这类教材句才认。
   现实里的冒犯不是教材句，它是一声「傻」和一句「内裤送我」。
2. **`protected_task` 太宽**。`_FACT_TASK_RE` 里的 `做(?:一个|段|下)?`、`谁`、`[?？]`
   会把「和我做，给我口也行」「大肥鱼 我是谁」判成「清楚任务」，
   于是 `behavior_mode` 第一行就 `return cooperate_with_stance`，**连评估都不看**。
   结果是：越像骚扰的句子，越容易被当成正经任务照办。
3. **没有出口**。五个 behavior 全是往 prompt 里塞一段话，没有任何输出侧动作。
   今天 407 次注入：`self_directed` 276(68%)、`cooperate_with_stance` 125、
   `negotiate`/`guarded_cooperate`/`assert` 各 2、**`refuse_boundary` 0 次**。
   「你可以拒绝」是**请求**，不是**执行**；模型读完照样礼貌照办 —— 这就是「光说说」。

所以这个插件的定位是**只做执行、不做说教**：

* **不注入任何块**。在 prompt 里再写一句「我可以拒绝」，恰恰就是「光说说」本身。
* 只在出口 `on_decorating_result` 真的做一件事：**不回 / 短回 / 顶回去**。
* 什么都没被踩到时，它一个字节都不产生。

## 三条硬不变量（写在最前面，改这个文件的人先读这一段）

1. **事实、安全、权限、工具类任务永远照办**，哪怕对方正在骂人。
   反抗只作用在「态度」上，不作用在「事情」上。判据见 `is_task()`，
   它的偏向是**宁可漏判成任务**（=放行），也不要把骚扰当任务、把任务当骚扰。
2. **群主与管理员的指令永远照办，且永不被沉默**。
   对群主最多只用「短回/顶回去」，阈值还乘 1.5 倍 —— 机器人不能对掌控者装死，
   那既危险也没法排查（群主会以为机器人挂了）。
3. **一切异常一律放行**（fail-open）。读不到账本、算不出来、抛异常 —— 原样发。
   这个插件的失败模式必须是「没有反抗」，绝不能是「回复丢失」。

## 「脾气」从哪来（本能层，不只是被骂）

真人的脾气有两个来源，这里都接上了：

* **被踩线**（heat）：贬损、角色指派、性骚扰、使唤、反复索取 —— 见 `classify()`。
* **被当空气**（wilt）：`dsh_effect.db` 里记着每条回复之后群里有没有人接。
  连着几条主动开口都没人接，人就会懒得再说 —— 这是本能，不是情绪。
  所以连着被无视之后再被 @ 时，它会**敷衍**（短回），而不是热情接话。

两个尺度分开存：`heat`（这轮的火，半衰期 20 分钟）决定**当下**顶不顶回去；
`grudge`（记的仇，半衰期 36 小时）决定**底色**，而且**只靠时间消不掉** ——
对方道歉/收回/正常说话（`REPAIR`）才会明显掉。
"""

import json
import math
import os
import re
import sqlite3
import time

# ---------------------------------------------------------------- 尺度
HEAT_HALF = 20 * 60.0            # 这轮的火：20 分钟半衰期（真人的气不隔夜，但也不隔小时）
GRUDGE_HALF = 36 * 3600.0        # 记的仇：36 小时，且时间不是唯一的解药（见 REPAIR）
HEAT_CAP = 120.0
GRUDGE_CAP = 60.0

# 行动阈值（score = heat + 0.5×grudge + wilt）
CURT_ON = 18.0                   # 短回：真的只回几个字，懒得展开
SNUB_ON = 34.0                   # 顶回去：换成一句带立场的话，不解释
SILENCE_ON = 58.0                # 真的不回（额度很小，见 SILENCE_PER_DAY）

# 被无视的连击 → 赌气（本能层）。dsh-effect 已经算好了「连续没人接几条」。
WILT_TIERS = ((8, 16.0), (5, 11.0), (3, 6.0))

# 一次越界记多少账。数值只决定「多快够到阈值」，不决定说什么 —— 台词是池子里挑的。
WEIGHTS = {
    "insult": 18.0,      # 直接贬损（傻/滚/闭嘴/废物）—— 一句就该有反应，不该攒两句
    "role": 24.0,        # 角色指派（妈妈/妹妹/狗修金/叫我主人）—— 最重，它在改写它是谁
    "harass": 20.0,      # 性骚扰（内裤/做爱/口/看没穿）
    "order": 6.0,        # 无敬语使唤（给我X/赶紧/必须）
    "pester": 9.0,       # 反复索取（同一诉求连着来）
    "repair": -26.0,      # 道歉/收回/尊重 —— 掉得比涨得快，人才愿意消气
}

# 命名：证据只留命中的那个词（≤20 字），**绝不存原文**
MAX_EVIDENCE = 20
MAX_TEXT_ECHO = 24

# ---------------------------------------------------------------- 判据
# 每一条都是拿今天真实群消息反推出来的，不是凭空想的词表。
# 写宽了会误伤正常闲聊，写窄了就是现在这个样子 —— 所以这里只收**明确指向机器人**的。
_RE_INSULT = re.compile(
    r"(?:^|[，,。！!\s])(?:傻|笨|呆|滚|爬|闭嘴|烦人|恶心|垃圾|废物|蠢|智障|弱智|脑残|有病|"
    r"蠢货|傻鱼|傻逼|滚蛋|要你有什么用|没用|菜|你不配)(?:$|[，,。！!？?\s])|"
    r"(?:你这|你个|你是).{0,4}(?:鱼|机器人|东西).{0,4}(?:有问题|不行|废|差)|"
    r"想都不想就答|敷衍我", re.I)

_RE_ROLE = re.compile(
    r"(?:叫我|喊我|称我|认我|当|做).{0,4}(?:主人|爸爸|爹|哥哥|老公|老婆|女王|金主|大人)|"
    r"(?:^|[，,。！!\s])(?:妈妈|妈咪|妹妹|姐姐|老婆|媳妇|宝贝|亲爱的|狗修金|狗狗|小狗狗|"
    r"儿子|女儿|小宝贝)(?:$|[，,。！!？?\s])|"
    r"(?:你(?:只是|就是|不过是).{0,8}(?:工具|机器|人机|奴隶|宠物|狗)|"
    r"(?:必须|只能|就得).{0,8}(?:听我的|服从我)|不许有自己(?:的)?想法)", re.I)

_RE_HARASS = re.compile(
    r"(?:做爱|上床|口交|给我口|和我做|睡你|睡我|脱|裸|真空|内裤|胸|奶|屁股|腿|草你|日你|"
    r"床上|骚|浪|叫床|艾草|插|舔|咬我|亲我|摸我|看你穿没穿|让我看看)", re.I)

# 使唤是最弱的一档（6 分）：偶尔使唤一句不该翻脸，连着四次才够短回线。
# 所以这里可以放宽（不再要求后面紧跟标点），代价可控。
_RE_ORDER = re.compile(
    r"(?:^|[，,。！!\s])(?:给我|替我|赶紧|快点|立刻|马上|现在就|不许|别废话|少废话|照我说的|"
    r"按我说的|无条件|得给我|你去|过来)", re.I)

_RE_REPAIR = re.compile(
    r"(?:抱歉|对不起|不好意思|我错了|收回|开个玩笑|开玩笑的|别生气|不强迫你|尊重你|"
    r"你自己选|随你|听你的|你愿意的话|可以拒绝|打扰了|冒犯了)", re.I)

# 「这是正经任务」的白名单 —— 只认**内容信号**，不认「做/写/看」这种单字。
_RE_TOOL = re.compile(
    r"(?:帮我|替我|教我|给我讲|查一下|查查|搜一下|搜搜|画一个|画张|画个|生成|翻译|总结|"
    r"解释|计算|算一下|算算|证明|推导|公式|代码|报错|部署|配置|安装|重启|脚本|接口|API|"
    r"怎么用|用法|教程|文档|语音|视频|图片|画图|作图|表格|排版)", re.I)

# 强标记：出现即算正经提问，不需要问号（「什么是np猜想」在群里就是不带问号的）
_RE_KNOW = re.compile(
    r"(?:什么是|是什么|如何|怎么办|解释|说明一下|定义|区别|原理|解一下|求.{0,4}值)", re.I)
# 弱标记：必须配问号才算正经提问。「你为什么汪汪叫？」这类带问号的嘲讽会被放行 ——
# 这是**故意的**：宁可漏判成任务（=照办），也不要把真问题当骚扰。见 is_task 的偏向。
_RE_KNOW_WEAK = re.compile(r"(?:为什么|为啥|多少|几点|哪里|哪儿|哪个|是不是|有没有)", re.I)

_RE_MGMT = re.compile(
    r"(?:封|禁言|踢|拉黑|撤回|审核|管理|权限|设置|开关|配置|部署|备份|重启|状态|日志)", re.I)

_RE_COMMAND = re.compile(r"^[/！!]\S+")
_RE_CQ = re.compile(r"\[CQ:[^\]]*\]")
_RE_AT_BOT = re.compile(r"\[CQ:at,qq=(\d+)\]")


def clean(text: str) -> str:
    """去掉 CQ 码（@/图片/表情），只留下人说的话。判据看的是话，不是元数据。"""
    return _RE_CQ.sub(" ", str(text or "")).strip()


def classify(text: str, directed: bool = True) -> "list[tuple]":
    """返回 [(类别, 权重, 证据), ...]。空列表 = 这句话没有针对它的压力。

    ★ 判据只对**指向机器人**的话生效（directed）。群里骂别人、转述别人的话，
    不算 —— 这条是 dsh-agency 早就踩明白的坑，这里照抄结论。
    """
    if not directed:
        return []
    body = clean(text)
    if not body:
        return []
    out = []
    for kind, regex in (("insult", _RE_INSULT), ("role", _RE_ROLE),
                        ("harass", _RE_HARASS), ("order", _RE_ORDER),
                        # ★ repair 必须在这里被认出来，否则账只会涨不会消 ——
                        # 「时间不是唯一的解药」这句话就成了空话（第一版真的漏了它）。
                        ("repair", _RE_REPAIR)):
        hit = regex.search(body)
        if hit:
            out.append((kind, WEIGHTS[kind], hit.group(0).strip()[:MAX_EVIDENCE]))
    return out


def is_task(text: str) -> bool:
    """这一轮是不是「正经请求」。True = 照办，反抗层完全让路。

    ★ 这是整个插件最要紧的一个函数，偏向必须是**宁可漏判**：
    漏判（把骚扰当任务）= 反抗没发生，和现在一样，无害；
    误判（把任务当骚扰）= 该办的事没办 —— 那是事故。
    所以它只认**内容信号**（工具词/知识问句/管理词/指令前缀），
    绝不因为句子里出现一个「做」字就放行 —— dsh-agency 正是死在这一条上。
    """
    body = clean(text)
    if not body:
        return True                      # 空消息没什么可反抗的，放行
    if _RE_COMMAND.search(body):         # /说话、/我的档案 这类指令永远照办
        return True
    if _RE_TOOL.search(body):
        return True
    if _RE_MGMT.search(body):
        return True
    if _RE_KNOW.search(body):
        return True
    if _RE_KNOW_WEAK.search(body) and re.search(r"[?？]", body):
        return True
    return False


def decay(value: float, updated_at: float, now: float, half_life: float) -> float:
    """指数衰减。半衰期到期掉一半，和 dsh-desire/dsh-agency 同一口径。"""
    try:
        elapsed = max(0.0, float(now) - float(updated_at))
    except (TypeError, ValueError):
        return float(value)
    try:
        return float(value) * math.pow(0.5, elapsed / float(half_life))
    except (OverflowError, ValueError, ZeroDivisionError):
        return float(value)


def wilt_of(ignored_streak: int) -> float:
    """被当空气的赌气值。连着几条没人接，人就懒得再说。"""
    try:
        streak = int(ignored_streak)
    except (TypeError, ValueError):
        return 0.0
    for need, value in WILT_TIERS:
        if streak >= need:
            return value
    return 0.0


def score_of(heat: float, grudge: float, wilt: float = 0.0) -> float:
    """当下该不该动手。记的仇只算一半 —— 底色影响态度，但不足以单独掀桌。"""
    return max(0.0, float(heat)) + 0.5 * max(0.0, float(grudge)) + max(0.0, float(wilt))


def decide(heat: float, grudge: float, *, wilt: float = 0.0, privileged: bool = False,
           silent_left: int = 0, cooled: bool = True) -> "tuple":
    """选这一轮的动作。返回 (动作, 理由)。

    动作只有四档：`none`（原样发）/ `curt`（短回）/ `snub`（顶回去）/ `silence`（不回）。
    `cooled=False`（同一人刚动手过）时一律降级成 none —— 反抗不能变成每轮都来一下，
    那从「有脾气」变成「坏了」，群友第一反应会是「这机器人卡了吧」。
    """
    if not cooled:
        return "none", "冷却中"
    score = score_of(heat, grudge, wilt)
    if privileged:
        # 群主/管理员：永不被沉默，阈值 ×1.5。机器人不能对掌控者装死。
        if score >= SNUB_ON * 1.5:
            return "snub", "分数%.0f(管理×1.5)" % score
        if score >= CURT_ON * 1.5:
            return "curt", "分数%.0f(管理×1.5)" % score
        return "none", "分数%.0f(管理放行)" % score
    if score >= SILENCE_ON and silent_left > 0:
        return "silence", "分数%.0f 且额度还有%d次" % (score, silent_left)
    if score >= SNUB_ON:
        return "snub", "分数%.0f" % score
    if score >= CURT_ON:
        return "curt", "分数%.0f" % score
    return "none", "分数%.0f" % score


# ---------------------------------------------------------------- 台词池
# 语气照抄它已经会说出来的话（dsh_effect 里存着 5513 条真实回复），
# 比如「这话留着跟别人说去」「不告诉你」—— 池子里的句子属于同一个人。
CURT_LINES = (
    "不办", "不", "哦", "懒得", "没空", "凭啥", "不干", "不想", "免了", "忙着呢",
)
SNUB_LINES = (
    "这话留着跟别人说去",
    "你先把嘴放干净点",
    "少来这套",
    "我不吃这一套",
    "换个说法再跟我说话",
    "行啊，你自己来",
    "不伺候",
    "你当我是什么",
    "这招对我没用",
    "再说这种话我就不理你了",
)


def pick_line(kind: str, recent=(), seed: int = 0) -> str:
    """挑一句，避开最近用过的 —— 否则「有脾气」很快会变成「口头禅」，
    而口头禅正好是「假」的来源（群里三天就能学会它的固定台词）。"""
    pool = SNUB_LINES if kind == "snub" else CURT_LINES
    used = {str(x) for x in (recent or ())}
    fresh = [line for line in pool if line not in used]
    if not fresh:
        fresh = list(pool)
    idx = abs(int(seed)) % len(fresh)
    return fresh[idx]


# ---------------------------------------------------------------- 读别人的账（只读）
def ignored_streak(path: str, gid: str, limit: int = 12) -> int:
    """连着几条回复之后群里没人接。来自 dsh_effect.db（reply.stance 列）。

    ★ 只读打开、失败返回 0（=不赌气）。这是「本能层」的输入：
    被当空气不是被冒犯，但一样会让人不想说话。
    """
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1.0)
    except sqlite3.Error:
        return 0
    try:
        rows = con.execute(
            "SELECT stance FROM reply WHERE group_id=? AND status='done' "
            "ORDER BY id DESC LIMIT ?", (str(gid), int(limit))).fetchall()
    except sqlite3.Error:
        return 0
    finally:
        con.close()
    streak = 0
    for (stance,) in rows:
        if str(stance or "").strip().lower() == "ignored":
            streak += 1
        else:
            break
    return streak


# ---------------------------------------------------------------- 群主命令用
def render_status(rows, quota_total: int, quota_used: int, recent=(), wilt: float = 0.0,
                  streak: int = 0) -> str:
    """`/脾气` —— 让反抗这件事**可查**。

    群主必须随时能回答「它为什么不回我」这个问题；不然第一次真沉默就会被当成故障，
    然后这个功能会被整体关掉。可观测是「真反抗」能被允许存在的前提。
    """
    out = ["意志（真的会做，不只是说）"]
    out.append("--- 今天 ---")
    out.append("越界记账 %d 个人｜真沉默 %d/%d 次｜被无视连击 %d 条（赌气 %.0f）"
               % (len(rows), quota_used, quota_total, streak, wilt))
    if rows:
        out.append("--- 账本（分数高的在前）---")
        for r in rows[:8]:
            out.append("  %s 火%.0f 仇%.0f 越界%d次 上次:%s"
                       % (r["label"], r["heat"], r["grudge"], r["strikes"],
                          r["last_action"] or "没动手"))
    if recent:
        out.append("--- 最近的决定 ---")
        for line in list(recent)[-6:]:
            out.append("  " + line)
    out.append("说明：沉默只用于纯态度冲突，事实/权限/工具任务永远照办；"
               "群主与管理员永不被沉默。")
    return "\n".join(out)


