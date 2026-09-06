# 插件目录 / Plugin Directory

28 个 AstrBot 插件的索引。每个插件的设计取舍、踩过的坑、实测数据都写在各自 `main.py` 的顶部注释里 —— 那里比这张表详细得多。

Index of the 28 AstrBot plugins. Design tradeoffs, pitfalls, and measured data live in the header comment of each `main.py`, which is far more detailed than this table.

## 安装 / Install

```bash
cp -r dsh-* /opt/qqbot/astrbot/data/plugins/
docker restart astrbot     # 改插件代码必须 restart；配置没变时 up -d 不会重启
```

配置全部走环境变量（写进 docker-compose 的 `env_file`）。每个插件的变量清单在其 `main.py` 顶部。

All configuration is via environment variables (put them in docker-compose's `env_file`). Each plugin lists its variables at the top of `main.py`.

## 一览 / Overview

| 插件 | 行数 | 指令 | LLM 工具 | 外部服务 |
|---|---|---|---|---|
| `dsh-acl` | 474 | `/权限` `/权限状态` `/reset` | — | — |
| `dsh-claimguard` | 524 | `/防骗状态` | — | — |
| `dsh-ctxclean` | 372 | `/上下文状态` | — | — |
| `dsh-decide` | 771 | `/插话判断` | — | 小模型（可与主模型同渠道） |
| `dsh-drift` | 181 | `/漂移状态` | — | — |
| `dsh-effect` | 455 | `/回复效果` | — | 小模型（只在窗口内有人说话时才调） |
| `dsh-emotion` | 506 | `/情绪状态` | — | — |
| `dsh-fwd` | 1050 | `/聊天记录状态` | — | 视觉模型（转发里的图） |
| `dsh-glossary` | 356 | `/黑话状态` | — | — |
| `dsh-guard` | 737 | `/禁言状态` | — | 小模型 |
| `dsh-human` | 332 | `/拟人状态` | — | — |
| `dsh-imagegen` | 1053 | `/画图` `/画图状态` | `generate_image` | 文生图 API |
| `dsh-imgctx` | 600 | `/图片上下文` | — | 视觉模型 |
| `dsh-initiate` | 620 | `/主动开口状态` `/主动开口测试` | — | 小模型 |
| `dsh-memory` | 1616 | `/我的档案` `/忘记我` `/记住` `/记忆状态` `/群记忆` 等 | — | 抽取用小模型 |
| `dsh-mention` | 291 | `/艾特模式` | — | — |
| `dsh-poke` | 319 | `/戳一戳状态` | — | — |
| `dsh-quote` | 231 | `/引用状态` | — | — |
| `dsh-slang` | 1072 | `/黑话学习状态` `/黑话候选` `/黑话详情` `/黑话确认` `/黑话拒绝` `/黑话备注` | — | 主模型（提取+考究+每 8h 自动审核） |
| `dsh-spine` | 542 | `/脊梁状态` | — | — |
| `dsh-sticker` | 344 | `/贴纸状态` | — | — |
| `dsh-style` | 333 | `/说话风格` | — | — |
| `dsh-typo` | 345 | `/错字状态` | — | — |
| `dsh-video` | 1290 | `/做视频` `/视频状态` | `generate_video` | 视觉模型 + 文生视频 API |
| `dsh-vischain` | 377 | `/识图状态` | — | 多个视觉模型（降级链） |
| `dsh-voice` | 812 | `/说话` `/音色` `/语音状态` | `send_voice` | TTS API |
| `dsh-web` | 1236 | `/看网页` `/搜` `/b站` `/联网状态` | `web_search` `read_webpage` `bilibili_video` | 搜索 API + B 站公开端点 |
| `dsh-welcome` | 351 | `/欢迎测试` `/欢迎状态` | — | 聊天模型 |


零外部依赖的插件（`acl`、`claimguard`、`ctxclean`、`drift`、`emotion`、`glossary`、`human`、`mention`、`poke`、`quote`、`spine`、`sticker`、`style`、`typo`、`slang`）拷进去就能用，不需要额外配 API —— 28 个里有 15 个属于这一类。

Plugins with no external dependencies (`acl`, `claimguard`, `ctxclean`, `drift`, `emotion`, `glossary`, `human`, `mention`, `poke`, `quote`, `spine`, `sticker`, `style`, `typo`, `slang`) work as soon as they're copied in — no extra API setup. That's 15 of the 28.

## 建议的启用顺序 / Suggested rollout order

1. **先装 `dsh-ctxclean`。** 如果机器人答非所问，这是根因所在，装了立刻见效，且不依赖任何外部服务。
2. **再装 `dsh-mention` + `dsh-style` + `dsh-human`。** 一个减少刷屏感，一个让句子变短，一个把逗号粘住的两句拆成两条 —— 都是零成本零依赖，对「像不像真人」的提升最直接。
3. **`dsh-quote` 越早装越好。** 零依赖、零风险：它只把引用消息改写成「谁引用了谁的哪句话」，不改变机器人说不说话。群里有重名昵称时尤其必要。
4. **`dsh-guard` / `dsh-emotion` / `dsh-initiate` 都先跑影子模式。** 分别设 `DSH_GUARD_SHADOW=1` / `DSH_EMOTION_SHADOW=1` / `DSH_INITIATE_SHADOW=1` 只判定不动作，观察日志确认判得准了再放开。`dsh-emotion` 另带回放脚本，能直接拿历史语料把误判找出来。
5. **`dsh-glossary` / `dsh-drift` / `dsh-typo` 属于「锦上添花」，随时可加可撤。** 三个都零依赖。词表要**自己填**（挖语料的脚本在 `dsh-glossary/tools/`，但候选一律要人过目 —— 释义错了比没有更糟）；错别字建议先把 `DSH_TYPO_RATE` 调到 0.03 观察群里反应，看着像坏了就关掉。
6. **`dsh-effect` 想什么时候装都行，它不改行为。** 只记录「说完之后群里什么反应」，关掉对机器人零影响。它是唯一能回答「这些改动到底有没有用」的插件，越早装积累的样本越多。
7. **`dsh-slang` 随时可装，自动跑。** 从群聊自动挖候选、主模型考究释义、每 8 小时 AI 自动审核转正，全程无需人工（群主命令可兜底）。影子模式（`DSH_SLANG_SHADOW=1`）先观察一轮再转正式更稳。
8. **多模态最后上。** 每个都要自己的 API key，缺哪个就少哪个功能。

---

1. **Start with `dsh-ctxclean`.** If the bot goes off-topic, this is the root cause; it takes effect immediately and needs no external service.
2. **Then `dsh-mention` + `dsh-style` + `dsh-human`.** One reduces mention spam, one shortens sentences, one splits comma-glued clauses into separate messages — all free, all dependency-free, and the most direct improvement to "does this read like a person".
3. **Install `dsh-quote` early.** Zero dependencies, zero risk: it only rewrites quoted messages into "who quoted whose line", never changing whether the bot speaks. Essential when nicknames collide in your group.
4. **Run `dsh-guard` / `dsh-emotion` / `dsh-initiate` in shadow mode first.** Set `DSH_GUARD_SHADOW=1` / `DSH_EMOTION_SHADOW=1` / `DSH_INITIATE_SHADOW=1` to judge without acting; review logs before enabling. `dsh-emotion` also ships a replay script that surfaces misjudgments against your own history.
5. **`dsh-glossary` / `dsh-drift` / `dsh-typo` are polish — add or drop them any time.** All three are dependency-free. The glossary must be **filled in yourself** (the corpus-mining script is in `dsh-glossary/tools/`, but every candidate needs human review — a wrong definition is worse than none); for typos, start at `DSH_TYPO_RATE=0.03` and watch the group's reaction.
6. **`dsh-effect` can go in at any point; it changes no behaviour.** It only records what the group does after the bot speaks. It is the only plugin that can answer "did any of this actually help", so the earlier it goes in, the more samples you have.
7. **`dsh-slang` can go in any time; it runs by itself.** It mines candidates from group chat, researches meanings with the main model, and auto-reviews every 8 hours — no manual work (owner commands remain as fallback). Shadow mode (`DSH_SLANG_SHADOW=1`) for one cycle first is safer.
8. **Multimodal last.** Each needs its own API key; a missing one only removes its own feature.

## 测试 / Tests

十四个插件带纯函数单元测试，不需要跑起 AstrBot：

Fourteen plugins ship pure-function unit tests that run without AstrBot:

```bash
python3 dsh-guard/test_guard.py       # 关键词预筛 + 决策函数
python3 dsh-style/test_style.py       # 样本抽取 + 去重 + 配额
python3 dsh-decide/test_decide.py     # 判定解析 + 限流
python3 dsh-poke/test_poke.py         # 三道闸门 + 回话文案
python3 dsh-sticker/test_sticker.py   # 标记剥离（含不误伤用例）
python3 dsh-emotion/test_emotion.py   # 36 项：抽取规则 + 状态机 + 清零 + 脏数据收敛
python3 dsh-quote/test_quote.py       # 25 项：@剥离 + 只认 QQ 号的身份判定 + 块形状
python3 dsh-acl/test_acl.py           # 三档权限 + 只按 QQ 号判身份 + 拒绝提示冷却
python3 dsh-fwd/test_fwd.py           # 嵌套展开 + 保头保尾 + 图片视频预算
python3 dsh-glossary/test_glossary.py # 真命中 + 防假命中 + 注入块形状 + 说明文字不许膨胀
python3 dsh-drift/test_drift.py       # 两条硬边界 + 判定顺序 + 每档都带约束
python3 dsh-typo/test_typo.py         # 同音表自检 + 48 条保护名单一个字都不许动
python3 dsh-effect/test_effect.py     # 建库幂等 + 脏输出退回安全值 + 枚举完整
python3 dsh-slang/test_slang.py       # 16 项：触发/幻觉拦截/注入预算/自动审核门/重启持久化
```

`dsh-glossary` 另带一个**只出报告、不写任何文件**的候选词体检脚本。它把 B 站热搜/排行榜
候选词跟本群语料对撞（一向 0 命中，这是正常结果），并从语料里挖「被当成一整条消息发出来、
且至少两个人发过」的短说法：

`dsh-glossary` also ships a **report-only** candidate audit script (it writes no files). It cross-checks
Bilibili trending terms against your own corpus (consistently 0 hits — that is the expected result) and
mines short phrases that "were sent as an entire message by at least two different people":

```bash
python3 dsh-glossary/tools/refresh_candidates.py [--no-net] [--top N]
```

`dsh-emotion` 另带一个回放脚本，拿你自己的历史语料统计触发分布与可疑样本 ——
上线前发现的两个真误判就是它找出来的，单元测试只能验证你已经想到的情况：

`dsh-emotion` also ships a replay script that measures trigger distribution and suspicious
samples against your own history — it found both real misjudgments before rollout, which
unit tests could never have caught:

```bash
python3 dsh-emotion/replay_emotion.py [plugin_path]
```
