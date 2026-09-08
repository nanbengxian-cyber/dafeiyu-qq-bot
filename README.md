# 大肥鱼 QQ 机器人 · DaFeiYu QQ Bot

[中文](#中文) · [English](#english)

一套让 QQ 群机器人「像真人群友一样说话」的完整工程：45 个 AstrBot 插件、一个 QQ↔AI 桥接程序、一个安卓控制台 App，以及记录每个问题根因与实测数据的技术文档。

A complete engineering effort to make a QQ group bot *talk like an actual group member*: 45 AstrBot plugins, a QQ↔AI bridge, an Android console app, and technical documents recording the root cause and measured data behind every fix.

---

## 中文

### 这是什么

一个跑在 QQ 群里的 AI 群友（人格叫「小鲸鱼」）。它不是客服式问答机器人 —— 目标是**混在群里像个真人**：会潜水、会插话、会发表情包、会戳回去、看得见图片和视频、记得住群友是谁。

仓库包含四块可独立使用的东西：

| 目录 | 内容 | 语言 |
|---|---|---|
| [`plugins/`](plugins/) | 45 个 AstrBot 插件 —— 机器人的全部能力 | Python |
| [`bridge/`](bridge/) | QQ ↔ DeepSeek Harness 桥接（另一条技术路线） | Node.js |
| [`console/`](console/) | 安卓控制台 App + 服务端后台 | Java / Python |
| [`docs/`](docs/) | 部署手册与 14 份问题根因分析 | Markdown |

### 最新更新 · 2026-09-08（整体整合：45 个插件连成断点管线）

这之后把仓库整理到了**45 个插件**。前面每一批都是在单点修故障，这一批是第一次把所有插件
**当成一套管线来布线**——让「像真人」这件事不再是一堆各自为战的开关，而是按优先级串联起来。

- **去 AI 味是一条断点管线。** `dsh-aiflavour`（先给回复「上色」，注入语气/人味的骨架）→
  `dsh-humanizer`（人味化改写，把句子掰回真人口语）→ `dsh-typo`（偶尔错一个字）→
  `dsh-noise`（噪点：换单字 / 截断 / 叠词）。四段依次吃掉机器人的「整齐」。
- **拦截组优先于注入组。** 插件按两类优先级布线：`armor` / `merge` / `decide` 这一类先跑
  （priority=2000），先判断「这句要不要接、怎么接」；`effect` / `emotion` 这类后跑
  （priority=100），负责给已经决定要说的句子调制语气。接不接是前提，怎么说是修饰。
- **互相联动。** `dsh-steal` 偷表情包要识图理解含义、只偷「多发」的同图、不宜公开的不偷（fail-close）；
  `dsh-noise` 的噪点**绝不**作用于被 @ 的正经回答，任何异常放行；`dsh-proactive` 的兴趣探头
  用纯正则零成本评分，白米饭 / 美食 / 深海鲸 / DeepSeek / 点名大肥鱼 命中才主动接话。
- **新增一批能力插件。** 事实护栏（`dsh-factguard`）、防泄露（`dsh-leakguard`）、同音昵称
  （`dsh-homophone`）、入群防护（`dsh-joinguard`）、被动学黑话（`dsh-listen`）、引用解析
  （`dsh-quoteref`）、场景判定（`dsh-scene`）、自保（`dsh-selfguard`）、付费额度（`dsh-pay`）。
- **已编译落地。** 45 个插件全部编译通过、加载无失败，管线接线按上面的优先级实装到线上。

验证：管线断裂处（每一段只依赖上一段的输出、不绕行）已确认；45 插件完整加载 0 失败。
这一批改动面大，README 的插件表也已重排到 45 个。

### 上一次更新 · 2026-09-05 晚（真人感第二批：黑话 / 打字节奏 / 错别字 / 效果观察）

四个新插件加一处框架配置。这一批跟前面几批性质不同 —— 前面修的都是**明确的故障**
（答非所问、被骗认输、认错人、崩溃），这一批修的是「没有故障，但一眼能看出是 AI」。

- **听懂群里的黑话（`dsh-glossary`）。** 43 条词条，命中才注入。词条不是抄现成热梗库 ——
  三个外部库合计 549 个词，拿本群 2478 条真语料回测只真命中 9 条，其中命中最多的词是
  `15`（对应「被蹲了 15 次」和一个 QQ 号）。改成挖自己的语料，判据是
  「这条短语被当成一整条消息发出来、且至少两个人发过」，一次就捞出全表命中最高的「神了」。
- **打字延迟按长度算。** 原来固定 1.2~2.8 秒随机，跟长度无关，所以有时是反的：
  一条「典」等 2.8 秒，一条二十字的 1.2 秒就出来。框架自带 `interval_method: "log"`，
  我们没开 —— 改一行配置，1 字变 0.7 秒、20 字变 3.2 秒。
- **偶尔打同音错别字（`dsh-typo`）。** 每条 6% 概率、一条最多错一个字、35% 概率补一条
  「\*的」自我纠正。用**人工核过的 60 组固定同音表**而不是 `pypinyin` 动态生成：
  动态生成的输出集合是开放的，在 94 人的真群里不赌生僻字。
- **允许被支线勾走（`dsh-drift`）。** 「永远严格贴着上一条回答」本身就是 AI 味。
  两条**结构性**硬边界防止把已修的「答非所问」引回来：被 @ 时绝不漂移、
  当前消息像在提问时绝不漂移。
- **有人在跟别的 AI 说话就不抢话（`dsh-decide`）。** 参考实现允许名字后面跟空格，
  原样搬过来在本群有两条假命中（`gpt progpt plus`、`gpt pro的缓存差不多95%左右`）——
  本群成天在聊模型，模型名就是话题词。收紧成「只认标点分隔符、不收 `GPT`/`ds` 这种短写法」后
  1899 条语料假命中归零。
- **说完之后看群里什么反应（`dsh-effect`）。** 这是最大的结构性缺口：在此之前所有改动都是
  **开环**的 —— 注入样本、注入词表，然后祈祷。现在每条回复后开 180 秒观察窗口，
  判断群里的反应（认可 / 跟着玩 / 平淡 / 没看懂 / 指出说错 / 嫌烦 / **没人理**）、
  反应冲的是**内容还是人设**、以及这句话**推进还是带偏**了对话。
  窗口内没人发言直接判「没人理」，不花模型调用。
  **这一版只测量、不自动改行为** —— 拿没验证过的信号自动拧旋钮，等于没有仪表就调发动机。

验证：黑话词表审查后注入块从 196 字压到 135 字、命中率不变（8.4%），证明删掉的 3 条是死词；
新插件四套纯函数单测 + 1899 条真语料回测 + `dsh-effect` 一次真实端到端结算全过。
细节见 [47-真人感第二批](docs/47-真人感第二批-黑话与打字节奏与效果观察.md)。

### 上一次更新 · 2026-09-05 白天（控制台改成服务端驱动）

安卓控制台 App 从「写死 14 个旋钮」改成**按服务端下发的 schema 渲染**。这解决的是一个很实际的问题：
插件从 16 个长到 22 个、可调变量长到 248 个之后，每加一个开关就要重新打包装 APK，根本跟不上。

- **一次暴露 104 个旋钮**，覆盖 22 个插件的总开关与主要调参（此仓库已开源其中 19 个插件）。
- **加功能不用重装 App。** 只要新旋钮用的是 App 已认得的类型（`bool` `int` `float` `enum` `str` `csv`），
  在服务端 `console_spec.py` 里加一条、重启后台，手机下拉刷新就出现。App 遇到认不出的类型是**跳过**而不是崩，
  这是向前兼容的关键。只有新增一种控件（比如以后要画时间选择器）才需要更新 APK。
- **插件开关直接读写 `imagegen.env`**（22 个插件的变量都在这个 `env_file` 里），行级替换：
  保留全部注释与顺序、保持原文件权限、原子替换、写前核对 MD5 防并发覆盖。
  那 55 行注释记着每个值为什么这么设，比变量本身值钱。
- **踩到的三条硬约束，全部由服务端兜住**：
  ① 布尔只能写 `1`/`0` —— 22 个插件对布尔有 4 种解析写法，其中 11 个变量用 `!= "0"` 判定，写 `false` 会被当成**开**；
  ② `int`/`float` 填非数字或留空会让插件 **import 时抛异常、整个插件加载失败**（只有 `dsh-memory` 有 try/except 回落），
  所以每个数值旋钮都带 min/max，越界直接 400 而不静默钳制；
  ③ 代码里 `max()/min()` 硬夹紧的变量，滑块下限必须写成它的硬下限，否则会出现「手机显示 1、实际生效 3」的错觉。
- **改完 env 必须重建容器。** astrbot 的环境变量来自 docker compose 的 `env_file`，容器内 `os.environ` 只在**容器重建**时更新 ——
  `docker restart` 不重读 `env_file`（这个坑踩过）。所以这类旋钮一律标 `hot=false`，界面上写明「要按一次重载环境变量」。
- **7 个敏感变量只报「已设置 / 未设置」**（画图 / 语音 / 视频 / 搜索的 API  key 与音色 ID），真实值一个字节都不出服务器，
  手机端也改不了 —— 控制台是明文 HTTP。
- **自更新只提示、不静默安装。** 新增轻量端点 `/api/console/version`（只 stat 一个文件），
  服务器上的包 `versionCode` 更新时弹一次提示、点「打开下载」跳浏览器。明文 HTTP 上自动装包不可控，所以刻意不做。

验证：后端 **105 项**单测、端到端 **83 项**、纯 JVM **391 项**，加上部署后 **18 项**真机实测全过。

### 更早 · 2026-09-04

三块新能力已在真群上线（不再是影子模式）：

- **冷场会自己开口。** 安静超过 15 分钟它自己起个话头，每天最多 3 次、两次间隔 1 小时、只在活跃时段（本群实测 10:00–02:59，03:00–09:59 几乎无人发言）。开口走完整消息管道，所以贴纸、@ 策略、分段发送全部照常。判断失败或超时一律沉默 —— fail-**closed**，因为主动说错话的代价大于不说话。
- **说话带情绪。** 六种情绪同时只有一种，一条消息最多触发一种。必须有「针对谁」的结构证据才算：群友互骂、自嘲、转述第三方、整句引用都不改变它的情绪。清零走道歉 / 连续 2 条中性 / 问题被回答 / 分情绪 TTL 四条路，而不是只等超时。
- **看得懂引用了。** 按 QQ 号说清「谁在说 / 引用谁的哪句 / 是不是自己说的 / @ 的是谁」。

同时修掉一个藏了半天的真崩溃：`dsh-decide` 把三元组按两元组解包，只要机器人刚说过话就必抛异常，当天崩 12 次 / 成功 9 次。因为兜底是 fail-open，表面上一切正常，实际超过一半发言都没收到「这句是谁对谁说的」的判断 —— 这就是「主谓宾弄错」的直接原因。

上线前：**61 项新增单元测试**（情绪 36 + 引用 25）＋ **1618 条真实历史回放**。回放揪出两个真误判：把「余额 / 额度」里的「额」当成尴尬语气词（9 次尴尬命中里 8 次误判），以及把没有指向的话当成在骂它（38 次命中里 25 次整句没有「你」）。细节见 [46-主动开口与情绪系统](docs/46-主动开口与情绪系统.md)。

### 成品下载

安卓控制台 App 在 [Releases](../../releases) 页面下载（约 121 KB，安卓 5.0+）。装完在登录页填自己的服务器地址即可，包内不含任何服务器信息。

### 45 个插件在解决什么

每个插件对应一个**实际发生过的问题**，不是功能清单式的堆砌。

**说话质量**

| 插件 | 解决的问题 |
|---|---|
| [`dsh-ctxclean`](plugins/dsh-ctxclean/) | 答非所问。框架把每轮注入的群聊上下文原样存进历史，且 `max_context_length=-1` 不截断，于是每次请求重发上百份互相矛盾的「最新群聊」——25.7 万字里注入块占 31%，真人原话只占 2.2%。 |
| [`dsh-style`](plugins/dsh-style/) | 说话太像 AI。从真人短句里抽样本注入，把平均句长从 16.5 字压到 9.5 字。 |
| [`dsh-decide`](plugins/dsh-decide/) | 主动插话前先用小模型判断「在聊什么、该不该开口」，判沉默就掐掉整次主模型调用，省约 9400 token。 |
| [`dsh-mention`](plugins/dsh-mention/) | 每条回复都 @ 人。只在「调了工具 / 被别人的消息刷走 / 隔太久」时才 @。 |
| [`dsh-claimguard`](plugins/dsh-claimguard/) | 被骗认输。有人说「叫我爸爸」「单挑你输了」它就当真 —— 靠注入事实而不是改人格来修。 |
| [`dsh-initiate`](plugins/dsh-initiate/) | 冷场就一直安静。真人会自己起话头，它不会。五道纯代码闸门（冷场 15 分钟~6 小时、活跃时段、1 小时冷却、每日 3 次）过了才让小模型看一眼有没有值得接的话头；决定开口就造合成事件走**完整消息管道**，贴纸剥离/@ 策略/分段发送全部照常。判断失败一律沉默。 |
| [`dsh-emotion`](plugins/dsh-emotion/) | 语气永远一个样。六种情绪同时只有一种，固定优先级仲裁保证一条消息只触发一种（「你好厉害但也真让我失望」只取低落）。清零四条路：道歉、连续 2 条中性、问题被回答、分情绪 TTL。 |
| [`dsh-quote`](plugins/dsh-quote/) | 看不懂引用，把别人做的事说成自己做的。框架的引用块只给昵称，而群里有真人把昵称改成和机器人一样 —— **凭昵称在群聊里永远认不了人**。改为按 QQ 号写清「谁在说 / 引用谁的哪句 / 是不是自己说的 / 这次 @ 谁」。 |
| [`dsh-glossary`](plugins/dsh-glossary/) | 听不懂群里的黑话。43 条词条，命中才注入，带真语料例句（只给释义时它听得懂「典」但永远不会自己说「典」）。词条挖自本群语料而非现成热梗库：三个外部库 549 个词只真命中 9 条。假命中比不注入更糟，所以每条带 `avoid` 排除上下文。 |
| [`dsh-human`](plugins/dsh-human/) | 回复的**形状**不像真人。实测真人带逗号 16.4%、机器人 67.7%，「短句，短句」句式机器人占 54.8% 而真人没有。提示词治不了（写在人格里两周照旧），按结构在发送前把逗号粘起来的两句拆成两条。 |
| [`dsh-typo`](plugins/dsh-typo/) | 几千条一个错别字都没有。60 组人工核过的同音表，每条 6% 概率错一个字，35% 概率补一条「\*的」自我纠正。48 条保护名单（黑话词条、人名、贴纸标记、链接）一个字都不许动。 |
| [`dsh-drift`](plugins/dsh-drift/) | 永远严格贴着上一条回答 —— 这种「过度切题」本身就是 AI 味。三档漂移写成分级提示词。两条结构性硬边界防止把已修的答非所问引回来：被 @ 不漂移、像提问不漂移。 |
| [`dsh-effect`](plugins/dsh-effect/) | 所有改动都是开环的：注入完就祈祷，说出去的话有没有落地系统完全不知道。说完开 180 秒观察窗口，判反应（认可/跟着玩/平淡/没看懂/指出说错/嫌烦/没人理）、冲内容还是冲人设、推进还是带偏。窗口内没人发言直接判「没人理」，不花调用。 |
| [`dsh-aiflavour`](plugins/dsh-aiflavour/) | 动态 AI 味拦截。静态强词 + 动态词根学习 + 会话刹车，压机器人「解释/总结」频率高的问题。 |
| [`dsh-humanizer`](plugins/dsh-humanizer/) | 去 AI 味的出口闸门。用 AI 痕迹特征扫回复正文，剥/拦 AI 味句子，是管线里「掰回人话」的那一段。 |
| [`dsh-noise`](plugins/dsh-noise/) | 真人噪点。换单字 / 说一半 / 整句叠词，只对没人叫的接话生效，被 @ 的正经回答绝不动。 |
| [`dsh-armor`](plugins/dsh-armor/) | 防破甲（输入侧注入拦截）。识别复述提示词、无视规则、诱导越权、身份逼问等破甲话术，priority 2000 先于注入组跑。 |
| [`dsh-merge`](plugins/dsh-merge/) | 被 @ 风暴的聚合回复。被 @ 太多次时整合成一条统一回复（总结上面说的、连着答，不点名不 @），不逐条刷屏。 |
| [`dsh-quoteref`](plugins/dsh-quoteref/) | 引用回复。每 N 次提问式 @ 用 QQ 引用回复，并对齐真人引用频率。 |

**多模态**

| 插件 | 解决的问题 |
|---|---|
| [`dsh-imgctx`](plugins/dsh-imgctx/) | 看不见图。框架只转述**当前这条**消息自带的图，而群里最常见的是「先发图，再问这什么」。动图抽 4 帧拼图后识别。 |
| [`dsh-vischain`](plugins/dsh-vischain/) | 识图模型只有一个格子，没有降级。主用模型在 CDN 后面约 20% 概率被弹 403，失败是瞬时的（0.0s），所以重试几乎不花钱。 |
| [`dsh-imagegen`](plugins/dsh-imagegen/) | 嘴上答应却不出图。便宜模型在长上下文里经常不调工具，必须有兜底钩子。 |
| [`dsh-video`](plugins/dsh-video/) | 看视频 + 生成视频。抽 4 帧比传整段快 5 倍、省 18 倍 token，质量一样。生成一次要 4 分钟，绝不能在工具里等（会锁死整个会话）。 |
| [`dsh-voice`](plugins/dsh-voice/) | 语音。内置 TTS 是「全局开关＋概率」，会把所有回复都念出来，立刻出戏。 |
| [`dsh-web`](plugins/dsh-web/) | 联网。链接就在消息里，插件自己抓完注入，不指望模型自觉调工具。 |
| [`dsh-sticker`](plugins/dsh-sticker/) | 表情包。关键是标记**无条件**剥掉 —— 否则 `[贴纸:xx]` 会原样漏进群聊。 |
| [`dsh-listen`](plugins/dsh-listen/) | 听语音。语音条/音轨交给转写服务转成文字，以语音上下文注入 —— 群友发语音它不再只知道「有人发了条语音」。 |

**记忆与群管**

| 插件 | 解决的问题 |
|---|---|
| [`dsh-memory`](plugins/dsh-memory/) | 记不住人。框架自带的只是「最近 20 条」滑动窗口，滑出去就没了。自建缓冲 + 后台抽取，带隐私脱敏、注入防护、按人容量上限、自助 `/忘记我`。 |
| [`dsh-guard`](plugins/dsh-guard/) | 违规禁言。三层：关键词预筛（0 成本，真群实测仅 2.2% 命中）→ 小模型只输出布尔 → **代码**决定禁不禁。 |
| [`dsh-poke`](plugins/dsh-poke/) | 戳一戳没反应。回话不问 LLM，三道限流防对戳循环。 |
| [`dsh-welcome`](plugins/dsh-welcome/) | 入群欢迎。用 LLM 现场生成而不是写死模板（模板会破人设）。 |
| [`dsh-acl`](plugins/dsh-acl/) | 指令谁都能发。三档权限（所有人/群主+管理员/仅群主），身份只按 **QQ 号**判 —— 群里有重名，群名片也能随时改。状态查询也要拦：它们不改东西，但会把渠道地址、模型名、配额、token 花销全打到群里。 |
| [`dsh-fwd`](plugins/dsh-fwd/) | 转发的聊天记录看不见。框架只给一个「[聊天记录]」占位符。按嵌套层数展开，保头保尾，图片视频有独立预算。 |
| [`dsh-spine`](plugins/dsh-spine/) | 被追问就改口、被质疑就道歉。跟 `dsh-claimguard` 的区别是它管的是**立场稳定性**而不是事实。 |
| [`dsh-slang`](plugins/dsh-slang/) | 黑话不用人喂。自动从群聊挖疑似黑话、查好含义进候选，每 8 小时 AI 自己审一遍（转正/拒绝/再等等）。 |
| [`dsh-factguard`](plugins/dsh-factguard/) | 记忆看门狗。问机器人自身属性只按事实表回答，无依据断言直接否认纠正，防乱承认与性别摇摆。 |
| [`dsh-leakguard`](plugins/dsh-leakguard/) | 拦系统提示词泄露。查回复有没有把人格专属标题/指令句原样抄进去。与 `dsh-humanizer` 联动做硬兜底。 |
| [`dsh-homophone`](plugins/dsh-homophone/) | 同音字/谐音识别。谐音称呼当被喊、谐音梗本意注入。 |
| [`dsh-joinguard`](plugins/dsh-joinguard/) | 入群 AI 审核。拿入群问题+答案问 AI 判 approve/reject 并真批/真拒。 |
| [`dsh-scene`](plugins/dsh-scene/) | 群级背景+作息。让机器人知道「这是哪个群、现在几点意味着什么」。 |
| [`dsh-selfguard`](plugins/dsh-selfguard/) | 出口自制。拦自己重复说过的话、群吵架时自己挑衅/骂人的话 —— `dsh-guard` 管群友，它管自己。 |
| [`dsh-pay`](plugins/dsh-pay/) | 赞助/打赏收款引导。反问支付方式→发收款码，仅群主确认到账后才感谢。 |
| [`dsh-proactive`](plugins/dsh-proactive/) | 兴趣探头。纯正则兴趣评分（零 LLM 成本），聊到白米饭/美食/鲸鱼/AI 等话题主动接一句。 |
| [`dsh-interest`](plugins/dsh-interest/) | 兴趣会变。时效热度 + 口味周期轮换「今日馋」，塞状态块让语气带出最近心痒的话题。 |
| [`dsh-steal`](plugins/dsh-steal/) | 偷表情包。识图理解含义，只偷「反复发、能看懂」的存库，不宜公开的不偷（fail-close），`/表情包` 随机发。 |

### 反复踩到的十一个坑

这些教训在多个插件上重复验证过，写在这里省得再踩：

1. **结构判断优于枚举词表。** 意图判定别列举关键词、别掐回复字数。正确形状是「用户要图 + 模型没明确拒绝 + 模型不是在反问」⇒ 出图。宾语过滤用排除法（列画不出的），而不是枚举画得出的。
2. **不触发时必须打日志。** 否则每次排查只能靠猜。`dsh-imagegen` 的「有时候不生图」查了三轮，前两轮全是因为没有日志。
3. **绕过消息管道直接调 LLM 的地方，必须自己复制一份标记剥离逻辑。** `llm_generate` 不走管道，贴纸钩子不会触发，`[贴纸:探头]` 就这样漏进过群聊。
4. **阈值必须由实测分布决定。** `dsh-mention` v1 有条「延迟 ≥8s 就 @」的规则，但量到的是机器人自己想了多久 —— 一次带人格的调用普遍 5~15 秒，于是等价于「无条件 @」，24 小时 8 条回复 100% 都 @ 了。
5. **fail-open 会把崩溃伪装成质量下降。** `dsh-decide` 有个三元组按两元组解包的 bug，只要机器人刚说过话就必抛 `ValueError`，整次判断落到 fail-open「照旧说话」——**当天崩 12 次、成功 9 次**，超过一半发言模型都没收到「这句是谁对谁说的」。功能看起来完全正常，只是说得不对。凡是 fail-open 的兜底，必须在兜底路径上打 WARN 并计数。
6. **命中数归零不等于误判修好了。** 收紧「提问」规则后 `dsh-emotion` 的好奇命中从 14 掉到 0，看着像不再误判，实际是全漏了 —— 群里最常见的追问「那你为什么没有」疑问词在句中，而新正则只锚定句首。规则改动必须逐样本对比，不能只看总数。
7. **影子模式的价值在于回放，不在于等。** 挂着跑一晚只能看到当晚那几条；拿 1618 条历史语料回放，两个真误判 20 分钟就暴露了（把「余额/额度」当成尴尬语气词、把没有指向的话当成在骂它）。

8. **自己群的语料 > 任何现成词库。** 三个外部热梗库合计 549 个词，在本群真命中 9 条；本群语料一次挖掘就贡献了全表命中最高的词。有效的结构信号是「这条短语被当成一整条消息发出来、且至少两个人发过」—— 口头禅和梗才会被单独发出来当一整条消息，普通词永远不会。
9. **先问模型知不知道，再决定要不要教它。** 43 个黑话词条逐条问主模型，它一条都没说「不确定」，绝大多数直接答对。这直接改变了插件的定位：从「词典」变成「语用层」，释义压到最短，力气花在它答错的 6 个词和它推不出的本群惯例上。没做这次探针，就会一直在维护一张模型不需要的词典。
10. **抄参考实现的判据必须重新回测。** MaiBot 的「别的 AI 被喊」正则原样搬过来在本群有两条假命中，因为本群的话题恰好就是模型本身；它的 30%/字 错别字率放到这里等于满屏错字。参考实现给的是思路，**阈值和边界必须拿自己的语料重定**。
11. **加「更像人」的功能前，先想清楚它会不会把已修的 bug 引回来。** 注意力漂移和「答非所问」是同一方向上的两端，所以漂移的两条边界（被 @ 不漂移、像提问不漂移）是**结构性**的，不是提示词里的一句提醒。

### 文档

| 文档 | 内容 |
|---|---|
| [10-云服务器部署手册](docs/10-云服务器部署手册.md) | 从买服务器到跑起来，Docker + NapCat + AstrBot 全流程 |
| [20-控制台App说明](docs/20-控制台App说明.md) | 安卓 App 的三个页签、104 个旋钮、七种预设模式、服务端驱动更新与安全边界 |
| [30-回复触发条件](docs/30-回复触发条件.md) | 什么情况下机器人会说话 |
| [31-记忆系统与群员轮廓](docs/31-记忆系统与群员轮廓.md) | 记忆的存储结构与隐私设计 |
| [40-答非所问根因与修复](docs/40-答非所问根因与修复.md) | 25.7 万字上下文的量化分析 |
| [41-被骗认输根因与修复](docs/41-被骗认输根因与修复.md) | 为什么改人格没用 |
| [42-空头承诺根因与修复](docs/42-空头承诺根因与修复.md) | 「嘴上答应却不做」的机制 |
| [43-说话更真实方案](docs/43-说话更真实方案.md) · [44-实施记录](docs/44-说话更真实实施记录.md) | 方案与落地数据 |
| [45-戳一戳与违规禁言](docs/45-戳一戳与违规禁言.md) | 权限模型与限流设计 |
| [46-主动开口与情绪系统](docs/46-主动开口与情绪系统.md) | 冷场主动开口的五道闸门、单一主情绪的仲裁与清零、引用按 QQ 号认人；含影子回放揪出的两个真误判 |
| [47-真人感第二批](docs/47-真人感第二批-黑话与打字节奏与效果观察.md) | 黑话词表（含三个外部热梗库的回测数据与「先问模型知不知道」的探针结论）、打字节奏、同音错别字、注意力漂移、回复效果闭环 |
| [50-多模态接入与验证记录](docs/50-多模态接入与验证记录.md) | 图/视频/语音/联网的逐项实测，含失败记录 |
| [60-长期目标与技术方案](docs/60-长期目标与技术方案.md) | 整体架构与演进方向 |

### 快速开始

需要一台已经跑起 [AstrBot](https://github.com/AstrBotDevs/AstrBot) + [NapCat](https://github.com/NapNeko/NapCatQQ) 的服务器（没有的话照 [部署手册](docs/10-云服务器部署手册.md) 走一遍）。

```bash
# 1. 装插件：整个目录拷进 AstrBot 的插件目录
cp -r plugins/dsh-* /opt/qqbot/astrbot/data/plugins/

# 2. 配置：所有旋钮都是环境变量，写进 docker-compose 的 env_file
#    每个插件的变量清单见其 main.py 顶部注释
#    例：DSH_GUARD_SHADOW=1 表示禁言功能只判定不动手（建议先这样跑几天）

# 3. 重启（改插件代码必须用 restart，up -d 在配置没变时不会重启容器）
docker restart astrbot
```

插件带单元测试，不需要跑起 AstrBot 就能测：

```bash
python3 plugins/dsh-guard/test_guard.py
python3 plugins/dsh-style/test_style.py
python3 plugins/dsh-decide/test_decide.py
python3 plugins/dsh-poke/test_poke.py
python3 plugins/dsh-sticker/test_sticker.py
python3 plugins/dsh-emotion/test_emotion.py    # 36 项
python3 plugins/dsh-quote/test_quote.py        # 25 项

# 拿你自己的历史语料回放情绪判定，找误判（单测只能验证你已经想到的情况）
python3 plugins/dsh-emotion/replay_emotion.py
```

控制台 App 自己出包（不需要 Gradle 和 Android Studio）：

```bash
cd console
KEYSTORE=/path/to/your.jks KS_PASS=yourpass bash build.sh
bash test/run-tests.sh              # 391 项纯 JVM 单测
python3 server/test_console.py      # 105 项后端单测
python3 server/test_qrweb_e2e.py    # 83 项端到端
```

### 需要自备的东西

代码里不含任何密钥。跑起来需要你自己的：

- 一个 QQ 小号（**不要用主号**，有封号风险）
- 一个 LLM API（聊天用；便宜的 flash 类模型够用）
- 可选：视觉模型（看图/看视频）、文生图、TTS、搜索 —— 缺哪个就少哪个功能，不影响其他部分

### 已知限制

- **语音识别做不到。** 试过的渠道要么欠费要么没有音频模型，群友发语音机器人只知道「有人发了条语音」。
- **QQ 账号会掉线。** 20~60 分钟一次，换 IP 没用（实测过，账号被打标后换 IP 救不回来）。仓库里的 `server/qq_watchdog.sh` 是掉线自动重启换码的看门狗。
- **控制台服务端是明文 HTTP。** 没有 TLS，密码在网络上不加密传输。这是端口限制导致的，公共 Wi-Fi 下要意识到。
- **插件依赖 AstrBot 内部结构。** 用了一些非公开的内部属性（`provider_manager.inst_map`、`req.contexts` 等），AstrBot 大版本升级可能需要跟着改。

### 隐私说明

发布前做过全量脱敏：真实服务器 IP、群号、QQ 号、真人昵称、私人邮箱、付费中转站域名全部替换为占位符（`your-server.example.com`、`100000001`、`群友A` 等）。文档里保留的实测数据（响应时间、命中率、token 数）不含可识别信息。

### 许可

MIT，见 [LICENSE](LICENSE)。

---

## English

### What this is

An AI "group member" that lives in a QQ group (persona: *Little Whale*). Not a Q&A support bot — the goal is to **blend in as a real person**: it lurks, jumps into conversations, sends stickers, pokes back, sees images and videos, and remembers who people are.

Four independently usable parts:

| Directory | Contents | Language |
|---|---|---|
| [`plugins/`](plugins/) | 45 AstrBot plugins — all bot capabilities | Python |
| [`bridge/`](bridge/) | QQ ↔ DeepSeek Harness bridge (an alternative approach) | Node.js |
| [`console/`](console/) | Android console app + server backend | Java / Python |
| [`docs/`](docs/) | Deployment manual and 14 root-cause analyses | Markdown |

### Latest update · 2026-09-08 (integration: 45 plugins wired into breakable pipelines)

The repo is now organized around **45 plugins**. Every earlier batch fixed a single defect; this one treats
the whole set as **one pipeline** — making "human" not a pile of independent switches but a chain with priority.

- **De-AI is a breakable pipeline.** `dsh-aiflavour` (paints the reply's tone first) →
  `dsh-humanizer` (humanizing rewrite) → `dsh-typo` (an occasional wrong character) →
  `dsh-noise` (noise: a swapped character / truncation / repeated word). Four stages chew away the "AI neatness".
- **Intercept runs before inject.** Plugins are wired by priority: `armor` / `merge` / `decide` run first
  (priority=2000) to decide "should we join in, and how"; `effect` / `emotion` run after (priority=100) to
  shape the tone of a reply already deemed worth sending. Whether to speak is the precondition; *how* to say
  it is decoration.
- **They interlock.** `dsh-steal` steals stickers but must visually understand meaning, only keeps "repeated"
  images, and skips anything unsuitable to share (fail-closed); `dsh-noise`'s noise **never** touches a proper
  answer that was @-mentioned — anything abnormal passes through; `dsh-proactive`'s interest probe uses pure
  regex scoring at zero LLM cost and only joins in when rice / food / deep-sea whale / DeepSeek / being named
  is on the topic.
- **A batch of new capability plugins.** Fact guard (`dsh-factguard`), leak guard (`dsh-leakguard`),
  homophone nickname (`dsh-homophone`), join guard (`dsh-joinguard`), passive slang learning (`dsh-listen`),
  quote resolution (`dsh-quoteref`), scene detection (`dsh-scene`), self-protection (`dsh-selfguard`),
  paid quota (`dsh-pay`).
- **Compiled and shipped.** All 45 plugins compile and load with no failures, and the priority wiring above
  is live.

### Previous update · 2026-09-05 evening (human-likeness, batch 2)

Four new plugins plus one framework config change. This batch differs in kind from the earlier ones:
those fixed **outright defects** (off-topic replies, being talked into submission, misidentifying people,
crashes). This one fixes "nothing is broken, but you can tell it's an AI at a glance".

- **Understanding the group's slang (`dsh-glossary`).** 43 entries, injected only on a hit. The entries are
  *not* imported from off-the-shelf meme dictionaries — three external libraries totalling 549 terms scored
  only 9 genuine hits against 2478 real messages, and their top hit was `15` (matching "got camped 15 times"
  and someone's QQ ID). Mining our own corpus works instead, using the signal "this phrase was sent as an
  entire message, by at least two different people" — which immediately surfaced the table's most-hit term.
- **Typing delay now scales with length.** It used to be a flat random 1.2–2.8s regardless of length, so it
  was sometimes backwards: a one-character reply waited 2.8s while a twenty-character one went out in 1.2s.
  The framework already ships `interval_method: "log"`; we simply had not enabled it.
- **Occasional homophone typos (`dsh-typo`).** 6% per message, at most one character, plus a 35% chance of a
  follow-up self-correction. Uses a **hand-reviewed 60-pair homophone table** rather than generating with
  `pypinyin`: generated output is an open set, and that is not a bet worth taking in a real 94-person group.
- **Letting it get pulled onto a tangent (`dsh-drift`).** Always answering strictly on-topic is itself an AI
  tell. Two **structural** hard limits keep the previously fixed off-topic bug from returning: never drift
  when mentioned, never drift when the current message looks like a question.
- **Not talking over another AI (`dsh-decide`).** The reference implementation allows whitespace after the
  name; copied verbatim it produced two false positives here, because model names *are* this group's topic.
  Tightening it to "punctuation delimiters only, no short forms like `GPT`/`ds`" brought false positives to
  zero across 1899 messages.
- **Watching what the group does after it speaks (`dsh-effect`).** This was the biggest structural gap:
  every change until now was **open-loop** — inject samples, inject glossary, then hope. Now each reply opens
  a 180s observation window and the group's reaction is classified (appreciation / playful / neutral /
  confusion / factual correction / rejection / **ignored**), along with whether the reaction targets the
  **content or the persona**, and whether the reply **advanced or derailed** the conversation. Zero messages
  in the window means "ignored" for free, with no model call. **This version only measures; it changes no
  behaviour** — auto-tuning on an unvalidated signal is adjusting an engine with no instruments.

Verification: after review the glossary's injected block shrank from 196 to 135 characters with an unchanged
8.4% hit rate, proving the three removed entries were dead. Four pure-function test suites, a 1899-message
corpus backtest, and one real end-to-end settlement of `dsh-effect` all pass.

### Previous update · 2026-09-05 daytime (the console is now server-driven)

The Android console app moved from *14 hard-coded knobs* to **rendering whatever schema the server sends**.
This solves a concrete problem: once the plugin count grew from 16 to 22 and the tunable variables to 248,
shipping a new APK for every new switch stopped being viable.

- **104 knobs exposed at once**, covering the master switches and main parameters of 22 plugins
  (19 of those plugins are open-sourced in this repo).
- **Adding features no longer needs a reinstall.** As long as a knob uses a type the app already knows
  (`bool` `int` `float` `enum` `str` `csv`), adding one line to `console_spec.py` and restarting the backend
  makes it appear after a pull-to-refresh. The app **skips** types it does not recognize instead of crashing —
  that is what makes forward compatibility work. Only a genuinely new widget requires a new APK.
- **Plugin switches read and write `imagegen.env` directly** (all 22 plugins get their variables from that
  `env_file`) using line-level edits: comments and ordering preserved, original file permissions kept,
  atomic replace, MD5 checked before writing to prevent clobbering a concurrent change.
  Those 55 comment lines record *why* each value is what it is — they are worth more than the values.
- **Three hard constraints, all absorbed server-side:**
  ① Booleans must be written as `1`/`0` — the 22 plugins parse booleans four different ways, and 11 variables
  test `!= "0"`, so `false` would read as **on**;
  ② an `int`/`float` set to a non-number or left empty makes the plugin **throw at import time and fail to load
  entirely** (only `dsh-memory` falls back), so every numeric knob carries min/max and out-of-range values
  return 400 rather than being silently clamped;
  ③ for variables clamped by `max()/min()` in code, the slider minimum must equal that hard floor, or the phone
  would show `1` while `3` is actually in effect.
- **Env changes require recreating the container.** AstrBot's environment comes from docker compose's `env_file`,
  and `os.environ` inside the container only updates on **container recreation** — `docker restart` does not
  re-read `env_file` (learned the hard way). Such knobs are marked `hot=false` and the UI says so explicitly.
- **7 secret variables report presence only** (image / voice / video / search API keys and the voice ID).
  Their real values never leave the server and cannot be edited from the phone — the console is plaintext HTTP.
- **Self-update prompts, never installs silently.** A new lightweight endpoint `/api/console/version`
  (a single `stat`) lets the app notice a newer `versionCode` on the server and offer a browser download.
  Automatic installation over plaintext HTTP is not controllable, so it is deliberately not done.

Verified by **105** backend unit tests, **83** end-to-end tests, **391** pure-JVM tests, and **18** live checks after deploy.

### Earlier · 2026-09-04

Three new capabilities are live in a real group (no longer shadow mode):

- **It starts conversations when the room goes quiet.** After 15 minutes of silence it opens a thread itself — at most 3 times a day, 1 hour apart, and only during active hours (measured 10:00–02:59 for this group; 03:00–09:59 is nearly empty). It speaks through the full message pipeline, so sticker stripping, mention policy, and segmented sending all still apply. Any judgment failure or timeout means silence — fail-**closed**, because saying the wrong thing unprompted costs more than saying nothing.
- **It speaks with emotion.** Six emotions, exactly one at a time, and a single message triggers at most one. Structural evidence of *who is being addressed* is required: members insulting each other, self-deprecation, third-party narration, and whole-message quotes never move its emotion. Resets go through apology / two consecutive neutral messages / question answered / per-emotion TTL, not just expiry.
- **It understands quotes.** By QQ ID it now states who is speaking, whose line was quoted, whether that line was its own, and who was mentioned.

Also fixed a real crash that had been hiding for half a day: `dsh-decide` unpacked a 3-tuple as a 2-tuple, so once the bot had spoken recently it always threw — 12 crashes vs 9 successful judgments that day. Because the fallback was fail-open, everything looked fine while over half the replies never received the "who is speaking to whom" verdict. That was the direct cause of the bot mixing up subject and object.

Before rollout: **61 new unit tests** (36 emotion + 25 quote) and a **1618-message replay over real history**. The replay caught two genuine misjudgments: treating the character in "balance/quota" as an awkward filler (8 of 9 awkward hits were false), and treating undirected insults as aimed at the bot (25 of 38 hits had no second-person reference at all). Details in [46-主动开口与情绪系统](docs/46-主动开口与情绪系统.md).

### Download

The Android console app is on the [Releases](../../releases) page (~110 KB, Android 5.0+). Enter your own server address on the login screen; the package embeds no server details.

### What the 45 plugins fix

Each plugin addresses a **problem that actually happened**, not a feature checklist.

**Speech quality**

| Plugin | Problem solved |
|---|---|
| [`dsh-ctxclean`](plugins/dsh-ctxclean/) | Off-topic replies. The framework stored per-turn injected group context verbatim into history, and `max_context_length=-1` disables truncation — so every request resent hundreds of mutually contradictory "latest group chat" blocks. Injected blocks were 31% of a 257k-character context; real human speech only 2.2%. |
| [`dsh-style`](plugins/dsh-style/) | Sounding like an AI. Samples real human short sentences as style examples, cutting mean sentence length from 16.5 to 9.5 characters. |
| [`dsh-decide`](plugins/dsh-decide/) | Before jumping in unprompted, a small model judges "what's being discussed, should I speak". A "stay silent" verdict cancels the entire main-model call, saving ~9400 tokens. |
| [`dsh-mention`](plugins/dsh-mention/) | @-mentioning on every reply. Now only when a tool was used, the message got buried, or too much time passed. |
| [`dsh-claimguard`](plugins/dsh-claimguard/) | Being talked into submission. Someone says "call me daddy" or "you lost our duel" and it complies — fixed by injecting facts, not by editing the persona. |
| [`dsh-initiate`](plugins/dsh-initiate/) | Staying silent forever once the room goes quiet. Five pure-code gates (idle 15min–6h, active hours, 1h cooldown, 3/day) run before a small model even looks for a thread worth picking up; once it decides, a **synthetic event goes through the full message pipeline**, so sticker stripping, mention policy, and segmented sending all still apply. Any judgment failure means silence. |
| [`dsh-emotion`](plugins/dsh-emotion/) | One flat tone forever. Six emotions, exactly one at a time; fixed-priority arbitration guarantees a single message triggers only one ("you're great, but you really let me down" yields sadness only). Four reset paths: apology, two consecutive neutral messages, question answered, per-emotion TTL. |
| [`dsh-quote`](plugins/dsh-quote/) | Misreading quotes and claiming someone else's work as its own. The framework's quote block carries only a nickname — and a real member had renamed themselves to match the bot. **Nicknames can never identify anyone in a group chat.** Now it states, by QQ ID: who is speaking, whose line they quoted, whether that line was the bot's own, and who was mentioned. |
| [`dsh-glossary`](plugins/dsh-glossary/) | Not understanding the group's slang. 43 entries, injected only on a hit, each carrying real corpus usage examples (with definitions alone it *understood* the local memes but never used them). Entries are mined from our own corpus, not imported: three external meme libraries totalling 549 terms produced only 9 genuine hits. A false hit is worse than no injection, so every entry carries an `avoid` exclusion context. |
| [`dsh-human`](plugins/dsh-human/) | The **shape** of replies doesn't look human. Measured: 16.4% of human messages contain a comma versus 67.7% of the bot's, and the "short clause, short clause" pattern accounts for 54.8% of bot messages and 0% of human ones. Prompting cannot fix this (it sat in the persona for two weeks unchanged); the fix splits comma-glued clauses into separate messages before sending. |
| [`dsh-typo`](plugins/dsh-typo/) | Thousands of messages without a single typo. A hand-reviewed 60-pair homophone table, 6% chance of one wrong character per message, 35% chance of a follow-up self-correction. A 48-entry protect list (glossary terms, names, sticker markers, links) is never touched. |
| [`dsh-drift`](plugins/dsh-drift/) | Always answering strictly on-topic — that over-precision is itself an AI tell. Three drift levels expressed as graded prompts. Two structural hard limits keep the previously fixed off-topic bug from returning: never drift when mentioned, never drift when the message looks like a question. |
| [`dsh-effect`](plugins/dsh-effect/) | Every change was open-loop: inject and hope, with no idea whether anything landed. Now each reply opens a 180s observation window and classifies the reaction (appreciation / playful / neutral / confusion / factual correction / rejection / ignored), whether it targets content or persona, and whether the reply advanced or derailed the thread. Zero messages in the window means "ignored" for free. |
| [`dsh-aiflavour`](plugins/dsh-aiflavour/) | Dynamic AI-flavor interception. Static strong words + learned word roots + a session brake, to curb the bot's over-explaining / over-summarising. |
| [`dsh-humanizer`](plugins/dsh-humanizer/) | De-AI exit gate. Scans the reply body for AI-trace features and strips/blocks AI-flavoured sentences — the pipeline stage that bends speech back into human phrasing. |
| [`dsh-noise`](plugins/dsh-noise/) | Human noise. Swaps a character / cuts a reply short / repeats a whole clause — only on unprompted replies, never on a proper @-mentioned answer. |
| [`dsh-armor`](plugins/dsh-armor/) | Anti-breakage on the input side. Recognises prompt re-copying, rule ignoring, privilege-induction and identity grillings; priority 2000 so it runs before the inject group. |
| [`dsh-merge`](plugins/dsh-merge/) | Aggregated replies under a mention storm. When @-mentioned too many times it merges into one unified reply (summarises and connects, names nobody), instead of replying line by line. |
| [`dsh-quoteref`](plugins/dsh-quoteref/) | Quote replies. Uses a QQ quote-reply for every Nth question-style @, aligned to the human quoting rate. |

**Multimodal**

| Plugin | Problem solved |
|---|---|
| [`dsh-imgctx`](plugins/dsh-imgctx/) | Blind to images. The framework only captions images attached to the *current* message, while the common pattern is "post image, then ask about it". Animated GIFs get 4 frames tiled before recognition. |
| [`dsh-vischain`](plugins/dsh-vischain/) | The vision model config is a single string — no fallback. The primary model sits behind a CDN that returns 403 about 20% of the time; failures are instant (0.0s), so retrying costs almost nothing. |
| [`dsh-imagegen`](plugins/dsh-imagegen/) | Agreeing to draw but not drawing. Cheap models routinely skip tool calls in long contexts, so a fallback hook is mandatory. |
| [`dsh-video`](plugins/dsh-video/) | Watching and generating video. Four frames are 5× faster and 18× cheaper than the full clip at equal quality. Generation takes 4 minutes — never wait inside the tool (it would lock the whole session). |
| [`dsh-voice`](plugins/dsh-voice/) | TTS. The built-in one is a global switch plus probability, reading *every* reply aloud, which instantly breaks character. |
| [`dsh-web`](plugins/dsh-web/) | Web access. The link is right there in the message, so the plugin fetches and injects it rather than hoping the model calls a tool. |
| [`dsh-sticker`](plugins/dsh-sticker/) | Stickers. The key rule: strip markers **unconditionally**, or `[sticker:xx]` leaks into the group verbatim. |
| [`dsh-listen`](plugins/dsh-listen/) | Hearing voice messages. Voice clips are handed to a transcription service and injected as voice context — so it no longer just knows "someone sent a voice message". |

**Memory and moderation**

| Plugin | Problem solved |
|---|---|
| [`dsh-memory`](plugins/dsh-memory/) | Not remembering people. The built-in feature is a 20-message sliding window; anything older is gone. Custom buffer plus background extraction, with PII scrubbing, prompt-injection defense, per-person caps, and self-service `/forget me`. |
| [`dsh-guard`](plugins/dsh-guard/) | Moderation. Three layers: regex prefilter (free; only 2.2% hit rate measured on real traffic) → small model emitting booleans only → **code** decides whether to mute. |
| [`dsh-poke`](plugins/dsh-poke/) | No reaction to pokes. The reply never calls an LLM; three rate limits prevent poke loops. |
| [`dsh-welcome`](plugins/dsh-welcome/) | Greeting new members. Generated by the LLM rather than a fixed template (templates break character). |
| [`dsh-acl`](plugins/dsh-acl/) | Anyone could run any command. Three tiers (everyone / owner + admins / owner only), with identity resolved **by QQ ID only** — nicknames collide and group cards can be changed at will. Status queries are gated too: they change nothing, but they print endpoint addresses, model names, quotas, and token spend into the group. |
| [`dsh-fwd`](plugins/dsh-fwd/) | Forwarded chat records are invisible — the framework only passes a `[chat record]` placeholder. Expands them by nesting depth, keeping head and tail, with separate budgets for images and video. |
| [`dsh-spine`](plugins/dsh-spine/) | Backing down when pressed, apologising when doubted. Unlike `dsh-claimguard` this governs **positional consistency** rather than facts. |
| [`dsh-slang`](plugins/dsh-slang/) | Slang learning with no human feeding. Automatically mines likely-slang from the chat, researches meanings into candidates, and every 8h an AI reviews the batch (promote / reject / wait). |
| [`dsh-factguard`](plugins/dsh-factguard/) | Memory watchdog. When asked about its own attributes it answers only from the fact table; ungrounded claims are denied and corrected — prevents random admission and gender wobble. |
| [`dsh-leakguard`](plugins/dsh-leakguard/) | Blocks system-prompt leakage. Checks whether a reply copied the persona's exclusive title / instruction verbatim. Works with `dsh-humanizer` as the hard fallback. |
| [`dsh-homophone`](plugins/dsh-homophone/) | Homophone / nickname recognition. A name said in homophones counts as being called; meme meanings in homophones get injected. |
| [`dsh-joinguard`](plugins/dsh-joinguard/) | AI-reviewed join gate. Sends the join question + answer to an AI to judge approve/reject and really approves/rejects. |
| [`dsh-scene`](plugins/dsh-scene/) | Group-level context and daily rhythm. Tells the bot which group this is and what time of day means here. |
| [`dsh-selfguard`](plugins/dsh-selfguard/) | Output self-restraint. Blocks repeating things it already said and, during a group argument, its own provoking / abusive lines — `dsh-guard` polices members, this polices the bot itself. |
| [`dsh-pay`](plugins/dsh-pay/) | Sponsorship / payment guidance. Asks how to pay → sends the QR code, and only thanks after the owner confirms receipt. |
| [`dsh-proactive`](plugins/dsh-proactive/) | Interest probe. Pure-regex interest scoring (zero LLM cost); joins in on rice / food / whale / AI topics actively. |
| [`dsh-interest`](plugins/dsh-interest/) | Interests change. Time-decayed heat + a rotating "today's craving" flavour cycle, injected as a state block so the tone carries what it's currently itching about. |
| [`dsh-steal`](plugins/dsh-steal/) | Sticker stealing. Understands image meaning, only keeps "repeated, understandable" ones, skips anything unsuitable to share (fail-closed); `/sticker` sends one at random. |

### Eleven lessons learned the hard way

Each was re-validated across multiple plugins:

1. **Structural judgement beats keyword lists.** For intent detection don't enumerate keywords or clamp reply length. The correct shape is "user wants an image + model did not explicitly refuse + model is not asking a back-question" ⇒ generate. Filter objects by exclusion (what can't be drawn), not by enumerating what can.
2. **Log every non-trigger.** Otherwise every debug is guesswork. `dsh-imagegen`'s "sometimes doesn't draw" took three rounds to find, and the first two failed purely because there were no logs.
3. **Where you skip the message pipeline and call the LLM directly, you must replicate the marker-stripping logic.** `llm_generate` does not go through the pipeline, so the sticker hook never fires, and `[sticker:peek]` leaked into the group that way.
4. **Thresholds must come from measured distributions.** `dsh-mention` v1 had a "reply within ≥8s → mention" rule, but what it measured was how long the bot itself thought — with persona a call routinely takes 5–15s, making it equivalent to "always mention". 8 replies in 24h were 100% mentioned.
5. **Fail-open disguises crashes as quality regressions.** `dsh-decide` unpacked a 3-tuple as a 2-tuple, so whenever the bot had just spoken it always threw `ValueError`, falling through fail-open to "reply as usual" — **12 crashes vs 9 successes that day**, so over half its replies never received the "who said this to whom" verdict. Everything looked fine, it just talked wrong. Every fail-open fallback must WARN and count on that path.
6. **A hit count of zero does not mean the misjudgment is fixed.** Tightening the "question" rule dropped `dsh-emotion`'s curiosity hits from 14 to 0, which looked like success but was a total miss — the common follow-up "but then why didn't you" has its question word mid-sentence, while the new regex only anchored at the start. Rule changes must be diffed sample by sample, not by total.
7. **Shadow mode's value is in replay, not in waiting.** Running overnight shows only that night's few lines; replaying 1618 messages of history exposed two real misjudgments in 20 minutes (treating the character of "balance/quota" as an awkward filler, and treating an undirected insult as aimed at the bot).

8. **Your own group's corpus beats any off-the-shelf dictionary.** Three external meme libraries totalling 549 terms produced 9 genuine hits here; one pass over our own corpus contributed the single most-hit entry in the table. The signal that works is "this phrase was sent as an entire message, by at least two different people" — catchphrases and memes get sent alone; ordinary words never do.
9. **Ask the model whether it already knows, before deciding to teach it.** Asked one by one, the main model said "not sure" for none of the 43 slang entries and got most of them right. That changed the plugin's purpose outright: from *dictionary* to *pragmatics layer*, with definitions compressed to the minimum and effort spent on the 6 terms it got wrong plus the local conventions it cannot infer. Without that probe we would still be maintaining a dictionary the model never needed.
10. **Re-benchmark any judgement borrowed from a reference implementation.** MaiBot's "another AI is being called" regex, copied verbatim, had two false positives here because this group's topic literally is the models themselves; its 30%-per-character typo rate here would mean screens full of typos. A reference gives you ideas — thresholds and boundaries must be re-derived from your own corpus.
11. **Before adding "more human" features, think about whether it will bring back a fixed bug.** Attention drift and off-topic replies are two ends of the same direction, so drift's two boundaries (never drift when mentioned, never drift when it looks like a question) are **structural**, not a reminder in the prompt.

### Docs

| Document | Description |
|---|---|
| [10 云服务器部署手册](docs/10-云服务器部署手册.md) | Full Docker + NapCat + AstrBot setup, from buying a server to running |
| [20 控制台App说明](docs/20-控制台App说明.md) | The app's three tabs, 104 knobs, seven preset modes, server-driven updates and security boundary |
| [31 记忆系统与群员轮廓](docs/31-记忆系统与群员轮廓.md) | Memory storage structure and privacy design |
| [40 答非所问根因与修复](docs/40-答非所问根因与修复.md) | Quantified analysis of a 257k-character context |
| [41 被骗认输根因与修复](docs/41-被骗认输根因与修复.md) | Why editing the persona doesn't work |
| [47 真人感第二批](docs/47-真人感第二批-黑话与打字节奏与效果观察.md) | Slang table, typing rhythm, homophone typos, attention drift, response-effect closed loop |
| [60 长期目标与技术方案](docs/60-长期目标与技术方案.md) | Overall architecture and direction |

### Quick start

You need a server already running [AstrBot](https://github.com/AstrBotDevs/AstrBot) + [NapCat](https://github.com/NapNeko/NapCatQQ) (or follow the [deployment manual](docs/10-云服务器部署手册.md) first).

```bash
# 1. Install: copy the whole plugins directory into AstrBot's plugin dir
cp -r plugins/dsh-* /opt/qqbot/astrbot/data/plugins/

# 2. Configure: every knob is an environment variable, write them into docker-compose's env_file
#    (each plugin lists its variables at the top of main.py)

# 3. Restart (restart, not up -d — up -d won't restart the container when config is unchanged)
docker restart astrbot
```

Plugins ship their own unit tests, no need to run AstrBot:

```bash
python3 plugins/dsh-guard/test_guard.py
python3 plugins/dsh-emotion/test_emotion.py   # 36 cases
python3 plugins/dsh-quote/test_quote.py       # 25 cases
```

### What you must supply

No keys are embedded. You provide your own:

- A QQ alt account (do not use your main account — ban risk)
- An LLM API (a cheap flash-class model suffices)
- Optional: vision (image/video), image-gen, TTS, search — each one is a feature you can do without

### Known limitations

- **Voice recognition is not available.** Every channel tried was either out of credit or lacked an audio model; a voice message reads as "someone sent a voice message".
- **The QQ account will drop.** Once every 20–60 minutes, and changing IP does not help (an account that is flagged cannot be rescued by an IP change). `server/qq_watchdog.sh` auto-restarts and swaps the code.
- **The console backend is plaintext HTTP.** No TLS; passwords travel unencrypted. This comes from the port restriction — be aware on public Wi-Fi.
- **Plugins depend on AstrBot internals.** A few non-public attributes are used (`provider_manager.inst_map`, `req.contexts`, …); major AstrBot upgrades may need matching changes.

### Privacy

The repo went through full desensitisation before release: real server IPs, group IDs, QQ IDs, member nicknames, personal emails and paid relay domains are all replaced with placeholders (`your-server.example.com`, `100000001`, `群友A`, etc.). Kept measured data (response times, hit rates, token counts) contains no identifying information.

### License

MIT, see [LICENSE](LICENSE).
