# -*- coding: utf-8 -*-
"""控制台的「能力清单」—— 纯数据，不含逻辑。

为什么单独一个文件：APK 不写死界面，它按 /api/console/schema 返回的东西渲染。
所以以后想多一个开关、多一个模式，**只改这个文件 + 重启 qrweb 服务**，
手机上下拉刷新就出现了，不用重新装包。这是「实时更新」的底座。

两种存储，靠 path 前缀区分：
  · `provider_settings.xxx`  → cmd_config.json，走 AstrBot dashboard API，**改完立即生效**
  · `env:DSH_XXX`            → imagegen.env，走行级文件编辑，**改完要按「重载环境变量」**

三张表：
  KNOBS  —— 可读可改的配置项（白名单，只认这里列的路径）
  MODES  —— 一键模式：一个模式 = 一组 KNOBS 值 + 模型 + 插件开关
  ACTIONS—— 动作按钮（重启之类）

安全边界（重要）：
  KNOBS 是**白名单**，不是黑名单。手机端只能改这里出现的路径，
  所以 dashboard.password / admins_id / provider_sources[].key / platform[]
  这些一律改不动 —— 就算 token 泄漏，也拿不到账号或密钥。
  platform_settings.reply_with_mention 故意不收录：dsh-mention 是唯一 At 来源，
  这个全局开关必须恒为 false，放出来只会制造互相打架的两条 @ 逻辑。
  标了 secret=True 的（7 个 API key / 音色 ID）**只报「已设置/未设置」**，
  值一个字节都不出服务器，手机端也改不了 —— 8088 是明文 HTTP。

env 旋钮的三条硬约束（实测出来的，写错就会出现「改不动 / 改错」的开关）：
  ① bool 只能写 1/0。22 个插件对布尔有 4 种解析写法，其中 11 个变量用 `!= "0"` 判，
     写 "false" 会被当成**开**。envfile._format 统一写 1/0 就是为这个。
  ② int/float 填非数字或留空会让插件 import 抛异常 = 整个插件加载失败
     （只有 dsh-memory 有 try/except 回落）。所以每个数值旋钮都必须给 min/max。
  ③ 代码里有 max()/min() 硬夹紧的变量，min 必须写成它的硬下限，
     否则会出现「手机上显示 1、实际生效 3」的错觉。
"""

# ---------------------------------------------------------------- 配置旋钮
#
# type: bool / int / float / enum / str / csv
# path: cmd_config.json 的点分路径，或 env:变量名
# min/max/step: 数值范围（服务端强制校验，越界直接 400，不做静默钳制）
# hot: True 表示改完立刻生效；env 旋钮一律 False（容器要重建才重读 env_file）
# default: env 旋钮**代码里的**默认值 —— 线上没设时实际生效的就是它
# hint: 手机上显示的一句话说明，写「这个开关会让机器人怎么样」而不是变量语义

_ENV = False  # env 旋钮的 hot 恒为 False，写成常量是为了下面别写错

KNOBS = [
    # ================================================================
    # 框架配置（cmd_config.json，改完立即生效）
    # ================================================================
    # ---- 说话频率 ----
    {
        "path": "provider_ltm_settings.active_reply.enable",
        "name": "主动插话",
        "group": "说话频率",
        "type": "bool",
        "hot": True,
        "hint": "关掉后只在被 @ 或叫名字时才回",
    },
    {
        "path": "provider_ltm_settings.active_reply.possibility_reply",
        "name": "插话概率",
        "group": "说话频率",
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "step": 0.05,
        "hot": True,
        "hint": "群里每条消息有多大概率主动接话。上面还压着「插话判断」那一层，"
                "所以实际开口比这个数低",
    },
    {
        "path": "platform_settings.rate_limit.count",
        "name": "限流条数",
        "group": "说话频率",
        "type": "int",
        "min": 1,
        "max": 60,
        "hot": True,
        "hint": "每个限流窗口内最多回几条（只算被 @ / 唤醒词 / 私聊，群闲聊不占额度）",
    },
    {
        "path": "platform_settings.rate_limit.time",
        "name": "限流窗口",
        "group": "说话频率",
        "type": "int",
        "min": 10,
        "max": 600,
        "unit": "秒",
        "hot": True,
        "hint": "限流窗口长度",
    },
    # ---- 上下文 ----
    {
        "path": "provider_settings.max_context_length",
        "name": "保留轮数",
        "group": "上下文",
        "type": "int",
        "min": 5,
        "max": 200,
        "unit": "轮",
        "hot": True,
        "hint": "带多少轮历史给模型。之前设 -1（不限）导致过答非所问",
    },
    {
        "path": "provider_settings.identifier",
        "name": "带说话人名字",
        "group": "上下文",
        "type": "bool",
        "hot": True,
        "hint": "关掉它模型就分不清谁在说话，之前答非所问的第二病因",
    },
    {
        "path": "provider_settings.dequeue_context_length",
        "name": "每次挤出轮数",
        "group": "上下文",
        "type": "int",
        "min": 1,
        "max": 10,
        "hot": True,
        "hint": "超长时一次丢掉几轮",
    },
    # ---- 工具与能力 ----
    {
        "path": "provider_settings.web_search",
        "name": "框架自带联网",
        "group": "工具与能力",
        "type": "bool",
        "hot": True,
        "hint": "一般保持关闭：联网走「联网搜索」那一组，两套同时开会重复搜",
    },
    {
        "path": "provider_settings.show_tool_use_status",
        "name": "显示「正在调用工具」",
        "group": "工具与能力",
        "type": "bool",
        "hot": True,
        "hint": "开了群里会多出一条状态提示，比较吵",
    },
    {
        "path": "provider_settings.show_tool_call_result",
        "name": "显示工具返回",
        "group": "工具与能力",
        "type": "bool",
        "hot": True,
        "hint": "调试用，平时别开",
    },
    {
        "path": "provider_settings.max_agent_step",
        "name": "工具最大步数",
        "group": "工具与能力",
        "type": "int",
        "min": 1,
        "max": 50,
        "hot": True,
        "hint": "一次回复里最多连续调几次工具",
    },
    # ---- 发送方式 ----
    {
        "path": "platform_settings.segmented_reply.enable",
        "name": "分段发送",
        "group": "发送方式",
        "type": "bool",
        "hot": True,
        "hint": "长回复拆成几条发，更像真人打字；但会拉长占屏时间",
    },
    {
        "path": "platform_settings.reply_with_quote",
        "name": "带引用回复",
        "group": "发送方式",
        "type": "bool",
        "hot": True,
        "hint": "回复时引用原消息",
    },
    {
        "path": "platform_settings.ignore_at_all",
        "name": "忽略 @全体",
        "group": "发送方式",
        "type": "bool",
        "hot": True,
        "hint": "别人 @全体成员 时不当成叫自己",
    },

    # ================================================================
    # 插件旋钮（imagegen.env，改完要按「重载环境变量」）
    # ================================================================

    # ---- 插话判断（dsh-decide）----
    {
        "path": "env:DSH_DECIDE",
        "name": "插话判断",
        "group": "插话判断",
        "type": "bool",
        "hot": _ENV,
        "default": "1",
        "hint": "总开关。开着＝插话前先用小模型看一眼群里在聊什么、该不该开口；"
                "关掉＝退回纯随机插话（曾占全群 56% 发言量）",
    },
    {
        "path": "env:DSH_DECIDE_SHADOW",
        "name": "只记录不拦（影子）",
        "group": "插话判断",
        "type": "bool",
        "hot": _ENV,
        "default": "0",
        "hint": "开了照样判断照样写日志，但不真拦。想先看数据、不想改行为时用",
    },
    {
        "path": "env:DSH_DECIDE_MIN_GAP",
        "name": "自己说完歇多久",
        "group": "插话判断",
        "type": "float",
        "min": 0,
        "max": 600,
        "step": 10,
        "unit": "秒",
        "hot": _ENV,
        "default": "60",
        "hint": "刚说完话，多少秒内不主动再插嘴（被 @ 不受影响）。"
                "压发言量最直接的一根杠杆",
    },
    {
        "path": "env:DSH_DECIDE_LOOKBACK",
        "name": "判断时回看条数",
        "group": "插话判断",
        "type": "int",
        "min": 3,
        "max": 20,
        "hot": _ENV,
        "default": "8",
        "hint": "判断该不该说话时往回看几条群消息",
    },
    {
        "path": "env:DSH_DECIDE_TIMEOUT",
        "name": "判断超时",
        "group": "插话判断",
        "type": "float",
        "min": 5,
        "max": 60,
        "step": 5,
        "unit": "秒",
        "hot": _ENV,
        "default": "25",
        "hint": "判断最多等几秒，超时就照旧说话。别调太小 —— 白天渠道慢时中位 17.8 秒，"
                "设 8 秒等于这个功能静默失效",
    },
    {
        "path": "env:DSH_DECIDE_MAX_SILENCE_STREAK",
        "name": "连续沉默几次后强制开口",
        "group": "插话判断",
        "type": "int",
        "min": 1,
        "max": 20,
        "hot": _ENV,
        "default": "6",
        "hint": "防模型某天开始无脑判沉默、把机器人变成哑巴",
    },
    {
        "path": "env:DSH_MUTE_GROUPS",
        "name": "完全不说话的群",
        "group": "插话判断",
        "type": "csv",
        "item_pattern": r"\d{5,12}",
        "hot": _ENV,
        "default": "",
        "hint": "填群号，逗号分隔。这些群里连被 @ 也不回，但消息照收、语料照攒",
    },

    # ---- 冷场开口（dsh-initiate）----
    {
        "path": "env:DSH_INITIATE",
        "name": "冷场自己起话头",
        "group": "冷场开口",
        "type": "bool",
        "hot": _ENV,
        "default": "1",
        "hint": "总开关。群里静太久时自己找个话题说一句",
    },
    {
        "path": "env:DSH_INITIATE_SHADOW",
        "name": "只判定不发言（影子）",
        "group": "冷场开口",
        "type": "bool",
        "hot": _ENV,
        "default": "1",
        "hint": "开了只写日志不真开口。注意代码默认是「开」，线上已改成「关」＝真会说话",
    },
    {
        "path": "env:DSH_INITIATE_IDLE",
        "name": "静多久算冷场",
        "group": "冷场开口",
        "type": "float",
        "min": 60,
        "max": 7200,
        "step": 60,
        "unit": "秒",
        "hot": _ENV,
        "default": "900",
        "hint": "群里没人说话超过这么久，就考虑开口",
    },
    {
        "path": "env:DSH_INITIATE_IDLE_MAX",
        "name": "静过头就不追了",
        "group": "冷场开口",
        "type": "float",
        "min": 3600,
        "max": 172800,
        "step": 3600,
        "unit": "秒",
        "hot": _ENV,
        "default": "21600",
        "hint": "冷场超过这么久就放弃 —— 半天没人说话再插一句很怪",
    },
    {
        "path": "env:DSH_INITIATE_DAY_MAX",
        "name": "每天最多开口几次",
        "group": "冷场开口",
        "type": "int",
        "min": 1,
        "max": 20,
        "hot": _ENV,
        "default": "3",
        "hint": "一天的主动开口次数上限",
    },
    {
        "path": "env:DSH_INITIATE_COOLDOWN",
        "name": "两次开口最少间隔",
        "group": "冷场开口",
        "type": "float",
        "min": 60,
        "max": 86400,
        "step": 300,
        "unit": "秒",
        "hot": _ENV,
        "default": "3600",
        "hint": "同一个群两次主动开口至少隔多久",
    },
    {
        "path": "env:DSH_INITIATE_HOURS",
        "name": "允许开口的时段",
        "group": "冷场开口",
        "type": "str",
        "max_len": 60,
        "pattern": r"\s*\d{1,2}\s*-\s*\d{1,2}\s*(,\s*\d{1,2}\s*-\s*\d{1,2}\s*)*",
        "hot": _ENV,
        "default": "10-23,0-2",
        "hint": "几点到几点之间才允许主动开口，写「起-止」用逗号隔开，可跨零点。"
                "凌晨 3~9 点群里没人，别打扰",
    },
    {
        "path": "env:DSH_INITIATE_CONFIRM",
        "name": "要连续判几次才开口",
        "group": "冷场开口",
        "type": "int",
        "min": 1,
        "max": 5,
        "hot": _ENV,
        "default": "2",
        "hint": "同一段上下文相邻两次判断会给出相反结论，只看一次等于对噪声取最大值",
    },
    {
        "path": "env:DSH_INITIATE_COLD_OPEN",
        "name": "允许起全新话题",
        "group": "冷场开口",
        "type": "bool",
        "hot": _ENV,
        "default": "1",
        "hint": "关掉＝只在能接上文时才开口",
    },
    {
        "path": "env:DSH_INITIATE_GROUPS",
        "name": "在哪些群主动开口",
        "group": "冷场开口",
        "type": "csv",
        "item_pattern": r"\d{5,12}",
        "hot": _ENV,
        "default": "100000001",
        "hint": "填群号。**留空不等于所有群**，这里必须写群号才会开口",
    },

    # ---- 艾特策略（dsh-mention）----
    {
        "path": "env:DSH_AT_MODE",
        "name": "@ 人的方式",
        "group": "艾特策略",
        "type": "enum",
        "options": [
            {"value": "0", "label": "从不 @"},
            {"value": "1", "label": "智能判断"},
            {"value": "2", "label": "每条都 @"},
        ],
        "hot": _ENV,
        "default": "1",
        "hint": "「智能判断」＝只在下面三种情况 @ 人。「每条都 @」是框架原行为，很烦人",
    },
    {
        "path": "env:DSH_AT_TOOL",
        "name": "干了活就 @",
        "group": "艾特策略",
        "type": "bool",
        "hot": _ENV,
        "default": "1",
        "hint": "画完图/做完视频/发完语音/搜完网页就 @ 对方 —— 活干完人早翻页了，这是主力规则",
    },
    {
        "path": "env:DSH_AT_INTERLEAVE",
        "name": "中间插了几条才 @",
        "group": "艾特策略",
        "type": "int",
        "min": 0,
        "max": 10,
        "hot": _ENV,
        "default": "3",
        "hint": "从他提问到现在别人又说了这么多条才 @。0＝关掉这条规则",
    },
    {
        "path": "env:DSH_AT_SLOW",
        "name": "慢过多久才 @",
        "group": "艾特策略",
        "type": "float",
        "min": 20,
        "max": 300,
        "step": 10,
        "unit": "秒",
        "hot": _ENV,
        "default": "60",
        "hint": "回复慢过这么多秒才 @。实测中位 22 秒，设 60 只兜渠道抽风",
    },

    # ---- 拟人化（dsh-human）----
    {
        "path": "env:DSH_HUMAN",
        "name": "拟人化改写",
        "group": "拟人化",
        "type": "bool",
        "hot": _ENV,
        "default": "1",
        "hint": "总开关。把「短句，短句」这种书面句式拆成两条短消息、去掉句末句号",
    },
    {
        "path": "env:DSH_HUMAN_SPLIT_RATE",
        "name": "拆成两条的概率",
        "group": "拟人化",
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "step": 0.05,
        "hot": _ENV,
        "default": "0.7",
        "hint": "不设 100% 是因为每次都拆反而成了新的机器规律",
    },
    {
        "path": "env:DSH_HUMAN_MAX_LEN",
        "name": "超过多少字不拆",
        "group": "拟人化",
        "type": "int",
        "min": 8,
        "max": 60,
        "unit": "字",
        "hot": _ENV,
        "default": "26",
        "hint": "长句拆两半还是两条长消息，不像人",
    },
    {
        "path": "env:DSH_HUMAN_DROP_PERIOD",
        "name": "去掉句末句号",
        "group": "拟人化",
        "type": "bool",
        "hot": _ENV,
        "default": "1",
        "hint": "真人只有 0.5% 用句末句号，书面句号是明显的 AI 味",
    },

    # ---- 说话风格（dsh-style）----
    {
        "path": "env:DSH_STYLE",
        "name": "学群里真人的语感",
        "group": "说话风格",
        "type": "bool",
        "hot": _ENV,
        "default": "1",
        "hint": "总开关。挑本群真人的短句给模型看，让它的句子长度和语气贴近这个群。"
                "给例子比给规则有效：实测从 16.5 字降到 9.5 字且没变干",
    },
    {
        "path": "env:DSH_STYLE_N",
        "name": "给几条例句",
        "group": "说话风格",
        "type": "int",
        "min": 1,
        "max": 15,
        "hot": _ENV,
        "default": "6",
        "hint": "4~6 条就够，再多只是挤占上下文",
    },
    {
        "path": "env:DSH_STYLE_MAX_LEN",
        "name": "例句最长多少字",
        "group": "说话风格",
        "type": "int",
        "min": 4,
        "max": 40,
        "unit": "字",
        "hot": _ENV,
        "default": "18",
        "hint": "要教「短」，太长的没有示范价值",
    },
    {
        "path": "env:DSH_STYLE_LOOKBACK",
        "name": "从多少条里挑例句",
        "group": "说话风格",
        "type": "int",
        "min": 20,
        "max": 240,
        "step": 20,
        "unit": "条",
        "hot": _ENV,
        "default": "160",
        "hint": "越小越跟得上当下的群风",
    },
    {
        "path": "env:DSH_STYLE_PER_PERSON",
        "name": "同一个人最多几条",
        "group": "说话风格",
        "type": "int",
        "min": 1,
        "max": 5,
        "hot": _ENV,
        "default": "2",
        "hint": "避免例句全是话最多那个人的",
    },
    {
        "path": "env:DSH_STYLE_TELL_MEDIAN",
        "name": "告诉它本群平均句长",
        "group": "说话风格",
        "type": "bool",
        "hot": _ENV,
        "default": "1",
        "hint": "顺便给个「本群一句话中位多少字」的锚点",
    },

    # ---- 情绪（dsh-emotion）----
    {
        "path": "env:DSH_EMOTION",
        "name": "情绪系统",
        "group": "情绪",
        "type": "bool",
        "hot": _ENV,
        "default": "1",
        "hint": "总开关。被骂了会不爽、被夸了会开心，并影响之后一段时间的说话语气",
    },
    {
        "path": "env:DSH_EMOTION_SHADOW",
        "name": "只记录不影响语气（影子）",
        "group": "情绪",
        "type": "bool",
        "hot": _ENV,
        "default": "1",
        "hint": "开了只写审计日志。注意代码默认是「开」，线上已改成「关」＝情绪真的影响语气",
    },
    {
        "path": "env:DSH_EMOTION_TTL_ANGRY",
        "name": "生气持续多久",
        "group": "情绪",
        "type": "float",
        "min": 60,
        "max": 86400,
        "step": 300,
        "unit": "秒",
        "hot": _ENV,
        "default": "1800",
        "hint": "被攻击后不爽多久",
    },
    {
        "path": "env:DSH_EMOTION_TTL_HAPPY",
        "name": "开心持续多久",
        "group": "情绪",
        "type": "float",
        "min": 60,
        "max": 86400,
        "step": 300,
        "unit": "秒",
        "hot": _ENV,
        "default": "900",
        "hint": "被夸之后高兴多久",
    },
    {
        "path": "env:DSH_EMOTION_TTL_SAD",
        "name": "低落持续多久",
        "group": "情绪",
        "type": "float",
        "min": 60,
        "max": 86400,
        "step": 300,
        "unit": "秒",
        "hot": _ENV,
        "default": "1800",
        "hint": "被冷落或说了难听话之后低落多久",
    },
    {
        "path": "env:DSH_EMOTION_NEUTRAL_RUN",
        "name": "几条平静消息后消气",
        "group": "情绪",
        "type": "int",
        "min": 1,
        "max": 10,
        "hot": _ENV,
        "default": "2",
        "hint": "连着这么多条没有情绪信号的消息，就把当前心情放掉",
    },
    {
        "path": "env:DSH_EMOTION_NAMES",
        "name": "算在喊它的名字",
        "group": "情绪",
        "type": "csv",
        "hot": _ENV,
        "default": "大肥鱼,小鲸鱼,肥鱼",
        "hint": "群里哪些叫法算在喊它 —— 用来判断这句话是不是针对它说的",
    },
]

# 22 个插件的中文名与用户分组。插件页即使某个插件尚未启用，也能显示完整能力清单。
PLUGIN_GROUPS = {
    "dsh-decide": "会不会说话",
    "dsh-initiate": "会不会说话",
    "dsh-mention": "会不会说话",
    "dsh-human": "说话的样子",
    "dsh-style": "说话的样子",
    "dsh-emotion": "说话的样子",
    "dsh-imagegen": "能发什么",
    "dsh-video": "能发什么",
    "dsh-voice": "能发什么",
    "dsh-sticker": "能发什么",
    "dsh-imgctx": "看得懂什么",
    "dsh-vischain": "看得懂什么",
    "dsh-fwd": "看得懂什么",
    "dsh-web": "看得懂什么",
    "dsh-quote": "看得懂什么",
    "dsh-ctxclean": "看得懂什么",
    "dsh-memory": "记住什么",
    "dsh-guard": "群管理与安全",
    "dsh-acl": "群管理与安全",
    "dsh-claimguard": "群管理与安全",
    "dsh-poke": "社交小动作",
    "dsh-welcome": "社交小动作",
}
PLUGIN_LABELS = {
    "dsh-acl": "指令权限", "dsh-claimguard": "防口头认定", "dsh-ctxclean": "上下文清理",
    "dsh-decide": "插话判断", "dsh-emotion": "情绪", "dsh-fwd": "转发记录",
    "dsh-guard": "违规禁言", "dsh-human": "拟人化", "dsh-imagegen": "出图",
    "dsh-imgctx": "图片上下文", "dsh-initiate": "冷场开口", "dsh-memory": "长期记忆",
    "dsh-mention": "艾特策略", "dsh-poke": "回戳", "dsh-quote": "引用理解",
    "dsh-sticker": "表情包", "dsh-style": "群聊语感", "dsh-video": "视频",
    "dsh-vischain": "识图主备", "dsh-voice": "语音", "dsh-web": "联网搜索",
    "dsh-welcome": "入群欢迎",
}

# 模式和动作继续保持服务端白名单；模式只使用热配置，避免一键操作后用户误以为
# env 已经在运行中的容器生效。
MODES = [
    {
        "id": "normal", "name": "正常模式", "desc": "恢复常用的主动回复设置", "danger": False,
        "knobs": {
            "provider_ltm_settings.active_reply.enable": True,
            "provider_ltm_settings.active_reply.possibility_reply": 0.25,
            "platform_settings.rate_limit.count": 8,
        },
        "models": {"chat": "deepseek-v4-flash-0731", "vision": "vision-opus5"},
    },
    {
        "id": "quiet", "name": "安静模式", "desc": "只响应明确召唤，减少群内主动发言", "danger": False,
        "knobs": {
            "provider_ltm_settings.active_reply.enable": False,
            "provider_ltm_settings.active_reply.possibility_reply": 0.0,
        },
    },
    {
        "id": "mute", "name": "闭嘴模式", "desc": "关闭主动回复和媒体、联网插件", "danger": False,
        "knobs": {
            "provider_ltm_settings.active_reply.enable": False,
            "provider_ltm_settings.active_reply.possibility_reply": 0.0,
        },
        "plugins": {"dsh-imagegen": False, "dsh-voice": False, "dsh-video": False, "dsh-web": False},
    },
    {
        "id": "media_on", "name": "全媒体模式", "desc": "打开画图、视频、语音和联网插件", "danger": False,
        "knobs": {"provider_ltm_settings.active_reply.enable": True},
        "plugins": {"dsh-imagegen": True, "dsh-voice": True, "dsh-video": True, "dsh-web": True},
    },
    {
        "id": "cheap", "name": "省钱模式", "desc": "使用便宜模型并限制工具步数", "danger": False,
        "knobs": {
            "provider_ltm_settings.active_reply.possibility_reply": 0.15,
            "provider_settings.max_agent_step": 10,
        },
        "models": {"chat": "glm-5.3", "vision": "zhipu-vision"},
    },
    {
        "id": "strong", "name": "增强模式", "desc": "使用主模型并保留较长上下文", "danger": False,
        "knobs": {
            "provider_settings.max_context_length": 40,
            "provider_settings.max_agent_step": 30,
        },
        "models": {"chat": "deepseek-v4-flash-0731", "vision": "vision-opus5"},
    },
    {
        "id": "lively", "name": "活跃模式", "desc": "提高主动回复概率和媒体能力", "danger": False,
        "knobs": {
            "provider_ltm_settings.active_reply.enable": True,
            "provider_ltm_settings.active_reply.possibility_reply": 0.45,
        },
        "models": {"chat": "glm-5.3", "vision": "vision-opus5"},
        "plugins": {"dsh-imagegen": True, "dsh-voice": True, "dsh-video": True, "dsh-web": True},
    },
]
ACTIONS = [
    {"id": "restart_astrbot", "name": "重启 AstrBot", "desc": "让环境变量和插件重新加载", "danger": True, "enabled": True},
    {"id": "recompose_astrbot", "name": "重载环境变量", "desc": "重建 AstrBot 容器并读取 imagegen.env", "danger": True, "enabled": True},
    {"id": "restart_napcat", "name": "重启 NapCat", "desc": "重新连接 QQ", "danger": True, "enabled": True},
]

MODE_IDS = {m["id"] for m in MODES}
ACTION_IDS = {a["id"] for a in ACTIONS if a.get("enabled", True)}

# 其余插件的总开关与少量高价值旋钮。用小构造器减少重复元数据，但最终仍展开成
# 普通 dict，schema 对外格式稳定，未来服务端增删单项不会要求 APK 重装。
def _env(path, name, group, kind="bool", default=None, hint="", **extra):
    item = {"path": "env:" + path, "name": name, "group": group,
            "type": kind, "hot": False, "hint": hint}
    if default is not None:
        item["default"] = default
    item.update(extra)
    return item

KNOBS.extend([
    _env("DSH_GUARD", "违规判定与禁言", "群管理与安全", default="1", hint="总开关；关闭后不再判定或禁言"),
    _env("DSH_GUARD_SHADOW", "禁言影子模式", "群管理与安全", default="1", hint="只判定写日志，不真正禁言；线上是 0，打开才会变成影子"),
    _env("DSH_GUARD_BAN_SEC", "一般违规禁言时长", "群管理与安全", "int", "600", "一般违规禁言多久", min=60, max=1800, unit="秒"),
    _env("DSH_GUARD_BAN_SEC_HIGH", "严重违规禁言时长", "群管理与安全", "int", "1800", "严重违规禁言多久", min=60, max=1800, unit="秒"),
    _env("DSH_GUARD_WARN_TIMES", "累计几次才禁言", "群管理与安全", "int", "2", "中等违规在窗口内累计到几次才真正禁言", min=1, max=10),
    _env("DSH_GUARD_GROUPS", "在哪些群管违规", "群管理与安全", "csv", "", "填群号，留空表示所有群", item_pattern=r"\d{5,12}"),
    _env("DSH_ACL", "指令权限门卫", "群管理与安全", default="1", hint="总开关；关闭后管理指令人人可用"),
    _env("DSH_ACL_OWNER", "群主 QQ 号", "群管理与安全", "csv", "100000002", "能用最高权限管理指令的 QQ 号", item_pattern=r"\d{5,12}"),
    _env("DSH_ACL_SCOPE", "权限检查范围", "群管理与安全", "enum", "both", "在哪些会话检查权限", options=["group", "private", "both"]),
    _env("DSH_ACL_QUIET", "越权时静默拦截", "群管理与安全", default="0", hint="拦下越权指令时不发送提示"),
    _env("DSH_CLAIM_ENABLE", "防口头认定", "群管理与安全", default="1", hint="不把别人单方面宣布的输赢、称呼和身份当成事实"),
    _env("DSH_MEM_ENABLE", "长期记忆", "记住什么", default="1", hint="保存群友档案和群共同记忆"),
    _env("DSH_MEM_MAX_FACTS", "每人最多记几条", "记住什么", "int", "12", "每个人最多保留多少条资料", min=3, max=40),
    _env("DSH_MEM_INJECT_BUDGET", "记忆注入上限", "记住什么", "int", "520", "每次最多塞多少字的记忆给模型", min=100, max=2000, unit="字"),
    _env("DSH_MEM_BUFFER", "原话缓冲条数", "记住什么", "int", "240", "每个群保留多少条原话供记忆和语感使用", min=60, max=1000),
    _env("DSH_MEM_DAILY_CAP", "每天整理上限", "记住什么", "int", "200", "全局每天最多整理多少次资料", min=20, max=1000),
    _env("DSH_IMGCTX", "历史图片上下文", "看得懂什么", default="1", hint="让机器人能看懂前几条消息里的图片和表情包"),
    _env("DSH_IMGCTX_LOOKBACK", "回看图片消息数", "看得懂什么", "int", "4", "往回翻几条消息找图片", min=1, max=10),
    _env("DSH_IMGCTX_MAX_IMAGES", "每轮最多看几张图", "看得懂什么", "int", "2", "防止连续图片烧掉视觉额度", min=1, max=5),
    _env("DSH_IMGCTX_GIF_FRAMES", "动图抽几帧", "看得懂什么", "int", "4", "动图拼图时抽取的帧数", min=1, max=9),
    _env("DSH_VIS_CHAIN_ENABLE", "识图主备链", "看得懂什么", default="1", hint="识图主模型失败时自动换备用模型"),
    _env("DSH_VIS_CHAIN", "识图渠道顺序", "看得懂什么", "csv", "vision-opus5,vision-opus5-thinking,zhipu-vision", "前面的识图渠道优先", max_len=300),
    _env("DSH_VIS_RETRY", "识图重试次数", "看得懂什么", "int", "2", "每一档渠道原地重试几次", min=0, max=5),
    _env("DSH_FWD", "读取转发记录", "看得懂什么", default="1", hint="展开群里的转发聊天记录给模型理解"),
    _env("DSH_FWD_MAX_DEPTH", "转发展开层数", "看得懂什么", "int", "3", "嵌套转发最多展开几层", min=1, max=5),
    _env("DSH_FWD_MAX_CHARS", "转发注入字数", "看得懂什么", "int", "2000", "一段转发最多给模型看多少字", min=500, max=8000),
    _env("DSH_WEB_ENABLE", "联网功能", "联网搜索", default="1", hint="自动读取链接、搜索网页和查询 B 站"),
    _env("DSH_WEB_TIMEOUT", "网页超时", "联网搜索", "int", "20", "抓一个网页最多等几秒", min=5, max=60, unit="秒"),
    _env("DSH_WEB_MAX_URLS", "每条消息最多抓几个链接", "联网搜索", "int", "2", "防止一条消息触发大量网络请求", min=1, max=5),
    _env("DSH_WEB_SEARCH_ENGINE", "搜索引擎档位", "联网搜索", "enum", "search_std", "搜索接口使用的档位", options=["search_std", "search_pro", "search_pro_sogou", "search_pro_quark", "search_pro_jina"]),
    _env("DSH_QUOTE", "引用理解", "看得懂什么", default="1", hint="说明引用了谁、引用内容是谁说的"),
    _env("DSH_QUOTE_MAX", "引用最多带多少字", "看得懂什么", "int", "120", "引用只作为背景，不挤占当前问题", min=20, max=500, unit="字"),
    _env("DSH_CTX_CLEAN_BLOCKS", "清理过期上下文", "上下文清理", default="1", hint="清掉历史里过期的群聊上下文、时间提醒和搜索块"),
    _env("DSH_CTX_DROP_THINK", "清理历史 think", "上下文清理", default="1", hint="不把机器人过去的内心戏重复发送给模型"),
    _env("DSH_CTX_KEEP_TURNS", "保留原始对话轮数", "上下文清理", "int", "30", "只留最近多少轮原始对话，0 表示不限", min=0, max=100),
    _env("DSH_IMG_AUTO", "画图自动兜底", "能发什么", default="1", hint="模型嘴上答应画图却没调用工具时自动补画"),
    _env("DSH_IMG_SIZE", "图片尺寸", "能发什么", "enum", "1024x1024", "出图尺寸", options=["1024x1024", "1024x1536", "1536x1024"]),
    _env("DSH_IMG_TIMEOUT", "出图超时", "能发什么", "int", "120", "一张图最多等几秒", min=30, max=300, unit="秒"),
    _env("DSH_VID_UNDERSTAND", "看视频", "能发什么", default="1", hint="抽取视频帧并让视觉模型理解"),
    _env("DSH_VID_GEN", "生成视频", "能发什么", default="1", hint="允许后台生成并发送视频"),
    _env("DSH_VID_FRAMES", "视频抽帧数量", "能发什么", "int", "4", "看视频时抽取几帧", min=1, max=8),
    _env("DSH_VID_GEN_SECONDS", "生成视频时长", "能发什么", "enum", "5", "渠道支持的时长", options=["5", "10"]),
    _env("DSH_VOICE_AUTO", "语音自动兜底", "能发什么", default="1", hint="用户要语音而模型只发文字时自动合成语音"),
    _env("DSH_VOICE_FORMAT", "语音格式", "能发什么", "enum", "wav", "语音输出格式", options=["wav", "mp3"]),
    _env("DSH_VOICE_MAX_CHARS", "语音最多念多少字", "能发什么", "int", "120", "一条语音超过这个长度会截断", min=20, max=300, unit="字"),
    _env("DSH_STICKER_QUOTA", "表情包频率限制", "能发什么", default="1", hint="限制表情包刷屏频率"),
    _env("DSH_STICKER_WINDOW", "表情包统计窗口", "能发什么", "int", "2", "最近多少次想发表情里限制频率", min=1, max=6),
    _env("DSH_POKE", "回戳", "社交小动作", default="1", hint="别人戳机器人时偶尔戳回去"),
    _env("DSH_POKE_BACK_RATE", "回戳概率", "社交小动作", "float", "1.0", "被戳后回戳的概率", min=0, max=1, step=0.05),
    _env("DSH_POKE_TALK_RATE", "回戳时说话概率", "社交小动作", "float", "0.25", "回戳的同时附一句话的概率", min=0, max=1, step=0.05),
    _env("DSH_WELCOME", "入群欢迎", "社交小动作", default="1", hint="新人进群时自动生成欢迎语"),
    _env("DSH_WELCOME_GROUPS", "在哪些群欢迎", "社交小动作", "csv", "", "填群号，留空表示所有群", item_pattern=r"\d{5,12}"),
])

# 允许改的路径集合（服务端唯一判据）
ALLOWED_PATHS = {k["path"] for k in KNOBS}
ENV_PATHS = {k["path"] for k in KNOBS if k["path"].startswith("env:")}
