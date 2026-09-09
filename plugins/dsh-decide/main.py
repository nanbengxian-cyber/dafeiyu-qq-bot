# dsh-decide —— 「先想清楚再开口」的决策步插件（多步思考）。
#
# ===========================================================================
# 一、要解决的问题
#
# 框架的主动插话是**纯掷骰子**：
#   builtin_stars/astrbot/group_chat_context.py need_active_reply()
#     -> random.random() < possibility_reply
# 它完全不看「这句话值不值得接」。active_reply.method 也只有一个可选值
# possibility_reply，没有「让模型判断」这一档。
# 实测后果：机器人占了全群 56% 的发言量，两个人在说正事它也一头撞进去。
#
# 二、为什么能省钱：在主模型开口前掐掉整次调用
#
# OnLLMRequestEvent 在 agent 真正开跑之前触发
# （agent_sub_stages/internal.py:269）：
#     if await call_event_hook(event, EventType.OnLLMRequestEvent, req):
#         return
# 而 call_event_hook 的返回值就是 event.is_stopped()
# （pipeline/context_utils.py:105-112）。插件在这个钩子里 event.stop_event()
# 就能把**整次主模型调用**掐掉，一个 token 都不花。
#   主调用 ≈ 9390 input / 118 output
#   判断步 ≈ 381 input / 61 output   （实测中位）
# 判断步约等于主调用的 4%。所以判沉默率只要超过 ~4%，这套多步思考净省钱。
#
# ===========================================================================
# 三、关键设计：模型只做**感知**，决策写在代码里
#
# 前四版都让模型直接输出「沉默/回话」这个结论，全部失败：
#   v1 结论式，沉默条件写在前面 -> 判沉默太黏，纯玩梗也闭嘴（真实窗口也沉默）
#   v2 加一句「拿不准就选回话」 -> 判沉默率 0%，诉苦、谈正事都要插嘴
#   v3 改成两步优先级          -> 判沉默率 62%，但玩梗/话头/吐槽全判哑（4/11 错）
# 同一个模型、同一批场景，只改措辞就在 0% 和 62% 之间乱跳 —— 说明「结论」这个
# 输出形状本身不稳，继续调提示词是白费功夫。
#
# 但**感知**是稳的：四版里「有人在诉苦」「有人说了别插话」「这句只是应答」
# 「两人在对具体安排一来一回」都判得准，翻来覆去错的只有「所以到底该不该说」。
# 于是 v4/v5 让模型只回答它擅长的那几个事实（布尔量），阈值和组合逻辑写进代码。
#
# v4 要求「必须感知到 banter/open/about_bot 才放行」，实测 9/12，错的三个全是
# 旗标一片空白的单行短消息（"你们都几点睡"、"这鱼今天怎么这么安静"）—— 模型对
# 只有一行的输入设不出 open/about_bot。
# v5 改成 **veto-only**：只有否决信号才闭嘴，其余一律开口 -> 11/12。
# 这与 dsh-imagegen 的教训同形：主证据是群里发生了什么，模型的意见只用来否决。
#
# 实测（deepseek-v4-flash-0731，12 个标注场景 + 18 个真实随机窗口）
#   判对 11/12（唯一错例见下方 _ASK_RE 那段，已用代码补掉）
#   延迟中位 2.2s，全部落在插话路径上，被 @ 的人感觉不到
#   真实窗口判沉默率 11% —— 这个群大部分时间真的在玩梗，11% 是对的
#   => 要维持实际开口 ≈25%，possibility_reply 设 0.28
#
# 四、三条纯代码的判断（不问模型，因为代码知道得更准）
#   1. 明确点名要能力（画/语音/视频/搜）-> 绝不沉默，且**跳过判断**直接放行。
#      v5 唯一的错例就是把「发个语音说群主是懒猪」判成「两人在谈具体安排」而闭嘴。
#      用户明确要的东西被路由否决，这是最不能接受的一类错 —— 和 dsh-imagegen
#      「用户请求是主证据，模型回复只用来否决」同一条原则。
#   2. 刚说过话就先闭嘴（MIN_GAP 秒）。「自言自语」这件事代码百分百知道，
#      问模型只会引入噪声。这也是压 56% 发言占比最直接的一根杠杆。
#   3. avoid 字段里出现「别画/别搜/别发」一律丢掉。v1 实测吐出过 "别真画" ——
#      路由的内容意见会被当成硬约束注入，直接把用户要的能力否掉。
#
# 五、失败一律 fail-open（照旧说话）
#   渠道抽风、超时、解析失败全都当「没判断」，正常往下走。
#   宁可多说一句，不能因为判断服务挂了就整个哑掉（和 QQ 代理守卫同一立场：
#   黑洞比被踢更糟）。连续失败到阈值自动熔断一段时间，不再拖延迟。
#
# 六、已知的模型坑
#   glm-5.3-flash / qwen3.7-flash 把答案全放 reasoning_content、content 返回
#   空字符串 -> 决策模型不要用它们（代码也兜了 content 空就读 reasoning）。
#   seed-2.1-turbo 单次 26s / 1544 output tokens，太啰嗦。
#   实测还收到过尾部多一个「略」字导致整条 json.loads 失败的输出 ->
#   schema 固定，逐字段正则兜底比整体解析稳。

# [patch:ownvoice-v1 机器人自己的话进判断窗口]
# [patch:followup-v1 被人接话时允许继续回]
# [patch:initiate-compat-v1 认识 dsh-initiate 的合成事件]
import asyncio
import json
import os
import random
import re
import sqlite3
import time
from collections import deque

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart

DB = os.environ.get("DSH_MEM_DB", "/AstrBot/data/dsh_memory.db")

ENABLED = os.environ.get("DSH_DECIDE", "1") != "0"
# 影子模式：照样判断、照样打日志，但**不**真的拦。想只看数据不改行为时开。
SHADOW = os.environ.get("DSH_DECIDE_SHADOW", "0") != "0"
# 静音群：这些群里机器人完全不说话（连被 @ 也不回），但消息照常进管道、
# 语料照常收集——只在这里把「说不说话」的出口掐掉。逗号分隔的群号列表。
MUTE_GROUPS = {
    g.strip() for g in os.environ.get("DSH_MUTE_GROUPS", "").split(",") if g.strip()
}
# 判断用哪个 provider。空 = 用当前会话的主 provider
PROVIDER = os.environ.get("DSH_DECIDE_PROVIDER", "").strip()
# 给判断多少秒。
# ★ 8s 太短，实测被打爆过 ★
# 深夜测的时候延迟中位 2.2s，8s 看着很宽裕。但白天渠道负载高，同一段代码
# 实测中位涨到 17.8s —— 于是每次插话判断都超时 fail-open，
# 多步思考完全失效（还照样把 token 花掉了，因为请求已经发出去）。
# 判断只发生在**插话**路径上（被 @ 直接跳过），没人在等这句话，
# 所以等久一点零代价；超时反而让功能静默失效。
# 25s 覆盖实测长尾；真卡死也有熔断（连续失败 3 次停 10 分钟）兜着。
TIMEOUT = float(os.environ.get("DSH_DECIDE_TIMEOUT", "25"))
LOOKBACK = max(3, int(os.environ.get("DSH_DECIDE_LOOKBACK", "8")))
# 窗口的**时间**上限。LOOKBACK 只管条数，不管这 8 条横跨多久 ——
# 实测 571 个窗口里跨度中位 3 分钟，但 9% 超过 30 分钟、最长 603 分钟。
# 那 9% 里「最近的话」其实是上一个话题，判断模型会拿上个话题的 tone
# 来判当前这句（真实事故：有人认真问转学籍，窗口里 5/8 是半小时前的
# 身份梗，于是 tone=轻松随和 → 机器人回「跟妈商量？长不大」）。
# 宁可窗口只剩两行：模型缺上下文会偏保守（沉默），拿错上下文会自信答错。
#
# 20 分钟是量出来的：759 个真实窗口回溯，SPAN=20 时 87.5% 的窗口仍是
# 完整 8 行、只有 3.6% 被削到 ≤2 行；同时它仍能砍掉事故窗口里那 4 条
# 27~29 分钟前的旧话（SPAN=30 就砍不掉了，事故会重演）。
SPAN = float(os.environ.get("DSH_DECIDE_SPAN", "1200"))
FAIL_MAX = max(1, int(os.environ.get("DSH_DECIDE_FAIL_MAX", "3")))
COOLDOWN = float(os.environ.get("DSH_DECIDE_COOLDOWN", "600"))
# 刚说完话多少秒内不主动再开口（只管插话路径，被 @ 不受影响）。
# 这是压「机器人占 56% 发言量」最直接的一根杠杆，且零成本。
MIN_GAP = float(os.environ.get("DSH_DECIDE_MIN_GAP", "60"))
# MIN_GAP 的硬地板。这么短的间隔一定是机器人自己在连着说，不值得花一次判断去问。
# 8s 是照 segmented_reply 的实际节奏定的：分段间隔 1.2~2.8s，一条回复最多几段，
# 所以 8s 内到达的消息基本不可能是「人看完了才回」。
GAP_HARD = float(os.environ.get("DSH_DECIDE_GAP_HARD", "8"))

# ---- 动态潜水（用户钦定：'潜水率可以做成动态的，没有什么可以接的对话就可以潜水'）
#
# verdict 判定「回话」后，再按「这轮有多值得接」掷一次骰子决定潜不潜水：
#   在回你（replying_to_bot）→ 几乎必回（潜水率最低）
#   提到你（about_bot）      → 可以回（很低潜水）
#   有现成话头（open）       → 多数回（低-中潜水）
#   纯闲聊玩梗（banter）     → 一半一半
#   无否决也无话头（fallback）→ 高潜水：没什么可接，潜水就是真人样
# 被 @ / 被回复 / 点名要能力 都在上面直接放行了，走不到这里 ——
# 这里只对「没人叫、纯看情况插话」的场景缩放，不会挡必回。
# DSH_DECIDE_DIVE=0 可整体关掉（回到旧的全开口行为）。
DIVE = os.environ.get("DSH_DECIDE_DIVE", "1") != "0"
DIVE_RATE_REPLY = min(1.0, max(0.0, float(os.environ.get("DSH_DECIDE_DIVE_REPLY", "0.02"))))
DIVE_RATE_ABOUT = min(1.0, max(0.0, float(os.environ.get("DSH_DECIDE_DIVE_ABOUT", "0.15"))))
DIVE_RATE_OPEN = min(1.0, max(0.0, float(os.environ.get("DSH_DECIDE_DIVE_OPEN", "0.35"))))
DIVE_RATE_BANTER = min(1.0, max(0.0, float(os.environ.get("DSH_DECIDE_DIVE_BANTER", "0.55"))))
DIVE_RATE_NONE = min(1.0, max(0.0, float(os.environ.get("DSH_DECIDE_DIVE_NONE", "0.80"))))

# ---- 作息：睡觉时段不主动插话（《回答.md》D14/D15）
#
# 旋钮跟 dsh-scene 共用同一批 env，别在两处各写一份时段，否则一定会漂。
#   DSH_SLEEP=0        整个作息关掉
#   DSH_SLEEP_FROM=3   含
#   DSH_SLEEP_TO=9     不含
# 时段是拿本群 2137 条真语料按小时切出来的：03:00~08:59 只有 13 条（0.6%），
# 而 21:00~01:59 占全天 54%。老蓝图那句「夜间降低活跃度」在本群是反的。
SLEEP_ON = os.environ.get("DSH_SLEEP", "1") != "0"
try:
    SLEEP_FROM = int(os.environ.get("DSH_SLEEP_FROM", "3"))
except Exception:
    SLEEP_FROM = 3
try:
    SLEEP_TO = int(os.environ.get("DSH_SLEEP_TO", "9"))
except Exception:
    SLEEP_TO = 9
SLEEP_TZ = os.environ.get("DSH_SCENE_TZ", "Asia/Shanghai")


def _sleep_hour(ts=None):
    """本群时区的小时。容器已设 TZ=Asia/Shanghai，拿不到 zoneinfo 就退本地时间。"""
    t = time.time() if ts is None else ts
    try:
        import datetime as _dt
        import zoneinfo as _zi

        return _dt.datetime.fromtimestamp(t, _zi.ZoneInfo(SLEEP_TZ)).hour
    except Exception:
        return time.localtime(t).tm_hour


def _in_sleep(hour):
    """睡觉时段？跨午夜的写法（22~6）也成立；FROM==TO 视为不启用。"""
    if not SLEEP_ON:
        return False
    a, b = SLEEP_FROM % 24, SLEEP_TO % 24
    if a == b:
        return False
    if a < b:
        return a <= hour < b
    return hour >= a or hour < b
# 连续判沉默这么多次后强制放行一次。防止判断模型某天开始无脑输出沉默
# 把机器人变成哑巴 —— 任何单点判断都要有「卡住了怎么办」的兜底。
MAX_STREAK = max(1, int(os.environ.get("DSH_DECIDE_MAX_SILENCE_STREAK", "6")))

SYS = ("你是一个只输出 JSON 的观察器。只描述你看到的事实，"
       "不要下结论，不要解释，不要 markdown。")

PROMPT = """看这段 QQ 群聊，回答几个关于**最后一条消息**的事实判断。
（标着「你自己」的那几行是你之前说的话。）

{transcript}

逐项回答（true/false，只看事实，不要考虑「机器人该不该说话」）：
- arrange：最后两三条是两个人在对**具体安排**一来一回吗（谁去做什么、几点、多少钱、修好了叫我、明天几点）
- venting：有人在诉苦、示弱、抱怨自己的遭遇、说自己心累难受吗
- stop：有人明确说了「别插话」「别说话」「我跟他说点事」这类话吗
- ack：最后一条只是一句没内容的应答吗（嗯、好、行、那就这样、哦）
- banter：他们在闲聊、玩梗、开玩笑、互相调侃、吐槽某人吗
- open：最后一条是个大家都能接的话头吗（问大家、发感慨、晒东西、起哄）
- about_bot：提到了这个机器人，或提到它擅长的事吗
- replying_to_bot：最后一条是在回应「你自己」刚说的那句吗（顺着它答、反驳它、追问它）

再补四个描述（给它开口时参考）：
- topic：他们在聊什么，≤12字
- to：最后一条是谁对谁说的，≤10字
- tone：如果开口，该用什么态度，≤8字
- avoid：社交分寸上别怎么做，≤10字。只谈态度分寸，不要写「别画」「别搜」这类拦动作的话

只输出一行 JSON：
{{"arrange":false,"venting":false,"stop":false,"ack":false,"banter":false,"open":false,"about_bot":false,"replying_to_bot":false,"topic":"","to":"","tone":"","avoid":""}}"""

_BOOLS = ("arrange", "venting", "stop", "ack", "banter", "open", "about_bot",
          "replying_to_bot")
_STRS = ("topic", "to", "tone", "avoid")

# avoid 里出现「拦掉能力」的说法就整条丢掉
_CAP_RE = re.compile(
    r"别(真)?(画|发|生成|搜|查|做图|出图|语音|视频|唱|放)"
    r"|不要(画|发|生成|搜|查|放)|别调用|别用工具"
)

# 明确点名要能力：绝不沉默，且直接跳过判断（省一次请求 + 省 2 秒）。
#
# 两次实测校准过（拿真群 archive 里含「画/搜/语音/生成/视频」的真句子跑）：
#   * 漏放行「你能参考这个，再画几张吗」—— 数量词在动词后面（画几张），
#     原来的 (张|个|段) 只认动词紧跟量词。补 (几|多)?(张|个|段|条|首|遍|次)。
#   * 误放行「生成视频好像要时间吧」—— 这是陈述句不是请求。误放行的代价比
#     漏放行大（白跳过判断 = 该沉默时也开口），所以加一条否决：
#     句中出现「好像|大概|应该|是不是|吧？|要时间|不了|不能」等推测/否定标记时，
#     不当成请求。这与 dsh-imagegen 的教训一致：宾语过滤用排除法。
#
# ---------------------------------------------------------------------------
# 「有人在跟**别的** AI 说话」就别抢话
#
# 真人不会去接别人对着另一个助手说的话。参考 MaiBot 的
# src/maisaka/reply_necessity.py:OTHER_ASSISTANT_ADDRESSEE_PATTERN。
#
# 判据必须是结构而不是词表 —— 本群成天在聊模型，只按「句里出现过模型名」判
# 会满屏假命中。拿 1897 条真语料回测校准过两处：
#   * 分隔符不收空格：收了空格，「gpt progpt plus」「gpt pro的缓存差不多
#     95%左右」这两条纯话题句会被误伤。只认标点后归零。
#   * 名字不收 `GPT`/`ds` 这种太短太泛的写法：群里 `ds` 本身就是话题词
#     （「ds后训练后甲上来了」「掺ds了」）。宁可漏判，不可误闭嘴 ——
#     用户的原始抱怨就是「回复太少」，误判沉默是往反方向走。
# 收紧后真语料 0 假命中，而「DeepSeek，帮我写个正则」「豆包：这题怎么解」
# 「claude！你在吗」照样抓得到。
OTHER_AI = os.environ.get("DSH_DECIDE_OTHER_AI", "1") != "0"
_OTHER_AI_RE = re.compile(
    r"^\s*(?:DeepSeek|ChatGPT|Grok|Claude|Gemini|Kimi|Qwen|Copilot"
    r"|豆包|千问|通义|元宝|文心|智谱|讯飞|星火|文小言)\s*[，,、：:!！?？]",
    re.IGNORECASE,
)

_ASK_RE = re.compile(
    r"(画|生成|做|发|来|整|录|唱)(一|几|多)?(张|个|段|条|首|遍|次)?"
    r"(图|照|壁纸|表情|视频|语音|音频|歌)"
    r"|(语音|视频|图|壁纸)(说|念|读|来|发)"
    r"|发(个|条|段)?(语音|视频|图)"
    r"|(搜|查)(一下|下|搜)"
    r"|搜索|百度|谷歌|google"
    r"|画(个|张|一|几)|唱(首|个|一)"
    r"|(再|多)(画|发|生成|来|做)(几|一)?(张|个|遍|次|条)"
)
# 出现这些标记说明是推测/陈述/否定/自述，不是对机器人的祈使请求 -> 不走白名单。
# 「我要录个视频」是说话人自己要去做，机器人插不上手；「我去搜一下」同理。
# 用「第一人称 + 动词」这个**结构**来判，而不是枚举句子。
_NOT_ASK_RE = re.compile(
    r"好像|大概|应该|估计|是不是|要时间|花时间|挺慢|很慢"
    r"|不了|不能|没法|做不到|生成不了|画不了"
    r"|我(要|去|来|自己|想)(录|画|搜|查|生成|做|发)"
    r"|吗？$|吧$|吧？$"
)

_stat = {
    "seen": 0, "skip_addressed": 0, "skip_cmd": 0, "skip_nogid": 0,
    "skip_thin": 0, "skip_ask": 0, "gap_silence": 0,
    "asked": 0, "silence": 0, "speak": 0,
    "parse_fallback": 0, "fail": 0, "timeout": 0, "breaker": 0,
    "streak_release": 0, "avoid_dropped": 0, "ms_total": 0.0,
    "span_dropped": 0, "muted_out": 0,
}
_fail_run = 0
_breaker_until = 0.0
_silence_streak: dict[str, int] = {}
_last_send: dict[str, float] = {}   # gid -> 机器人最后一次发言时间
# gid -> 机器人自己最近说过的话 [(text, ts)]。
# 为什么要自己存：dsh-memory 的 buffer 表**故意**不收机器人的话（那张表同时是
# 群员画像的抽取源，收了会把机器人的话抽成群员事实）。但判断模型必须看见
# 机器人说过什么，否则「这句是不是在回我」无从判断。所以在本插件进程内单独留
# 一份，只服务于判断窗口，不落盘、重启即空（重启后最多前几轮判不出 follow_up，
# 可接受——比污染长期记忆强）。
_own: dict[str, "deque[tuple[str, float]]"] = {}
OWN_MAX = max(1, int(os.environ.get("DSH_DECIDE_OWN_MAX", "4")))
_last: list[str] = []


def _ago(sec: float) -> str:
    """把「距今多少秒」说成人话。判断模型靠这个看出对话是否连贯。"""
    if sec < 45:
        return "刚刚"
    if sec < 3600:
        return "%d分钟前" % max(1, int(round(sec / 60.0)))
    return "%d小时前" % max(1, int(round(sec / 3600.0)))


def _recent(gid: str, cur: str) -> str:
    """从 dsh-memory 的 buffer 读最近的群聊原话。

    为什么不读 req.contexts：那里是「一轮 user/assistant」的形状，被 ctxclean
    截断过，插件注入块也混在里面，判断话题不如原始群聊干净。
    buffer 表实测只存真人（机器人自己 0 行），所以它就是「群里在聊什么」。

    两道闸门，条数和时间都要过：
      · LOOKBACK 条 —— 控 token
      · SPAN 秒   —— 控话题。超过 SPAN 的消息属于上一个话题，留着有害
        （实测 9% 的窗口跨度 >30 分钟，最长 603 分钟）
    """
    lines: list[str] = []
    # (显示名, ts, 文本)。为了能和机器人自己的话按时间归并，光有渲染好的
    # 字符串不够，得留着排序键。
    _rows: list[tuple[str, float, str]] = []
    dropped = 0
    span_s = 0.0
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2.0)
    except BaseException:
        con = None
    if con is not None:
        try:
            rows = con.execute(
                "SELECT name, user_id, text, ts FROM buffer WHERE group_id=? "
                "ORDER BY ts DESC LIMIT ?",
                (gid, LOOKBACK),
            ).fetchall()
            rows.reverse()
            # 基准时间取窗口里最新那条，不取 time.time()：
            # 判断是被这条新消息触发的，它自己就是「现在」。用挂钟时间
            # 会把「机器人想了 5 秒」也算进跨度里。
            newest = max((r[3] or 0) for r in rows) if rows else 0
            for name, uid, text, ts in rows:
                t = (text or "").strip()
                if not t:
                    continue
                age = float(newest - (ts or 0))
                if age > SPAN:
                    dropped += 1
                    continue
                span_s = max(span_s, age)
                lines.append(
                    "[%s] %s：%s" % (_ago(age), (name or uid).strip(), t[:60])
                )
                _rows.append(((name or uid).strip(), float(ts or 0), t))
        except BaseException:
            pass
        finally:
            try:
                con.close()
            except BaseException:
                pass
    if dropped:
        # 必须打日志：不打的话下次再出「答非所问」还是只能靠猜是哪一环。
        _stat["span_dropped"] += dropped
        logger.info(
            "[decide] 窗口砍掉 %d 条超过 %.0f 分钟的旧话（剩 %d 条／跨度 %.0f 分钟）",
            dropped, SPAN / 60.0, len(lines), span_s / 60.0,
        )
    # 把机器人自己说过的话按时间戳归并进去。
    # 必须是**归并**不是追加：模型靠先后顺序判断谁在回谁，顺序错了不如不给。
    # lines 此刻是 [(age, 文本)] 之前的纯字符串形式装不下排序键，所以上面
    # 改成同时收集 _rows；这里统一排序后再渲染。
    own = list(_own.get(gid) or ())
    if own and _rows:
        # _rows 是 (显示名, ts, 文本) 三元组，这里只需要 ts。
        # 原来写成 `for _n, ts in _rows` 按两元组解包，只要机器人最近说过话
        # （_own 非空）就必抛 ValueError: too many values to unpack，
        # 整个 decide 落到 fail-open「照旧说话」——实测今天崩 12 次 / 判成 9 次。
        # 后果正是「主谓宾弄错」：to（这句是谁对谁说的）这条判断根本没送进模型。
        newest_ts = max(r[1] for r in _rows)
        for text, ts in own:
            age = float(newest_ts - ts)
            if age < 0 or age > SPAN:
                continue
            _rows.append(("你自己", ts, text))
        _rows.sort(key=lambda r: r[1])
        lines = ["[%s] %s：%s" % (_ago(float(newest_ts - r[1])), r[0], r[2][:60])
                 for r in _rows]
    c = (cur or "").strip()
    if c and (not lines or c not in lines[-1]):
        lines.append("（刚刚这条）%s" % c[:60])
    # 上限放宽到 LOOKBACK + OWN_MAX：LOOKBACK 是为了控真人消息的 token，
    # 机器人自己的话是新增的必要信息，不该把真人消息挤掉。
    return "\n".join(lines[-(LOOKBACK + OWN_MAX):])


def _parse(raw: str) -> dict | None:
    """先整体 json.loads，失败就逐字段正则抠。

    实测收到过 {"...","avoid":"别太较真"略} —— 尾部多一个字整条解析就失败。
    schema 是固定的小结构，逐字段抠比整体解析稳得多。
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
        _stat["parse_fallback"] += 1
        out = {}
        for k in _BOOLS:
            m = re.search(r'"%s"\s*:\s*(true|false|True|False)' % k, body)
            if m:
                out[k] = m.group(1).lower() == "true"
        for k in _STRS:
            m = re.search(r'"%s"\s*:\s*"([^"]*)"' % k, body)
            if m:
                out[k] = m.group(1)
    # 一个布尔字段都没有 => 模型根本没回答感知问题，这不是「全 False」而是
    # 「没结果」。两条路径都要查：整体 json.loads 成功但内容是
    # {"topic":"x"} 这种也必须判无效，否则退化的模型输出会被当成正常判断
    # 混过去，还把熔断计数器喂成「成功」——熔断本来就是为了发现这种退化。
    if not any(k in out for k in _BOOLS):
        return None
    r: dict = {k: bool(out.get(k, False)) for k in _BOOLS}
    for k in _STRS:
        v = out.get(k)
        r[k] = v.strip()[:20] if isinstance(v, str) else ""
    if r["avoid"] and _CAP_RE.search(r["avoid"]):
        r["avoid_dropped"] = r["avoid"]
        r["avoid"] = ""
        _stat["avoid_dropped"] += 1
    return r


def verdict(f: dict) -> tuple[str, str]:
    """veto-only：只有**否决信号**才闭嘴，其余一律开口。

    纯函数、无副作用，好单测。改判断倾向就是改这几行，不用重新调提示词。
    """
    if f["stop"]:
        return "沉默", "有人叫别插话"
    # 「在回你」是最强的正向信号：被人接了话还装死，是真人绝不会有的行为。
    # 位置有讲究 —— 排在 stop 后面（明说别插话就闭嘴），但排在 venting/arrange
    # 前面（诉苦的人回了你还不理，比抖机灵更伤人）。ack 仍然否决，见下。
    if f.get("replying_to_bot") and not f["ack"]:
        return "回话", "在回你"
    if f["venting"]:
        return "沉默", "有人在诉苦"
    if f["arrange"] and not f["banter"]:
        return "沉默", "两人在谈具体安排"
    if f["ack"] and not f["open"]:
        return "沉默", "只是一句应答"
    pos = [k for k in ("banter", "open", "about_bot") if f[k]]
    return "回话", "可接（%s）" % (",".join(pos) if pos else "无否决信号")


def dive_rate(f: dict) -> float:
    """这轮「可接但没什么好接的」时潜水的概率（0~1），按信号强度分级。

    纯函数、无副作用，好单测。信号越「有得接」潜水率越低：
      · 在回你    → 几乎必回（0.02，留一丝随机性）
      · 提到你    → 很低潜水（0.15）
      · 有现成话头 → 多数回（0.35）
      · 纯闲聊    → 一半一半（0.55）
      · 什么都不可接 → 高潜水（0.80）—— 没人叫、没话头，潜水才是真人样
    """
    if f.get("replying_to_bot") and not f.get("ack"):
        return DIVE_RATE_REPLY
    if f.get("about_bot"):
        return DIVE_RATE_ABOUT
    if f.get("open"):
        return DIVE_RATE_OPEN
    if f.get("banter"):
        return DIVE_RATE_BANTER
    return DIVE_RATE_NONE


def _render(f: dict) -> str:
    """把感知结果包成注入块。只给意图，不给成句台词。

    块名 <judgement> 是小写标签，dsh-ctxclean 的
    _TAG_RE = ^\\s*<([a-z][a-z0-9_]*)> 会在下一轮把它从历史里结构性清掉，
    不会重演「注入块堆进 conversations.content 导致答非所问」那个坑。
    实测「让模型在一次调用里自己输出判断+正文」会把判断行漏进正文，
    而给了台词模型就照念、人设当场垮 —— 所以这里只放意图。
    """
    lines = ["<judgement>",
             "开口前你已经看过一眼群里的情况了，这是你自己的判断："]
    if f.get("topic"):
        lines.append("· 他们在聊：%s" % f["topic"])
    if f.get("to"):
        lines.append("· 这句是：%s" % f["to"])
    if f.get("tone"):
        lines.append("· 你该用的态度：%s" % f["tone"])
    if f.get("avoid"):
        lines.append("· 分寸上别：%s" % f["avoid"])
    if f.get("venting"):
        lines.append("· 有人心里不痛快，别抖机灵。")
    if f.get("arrange"):
        lines.append("· 他们在说正事，你只是路过搭一句，别接管话题。")
    lines.append("· 只接你真能接的那一点，一句话说完。")
    lines.append("按这个判断说话，但**不要**把上面任何一条读出来、复述或提到。")
    lines.append("</judgement>")
    return "\n".join(lines)


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        logger.info(
            "[decide] 已加载：%s%s 超时%.0fs 回看%d条/%.0f分钟内 "
            "刚说过%.0fs内只回接话/%.0fs内全闭嘴 自己的话记%d条 熔断%d次/%.0fs "
            "静音群=%s 别的AI在被喊时闭嘴=%s 睡觉时段=%s",
            "开" if ENABLED else "关",
            "（影子模式，只看不拦）" if SHADOW else "",
            TIMEOUT, LOOKBACK, SPAN / 60.0, MIN_GAP, GAP_HARD, OWN_MAX,
            FAIL_MAX, COOLDOWN,
            "、".join(sorted(MUTE_GROUPS)) if MUTE_GROUPS else "无",
            "开" if OTHER_AI else "关",
            ("%d~%d点不主动插话" % (SLEEP_FROM, SLEEP_TO)) if SLEEP_ON else "关",
        )

    # ---------------------------------------------------------- 记自己何时说过
    @filter.after_message_sent()
    async def note_sent(self, event: AstrMessageEvent) -> None:
        """记下机器人在哪个群、什么时候说过话。

        「刚说完又自己接一句」这件事代码百分百知道，不该去问模型。
        """
        try:
            gid = str(event.get_group_id() or "")
            if not gid:
                return
            _last_send[gid] = time.time()
            # 顺手记下正文。respond/stage.py 在 OnAfterMessageSentEvent 之后才
            # clear_result()，所以这里还拿得到（已核对源码）。
            text = ""
            try:
                res = event.get_result()
                if res is not None:
                    text = (res.get_plain_text() or "").strip()
            except BaseException:
                text = ""
            if not text:
                return
            q = _own.get(gid)
            if q is None:
                q = _own[gid] = deque(maxlen=OWN_MAX)
            q.append((text[:60], time.time()))
        except BaseException:
            pass

    async def _ask(self, umo: str, transcript: str) -> dict | None:
        pid = PROVIDER
        if not pid:
            try:
                # get_current_chat_provider_id 是**协程**，必须 await。
                # 不 await 会把 coroutine 对象一路传下去，报
                # 「Provider <coroutine object ...> not found」——
                # dsh-welcome 和 dsh-memory 都栽过这一下。
                pid = await self.context.get_current_chat_provider_id(umo)
            except BaseException as e:
                logger.debug("[decide] 取 provider 失败: %s", e)
                return None
        if not pid:
            return None
        # 温度显式给 0：这是**事实抽取**不是创作，随机性只会带来摇摆。
        # llm_generate 的 **kwargs 会透传给 provider，不支持时也不会报错。
        resp = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=pid,
                prompt=PROMPT.format(transcript=transcript),
                system_prompt=SYS,
                temperature=0,
            ),
            timeout=TIMEOUT,
        )
        raw = (getattr(resp, "completion_text", "") or "").strip()
        if not raw:
            # 有的模型把正文全放 reasoning_content，content 是空的
            raw = (getattr(resp, "reasoning_content", "") or "").strip()
        return _parse(raw)

    # priority=2000：输入侧拦截组，同 armor/merge。静音时 stop_event 令同批
    # 后续 handler 全跳过，挡住 effect/emotion 白烧 token；未静音时它自己
    # 注入潜水判断，与其余注入可并存。
    @filter.on_llm_request(priority=2000)
    async def decide(self, event: AstrMessageEvent, req) -> None:
        global _fail_run, _breaker_until
        if not ENABLED:
            return
        try:
            from astrbot.core.platform.message_type import MessageType

            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            # dsh-initiate 造的「主动开口」合成事件：直接让路。
            # 本插件判的是「要不要插进正在进行的对话」，而主动开口的前提是
            # **没有**正在进行的对话 —— 同一把尺子量出来的结论是反的，
            # 而且这里有 stop_event 权限，判沉默就把整次开口掐死了。
            # 那边已经用相反形状的判据（必须有正向证据才开口）判过一轮。
            if event.get_extra("dsh_initiate"):
                _stat["skip_initiate"] = _stat.get("skip_initiate", 0) + 1
                logger.info("[decide] 主动开口事件，让路不判")
                return
            # dsh-proactive 造的「兴趣探头」合成事件：同样让路。
            # 它的触发凭据是纯正则兴趣分（代码判的，在 collect 里已经过了
            # 低价值/服务请求/冷却/额度四道闸），不是掷骰子也不是小模型
            # 判断 —— 拿「要不要插话」的尺子重判会把它掐死（判据形状不同）。
            if event.get_extra("dsh_proactive"):
                _stat["skip_proactive"] = _stat.get("skip_proactive", 0) + 1
                logger.info("[decide] 兴趣探头事件，让路不判")
                return
            gid = str(event.get_group_id() or "")
            if not gid:
                _stat["skip_nogid"] += 1
                return
            _stat["seen"] += 1

            # 静音群：完全沉默，连被 @ 也不回。判断放在最前面，
            # 必须在「被点名就直接放行」之前，否则 @ 会绕过静音。
            if MUTE_GROUPS and gid in MUTE_GROUPS:
                _stat["muted"] = _stat.get("muted", 0) + 1
                logger.info("[decide] 群 %s 在静音名单，不说话（语料收集模式）", gid)
                event.stop_event()
                return

            # 被点名就没有「要不要回」的自由，也不该为此多等两秒
            if bool(getattr(event, "is_at_or_wake_command", False)):
                _stat["skip_addressed"] += 1
                return

            msg = (event.message_str or "").strip()
            if msg.startswith(("/", "／", "!", "！")):
                _stat["skip_cmd"] += 1
                return

            # 有人在跟别的 AI 说话（「豆包：这题怎么解」），别抢话。
            # 位置有讲究，两边都是必须的：
            #   * 必须在「被 @ 就放行」**之后** —— 真被喊了就该答，
            #     哪怕他这句话里带着别的模型名。
            #   * 必须在下面 _ASK_RE「点名要东西就放行」**之前** ——
            #     「豆包，画张图」会命中 _ASK_RE，放行了就抢了豆包的活。
            if OTHER_AI and _OTHER_AI_RE.search(msg):
                _stat["skip_other_ai"] = _stat.get("skip_other_ai", 0) + 1
                brief = "在跟别的 AI 说话，不抢话：%s" % msg[:30]
                _last.append(time.strftime("%H:%M:%S ") + brief)
                del _last[:-12]
                if SHADOW:
                    logger.info("[decide] 影子模式：%s（放行）", brief)
                    return
                logger.info("[decide] %s", brief)
                event.stop_event()
                return

            # 明确点名要能力 -> 绝不沉默，直接放行（省一次请求、省 2 秒）。
            # v5 唯一的错例就是把「发个语音说群主是懒猪」判成谈正事而闭嘴。
            # 用户明确要的东西被路由否决，是最不能接受的一类错。
            if _ASK_RE.search(msg) and not _NOT_ASK_RE.search(msg):
                _stat["skip_ask"] += 1
                logger.info("[decide] 有人明确点名要东西，直接放行：%s", msg[:40])
                return

            # 睡觉时段不主动插话（《回答.md》D14/D15 群主选的 ④「只被@才回」）。
            # 走到这里的**只剩随机插话** —— 被@、指令、别的AI、明确点名要能力
            # 在上面全处理完了，所以这道门碰不到那几类。犯困的语气和「回得慢」
            # 由 dsh-scene 负责，这里只管「别主动开口」。
            if _in_sleep(_sleep_hour()):
                _stat["sleep_silence"] = _stat.get("sleep_silence", 0) + 1
                brief = "睡觉时段（%d~%d点），不主动插话" % (SLEEP_FROM, SLEEP_TO)
                _last.append(time.strftime("%H:%M:%S ") + brief)
                del _last[:-12]
                if SHADOW:
                    logger.info("[decide] 影子模式：%s（放行）", brief)
                    return
                logger.info("[decide] %s", brief)
                event.stop_event()
                return

            # 刚说过话就先闭嘴。压发言占比最直接、且完全不花钱的一根杠杆。
            gap = time.time() - _last_send.get(gid, 0.0)
            # 硬地板：一定是自己连着说，不问模型（零成本）。
            if gap < GAP_HARD:
                _stat["gap_silence"] += 1
                brief = "刚说过 %.0fs 前（<%.0fs 硬地板），这轮不说话" % (
                    gap, GAP_HARD)
                _last.append(time.strftime("%H:%M:%S ") + brief)
                del _last[:-12]
                if SHADOW:
                    logger.info("[decide] 影子模式：%s（放行）", brief)
                    return
                logger.info("[decide] %s（省一次主调用）", brief)
                event.stop_event()
                return
            # 软区间：照常问模型，但下面只有「在回你」才放行。
            # 这里多花一次判断步（≈主调用的 4%），换「被人回了不装死」。
            in_gap = gap < MIN_GAP
            if in_gap:
                _stat["gap_soft"] = _stat.get("gap_soft", 0) + 1

            if time.time() < _breaker_until:
                _stat["breaker"] += 1
                logger.info("[decide] 熔断中，跳过判断（还有 %.0fs）",
                            _breaker_until - time.time())
                return

            transcript = _recent(gid, msg)
            if len(transcript) < 6:
                _stat["skip_thin"] += 1
                logger.info("[decide] 群聊内容太少，不判断 gid=%s", gid)
                return

            t0 = time.time()
            try:
                f = await self._ask(event.unified_msg_origin, transcript)
            except asyncio.TimeoutError:
                _stat["timeout"] += 1
                _fail_run += 1
                logger.warning("[decide] 判断超时 %.0fs，照旧说话（fail-open）",
                               TIMEOUT)
                f = None
            except BaseException as e:
                _stat["fail"] += 1
                _fail_run += 1
                logger.warning("[decide] 判断失败，照旧说话（fail-open）: %s", e)
                f = None
            ms = (time.time() - t0) * 1000
            _stat["ms_total"] += ms

            if f is None:
                if _fail_run >= FAIL_MAX:
                    _breaker_until = time.time() + COOLDOWN
                    _fail_run = 0
                    logger.warning(
                        "[decide] 连续失败 %d 次，熔断 %.0fs（这期间照旧说话）",
                        FAIL_MAX, COOLDOWN,
                    )
                return
            _fail_run = 0
            _stat["asked"] += 1

            act, why = verdict(f)
            # 软区间收紧：刚说过话，只有「在回你」才准开口。
            # 不在这里提前 return 是刻意的 —— 走同一条沉默路径，日志形状一致，
            # 统计口径也一致（否则「为什么没说话」又要分两处查）。
            if in_gap and act == "回话" and not f.get("replying_to_bot"):
                _stat["gap_soft_silence"] = _stat.get("gap_soft_silence", 0) + 1
                act = "沉默"
                why = "刚说过 %.0fs（<%.0fs）且不是在回你" % (gap, MIN_GAP)
            # 动态潜水：verdict 判「回话」，但按信号强度掷骰子，
            # 没什么可接的对话（没人叫、没话头）就潜水 —— 用户钦定。
            if DIVE and act == "回话":
                rate = dive_rate(f)
                if random.random() < rate:
                    _stat["dive"] = _stat.get("dive", 0) + 1
                    act = "沉默"
                    why = "潜水（可接但没什么好接，骰中 %.0f%%）" % (rate * 100)
            flags = "".join(k[0].upper() if f[k] else "." for k in _BOOLS)
            brief = "%s [%s] %s topic=%s tone=%s avoid=%s %.0fms" % (
                act, flags, why, f.get("topic") or "-", f.get("tone") or "-",
                f.get("avoid") or "-", ms,
            )
            _last.append(time.strftime("%H:%M:%S ") + brief)
            del _last[:-12]

            if act == "沉默":
                n = _silence_streak.get(gid, 0) + 1
                if n > MAX_STREAK:
                    _silence_streak[gid] = 0
                    _stat["streak_release"] += 1
                    logger.info("[decide] 连续沉默 %d 次，强制放行一次｜%s",
                                n, brief)
                    return
                _silence_streak[gid] = n
                _stat["silence"] += 1
                if SHADOW:
                    logger.info("[decide] 影子模式：本该沉默但放行｜%s", brief)
                    return
                logger.info("[decide] 这轮不说话（省一次主调用）｜%s", brief)
                event.stop_event()
                return

            _silence_streak[gid] = 0
            _stat["speak"] += 1
            req.extra_user_content_parts.append(TextPart(text=_render(f)))
            logger.info("[decide] 开口｜%s", brief)
        except BaseException as e:
            # 决策步自己出问题，绝不能连累正常对话
            logger.warning("[decide] 整体失败，照旧说话: %s", e)

    # ------------------------------------------------------------ 静音兜底
    #
    # 上面那道静音闸门挂在 on_llm_request 上，只掐得住**主模型**这一条出口。
    # 但指令回复（/权限 /我的档案 /上下文状态 …）根本不问模型，走的是
    # ResultDecorateStage → RespondStage，压根不经过 on_llm_request ——
    # 静音群里任何人敲一条公开指令，机器人照样会出声。
    # 这不是假想：dsh-guard 的警告是 event.send 直发，同样绕过那道闸门，
    # 已经真的在 100000001 说过一句「群里不聊这个，收着点」。
    #
    # 所以这里补一道**出口级**兜底：结果装好、还没发出去时，
    # 群在静音名单里就把整个 result 清掉。clear_result() 是框架自己的 API，
    # ResultDecorateStage 每跑完一个钩子都查 `result is None or not result.chain`，
    # RespondStage 开头也是 `if result is None: return`，所以清掉等于不发，
    # 不会报错、也不会留半条消息。
    #
    # 为什么不放在 dsh-acl 那个 priority=1000 的门卫里：那个 handler 在
    # StarRequestSubStage，stop_event 会让**同批**后面的 handler 全部 break，
    # 而 dsh-memory 的语料采集正挂在同一批里 —— 那样会把「只收语料」这件事
    # 本身弄坏。出口级清结果发生在采集之后，动不到语料。
    #
    # 仍然管不到的：不产生 result、直接 event.send() 的插件
    # （dsh-poke / dsh-welcome / dsh-guard）。它们只能靠各自的群白名单，
    # 现已逐个钉死在 100000001。这条边界写在这里，免得下次再查一遍。
    @filter.on_decorating_result()
    async def mute_out(self, event: AstrMessageEvent) -> None:
        if not MUTE_GROUPS:
            return
        try:
            gid = str(event.get_group_id() or "")
            if not gid or gid not in MUTE_GROUPS:
                return
            result = event.get_result()
            if result is None or not result.chain:
                return
            outline = "".join(
                (getattr(c, "text", None) or "[%s]" % getattr(c, "type", "?"))
                for c in result.chain
            )[:60]
            event.clear_result()
            _stat["muted_out"] += 1
            logger.info("[decide] 静音群 %s 出口拦下一条回复：%s", gid, outline)
            event.stop_event()
        except BaseException as exc:
            # 兜底自己出问题就什么都不做（退回原行为），别连累正常群
            logger.warning("[decide] 静音出口拦截失败: %r", exc)

    @filter.command("插话判断")
    async def cmd_status(self, event: AstrMessageEvent):
        s = _stat
        judged = s["silence"] + s["speak"]
        rate = (s["silence"] / judged * 100) if judged else 0.0
        saved = s["silence"] + s["gap_silence"]
        avg = s["ms_total"] / max(1, s["asked"])
        lines = [
            "插话判断：%s%s" % ("开" if ENABLED else "关",
                              "（影子模式，只看不拦）" if SHADOW else ""),
            "静音群（只收语料不说话）：%s｜模型出口拦下 %d 条、指令等出口拦下 %d 条"
            % ("、".join(sorted(MUTE_GROUPS)) if MUTE_GROUPS else "无",
               s.get("muted", 0), s.get("muted_out", 0)),
            "看到群消息 %d 次" % s["seen"],
            "  不判断：被喊 %d｜指令 %d｜明确要东西 %d｜内容太少 %d｜熔断 %d"
            % (s["skip_addressed"], s["skip_cmd"], s["skip_ask"],
               s["skip_thin"], s["breaker"]),
            "  在跟别的 AI 说话（%s）不抢话 %d 次"
            % ("开" if OTHER_AI else "关", s.get("skip_other_ai", 0)),
            "  刚说过%.0fs内直接闭嘴 %d 次" % (MIN_GAP, s["gap_silence"]),
            "  动态潜水（没得接就潜）%d 次｜潜水率 在回你%.0f%% 提到你%.0f%% 有话头%.0f%% 闲聊%.0f%% 无可接%.0f%%"
            % (s.get("dive", 0), DIVE_RATE_REPLY * 100, DIVE_RATE_ABOUT * 100,
               DIVE_RATE_OPEN * 100, DIVE_RATE_BANTER * 100, DIVE_RATE_NONE * 100),
            "  睡觉时段不插话 %d 次" % s.get("sleep_silence", 0),
            "  睡觉时段不插话 %d 次" % s.get("sleep_silence", 0),
            "  窗口砍掉超过%.0f分钟的旧话 %d 条（防拿上个话题的语气判这句）"
            % (SPAN / 60.0, s["span_dropped"]),
            "真问模型 %d 次，平均 %.0fms（正则兜底解析 %d 次）"
            % (s["asked"], avg, s["parse_fallback"]),
            "  结果：沉默 %d｜开口 %d  →  判沉默率 %.0f%%"
            % (s["silence"], s["speak"], rate),
            "省下的主调用：约 %d 次（每次约 9400 输入 token）" % saved,
            "失败 %d｜超时 %d｜连续沉默强制放行 %d｜拦下越权 avoid %d"
            % (s["fail"], s["timeout"], s["streak_release"], s["avoid_dropped"]),
        ]
        if _last:
            lines.append("最近几次判断：")
            lines += ["  " + x for x in _last[-6:]]
        yield event.plain_result("\n".join(lines))
