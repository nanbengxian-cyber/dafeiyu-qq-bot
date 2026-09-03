# -*- coding: utf-8 -*-
"""控制台的「能力清单」—— 纯数据，不含逻辑。

为什么单独一个文件：APK 不写死界面，它按 /api/console/schema 返回的东西渲染。
所以以后想多一个开关、多一个模式，只改这里 + 重启 qrweb 服务，
手机上下拉刷新就出现了，**不用重新装包**。

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
"""

# ---------------------------------------------------------------- 配置旋钮
#
# type: bool / int / float / enum
# path: cmd_config.json 里的点分路径
# min/max/step: 数值范围（服务端强制校验，越界直接 400，不做静默钳制）
# hot: True 表示改完立刻生效，无需重启容器
# hint: 手机上显示的一句话说明

KNOBS = [
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
        "hint": "群里每条消息有多大概率主动接话。随机插话不走限流，群越活跃它说得越多",
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
        "hint": "一般保持关闭：联网走 dsh-web 插件，两套同时开会重复搜",
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
]

# 允许改的路径集合（服务端唯一判据）
ALLOWED_PATHS = {k["path"] for k in KNOBS}


# ---------------------------------------------------------------- 一键模式
#
# knobs:   要写的配置（路径 -> 值）
# models:  {"chat": 模型名, "vision": provider_id}，省略表示不动
# plugins: {插件名: True/False}，省略表示不动
#
# 模式只用「热生效」的东西：配置走框架自己的保存接口（会 reload 流水线），
# 模型走 provider/update（框架说「已经实时生效」），插件走 plugin/on|off。
# 所以切模式不重启容器、不掉 QQ 连接。
#
# 故意不做的：凡是要改 imagegen.env 的（识图链、语音音色这些 env 旋钮）都不放进模式，
# 因为那必须 `docker compose up -d astrbot` 重建容器，机器人会哑 15~25 秒。
# 需要动 env 的场合走 ACTIONS 里的显式按钮，让人知道自己在重启。

MODES = [
    {
        "id": "quiet",
        "name": "安静",
        "emoji": "🤫",
        "desc": "群里嫌它吵的时候用。只在被叫到时回，主动插话关掉",
        "knobs": {
            "provider_ltm_settings.active_reply.enable": False,
            "platform_settings.rate_limit.count": 4,
            "platform_settings.segmented_reply.enable": False,
        },
    },
    {
        "id": "normal",
        "name": "日常",
        "emoji": "🙂",
        "desc": "现在这套调好的参数：插话 25%、留 40 轮、限流 8/60s",
        "knobs": {
            "provider_ltm_settings.active_reply.enable": True,
            "provider_ltm_settings.active_reply.possibility_reply": 0.25,
            "provider_settings.max_context_length": 40,
            "provider_settings.identifier": True,
            "platform_settings.rate_limit.count": 8,
            "platform_settings.rate_limit.time": 60,
            "provider_settings.show_tool_use_status": False,
            "provider_settings.show_tool_call_result": False,
        },
        "models": {"chat": "deepseek-v4-flash-0731", "vision": "vision-opus5"},
    },
    {
        "id": "lively",
        "name": "活跃",
        "emoji": "🔥",
        "desc": "群冷的时候用。插话概率翻倍到 50%，限流放到 16 条",
        "knobs": {
            "provider_ltm_settings.active_reply.enable": True,
            "provider_ltm_settings.active_reply.possibility_reply": 0.5,
            "platform_settings.rate_limit.count": 16,
        },
    },
    {
        "id": "cheap",
        "name": "省钱",
        "emoji": "💰",
        "desc": "识图换回智谱 flash（1.9 秒、提示词几百 token，opus 那条链是 7400），"
                "聊天用最便宜的 flash，历史砍到 20 轮",
        "knobs": {
            "provider_settings.max_context_length": 20,
            "provider_settings.max_agent_step": 10,
        },
        "models": {"chat": "deepseek-v4-flash-0731", "vision": "zhipu-vision"},
    },
    {
        "id": "strong",
        "name": "强力",
        "emoji": "🧠",
        "desc": "聊天换 glm-5.3、识图走 opus-5 三档链，历史给到 60 轮。慢一些但聪明",
        "knobs": {
            "provider_settings.max_context_length": 60,
            "provider_settings.max_agent_step": 30,
        },
        "models": {"chat": "glm-5.3", "vision": "vision-opus5"},
    },
    {
        "id": "mute",
        "name": "闭嘴",
        "emoji": "🔇",
        "desc": "彻底不说话但 QQ 保持在线（掉线要重新扫码，所以不停 napcat）。"
                "关掉所有媒体插件和主动插话，限流压到 1 条",
        "knobs": {
            "provider_ltm_settings.active_reply.enable": False,
            "platform_settings.rate_limit.count": 1,
            "platform_settings.rate_limit.time": 600,
        },
        "plugins": {
            "dsh-imagegen": False,
            "dsh-voice": False,
            "dsh-video": False,
            "dsh-web": False,
        },
    },
    {
        "id": "media_on",
        "name": "全媒体",
        "emoji": "🎨",
        "desc": "把出图/语音/视频/联网四个插件全打开",
        "plugins": {
            "dsh-imagegen": True,
            "dsh-voice": True,
            "dsh-video": True,
            "dsh-web": True,
        },
    },
]

MODE_IDS = {m["id"] for m in MODES}


# ---------------------------------------------------------------- 动作
#
# danger=True 的动作，APK 会弹二次确认。
# 这些是真会造成中断的操作，所以不塞进「一键模式」里偷偷做。

ACTIONS = [
    {
        "id": "restart_astrbot",
        "name": "重启机器人",
        "desc": "docker restart astrbot。约 15~25 秒不回消息，QQ 连接不掉",
        "danger": True,
    },
    {
        "id": "recompose_astrbot",
        "name": "重载环境变量",
        "desc": "docker compose up -d astrbot。改过 imagegen.env 才需要，同样 15~25 秒",
        "danger": True,
    },
    {
        "id": "restart_napcat",
        "name": "重启 QQ 客户端",
        "desc": "docker restart napcat。会掉线并换二维码，**大概率要重新扫码**",
        "danger": True,
    },
    {
        "id": "clear_context",
        "name": "清当前群历史",
        "desc": "留着占位，暂未实装",
        "danger": True,
        "enabled": False,
    },
]

ACTION_IDS = {a["id"] for a in ACTIONS if a.get("enabled", True)}


# ---------------------------------------------------------------- 插件说明
# 手机上显示中文名比 dsh-xxx 好认。没列的插件按原名显示。

PLUGIN_LABELS = {
    "dsh-imagegen": "出图",
    "dsh-voice": "语音",
    "dsh-video": "视频",
    "dsh-web": "联网搜索",
    "dsh-sticker": "表情贴纸",
    "dsh-mention": "智能 @",
    "dsh-memory": "群员记忆",
    "dsh-ctxclean": "上下文清理",
    "dsh-welcome": "入群欢迎",
    "dsh-imgctx": "图片上下文",
    "dsh-vischain": "识图故障转移",
}
