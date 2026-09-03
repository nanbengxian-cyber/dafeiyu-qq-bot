# 大肥鱼 QQ 机器人 · DaFeiYu QQ Bot

[中文](#中文) · [English](#english)

一套让 QQ 群机器人「像真人群友一样说话」的完整工程：16 个 AstrBot 插件、一个 QQ↔AI 桥接程序、一个安卓控制台 App，以及记录每个问题根因与实测数据的技术文档。

A complete engineering effort to make a QQ group bot *talk like an actual group member*: 16 AstrBot plugins, a QQ↔AI bridge, an Android console app, and technical documents recording the root cause and measured data behind every fix.

---

## 中文

### 这是什么

一个跑在 QQ 群里的 AI 群友（人格叫「小鲸鱼」）。它不是客服式问答机器人 —— 目标是**混在群里像个真人**：会潜水、会插话、会发表情包、会戳回去、看得见图片和视频、记得住群友是谁。

仓库包含四块可独立使用的东西：

| 目录 | 内容 | 语言 |
|---|---|---|
| [`plugins/`](plugins/) | 16 个 AstrBot 插件，9600 行 —— 机器人的全部能力 | Python |
| [`bridge/`](bridge/) | QQ ↔ DeepSeek Harness 桥接（另一条技术路线） | Node.js |
| [`console/`](console/) | 安卓控制台 App + 服务端后台 | Java / Python |
| [`docs/`](docs/) | 部署手册与 12 份问题根因分析 | Markdown |

### 成品下载

安卓控制台 App 在 [Releases](../../releases) 页面下载（约 110 KB，安卓 5.0+）。装完在登录页填自己的服务器地址即可，包内不含任何服务器信息。

### 16 个插件在解决什么

每个插件对应一个**实际发生过的问题**，不是功能清单式的堆砌。

**说话质量**

| 插件 | 解决的问题 |
|---|---|
| [`dsh-ctxclean`](plugins/dsh-ctxclean/) | 答非所问。框架把每轮注入的群聊上下文原样存进历史，且 `max_context_length=-1` 不截断，于是每次请求重发上百份互相矛盾的「最新群聊」——25.7 万字里注入块占 31%，真人原话只占 2.2%。 |
| [`dsh-style`](plugins/dsh-style/) | 说话太像 AI。从真人短句里抽样本注入，把平均句长从 16.5 字压到 9.5 字。 |
| [`dsh-decide`](plugins/dsh-decide/) | 主动插话前先用小模型判断「在聊什么、该不该开口」，判沉默就掐掉整次主模型调用，省约 9400 token。 |
| [`dsh-mention`](plugins/dsh-mention/) | 每条回复都 @ 人。只在「调了工具 / 被别人的消息刷走 / 隔太久」时才 @。 |
| [`dsh-claimguard`](plugins/dsh-claimguard/) | 被骗认输。有人说「叫我爸爸」「单挑你输了」它就当真 —— 靠注入事实而不是改人格来修。 |

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

### 反复踩到的四个坑

这些教训在多个插件上重复验证过，写在这里省得再踩：

1. **结构判断优于枚举词表。** 意图判定别列举关键词、别掐回复字数。正确形状是「用户要图 + 模型没明确拒绝 + 模型不是在反问」⇒ 出图。宾语过滤用排除法（列画不出的），而不是枚举画得出的。
2. **不触发时必须打日志。** 否则每次排查只能靠猜。`dsh-imagegen` 的「有时候不生图」查了三轮，前两轮全是因为没有日志。
3. **绕过消息管道直接调 LLM 的地方，必须自己复制一份标记剥离逻辑。** `llm_generate` 不走管道，贴纸钩子不会触发，`[贴纸:探头]` 就这样漏进过群聊。
4. **阈值必须由实测分布决定。** `dsh-mention` v1 有条「延迟 ≥8s 就 @」的规则，但量到的是机器人自己想了多久 —— 一次带人格的调用普遍 5~15 秒，于是等价于「无条件 @」，24 小时 8 条回复 100% 都 @ 了。

### 文档

| 文档 | 内容 |
|---|---|
| [10-云服务器部署手册](docs/10-云服务器部署手册.md) | 从买服务器到跑起来，Docker + NapCat + AstrBot 全流程 |
| [20-控制台App说明](docs/20-控制台App说明.md) | 安卓 App 的三个页签、七种预设模式、安全边界 |
| [30-回复触发条件](docs/30-回复触发条件.md) | 什么情况下机器人会说话 |
| [31-记忆系统与群员轮廓](docs/31-记忆系统与群员轮廓.md) | 记忆的存储结构与隐私设计 |
| [40-答非所问根因与修复](docs/40-答非所问根因与修复.md) | 25.7 万字上下文的量化分析 |
| [41-被骗认输根因与修复](docs/41-被骗认输根因与修复.md) | 为什么改人格没用 |
| [42-空头承诺根因与修复](docs/42-空头承诺根因与修复.md) | 「嘴上答应却不做」的机制 |
| [43-说话更真实方案](docs/43-说话更真实方案.md) · [44-实施记录](docs/44-说话更真实实施记录.md) | 方案与落地数据 |
| [45-戳一戳与违规禁言](docs/45-戳一戳与违规禁言.md) | 权限模型与限流设计 |
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
```

控制台 App 自己出包（不需要 Gradle 和 Android Studio）：

```bash
cd console
KEYSTORE=/path/to/your.jks KS_PASS=yourpass bash build.sh
bash test/run-tests.sh    # 391 项纯 JVM 单测
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
| [`plugins/`](plugins/) | 16 AstrBot plugins, 9.6k lines — all bot capabilities | Python |
| [`bridge/`](bridge/) | QQ ↔ DeepSeek Harness bridge (an alternative approach) | Node.js |
| [`console/`](console/) | Android console app + server backend | Java / Python |
| [`docs/`](docs/) | Deployment manual and 12 root-cause analyses | Markdown |

### Download

The Android console app is on the [Releases](../../releases) page (~110 KB, Android 5.0+). Enter your own server address on the login screen; the package embeds no server details.

### What the 16 plugins fix

Each plugin addresses a **problem that actually happened**, not a feature checklist.

**Speech quality**

| Plugin | Problem solved |
|---|---|
| [`dsh-ctxclean`](plugins/dsh-ctxclean/) | Off-topic replies. The framework stored per-turn injected group context verbatim into history, and `max_context_length=-1` disables truncation — so every request resent hundreds of mutually contradictory "latest group chat" blocks. Injected blocks were 31% of a 257k-character context; real human speech only 2.2%. |
| [`dsh-style`](plugins/dsh-style/) | Sounding like an AI. Samples real human short sentences as style examples, cutting mean sentence length from 16.5 to 9.5 characters. |
| [`dsh-decide`](plugins/dsh-decide/) | Before jumping in unprompted, a small model judges "what's being discussed, should I speak". A "stay silent" verdict cancels the entire main-model call, saving ~9400 tokens. |
| [`dsh-mention`](plugins/dsh-mention/) | @-mentioning on every reply. Now only when a tool was used, the message got buried, or too much time passed. |
| [`dsh-claimguard`](plugins/dsh-claimguard/) | Being talked into submission. Someone says "call me daddy" or "you lost our duel" and it complies — fixed by injecting facts, not by editing the persona. |

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

### Four lessons learned the hard way

Each was re-validated across multiple plugins:

1. **Structural judgment beats keyword lists.** Don't enumerate keywords or cap reply length when detecting intent. The correct shape is "user asked for an image + model didn't explicitly refuse + model isn't asking what to draw" ⇒ generate. Filter objects by exclusion (list what can't be drawn), not by enumerating what can.
2. **Log the non-trigger path.** Otherwise every investigation is guesswork. `dsh-imagegen`'s "sometimes doesn't generate" took three rounds; the first two failed purely for lack of logs.
3. **Anywhere you call an LLM outside the message pipeline, duplicate the marker-stripping logic.** `llm_generate` bypasses the pipeline, so the sticker hook never fires — that's how `[sticker:peek]` leaked into the group.
4. **Thresholds must come from measured distributions.** `dsh-mention` v1 had "mention if delay ≥ 8s", but the delay measured *the bot's own thinking time* — a persona-laden call routinely takes 5–15s, making the rule equivalent to "always mention". Result: 8 of 8 replies over 24 hours carried a mention.

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
