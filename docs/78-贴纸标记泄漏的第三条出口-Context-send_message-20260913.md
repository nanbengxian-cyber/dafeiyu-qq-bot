# 贴纸标记泄漏的第三条出口：Context.send_message

## 发现时间
2026-09-13 12:43（群友引用暴露）→ 2026-09-13 14:05（根因定位）→ 14:09（修复部署）

## 现象
群友 汪 在 12:43:40 引用了一条大肥鱼的消息：

```
[引用消息(大肥鱼: 我没发涩图啊，头像那个别赖我[贴纸:装可怜])]
[At:3752949717] 你发了
```

这条被引用的消息文本里带着 `[贴纸:装可怜]` 标记原件，说明大肥鱼在发出时就没有把标记去掉。

## 第一次误判
从日志看，使用工具全天 0 次（send_message_to_user 工具没被调用→排除），三条空 Prepare to send + 0 次 guard 命中 → 以为还是 1.1.0 部署前的历史泄漏被翻旧账。但汪引用的 message_id 是 1200722939，无法得知发送时间（日志只有 09-04 起，nt_msg.db 加密不可读）。

## 第二次突破
盯关键时间线：
- 12:43:31.677 `[merge] gid=476573490 停歇后重扫并发送：上下文24条／其中艾特5条` ← dsh-merge 发送合并消息
- 12:43:40.310 汪引用「大肥鱼: 我没发涩图啊，头像那个别赖我[贴纸:装可怜]」说「你发了」
- 12:43:44.341 Prepare to send - 汪: 我哪发了 你翻记录去 ← 机器人回应

dsh-merge 发送后仅 9 秒汪就引用到，这条消息必须是 merge 刚发的。

## 根因
dsh-merge 的 `flush()` 函数（main.py:216-221）使用 `star_ctx.send_message(session, chain)`发送合并后的 LLM 回复。这段代码走的是 `star.Context.send_message`，而 dsh-sticker 1.1.0 只 patch 了 `AstrMessageEvent.send`（覆盖 event.send 直发路径），**没有 patch `Context.send_message`**。

两个 API 是并列的出口，内部调用路径完全不同：
- `AstrMessageEvent.send(message)` → 平台 adapter 的 event 级直接发送
- `Context.send_message(session, message_chain)` → 按 session 找平台 adapter，调 `platform.send_by_session()`发送

dsh-merge 还通过 `star_ctx.llm_generate()` 生成 LLM 文本（不入 on_llm_response 管道），生成的文本里有 `[贴纸:装可怜]` 标记。标记既没被 on_llm_response 剥（因为 llm_generate 不走这钩子），也没被 AstrMessageEvent.send patch 剥（因为走的是 Context.send_message）。

同样存在隐患的还有 dsh-merge `bare_at` 路径（第 290 行 `self.context.send_message()`）。

## 修复（dsh-sticker 1.2.0）

### 改动
给 `star.Context.send_message` 加同名兜底闸 `_guarded_ctx_send`，复用同一个 `guard_outgoing()` 纯函数，fail-open 语义完全一致：

```python
_ORIG_CTX_SEND = Context.send_message

async def _guarded_ctx_send(self, session, message_chain, *args, **kwargs):
    try:
        guard_outgoing(message_chain)
    except BaseException:
        pass
    return await _ORIG_CTX_SEND(self, session, message_chain, *args, **kwargs)

Context.send_message = _guarded_ctx_send
```

### 覆盖范围
- dsh-merge 的两条发送路径（整合回复 + bare_at）
- 内置工具 `send_message_to_user`（`message_tools.py:339` — `context.context.context.send_message()`）
- 任何以后使用 `Context.send_message` 的插件

### 三条防线的覆盖情况（截至 1.2.0）

| 路径 | 防护层 | 修复版本 |
|---|---|---|
| 主回复链（respond.stage / result.chain） | on_llm_response + on_decorating_result | 1.0.0 |
| event.send() 直发（dsh-welcome/imagegen/video/guard/poke） | AstrMessageEvent.send patch | 1.1.0 |
| Context.send_message（dsh-merge / send_message_to_user） | Context.send_message patch | 1.2.0 |
| send_streaming | AstrMessageEvent.send_streaming patch | 1.1.0 |

## 验证
1. 测试覆盖：`test_outbound_guard.py` 第 9 节（端到端 dsh-merge 真实泄漏文本 + fail-open + 干净文本不误伤 + 闸标记检查），10 节全过
2. 启动日志确认新闸门加载：
   ```
   14:09:20.850 [贴纸] 出口兜底闸已装：Context.send_message 直发的链也会剥标记
   Plugin dsh-sticker (1.2.0) ... 已加载
   ```
3. 生产重启后 `Context.send_message` 带 `_dsh_sticker_guard` 标记

## 教训
1. 「使用工具」0 次不能证明没走外部路径 —— dsh-merge 用的是 llm_generate + send_message，不是工具调用
2. 空 Prepare to send 行的解析：merge 触发的艾特风暴收集会 intercept 事件，导致多条空 Prepare to send。它们不是「消息被吞」而是插件拦截后的空回显
3. 942 5 类出口的枚举（docs/77）漏了 `Context.send_message` 这一大类。现在补上，三类（event.send / event.send_streaming / Context.send_message）已覆盖所有已知框架层出站隧道
4. AstrBot 的框架层出站隧道只有三条（event.send、event.send_streaming、Context.send_message），插件层出口（llm_generate、result.chain 等）最终都汇总到这三条之一