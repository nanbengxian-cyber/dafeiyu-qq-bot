# Android / DSH 手机版适配说明（本机安装记录）

本文件由适配安装过程生成，记录本机（DSH 移动版 / Android）与仓库原始 Windows 假设的差异，以及已经做了哪些改动。仓库原版面向 Windows 桌面，本机是 Android 上的 DSH shell，两者在「进程控制」「启动脚本」「node 路径」三处不兼容，已逐项处理。

## 安装位置

- 桥接仓库：`<sdcard>/qq-bridge`
- DSH preset：`~/.dsh/.agent-presets/qq-chat`、`~/.dsh/.agent-presets/qq-chat-v2`
- DSH 插件链接：`~/.dsh/plugins/qq-mode-console` →（符号链接）仓库内 `plugins/qq-mode-console`
- MCP 挂载：`~/.dsh/profiles/web/cordis.patch.yml` 的 `# === qq-bridge MCP BEGIN/END ===` 块

> 注意：MCP 与插件链接都是**绝对路径**。移动或重装 qq-bridge 目录后，必须重跑
> `DSH_NODE_BIN=$(command -v node) node scripts/setup-dsh.mjs web`。

## 已做的代码适配（相对上游 main 分支）

1. `scripts/setup-dsh.mjs` — `resolveNodeBin()`
   上游用 `process.execPath` 作为 MCP `command`。Android 版 DSH 上 `process.execPath`
   解析成 `/apex/com.android.runtime/bin/linker64`（动态链接器，不是 node），
   写进 cordis.patch.yml 会让三个 MCP server 全部起不来。
   现在按 `DSH_NODE_BIN` → PATH 上的 `node` → `process.execPath`（仅当它确实叫 node）优先级探测。

2. `src/mcp-host-server.js` — `PROCESS_CONTROL_SUPPORTED`
   `start_snowluma` / `stop_snowluma` / `findSnowLumaPids()` 依赖
   `powershell.exe`、`taskkill.exe`、`cmd.exe /c start`，Android 上都不存在。
   现在非 Windows 宿主上这两个工具不注册、PID 查询直接返回空数组（失败关闭），
   只保留 `snowluma_status` 的 HTTP 探活——它照样能探本机或局域网内 Windows 上的 OneBot 网关。

3. 新增 `start.sh` / `restart.sh`
   `start.bat` / `restart.bat` 的等价物（bat 保留未删，方便回到 Windows 用）。
   - `bash start.sh`：前台守护，崩溃 5 秒后自动拉起，退出码 2（已有实例）不重启。
   - `nohup setsid bash start.sh >/dev/null 2>&1 &`：后台守护，脱离进程树。
   - `bash restart.sh`：杀旧守护与旧 `bridge.js`（跳过 `mcp-*` 子进程）→ 删过期锁 → 后台重启。

## 本机 config.json 的取值

- `dsh.provider` = `opi`，`dsh.model` = `claude-opus-4-8`
  （原示例的 `deepseek-official` / `deepseek-v4-flash-vision-exp` 在本机 DSH 设置里不存在；
  选不到模型只打日志不阻塞启动，但会退回默认模型，所以直接对齐本机可用值。）
- `dsh.reasoningEffort` = `""`（留空）。opi 的 Claude 系列不带 `reasoning.efforts`，
  传 `max` 会 `model-unavailable` 让 selectModel 整个失败（08-30 日志里连续两次失败即此因）。

## 视觉（看图）能力：2026-08-30 已打通

`qq_get_message_images` 之前一直返回
`[image unavailable: image/jpeg; model "claude-opus-4-8" does not declare image input]`。
根因**不是** opi 站没有视觉模型，而是 DSH 侧的模型声明缺了图片模态：

- `dsh-mcp-client` 的 `resolveImageAdmission()` 在把 MCP 图片落成 attachment 之前，
  会调 `llm.resolveModelInfo(provider, model)` 检查 `inputModalities.includes('image')`；
  不含 image 就把整批图片投影成上面那行占位文字（原始字节仍在，只是模型看不到）。
- `llm-pi-ai` 对**手写声明的 provider**（opi 这种 pi-ai 自带目录里没有的站）
  默认 `defaultInput: ["text"]`，所以 claude-opus-4-8 被标成纯文本模型 —— 而
  该站上游实际会路由到 `claude-opus-5`，本身完全支持图片。

修复：在 `~/.dsh/settings.yaml` 的 `llm-pi-ai.providers.opi.models` 里给四个 Claude
条目都显式写上 `input: [text, image]`。settings 文件是热加载（chokidar watch），
改完立即生效，无需重启 DSH 或桥接。

实测（独立会话，只读，不发 QQ 消息）：让 agent 调
`mcp__snowluma__qq_get_message_images` 读群里一张真实图片 → 工具结果里出现
`{"type":"image","attachment":{...,"mediaType":"image/jpeg","width":230,"height":265}}`，
模型正确描述出画面内容（Q 版蓝发女仆装少女）。

### 备用：智谱免费视觉模型（已装，非默认）

同时在 `llm-pi-ai` 里注册了 `zhipu` 路由（免费额度，作为不想烧 opi 额度时的备选）：

- baseURL `https://open.bigmodel.cn/api/paas/v4`，api `openai-completions`，
  密钥存在 DSH 凭据库的 `ZHIPU_API_KEY`（`.credentials.yaml`，不落 settings）
- `glm-4.6v-flash`：contextWindow 131072（实测 130806 tokens 通过、150000 报 1261
  `Prompt exceeds max length`），maxTokens 16384，`input: [text, image]`
- `glm-4v-flash`：轻量老款，8192 / 1024，也支持图片
- **模型 id 必须小写**：文档上写的 `GLM-4.6V-Flash` 发过去会稳定返回 1305
  「访问量过大」，`glm-4.6v-flash` 才正常 —— 大小写不同被当成不同模型路由
- `reasoning: high`（provider 级默认）。pi-ai 检测到 `open.bigmodel.cn` 会自动走
  `thinkingFormat: "zai"`：不传 reasoning 时发 `thinking:{type:"disabled"}`，
  传了才 `enabled`。**必须开着**：关掉思考时智谱把推理过程混进 `content`
  并吐出裸 `</think>`，DSH 只看到一个 text block；开着则走 `reasoning_content`
  独立通道，DSH 正确分成 reasoning + text 两个 block

已知短板（所以没设成 QQ 默认模型）：

- 免费额度限流很紧，1305 / 1302 频繁；DSH 的 llm-retry 会按 RATE_LIMIT 重试
  （实测一次提问重试 3 次才通），QQ 场景响应会明显变慢
- 工具调用不稳：MCP 实测里它出现过把 tool call 写成裸
  `<arg_key>/<arg_value>` 文本而非结构化 tool_calls，对二代仿真（全靠工具收发）
  是硬伤。要用它当 QQ 大脑得先解决这点

切换方式：改 `config.json` 的 `dsh.provider` = `zhipu`、`dsh.model` = `glm-4.6v-flash`
（`reasoningEffort` 可留空或填 `high`），然后 `bash restart.sh`。
- `snowluma.launcherPath` / `homeDir` 留空，`allowProcessControl` = `false`（Android 上无法进程控制）
- `ownerQQ` = `null`，`allow.private` / `allow.groups` = `[]`，`allowAllWhenEmpty` = `false`
  → 当前是**全部拒绝**的安全默认值。接 QQ 前必须填白名单和 ownerQQ，否则桥接不会处理任何会话。

## 与 dsh-mcp-lazy 的互锁（重要，本机特有）

本机装了 `@yilinxiao/dsh-mcp-lazy`（MCP 懒加载）。它默认接管所有兼容 MCP，把
`mcp__snowluma__*` 等工具的说明从工具面隐藏，只留一个路由工具
`mcp__router__search_and_activate`，需要时按会话披露。

而上游的 `qq-tool-restrict.mjs` 执行期白名单只放行三个 `mcp__snowluma*` / 
`mcp__web-search-safe__` 前缀 + `ask_user_question` / `todo_write`。两者叠加会互锁：
QQ agent 看不到 QQ 工具，又因为路由工具不在白名单里而无法把它们唤出来 —— 整个桥接失能。
实测确认过这个死锁（agent 报 `工具 "mcp__router__search_and_activate" 不在 QQ 桥接白名单内`）。

修复：两个 preset 的 `qq-tool-restrict.mjs` 的 `SAFE_EXACT` 加入
`mcp__router__search_and_activate`。它只做「显示某个 MCP 的工具」，不执行外部动作，
被唤出的工具仍要逐个过前缀白名单，所以安全边界没有放松。

> 如果哪天卸载了 dsh-mcp-lazy，这条放行是无害的死条目，不用删。
> 反之如果想彻底关掉懒加载（让 QQ 工具常驻工具面），按 mcp-lazy README 在
> `cordis.patch.yml` 里加 `- id: mcp-lazy-manager` + `disabled: true`。

## 顺带修的 DSH profile 问题：plugin-market 重复挂载

`dsh-plugin-market` 的 package.json 声明了 `dsh.bundle.patch`，只要它还在 profile
`package.json` 的 dependencies 里，宿主 `dsh plugin install` 的 `reconcilePlugins()`
就会把它自动写回 `dsh.profile.bundles`（见 `dsh/lib/plugin-*.js`）——所以「从 bundles 里删掉」
是治不住的，跑一次 setup-dsh（内部会调 `dsh plugin install`）它就回来了，本次实测复现过一轮。

真正的根因是 profile 的 `cordis.patch.yml` 里还有一条**自己写的** `- insert: [id: plugin-market]`，
与它自带 bundle patch 的同 id insert 撞车 → `duplicate loader entry id: plugin-market` 启动失败。
（这个冲突装 qq-bridge 之前就存在，不是本次引入。）

已改成：保留 bundles 里的 `dsh-plugin-market`（顺应宿主行为），把 profile patch 里那条
insert 改写成**同 id 的非 insert 覆盖补丁**——bundle patch 先生效，本文件后生效，
正好用来覆盖插件自带的作者机器默认值（`pnpmCommand: npx pnpm@11.7.0`、
`proxyUrl: http://127.0.0.1:7897`）。dump-config 已验证 160 个 id 无重复、
composed 树里 market 的 config 是本机值。

## 已验证 / 未验证

已验证：
- `npm install` 通过（126 包，postinstall 修补 @snowluma/sdk 13 个文件）
- 三个 MCP server stdio 直连握手 + `tools/list`：snowluma 37 个工具、web-search-safe 3 个、snowluma-host 2 个
- `dsh --profile web --dump-config` 退出码 0、stderr 空、160 个 id 无重复
- 3081 端口第二实例真实启动 HTTP 200，日志 `[qq-mode-console] active (namespace=qq-mode)`
- 主引擎（3080）`settings.describe` 有 `qq-mode` 命名空间（mode=reserved2、四模式齐全、applies=live、writable）
- 主引擎 `agentPreset.list` 能看到 `qq-chat` / `qq-chat-v2`（trust=user）
- `npm run self-test`：DSH 侧链路通，agent 回复正常
- `node scripts/verify-install.mjs`：一键自检（命名空间 + preset + 工具面 + dev_* 隐藏）
- **端到端工具面实测**：qq-chat-v2 会话里 router 放行成功 → 披露 35 个 `mcp__snowluma__*` 工具 →
  真实调用只读工具 `qq_status` 通过白名单并执行（因无 SnowLuma 后端返回 fetch failed，
  这正是预期结果：说明链路打到了最后一环）；`dev_*` 开发工具全部不可见

未验证（缺 SnowLuma，非本机能补齐）：
- QQ 收发消息全链路。SnowLuma 是 Windows 客户端（`launcher.bat` + 完整版自带 Node 运行时），
  本机 Android 上跑不了。要真正接 QQ，需要在一台 Windows 机器（或同网段设备）上跑 SnowLuma，
  然后把 `config.json` 的 `snowluma.wsUrl` / `httpUrl` 改成那台机器的地址，
  例如 `ws://192.168.1.x:3001` / `http://192.168.1.x:3000`，并在 SnowLuma WebUI 里
  同时开 WebSocket 服务端与 HTTP API、两端 accessToken 设为相同值。
- 当前直接 `node src/bridge.js` 会因为连不上 `ws://127.0.0.1:3001` 而启动失败退出，这是预期行为。

## 启动顺序（真正接 QQ 时）

1. 确认 DSH（3080）在跑，且已重启过一次让 preset/MCP 生效。
2. 在 Windows 侧启动 SnowLuma，记下 OneBot 的 WS 端口、HTTP 端口、accessToken。
3. 编辑 `config.json`：`snowluma.*` 三项、`ownerQQ`、`allow.private` / `allow.groups`。
4. `bash restart.sh`（或 `nohup setsid bash start.sh >/dev/null 2>&1 &`）。
5. 看 `state/bridge.log` 出现 `SnowLuma 已连接` 即成功；控制台 `http://127.0.0.1:3100`
   （令牌在启动日志里，或 `state/console-token`）。
