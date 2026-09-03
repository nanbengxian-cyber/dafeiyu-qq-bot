# 文档索引 / Documentation Index

按阅读顺序编号。想看「怎么跑起来」读 10；想看「为什么这么设计」读 40 之后那几份 —— 那些是问题根因分析，含实测数据和失败记录。

Numbered by reading order. For "how to run it", read 10; for "why it's built this way", read 40 and after — those are root-cause analyses with measured data and failure records.

## 部署与使用 / Deployment and usage

| 文档 | 内容 |
|---|---|
| [10-云服务器部署手册](10-云服务器部署手册.md) | 从买服务器到跑起来：Docker、NapCat、AstrBot、端口、扫码登录、常见故障 |
| [20-控制台App说明](20-控制台App说明.md) | 安卓 App 的三个页签、七种预设模式、安全边界、故障排查 |
| [30-回复触发条件](30-回复触发条件.md) | 什么情况下机器人会说话（白名单、唤醒词、主动插话、指令） |

## 设计方案 / Design

| 文档 | 内容 |
|---|---|
| [31-记忆系统与群员轮廓](31-记忆系统与群员轮廓.md) | 记忆的存储结构、抽取时机、隐私脱敏与容量上限 |
| [43-说话更真实方案](43-说话更真实方案.md) | 让机器人说话像真人的整体方案（含被否决的选项和否决理由） |
| [45-戳一戳与违规禁言](45-戳一戳与违规禁言.md) | 权限模型、限流设计、影子模式 |
| [60-长期目标与技术方案](60-长期目标与技术方案.md) | 整体架构与演进方向 |

## 根因分析 / Root-cause analyses

这几份是本仓库最有参考价值的部分 —— 每一份都是「症状 → 量化测量 → 找到真因 → 修复 → 验证」的完整记录，包括走错的路。

These are the most useful part of this repository — each is a complete record of symptom → quantified measurement → real cause → fix → verification, including the wrong turns.

| 文档 | 症状 | 真因 |
|---|---|---|
| [40-答非所问根因与修复](40-答非所问根因与修复.md) | 「说一件事它扯到另一件事上」 | 不是人格问题，是历史形状：注入块被存进历史且不截断，每次重发上百份矛盾的「最新群聊」。25.7 万字里注入块占 31%，真人原话仅 2.2% |
| [41-被骗认输根因与修复](41-被骗认输根因与修复.md) | 有人说「叫我爸爸」它就叫了 | 改人格没用；要注入事实 |
| [42-空头承诺根因与修复](42-空头承诺根因与修复.md) | 嘴上答应却不发图/不发语音 | 便宜模型在长上下文里不调工具；必须有兜底钩子，且兜底判定不能枚举关键词 |
| [44-说话更真实实施记录](44-说话更真实实施记录.md) | 说话太像 AI | 句长分布对不上真人；抽真人短句作样本，均值从 16.5 字降到 9.5 字 |
| [50-多模态接入与验证记录](50-多模态接入与验证记录.md) | 看不见图/视频、发不出语音 | 逐项实测记录，含每个渠道的成功率、延迟、token 数，以及做不到的事（语音识别） |

## 阅读提示 / Reading notes

文档里的数字都是在实际环境里量出来的，不是估算：响应时间、成功率、token 数、命中率、字数分布。凡是「实测」二字出现的地方，都有对应的测量方法写在旁边。

所有可识别信息已脱敏：服务器地址是 `your-server.example.com`，群号是 `100000001`，群友是「群友A/B/C」。保留的数据本身不含隐私。

All numbers were measured in a real deployment, not estimated: latencies, success rates, token counts, hit rates, length distributions. Wherever a measurement is cited, the method is described alongside it.

All identifying information is redacted: the server is `your-server.example.com`, the group ID is `100000001`, members are 群友A/B/C. The retained data itself contains nothing identifying.
