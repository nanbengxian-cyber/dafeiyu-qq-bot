# 插件目录 / Plugin Directory

19 个 AstrBot 插件的索引。每个插件的设计取舍、踩过的坑、实测数据都写在各自 `main.py` 的顶部注释里 —— 那里比这张表详细得多。

Index of the 19 AstrBot plugins. Design tradeoffs, pitfalls, and measured data live in the header comment of each `main.py`, which is far more detailed than this table.

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
| `dsh-claimguard` | 405 | `/防骗状态` | — | — |
| `dsh-ctxclean` | 369 | `/上下文状态` | — | — |
| `dsh-decide` | 663 | `/插话判断` | — | 小模型（可与主模型同渠道） |
| `dsh-guard` | 501 | `/禁言状态` | — | 小模型 |
| `dsh-imagegen` | 824 | `/画图` `/画图状态` | `generate_image` | 文生图 API |
| `dsh-imgctx` | 600 | `/图片上下文` | — | 视觉模型 |
| `dsh-memory` | 1394 | `/我的档案` `/忘记我` `/记住` `/记忆状态` `/群记忆` 等 | — | 抽取用小模型 |
| `dsh-emotion` | 437 | `/情绪状态` | — | — |
| `dsh-initiate` | 620 | `/主动开口状态` `/主动开口测试` | — | 小模型 |
| `dsh-mention` | 291 | `/艾特模式` | — | — |
| `dsh-poke` | 319 | `/戳一戳状态` | — | — |
| `dsh-quote` | 228 | `/引用状态` | — | — |
| `dsh-sticker` | 280 | `/贴纸状态` | — | — |
| `dsh-style` | 264 | `/说话风格` | — | — |
| `dsh-video` | 1215 | `/做视频` `/视频状态` | `generate_video` | 视觉模型 + 文生视频 API |
| `dsh-vischain` | 310 | `/识图状态` | — | 多个视觉模型（降级链） |
| `dsh-voice` | 806 | `/说话` `/音色` `/语音状态` | `send_voice` | TTS API |
| `dsh-web` | 1210 | `/看网页` `/搜` `/b站` `/联网状态` | `web_search` `read_webpage` `bilibili_video` | 搜索 API + B 站公开端点 |
| `dsh-welcome` | 351 | `/欢迎测试` `/欢迎状态` | — | 聊天模型 |

零外部依赖的插件（`claimguard`、`ctxclean`、`emotion`、`mention`、`poke`、`quote`、`sticker`、`style`）拷进去就能用，不需要额外配 API。

Plugins with no external dependencies (`claimguard`, `ctxclean`, `emotion`, `mention`, `poke`, `quote`, `sticker`, `style`) work as soon as they're copied in — no extra API setup.

## 建议的启用顺序 / Suggested rollout order

1. **先装 `dsh-ctxclean`。** 如果机器人答非所问，这是根因所在，装了立刻见效，且不依赖任何外部服务。
2. **再装 `dsh-mention` + `dsh-style`。** 一个减少刷屏感，一个让句子变短，都是零成本。
3. **`dsh-quote` 越早装越好。** 零依赖、零风险：它只把引用消息改写成「谁引用了谁的哪句话」，不改变机器人说不说话。群里有重名昵称时尤其必要。
4. **`dsh-guard` / `dsh-emotion` / `dsh-initiate` 都先跑影子模式。** 分别设 `DSH_GUARD_SHADOW=1` / `DSH_EMOTION_SHADOW=1` / `DSH_INITIATE_SHADOW=1` 只判定不动作，观察日志确认判得准了再放开。`dsh-emotion` 另带回放脚本，能直接拿历史语料把误判找出来。
5. **多模态最后上。** 每个都要自己的 API key，缺哪个就少哪个功能。

---

1. **Start with `dsh-ctxclean`.** If the bot goes off-topic, this is the root cause; it takes effect immediately and needs no external service.
2. **Then `dsh-mention` + `dsh-style`.** One reduces mention spam, the other shortens sentences. Both free.
3. **Install `dsh-quote` early.** Zero dependencies, zero risk: it only rewrites quoted messages into "who quoted whose line", never changing whether the bot speaks. Essential when nicknames collide in your group.
4. **Run `dsh-guard` / `dsh-emotion` / `dsh-initiate` in shadow mode first.** Set `DSH_GUARD_SHADOW=1` / `DSH_EMOTION_SHADOW=1` / `DSH_INITIATE_SHADOW=1` to judge without acting; review logs before enabling. `dsh-emotion` also ships a replay script that surfaces misjudgments against your own history.
5. **Multimodal last.** Each needs its own API key; a missing one only removes its own feature.

## 测试 / Tests

七个插件带纯函数单元测试，不需要跑起 AstrBot：

Seven plugins ship pure-function unit tests that run without AstrBot:

```bash
python3 dsh-guard/test_guard.py       # 关键词预筛 + 决策函数
python3 dsh-style/test_style.py       # 样本抽取 + 去重 + 配额
python3 dsh-decide/test_decide.py     # 判定解析 + 限流
python3 dsh-poke/test_poke.py         # 三道闸门 + 回话文案
python3 dsh-sticker/test_sticker.py   # 标记剥离（含不误伤用例）
python3 dsh-emotion/test_emotion.py   # 36 项：抽取规则 + 状态机 + 清零 + 脏数据收敛
python3 dsh-quote/test_quote.py       # 25 项：@剥离 + 只认 QQ 号的身份判定 + 块形状
```

`dsh-emotion` 另带一个回放脚本，拿你自己的历史语料统计触发分布与可疑样本 ——
上线前发现的两个真误判就是它找出来的，单元测试只能验证你已经想到的情况：

`dsh-emotion` also ships a replay script that measures trigger distribution and suspicious
samples against your own history — it found both real misjudgments before rollout, which
unit tests could never have caught:

```bash
python3 dsh-emotion/replay_emotion.py [plugin_path]
```
