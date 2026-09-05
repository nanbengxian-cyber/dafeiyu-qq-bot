# 大肥鱼 QQ 机器人 · DaFeiYu QQ Bot

[中文](#中文) · [English](#english)

一套让 QQ 群机器人「像真人群友一样说话」的完整工程：19 个 AstrBot 插件、一个 QQ↔AI 桥接程序、一个安卓控制台 App，以及记录每个问题根因与实测数据的技术文档。

A complete engineering effort to make a QQ group bot *talk like an actual group member*: 19 AstrBot plugins, a QQ↔AI bridge, an Android console app, and technical documents recording the root cause and measured data behind every fix.

---

## 中文

### 这是什么

一个跑在 QQ 群里的 AI 群友（人格叫「小鲸鱼」）。它不是客服式问答机器人 —— 目标是**混在群里像个真人**：会潜水、会插话、会发表情包、会戳回去、看得见图片和视频、记得住群友是谁。

仓库包含四块可独立使用的东西：

| 目录 | 内容 | 语言 |
|---|---|---|
| [`plugins/`](plugins/) | 19 个 AstrBot 插件，约 1.09 万行 —— 机器人的全部能力 | Python |
| [`bridge/`](bridge/) | QQ ↔ DeepSeek Harness 桥接（另一条技术路线） | Node.js |
| [`console/`](console/) | 安卓控制台 App + 服务端后台 | Java / Python |
| [`docs/`](docs/) | 部署手册与 13 份问题根因分析 | Markdown |

### 最新更新 · 2026-09-05（控制台改成服务端驱动）

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
- **7 个敏感变量只报「已设置 / 未设置」**（画图 / 语音 / 视频 / 搜索的 API key 与音色 ID），真实值一个字节都不出服务器，
  手机端也改不了 —— 控制台是明文 HTTP。
- **自更新只提示、不静默安装。** 新增轻量端点 `/api/console/version`（只 stat 一个文件），
  服务器上的包 `versionCode` 更新时弹一次提示、点「打开下载」跳浏览器。明文 HTTP 上自动装包不可控，所以刻意不做。

验证：后端 **105 项**单测、端到端 **83 项**、纯 JVM **391 项**，加上部署后 **18 项**真机实测全过。

### 上一次更新 · 2026-09-04

三块新能力已在真群上线（不再是影子模式）：

- **冷场会自己开口。** 安静超过 15 分钟它自己起个话头，每天最多 3 次、两次间隔 1 小时、只在活跃时段（本群实测 10:00–02:59，03:00–09:59 几乎无人说话）。开口走完整消息管道，所以贴纸、@ 策略、分段发送全部照常。判断失败或超时一律沉默 —— fail-**closed**，因为主动说错话的代价大于不说话。
- **说话带情绪。** 六种情绪同时只有一种，一条消息最多触发一种。必须有「针对谁」的结构证据才算：群友互骂、自嘲、转述第三方、整句引用都不改变它的情绪。清零走道歉 / 连续 2 条中性 / 问题被回答 / 分情绪 TTL 四条路，而不是只等超时。
- **看得懂引用了。** 按 QQ 号说清「谁在说 / 引用谁的哪句 / 是不是自己说的 / @ 的是谁」。

同时修掉一个藏了半天的真崩溃：`dsh-decide` 把三元组按两元组解包，只要机器人刚说过话就必抛异常，当天崩 12 次 / 成功 9 次。因为兜底是 fail-open，表面上一切正常，实际超过一半发言都没收到「这句是谁对谁说的」的判断 —— 这就是「主谓宾弄错」的直接原因。

上线前：**61 项新增单元测试**（情绪 36 + 引用 25）＋ **1618 条真实历史回放**。回放揪出两个真误判：把「余额 / 额度」里的「额」当成尴尬语气词（9 次尴尬命中里 8 次误判），以及把没有指向的话当成在骂它（38 次命中里 25 次整句没有「你」）。细节见 [46-主动开口与情绪系统](docs/46-主动开口与情绪系统.md)。

### 成品下载

安卓控制台 App 在 [Releases](../../releases) 页面下载（约 121 KB，安卓 5.0+）。装完在登录页填自己的服务器地址即可，包内不含任何服务器信息。

### 19 个插件在解决什么

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

**记忆与群管**

| 插件 | 解决的问题 |
|---|---|
| [`dsh-memory`](plugins/dsh-memory/) | 记不住人。框架自带的只是「最近 20 条」滑动窗口，滑出去就没了。自建缓冲 + 后台抽取，带隐私脱敏、注入防护、按人容量上限、自助 `/忘记我`。 |
| [`dsh-guard`](plugins/dsh-guard/) | 违规禁言。三层：关键词预筛（0 成本，真群实测仅 2.2% 命中）→ 小模型只输出布尔 → **代码**决定禁不禁。 |
| [`dsh-poke`](plugins/dsh-poke/) | 戳一戳没反应。回话不问 LLM，三道限流防对戳循环。 |
| [`dsh-welcome`](plugins/dsh-welcome/) | 入群欢迎。用 LLM 现场生成而不是写死模板（模板会破人设）。 |

### 反复踩到的七个坑

这些教训在多个插件上重复验证过，写在这里省得再踩：

1. **结构判断优于枚举词表。** 意图判定别列举关键词、别掐回复字数。正确形状是「用户要图 + 模型没明确拒绝 + 模型不是在反问」⇒ 出图。宾语过滤用排除法（列画不出的），而不是枚举画得出的。
2. **不触发时必须打日志。** 否则每次排查只能靠猜。`dsh-imagegen` 的「有时候不生图」查了三轮，前两轮全是因为没有日志。
3. **绕过消息管道直接调 LLM 的地方，必须自己复制一份标记剥离逻辑。** `llm_generate` 不走管道，贴纸钩子不会触发，`[贴纸:探头]` 就这样漏进过群聊。
4. **阈值必须由实测分布决定。** `dsh-mention` v1 有条「延迟 ≥8s 就 @」的规则，但量到的是机器人自己想了多久 —— 一次带人格的调用普遍 5~15 秒，于是等价于「无条件 @」，24 小时 8 条回复 100% 都 @ 了。
5. **fail-open 会把崩溃伪装成质量下降。** `dsh-decide` 有个三元组按两元组解包的 bug，只要机器人刚说过话就必抛 `ValueError`，整次判断落到 fail-open「照旧说话」——**当天崩 12 次、成功 9 次**，超过一半发言模型都没收到「这句是谁对谁说的」。功能看起来完全正常，只是说得不对。凡是 fail-open 的兜底，必须在兜底路径上打 WARN 并计数。
6. **命中数归零不等于误判修好了。** 收紧「提问」规则后 `dsh-emotion` 的好奇命中从 14 掉到 0，看着像不再误判，实际是全漏了 —— 群里最常见的追问「那你为什么没有」疑问词在句中，而新正则只锚定句首。规则改动必须逐样本对比，不能只看总数。
7. **影子模式的价值在于回放，不在于等。** 挂着跑一晚只能看到当晚那几条；拿 1618 条历史语料回放，两个真误判 20 分钟就暴露了（把「余额/额度」当成尴尬语气词、把没有指向的话当成在骂它）。

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
| [`plugins/`](plugins/) | 19 AstrBot plugins, ~10.9k lines — all bot capabilities | Python |
| [`bridge/`](bridge/) | QQ ↔ DeepSeek Harness bridge (an alternative approach) | Node.js |
| [`console/`](console/) | Android console app + server backend | Java / Python |
| [`docs/`](docs/) | Deployment manual and 13 root-cause analyses | Markdown |

### Latest update · 2026-09-05 (the console is now server-driven)

The Android console app moved from *14 hard-coded knobs* to **rendering whatever schema the server sends**.
This solves a concrete problem: once the plugin count grew from 16 to 22 and the tunable variables to 248,
shipping a new APK for every new switch stopped being viable.

- **104 knobs exposed at once**, covering the master switches and main parameters of 22 plugins
  (19 of those plugins are open-sourced in this repo).
- **Adding features no longer needs a reinstall.** As long as a new knob uses a type the app already knows
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

### Previous update · 2026-09-04

Three new capabilities are live in a real group (no longer shadow mode):

- **It starts conversations when the room goes quiet.** After 15 minutes of silence it opens a thread itself — at most 3 times a day, 1 hour apart, and only during active hours (measured 10:00–02:59 for this group; 03:00–09:59 is nearly empty). It speaks through the full message pipeline, so sticker stripping, mention policy, and segmented sending all still apply. Any judgment failure or timeout means silence — fail-**closed**, because saying the wrong thing unprompted costs more than saying nothing.
- **It speaks with emotion.** Six emotions, exactly one at a time, and a single message triggers at most one. Structural evidence of *who is being addressed* is required: members insulting each other, self-deprecation, third-party narration, and whole-message quotes never move its emotion. Resets go through apology / two consecutive neutral messages / question answered / per-emotion TTL, not just expiry.
- **It understands quotes.** By QQ ID it now states who is speaking, whose line was quoted, whether that line was its own, and who was mentioned.

Also fixed a real crash that had been hiding for half a day: `dsh-decide` unpacked a 3-tuple as a 2-tuple, so once the bot had spoken recently it always threw — 12 crashes vs 9 successful judgments that day. Because the fallback was fail-open, everything looked fine while over half the replies never received the "who is speaking to whom" verdict. That was the direct cause of the bot mixing up subject and object.

Before rollout: **61 new unit tests** (36 emotion + 25 quote) and a **1618-message replay over real history**. The replay caught two genuine misjudgments: treating the character in "balance/quota" as an awkward filler (8 of 9 awkward hits were false), and treating undirected insults as aimed at the bot (25 of 38 hits had no second-person reference at all). Details in [46-主动开口与情绪系统](docs/46-主动开口与情绪系统.md).

### Download

The Android console app is on the [Releases](../../releases) page (~110 KB, Android 5.0+). Enter your own server address on the login screen; the package embeds no server details.

### What the 19 plugins fix

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

**Memory and moderation**

| Plugin | Problem solved |
|---|---|
| [`dsh-memory`](plugins/dsh-memory/) | Not remembering people. The built-in feature is a 20-message sliding window; anything older is gone. Custom buffer plus background extraction, with PII scrubbing, prompt-injection defense, per-person caps, and self-service `/forget me`. |
| [`dsh-guard`](plugins/dsh-guard/) | Moderation. Three layers: regex prefilter (free; only 2.2% hit rate measured on real traffic) → small model emitting booleans only → **code** decides whether to mute. |
| [`dsh-poke`](plugins/dsh-poke/) | No reaction to pokes. The reply never calls an LLM; three rate limits prevent poke loops. |
| [`dsh-welcome`](plugins/dsh-welcome/) | Greeting new members. Generated by the LLM rather than a fixed template (templates break character). |

### Seven lessons learned the hard way

Each was re-validated across multiple plugins:

1. **Structural judgment beats keyword lists.** Don't enumerate keywords or cap reply length when detecting intent. The correct shape is "user asked for an image + model didn't explicitly refuse + model isn't asking what to draw" ⇒ generate. Filter objects by exclusion (list what can't be drawn), not by enumerating what can.
2. **Log the non-trigger path.** Otherwise every investigation is guesswork. `dsh-imagegen`'s "sometimes doesn't generate" took three rounds; the first two failed purely for lack of logs.
3. **Anywhere you call an LLM outside the message pipeline, duplicate the marker-stripping logic.** `llm_generate` bypasses the pipeline, so the sticker hook never fires — that's how `[sticker:peek]` leaked into the group.
4. **Thresholds must come from measured distributions.** `dsh-mention` v1 had "mention if delay ≥ 8s", but the delay measured *the bot's own thinking time* — a persona-laden call routinely takes 5–15s, making the rule equivalent to "always mention". Result: 8 of 8 replies over 24 hours carried a mention.
5. **Fail-open disguises a crash as a quality regression.** `dsh-decide` unpacked a 3-tuple as a 2-tuple, so once the bot had spoken recently it always raised `ValueError` and fell through to fail-open "just talk anyway" — **12 crashes vs 9 successful judgments in one day**, meaning over half the replies never received the "who is speaking to whom" verdict. The feature looked perfectly healthy; it was just wrong. Any fail-open fallback must log at WARN and count.
6. **A trigger count dropping to zero doesn't mean the misjudgment is fixed.** After tightening the question rule, `dsh-emotion`'s curiosity hits fell from 14 to 0 — which looked like "no more false positives" but was total blindness: the most common follow-up in this group ("so why don't you") has its question word mid-sentence, while the new regex anchored to the start. Rule changes require per-sample comparison, never just totals.
7. **Shadow mode is valuable for replay, not for waiting.** Running it overnight only reveals that night's handful of messages; replaying 1618 archived messages exposed both real misjudgments in 20 minutes (treating the substring in "balance/quota" as an awkward filler word, and treating undirected insults as aimed at the bot).

### Quick start

You need a server already running [AstrBot](https://github.com/AstrBotDevs/AstrBot) + [NapCat](https://github.com/NapNeko/NapCatQQ) (if not, follow the [deployment manual](docs/10-云服务器部署手册.md)).

```bash
# 1. Install: copy plugin directories into AstrBot's plugin folder
cp -r plugins/dsh-* /opt/qqbot/astrbot/data/plugins/

# 2. Configure: every knob is an environment variable, set via docker-compose env_file.
#    Each plugin's variable list is documented at the top of its main.py.
#    e.g. DSH_GUARD_SHADOW=1 runs moderation in judge-only mode (recommended at first)

# 3. Restart (plugin code changes need `restart`; `up -d` won't restart when config is unchanged)
docker restart astrbot
```

Unit tests run without AstrBot:

```bash
python3 plugins/dsh-guard/test_guard.py
python3 plugins/dsh-style/test_style.py
python3 plugins/dsh-decide/test_decide.py
python3 plugins/dsh-poke/test_poke.py
python3 plugins/dsh-sticker/test_sticker.py
python3 plugins/dsh-emotion/test_emotion.py    # 36 cases
python3 plugins/dsh-quote/test_quote.py        # 25 cases

# Replay emotion decisions over your own history to surface misjudgments
python3 plugins/dsh-emotion/replay_emotion.py
```

Build the console APK yourself (no Gradle, no Android Studio):

```bash
cd console
KEYSTORE=/path/to/your.jks KS_PASS=yourpass bash build.sh
bash test/run-tests.sh    # 391 pure-JVM unit tests
```

### What you must supply

No credentials are included. You need your own:

- A secondary QQ account (**not your main one** — ban risk)
- An LLM API for chat (a cheap flash-tier model is enough)
- Optional: vision model, text-to-image, TTS, search — each missing one only removes its own feature

### Known limitations

- **Speech recognition doesn't work.** Every channel tried was either out of credit or had no audio model, so a voice message only registers as "someone sent a voice message".
- **The QQ account goes offline.** Every 20–60 minutes. Changing IP doesn't help (measured: once an account is flagged, a new IP won't save it). `server/qq_watchdog.sh` is the watchdog that auto-restarts and refreshes the login QR.
- **The console backend is plaintext HTTP.** No TLS, so the password travels unencrypted. This is a port-availability constraint — be aware on public Wi-Fi.
- **Plugins depend on AstrBot internals.** They touch non-public attributes (`provider_manager.inst_map`, `req.contexts`, etc.), so major AstrBot upgrades may require adjustments.

### Privacy

Everything was redacted before publishing: real server IPs, group IDs, QQ numbers, member nicknames, personal email, and paid API relay domains were all replaced with placeholders (`your-server.example.com`, `100000001`, `群友A`, etc.). Measured data kept in the docs (latencies, hit rates, token counts) contains no identifying information.

### License

MIT — see [LICENSE](LICENSE).
