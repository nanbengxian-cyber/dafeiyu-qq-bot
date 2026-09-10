# Windows 新手安装与启动

## 1. 下载什么

打开仓库右侧 **Releases**，进入 `v0.1.0`，下载：

- `dafeiyu-node-studio.exe`：程序本体；
- `dafeiyu-node-studio.exe.sha256`：校验文件；
- `dafeiyu-node-lab-source-v0.1.0.zip`：留存源码，不运行程序时可以不下载。

不要从不明网盘下载同名程序。

## 2. 第一次启动

双击 `dafeiyu-node-studio.exe`。程序会打开一个控制台窗口，并在默认浏览器显示节点编辑器。

- 控制台窗口必须保持打开；关闭它即停止 Studio。
- 页面地址通常是 `http://127.0.0.1:8765`。
- 若 8765 被占用，程序会自动选择另一个本机端口，并在控制台显示实际地址。
- Windows 安全中心首次提示时，可选择保留；本程序只监听本机回环地址，不要求放行公网防火墙。

若浏览器没有自动打开，请复制控制台中 `Studio 已启动` 后的地址到浏览器。

## 3. 安全校验（推荐）

在 EXE 所在目录打开 PowerShell：

```powershell
(Get-FileHash .\dafeiyu-node-studio.exe -Algorithm SHA256).Hash.ToLower()
Get-Content .\dafeiyu-node-studio.exe.sha256
```

两处 64 位哈希应完全相同。

## 4. 常见问题

### 双击后窗口一闪而过
在文件夹地址栏输入 `powershell`，然后运行：

```powershell
.\dafeiyu-node-studio.exe --no-browser
```

控制台会保留具体报错。

### 页面打不开
确认控制台仍在运行，并使用控制台显示的实际地址。不要把 `127.0.0.1` 改成服务器地址。

### 修改为什么不见了
v0.1.0 是架构验证版，画布修改只存在当前页面内存中。刷新、切换示例或退出都会丢失，这是已知限制。

### 这会改动现有机器人吗
不会。此版本没有 QQ、AstrBot、Docker、SSH、数据库、模型接口或文件写入集成。
