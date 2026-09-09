# 插件目录 / Plugin Directory

45 个 AstrBot 插件的索引。每个插件的设计取舍、踩过的坑、实测数据都写在各自 `main.py` 顶部注释里 —— 那里比这张表详细得多。

Index of 45 AstrBot plugins. Design tradeoffs, pitfalls, and measured data live in the header comment of each `main.py`, which is far more detailed than this table.

## 安装 / Install

```bash
cp -r dsh-* /opt/qqbot/astrbot/data/plugins/
```

配置全走环境变量,写进 docker-compose 的 `env_file`。改 `imagegen.env` 后用 `docker compose up -d astrbot`(重建容器,env 才会重新加载)。

All configuration is via environment variables in docker-compose's `env_file`. After editing `imagegen.env`, run `docker compose up -d astrbot` — a plain `restart` won't re-read the env file.

## 一览 / Overview

### 说话质量 / Speech quality

| 插件 | 行数 | 指令 | LLM 工具 | 外部服务 |
|---|---|---|---|---|
| `dsh-ctxclean` | 385 | `/上下文状态` | — | — |
| `dsh-style` | 333 | `/说话风格` | — | — |
| `dsh-decide` | 895 | `/插话判断` | — | 小模型 |
| `dsh-mention` | 319 | `/艾特模式` | — | — |
| `dsh-claimguard` | 525 | `/防骗状态` | — | — |
| `dsh-initiate` | 620 | `/主动开口状态` `/主动开口测试` | — | 小模型 |
| `dsh-emotion` | 582 | `/情绪状态` | — | — |
| `dsh-quote` | 231 | `/引用状态` | — | — |
| `dsh-glossary` | 408 | `/黑话状态` | — | — |
| `dsh-human` | 332 | `/拟人状态` | — | — |
| `dsh-typo` | 352 | `/错字状态` | — | — |
| `dsh-drift` | 181 | `/漂移状态` | — | — |
| `dsh-effect` | 562 | `/回复效果` | — | 小模型 |
| `dsh-aiflavour` | 257 | `/AI味状态` `/AI味模式` `/AI味名单` `/AI味清名单` | — | — |
| `dsh-humanizer` | 192 | `/人味状态` `/人味模式` | — | — |
| `dsh-noise` | 237 | `/噪点状态` | — | — |
| `dsh-armor` | 238 | `/破甲状态` `/破甲模式` `/破甲名单` `/破甲清名单` | — | — |
| `dsh-merge` | 436 | (自动聚合) | — | — |
| `dsh-quoteref` | 215 | (自动运行) | — | — |

### 多模态 / Multimodal

| 插件 | 行数 | 指令 | LLM 工具 | 外部服务 |
|---|---|---|---|---|
| `dsh-imgctx` | 600 | `/图片上下文` | — | 视觉模型 |
| `dsh-vischain` | 377 | `/识图状态` | — | 多个视觉模型(降级链) |
| `dsh-imagegen` | 1053 | `/画图` `/画图状态` | `generate_image` | 文生图 API |
| `dsh-video` | 1342 | `/做视频` `/视频状态` | `generate_video` | 视觉模型 + 文生视频 API |
| `dsh-voice` | 947 | `/说话` `/音色` `/语音状态` | `send_voice` | TTS API |
| `dsh-web` | 1252 | `/看网页` `/搜` `/b站` `/联网状态` | `web_search` `read_webpage` `bilibili_video` | 搜索 API + B 站公开端点 |
| `dsh-sticker` | 344 | `/贴纸状态` | — | — |
| `dsh-listen` | 488 | `/听语音状态` | — | AssemblyAI(转写) |

### 记忆与群管 / Memory & group management

| 插件 | 行数 | 指令 | LLM 工具 | 外部服务 |
|---|---|---|---|---|
| `dsh-memory` | 1810 | `/我的档案` `/忘记我` `/记住` `/记忆状态` `/群记忆` 等 | — | 抽取用小模型 |
| `dsh-guard` | 828 | `/禁言状态` | — | 小模型 |
| `dsh-poke` | 319 | `/戳一戳状态` | — | — |
| `dsh-welcome` | 351 | `/欢迎测试` `/欢迎状态` | — | 聊天模型 |
| `dsh-acl` | 534 | `/权限` `/权限状态` `/reset` | — | — |
| `dsh-fwd` | 1050 | `/聊天记录状态` | — | 视觉模型(转发里的图) |
| `dsh-spine` | 542 | `/脊梁状态` | — | — |
| `dsh-slang` | 1097 | `/黑话学习状态` `/黑话候选` `/黑话详情` `/黑话确认` `/黑话拒绝` `/黑话备注` | — | 主模型(提取+考究+每 8h 自动审核) |
| `dsh-factguard` | 204 | (自动运行) | — | — |
| `dsh-leakguard` | 400 | (自动运行) | — | — |
| `dsh-homophone` | 155 | (自动运行) | — | — |
| `dsh-joinguard` | 422 | `/入群问题` `/入群审核模式` | — | 小模型(审核) |
| `dsh-scene` | 466 | (自动运行) | — | — |
| `dsh-selfguard` | 376 | (自动运行) | — | — |
| `dsh-pay` | 354 | `/赞助状态` `/赞助模式 shadow\|live` `/赞助重置` | — | — |
| `dsh-proactive` | 572 | `/探头状态` | — | — |
| `dsh-interest` | 370 | `/馋什么` | — | — |
| `dsh-steal` | 610 | `/表情包` | — | 视觉模型(识图) |

零外部依赖的插件(`acl`、`aiflavour`、`armor`、`claimguard`、`ctxclean`、`drift`、`emotion`、`factguard`、`glossary`、`homophone`、`human`、`humanizer`、`interest`、`leakguard`、`merge`、`mention`、`noise`、`pay`、`poke`、`proactive`、`quote`、`quoteref`、`scene`、`selfguard`、`spine`、`sticker`、`style`、`typo`)拷进去就能用,不需要额外配 API —— **45 个里有 28 个属于这一类**。

Plugins with no external dependencies (`acl`, `aiflavour`, `armor`, `claimguard`, `ctxclean`, `drift`, `emotion`, `factguard`, `glossary`, `homophone`, `human`, `humanizer`, `interest`, `leakguard`, `merge`, `mention`, `noise`, `pay`, `poke`, `proactive`, `quote`, `quoteref`, `scene`, `selfguard`, `spine`, `sticker`, `style`, `typo`) work as soon as they are copied in — no extra API setup. That's **28 of the 45**.

## 建议的启用顺序 / Suggested rollout order

1. **先装 `dsh-ctxclean`。** 如果机器人答非所问,这是根因所在,装了立刻见效,且不依赖任何外部服务。
2. **再装 `dsh-mention` + `dsh-style` + `dsh-human`。** 一个减少刷屏感,一个让句子变短,一个把逗号粘住的两句拆成两条 —— 都是零成本零依赖,对「像不像真人」的提升最直接。
3. **`dsh-quote` 越早装越好。** 零依赖、零风险:它只把引用消息改写成「谁引用了谁的哪句话」,不改变机器人说不说话。群里有重名昵称时尤其必要。
4. **`dsh-armor` / `dsh-aiflavour` / `dsh-merge` 在准入组先装。** 防破甲(输入侧)、AI 味剥离(输出侧)、艾特风暴聚合,priority 2000 先于注入组跑:先判断「要不要接」,再谈「怎么说」。
5. **`dsh-guard` / `dsh-emotion` / `dsh-initiate` 都先跑影子模式。** 分别设 `DSH_GUARD_SHADOW=1` / `DSH_EMOTION_SHADOW=1` / `DSH_INITIATE_SHADOW=1` 只判定不动作,观察日志确认判得准了再放开。`dsh-emotion` 另带回放脚本,能直接拿历史语料把误判找出来。
6. **`dsh-glossary` / `dsh-drift` / `dsh-typo` / `dsh-noise` / `dsh-humanizer` 属于「锦上添花」,随时可加可撤。** 五个都零依赖。错别字建议先把 `DSH_TYPO_RATE` 调到 0.03 观察群里反应;噪点只作用于没人 @ 的接话,被 @ 的正经回答绝不动。
7. **`dsh-effect` 想什么时候装都行,它不改行为。** 只记录「说完之后群里什么反应」,关掉对机器人零影响。它是唯一能回答「这些改动到底有没有用」的插件,越早装积累的样本越多。
8. **`dsh-slang` 随时可装,自动跑。** 从群聊自动挖候选、主模型考究释义、每 8 小时 AI 自动审核转正,全程无需人工(群主命令可兜底)。影子模式(`DSH_SLANG_SHADOW=1`)先观察一轮再转正式更稳。
9. **`dsh-proactive` / `dsh-interest` / `dsh-initiate` / `dsh-steal` 在注入组确认稳定后上线。** 这些是主动行为插件,先看基本管线跑顺了再开。
10. **`dsh-joinguard` / `dsh-pay` 等群管类插件按需开启。** 启用前确认好入群问题与收款码路径。
11. **多模态最后上。** 每个都要自己的 API key,缺哪个就少哪个功能。

---

1. **Start with `dsh-ctxclean`.** If the bot goes off-topic, this is the root cause; it takes effect immediately and needs no external service.
2. **Then `dsh-mention` + `dsh-style` + `dsh-human`.** One reduces mention spam, one shortens sentences, one splits comma-glued clauses into separate messages — all free, all dependency-free, and the most direct improvement to "does this read like a person".
3. **Install `dsh-quote` early.** Zero dependencies, zero risk: it only rewrites quoted messages into "who quoted whose line", never changing whether the bot speaks. Essential when nicknames collide in your group.
4. **`dsh-armor` / `dsh-aiflavour` / `dsh-merge` first in the guard group.** Input-side injection defense, output-side AI flavor stripping, and @-storm reply merging run at priority 2000, before the injection group: decide "should I reply" before "how should I say it".
5. **Run `dsh-guard` / `dsh-emotion` / `dsh-initiate` in shadow mode first.** Set `DSH_GUARD_SHADOW=1` / `DSH_EMOTION_SHADOW=1` / `DSH_INITIATE_SHADOW=1` to judge without acting; review logs before enabling. `dsh-emotion` also ships a replay script that surfaces misjudgments against your own history.
6. **`dsh-glossary` / `dsh-drift` / `dsh-typo` / `dsh-noise` / `dsh-humanizer` are polish — add or drop them any time.** All five are dependency-free. For typos, start at `DSH_TYPO_RATE=0.03` and watch the group's reaction; noise only affects unmentioned replies, never @'d serious answers.
7. **`dsh-effect` can go in at any point; it changes no behaviour.** It only records what the group does after the bot speaks. It is the only plugin that can answer "did any of this actually help", so the earlier it goes in, the more samples you have.
8. **`dsh-slang` can go in any time; it runs by itself.** It mines candidates from group chat, researches meanings with the main model, and auto-reviews every 8 hours — no manual work. Shadow mode (`DSH_SLANG_SHADOW=1`) for one cycle first is safer.
9. **`dsh-proactive` / `dsh-interest` / `dsh-initiate` / `dsh-steal` go in after the injection pipeline is stable.** These are proactive behaviour plugins; wait until basic pipelines run cleanly.
10. **`dsh-joinguard` / `dsh-pay` — enable on demand.** Set up entry questions and pay QR codes before enabling.
11. **Multimodal last.** Each needs its own API key; a missing one only removes its own feature.

## 测试 / Tests

仓库里 24 个插件带纯函数单元测试,不需要跑起 AstrBot:

24 plugins in the repo ship pure-function unit tests that run without AstrBot:

```bash
python3 dsh-acl/test_acl.py                 # 三档权限 + 只按 QQ 号判身份 + 拒绝提示冷却
python3 dsh-aiflavour/test_aiflavour.py     # 静态强词 + 动态词根升级 + 会话刹车
python3 dsh-armor/test_armor.py             # 强/弱信号 + 累计升级降级 + 刹车
python3 dsh-decide/test_decide.py           # 判定解析 + 限流 + 熔断
python3 dsh-drift/test_drift.py             # 两条硬边界 + 判定顺序 + 每档都带约束
python3 dsh-effect/test_effect.py           # 建库幂等 + 脏输出退回安全值 + 枚举完整
python3 dsh-emotion/test_emotion.py         # 36 项:抽取规则 + 状态机 + 清零 + 脏数据收敛
python3 dsh-fwd/test_fwd.py                 # 嵌套展开 + 保头保尾 + 图片视频预算
python3 dsh-glossary/test_glossary.py       # 真命中 + 防假命中 + 注入块形状 + 说明文字不许膨胀
python3 dsh-guard/test_guard.py             # 关键词预筛 + 决策函数
python3 dsh-interest/test_interest.py       # 热度计算 + 口味轮换 + 注入预算
python3 dsh-leakguard/test_leakguard.py     # 强/弱标题档 + 指令句影子层
python3 dsh-memory/test_memory.py           # 抽取/去重/容量/过期 + 注入预算
python3 dsh-poke/test_poke.py               # 三道闸门 + 回话文案
python3 dsh-proactive/test_proactive.py     # 正则兴趣评分 + 限流 + 每日额度
python3 dsh-quote/test_quote.py             # 25 项:@剥离 + 只认 QQ 号的身份判定 + 块形状
python3 dsh-quoteref/test_quoteref.py       # 计数器 + 冷却 + At 摘除
python3 dsh-scene/test_scene.py             # 背景加载 + 作息判定
python3 dsh-selfguard/test_selfguard.py     # 重复判定 + 冲突拱火 + 豁免
python3 dsh-slang/test_slang.py             # 16 项:触发/幻觉拦截/注入预算/自动审核门/重启持久化
python3 dsh-sticker/test_sticker.py         # 标记剥离(含不误伤用例)
python3 dsh-style/test_style.py             # 样本抽取 + 去重 + 配额
python3 dsh-typo/test_typo.py               # 同音表自检 + 48 条保护名单一个字都不许动
python3 dsh-video/test_video.py             # 抽帧 + 生成任务排队
```

`dsh-glossary` 另带一个**只出报告、不写任何文件**的候选词体检脚本。它把 B 站热搜/排行榜
候选词跟本群语料对撞(一向 0 命中,这是正常结果),并从语料里挖「被当成一整条消息发出来、
且至少两个人发过」的短说法:

`dsh-glossary` also ships a **report-only** candidate audit script (it writes no files). It cross-checks
Bilibili trending terms against your own corpus (consistently 0 hits — that is the expected result) and
mines short phrases that "were sent as an entire message by at least two different people":

```bash
python3 dsh-glossary/tools/refresh_candidates.py [--no-net] [--top N]
```

`dsh-emotion` 另带一个回放脚本,拿你自己的历史语料统计触发分布与可疑样本 ——
上线前发现的两个真误判就是它找出来的,单元测试只能验证你已经想到的情况:

`dsh-emotion` also ships a replay script that measures trigger distribution and suspicious
samples against your own history — it found both real misjudgments before rollout, which
unit tests could never have caught:

```bash
python3 dsh-emotion/replay_emotion.py [plugin_path]
```