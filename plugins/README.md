# 插件目录 / Plugin Directory

16 个 AstrBot 插件的索引。每个插件的设计取舍、踩过的坑、实测数据都写在各自 `main.py` 的顶部注释里 —— 那里比这张表详细得多。

Index of the 16 AstrBot plugins. Design tradeoffs, pitfalls, and measured data live in the header comment of each `main.py`, which is far more detailed than this table.

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
| `dsh-ctxclean` | 355 | `/上下文状态` | — | — |
| `dsh-decide` | 517 | `/插话判断` | — | 小模型（可与主模型同渠道） |
| `dsh-guard` | 501 | `/禁言状态` | — | 小模型 |
| `dsh-imagegen` | 824 | `/画图` `/画图状态` | `generate_image` | 文生图 API |
| `dsh-imgctx` | 600 | `/图片上下文` | — | 视觉模型 |
| `dsh-memory` | 1394 | `/我的档案` `/忘记我` `/记住` `/记忆状态` `/群记忆` 等 | — | 抽取用小模型 |
| `dsh-mention` | 279 | `/艾特模式` | — | — |
| `dsh-poke` | 319 | `/戳一戳状态` | — | — |
| `dsh-sticker` | 280 | `/贴纸状态` | — | — |
| `dsh-style` | 264 | `/说话风格` | — | — |
| `dsh-video` | 1215 | `/做视频` `/视频状态` | `generate_video` | 视觉模型 + 文生视频 API |
| `dsh-vischain` | 310 | `/识图状态` | — | 多个视觉模型（降级链） |
| `dsh-voice` | 806 | `/说话` `/音色` `/语音状态` | `send_voice` | TTS API |
| `dsh-web` | 1210 | `/看网页` `/搜` `/b站` `/联网状态` | `web_search` `read_webpage` `bilibili_video` | 搜索 API + B 站公开端点 |
| `dsh-welcome` | 351 | `/欢迎测试` `/欢迎状态` | — | 聊天模型 |

零外部依赖的插件（`claimguard`、`ctxclean`、`mention`、`poke`、`sticker`、`style`）拷进去就能用，不需要额外配 API。

Plugins with no external dependencies (`claimguard`, `ctxclean`, `mention`, `poke`, `sticker`, `style`) work as soon as they're copied in — no extra API setup.

## 建议的启用顺序 / Suggested rollout order

1. **先装 `dsh-ctxclean`。** 如果机器人答非所问，这是根因所在，装了立刻见效，且不依赖任何外部服务。
2. **再装 `dsh-mention` + `dsh-style`。** 一个减少刷屏感，一个让句子变短，都是零成本。
3. **`dsh-guard` 先跑影子模式。** 设 `DSH_GUARD_SHADOW=1` 只判定不禁言，观察几天日志确认判得准了再放开。
4. **多模态最后上。** 每个都要自己的 API key，缺哪个就少哪个功能。

---

1. **Start with `dsh-ctxclean`.** If the bot goes off-topic, this is the root cause; it takes effect immediately and needs no external service.
2. **Then `dsh-mention` + `dsh-style`.** One reduces mention spam, the other shortens sentences. Both free.
3. **Run `dsh-guard` in shadow mode first.** Set `DSH_GUARD_SHADOW=1` to judge without muting; review logs for a few days before enabling enforcement.
4. **Multimodal last.** Each needs its own API key; a missing one only removes its own feature.

## 测试 / Tests

五个插件带纯函数单元测试，不需要跑起 AstrBot：

Five plugins ship pure-function unit tests that run without AstrBot:

```bash
python3 dsh-guard/test_guard.py       # 关键词预筛 + 决策函数
python3 dsh-style/test_style.py       # 样本抽取 + 去重 + 配额
python3 dsh-decide/test_decide.py     # 判定解析 + 限流
python3 dsh-poke/test_poke.py         # 三道闸门 + 回话文案
python3 dsh-sticker/test_sticker.py   # 标记剥离（含不误伤用例）
```
