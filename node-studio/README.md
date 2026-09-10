# 大肥鱼行为节点（离线原型）

> 新手入口：[Windows 安装与启动](docs/01-Windows新手安装与启动.md) · [五分钟节点编辑](docs/02-五分钟节点编辑入门.md) · [源码构建与排错](docs/03-源码构建与故障排查.md) · [更新记录](CHANGELOG.md)

这是一个与现有机器人、AstrBot、QQ、Docker、Windows 控制台 **完全分离** 的实验项目，用来验证类似 Blender 几何节点的可视化机器人行为编排。

当前版本完成：

- **Windows 成品**：在仓库 Releases 下载 `dafeiyu-node-studio.exe` 及其 SHA-256；
- 类型化端口和节点注册表；
- DAG、端口、类型、必需输入和重复连线校验；
- 按拓扑顺序执行并记录每个节点的输入、输出、耗时和错误；
- 12 个基础节点；
- 3 张离线示例图；
- 本地浏览器节点画布：拖动节点、检查端口/配置、检查图、离线回放、查看轨迹；
- Python 标准库实现，无需 `pip install`，兼容 Python 3.8；
- 不含网络动作节点、文件读写节点、Shell 节点或任意代码执行能力。

## 安全边界

该原型：

- 不读取或修改任何现有机器人部署目录；
- 不连接 QQ、AstrBot、OneBot、Docker、服务器或模型接口；
- 不读取原机器人的数据库和插件状态；
- `FakeLLM` 只把脱敏输入拼成固定演示回复；
- `PreviewOutput` 的 `sent` 永远是 `false`；
- Studio 默认只绑定 `127.0.0.1`，变更请求要求同源 Origin、会话 Cookie 与 CSRF 令牌；
- 不使用 `eval`、`exec`、动态导入或 shell；
- 示例只使用 `group-demo`、`member-a`、`owner-demo` 等占位符。

## 运行 Studio

```bash
git clone https://github.com/nanbengxian-cyber/dafeiyu-qq-bot.git
cd dafeiyu-qq-bot/node-studio
bash scripts/start-studio.sh
```

浏览器打开：

```text
http://127.0.0.1:8765
```

可切换三张图：

1. `基础聊天`：最小安全回复管线；
2. `主动插话（离线）`：关闭“必须点名”，加模拟记忆和关系上下文；
3. `图片理解后回复（离线）`：把脱敏图片描述作为输入，验证上下文合并。

本版画布编辑是**临时演示**：刷新页面、切换示例或退出程序会丢失修改；没有承诺保存用户图，也不会尝试写入 PyInstaller 的临时解包目录。后续如增加持久化，应写入用户目录并经过后端校验与原子替换。

## Windows EXE 打包

Windows 10/11 上安装 Python 3.8 后，在 PowerShell 中运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\packaging\build-windows.ps1
```

脚本会创建隔离的 `.build-venv`，安装锁定版本 `pyinstaller==6.11.1`，先运行编译检查和全部测试，再生成：

```text
dist\dafeiyu-node-studio.exe
dist\dafeiyu-node-studio.exe.sha256
```

也可以在仓库的 Actions 页面手动运行 `.github/workflows/build-node-studio-windows.yml`。工作流只生成 Artifact；正式 Release 由维护者在验证后发布。Linux 上的 PyInstaller 不能生成可信的 Windows PE，因此不要把 Linux 构建物冒充 Windows EXE。

EXE 启动后仅监听 `127.0.0.1:8765` 并打开系统浏览器。端口占用时可运行：

```powershell
.\dafeiyu-node-studio.exe --port 0
```

## 源码归档

运行 `python3 scripts/make-release.py` 会生成不含缓存、虚拟环境、构建目录和凭据的源码 ZIP，以及 SHA-256 校验文件。归档默认放在 `release/`，不会上传。

## 命令行运行

```bash
python3 -m dafeiyu_flow.cli graphs/01-basic-chat.json
python3 -m dafeiyu_flow.cli graphs/02-proactive-chat.json --validate
```

## 运行测试

```bash
python3 -m compileall -q dafeiyu_flow tests
python3 -m unittest discover -s tests -v
```

## 12 个节点

| 类型 | 节点 | 输入 → 输出 |
|---|---|---|
| 输入 | 消息输入 | 配置 → `MessageEvent` |
| 输入 | 消息规范化 | `MessageEvent` → `NormalizedMessage` |
| 判断 | 是否群消息 | 消息 → `Decision` |
| 判断 | 权限检查 | 消息 + 判断 → 新判断 |
| 判断 | 是否应该回复 | 消息 + 判断 → 新判断 |
| 上下文 | 群员记忆（模拟） | 消息 + 判断 → `ContextPatch` |
| 上下文 | 社交关系（模拟） | 消息 + 判断 → `ContextPatch` |
| 编排 | 上下文合并 | 消息 + 多个上下文 → `PromptPlan` |
| 模型 | 假模型（离线） | `PromptPlan` → `ModelResponse` |
| 安全 | 出口审核 | 回复 → `ApprovedSendPlan` |
| 输出 | 人味处理 | 审核后的计划 → 审核后的计划 |
| 输出 | 预览输出 | 计划 → `PreviewResult(sent=false)` |

## 图格式

节点：

```json
{
  "id": "moderate",
  "type": "safety.output_moderation",
  "position": {"x": 1580, "y": 160},
  "config": {"blocked_words": ["show-token"]}
}
```

连线：

```json
{
  "id": "e10",
  "from": {"node": "llm", "port": "response"},
  "to": {"node": "moderate", "port": "response"}
}
```

图加载时会检查：

- 节点类型和端口存在；
- 两端数据类型完全相同；
- 单值输入不能重复连接；
- 所有必需输入都已连接；
- 图中不存在环；
- 节点数量不超过 200、边不超过 1000。

## 当前限制

这仍是第一版架构验证，不是生产替代品：

- 画布支持拖动、从节点库添加节点、配置编辑、创建兼容连线、删除节点/连线、运行和回放；
- 条件分支当前通过类型化 `Decision` 数据向下传播，尚未实现控制端口和“未选分支跳过”；
- 调度器目前按确定性拓扑顺序同步运行，尚未加入异步并发、超时和取消；
- 运行记录只在 Studio 进程内存中保留最近 100 条，重启即清空；
- 模拟记忆与关系来自图配置，不读取真实数据库；
- 没有 AstrBot 兼容层，故不会影响正在运行的机器人。

下一阶段建议先补“控制端口/显式分支/跳过语义”，再增加浏览器连线编辑；不要先接生产。
