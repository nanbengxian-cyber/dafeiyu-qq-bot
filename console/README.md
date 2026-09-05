# 安卓控制台 App / Android Console App

手机上装一个 App，看机器人在不在线、改说话频率、一键切换预设模式。包名 `com.dafeiyu.console`。

An Android app to check whether the bot is online, tune its chattiness, and switch preset modes with one tap. Package `com.dafeiyu.console`.

**界面是服务端下发的，不是写死的。** App 按 `/api/console/schema` 返回的能力清单渲染，
认不出的字段跳过而不是崩。所以在服务器上给 `console_spec.py` 添旋钮、模式、动作或插件中文名，
手机下拉刷新就能看到，**不用重新打包安装**；只有新增一种渲染方式（新的控件类型）才需要更新 APK。
当前清单：**104 个旋钮 / 22 个插件 / 7 个模式 / 3 个动作**。

**The UI is server-driven, not hard-coded.** The app renders whatever `/api/console/schema`
returns and skips fields it does not understand instead of crashing. Adding knobs, modes, actions,
or plugin labels to `console_spec.py` on the server shows up after a pull-to-refresh — **no reinstall**.
Only a genuinely new widget type requires a new APK. Current surface: **104 knobs, 22 plugins,
7 modes, 3 actions**.

完整使用说明见 [`../docs/20-控制台App说明.md`](../docs/20-控制台App说明.md)。

Full usage guide: [`../docs/20-控制台App说明.md`](../docs/20-控制台App说明.md).

## 目录 / Layout

```
app/          安卓源码（9 个 Java 文件，无第三方依赖）
app/res/      资源；图标由 app/mkicon.py 脚本生成（没有 PIL 也能跑）
server/       服务端后台：console_api.py（逻辑）+ console_spec.py（能力清单）
              envfile.py 行级读写插件 env 文件（保留注释、顺序、权限）
              patch_qrweb.py 把控制台挂到已有的扫码页服务上
test/         391 项纯 JVM 单测，不需要模拟器
build.sh      不用 Gradle 的七步构建
deploy.sh     部署到服务器 + 实测验证 + 回滚
```

## 构建 / Build

不需要 Gradle、Android Studio、sdkmanager。只要 build-tools 里的 `aapt2` / `d8` / `zipalign` / `apksigner`，加上 JDK 和 `zip`。

No Gradle, Android Studio, or sdkmanager needed — just `aapt2` / `d8` / `zipalign` / `apksigner` from build-tools, plus a JDK and `zip`.

```bash
# 先准备一枚签名密钥（仓库里不含任何密钥）
keytool -genkeypair -keystore release.jks -storetype JKS \
  -keyalg RSA -keysize 2048 -validity 10000 -alias release \
  -dname "CN=console" -storepass yourpass -keypass yourpass

# 构建
KEYSTORE=$PWD/release.jks KS_PASS=yourpass bash build.sh

# 也可以改 build.sh 顶部的 TOOLS 指向你的 build-tools 位置
```

`build.sh` 把 Gradle 平时代劳的七步显式写出来：aapt2 compile → aapt2 link → javac → d8 → zip -0 塞 dex → zipalign → apksigner。顺序不能反，**zipalign 必须在签名之前** —— 先签再对齐会破坏 v2 签名块。

`build.sh` spells out the seven steps Gradle normally hides. The order matters: **zipalign must run before signing**, or the v2 signature block breaks.

## 测试 / Test

```bash
bash test/run-tests.sh              # 391 项，纯 JVM，秒级
python3 server/test_console.py      # 105 项后端单测（假 dashboard，不碰真服务器）
python3 server/test_qrweb_e2e.py    # 83 项端到端（真起 HTTP 服务，含「后端崩了扫码页照旧」）
```

覆盖 JSON 解析、地址归一化、状态格式化、旋钮模型、UI 摘要逻辑。Android 框架调用被隔离在 Activity 里，所以纯逻辑部分可以直接在 JVM 上测。

Covers JSON parsing, address normalization, status formatting, the knob model, and UI summary logic. Android framework calls are confined to the Activities, so the pure logic is JVM-testable.

## 安全设计 / Security

- **App 里没有任何密钥。** 不含 SSH 私钥，也不含模型 API key。所有配置改动都是 App 请求服务器、服务器自己去调 AstrBot 后台完成的。
- **不存密码。** 只存服务器地址和服务器签发的登录凭据（7 天失效）。
- **可改项是白名单定死的。** 只有 `console_spec.py` 里显式列出的 104 项可改；密码、管理员列表、API key、平台账号配置一律由服务端拒绝并返回 403。
- **敏感变量只报「已设置 / 未设置」。** 7 个 API key / 音色 ID 的真实值一个字节都不出服务器，手机端也改不了。
- **插件 env 写入是行级的。** 保留全部注释与顺序、保持原文件权限、原子替换、写前核对 MD5；布尔统一写 `1`/`0`（有 11 个变量用 `!= "0"` 判定，写 `false` 会被当成开），数值越界直接 400 而不静默钳制（除一个插件外，非法值会让插件 import 失败）。
- **权限只要两个**：联网、读网络状态。
- **⚠️ 服务端是明文 HTTP，没有 TLS。** 密码在网络上不加密传输。这是端口可用性限制（只有一个端口能放行，且没有域名证书），不是 App 引入的。公共 Wi-Fi 下请意识到这一点。
- **写配置前会核对文件指纹**，对不上就拒绝，防止覆盖掉别人刚在网页后台保存的改动。

---

- **No credentials in the app.** No SSH keys, no model API keys. All config changes go App → server → AstrBot's own admin API.
- **No stored password.** Only the server address and a server-issued 7-day credential.
- **Editable fields are a hard-coded allowlist.** Only the 104 entries listed in `console_spec.py`; passwords, admin lists, API keys, and platform account config are rejected server-side with 403.
- **Secret variables report presence only.** The real values of the 7 API keys / voice IDs never leave the server and cannot be edited from the phone.
- **Plugin env writes are line-level.** Comments, ordering, and file permissions are preserved; writes are atomic and guarded by an MD5 check. Booleans are always written as `1`/`0` (11 variables test `!= "0"`, so `false` would read as *on*), and out-of-range numbers return 400 rather than being silently clamped (an invalid value makes the plugin fail to import).
- **Two permissions only**: internet, network state.
- **⚠️ The backend is plaintext HTTP with no TLS.** The password travels unencrypted. This stems from port availability (a single openable port, no domain certificate), not from the app. Be aware on public Wi-Fi.
- **Config writes verify a file fingerprint** and refuse on mismatch, so they can't clobber a change someone just saved in the web admin.
