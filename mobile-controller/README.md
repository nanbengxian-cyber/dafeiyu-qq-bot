# 手机控制台 App · 开放式登录 + 部署 / Mobile Console (open login + deploy)

手机上的大肥鱼控制台第一版：**开放式登录**（谁的服务器都能连，没有任何预置配置）+ 桌面控制台的三步部署流程。包名 `com.dafeiyu.controller`，安装后叫「大肥鱼控制台」。

An Android console app, v1: **open login** — you bring your own server and NapCat WebUI token, nothing about anyone's server is baked in — plus the desktop controller's three-step deploy flow. Package `com.dafeiyu.controller`.

## 它做什么 / What it does

**「登录 QQ」页（开放式登录）**

- 连接任意一台你自己服务器的 NapCat WebUI：填 `地址:端口` + WebUI Token（可选两步验证动态码）；
- 填的 QQ 号还兼**防呆校验**：登录后与服务器上真正登录的号比对，不一致红字提醒。
- **扫码登录**：WebUI 返回的二维码内容在手机本地画成码，手机 QQ 直接扫；
- **密码登录**：填 QQ 号 + QQ 密码（MD5 后走 NapCat 的 `PasswordLogin`），要求腾讯安全验证时切到**内置网页登录页**完成；
- **快速登录**：服务器上登录过的号一键再登（等价于 compose 里的 `ACCOUNT` 快速登录）；
- 机器人状态每 2.5 秒自动刷新；在线/掉线/出码中一目了然。

**「控制台」页（与桌面控制台同一套流程）**

- 填服务器 SSH → **测试连接**（只读预检）→ **开始部署**（下载源码 → 生成 compose → 拉镜像 → 启动）；
- 运行状态：两个容器的运行情况一键刷新；
- 配置旋钮：由仓库里的 `deploy/console-config.json` 下发，行级写回 `deploy/robot.env`（保留注释与顺序），需要重启的项自动重建容器；
- **⑤ 聊天范围 + 主聊天 API（都写进你的服务器）**：
  - 群号、私聊 QQ 号 —— 机器人**只在这些会话里说话**，别处一律不理
    （写进 AstrBot 的 `platform_settings.enable_id_white_list` + `id_whitelist`；
    私聊条目按官方格式写成 `<平台id>:FriendMessage:<QQ号>`）；
  - 主聊天 API：接口地址 + API Key + 模型名，**用你自己的** ——
    App 不带任何 Key，你不填机器人就没法回答；
  - API Key 不进命令行、只在 SFTP 传的 600 文件里出现、读完即删；
    App 侧**不保存、不回显**（和 SSH 密码同一待遇）；
  - 写完重新读一遍配置做**回读校验**，对不上就报错；改前自动备份，改完重启 AstrBot 生效。

**QQ 号防呆**：登录页填的 QQ 号会与服务器上**真正登录**的号比对，不一致就红字提醒 ——
登录错号从二维码上根本看不出来。

**「教程」页**：从准备服务器到登录成功的完整新手路径。

## 目录 / Layout

```
app/           安卓源码（Java，无 Gradle，无第三方依赖——除两个明确引入的库）
app/src/io/nayuki/qrcodegen/   二维码生成（Nayuki QR-Code-generator，MIT）
lib/jsch-0.2.17.jar            SSH2 客户端（mwiede/jsch，Revised BSD）
test/          纯 JVM 单测（不需要模拟器）
build.sh       七步构建：aapt2 → javac → d8 → zip → zipalign → apksigner
```

## 构建 / Build

```bash
# 一枚签名密钥（仓库里不含任何密钥，生成一次后别提交）
keytool -genkeypair -keystore release.jks -storetype JKS \
  -keyalg RSA -keysize 2048 -validity 10000 -alias release \
  -dname "CN=controller" -storepass yourpass -keypass yourpass

# 构建（默认找 /opt/android-sdk；没有就设 ANDROID_BUILD_TOOLS / ANDROID_JAR）
KEYSTORE=$PWD/release.jks KS_PASS=yourpass bash build.sh
```

## 测试 / Test

```bash
bash test/run-tests.sh    # 纯 JVM，秒级，不需要模拟器
```

覆盖（264 项）：NapCat WebUI 协议（登录换凭据、凭据过期自动重登、密码登录分支、快速登录回退）、
远程部署脚本生成与脱敏、配置旋钮解析与行级写回、TOTP 的 RFC 6238 测试向量、JSON 解析，
以及聊天范围 / 主聊天 API 的写入 —— 最后一项会**真的用 python3 跑一遍生成的脚本**
改一份临时 `cmd_config.json`，并带变异验证（把落盘内容改坏，回读校验必须报错）。
Activity/View 类刻意不进测试面（见 run-tests.sh 里的反向检查）。

## 私有定制版 / Private preset build

想让 App **默认就连你这台服务器**（用户只需填三配置：API、群/私聊号、人格），用定制版构建：

```bash
bash build-preset.sh --host <服务器> --ssh-port <端口> \
                    --ssh-user <受限账号> --key-file <RSA私钥路径> \
                    --fingerprint SHA256:... --token <管理口令>
# 产物：build/dafeiyu-controller-mine.apk
```

### ⚠ 发放口径：只私下给，绝不上公开 Release

内置服务器信息的 APK **等同于一把能管服务器的钥匙**（拿到的人就能管理实例）。
因此本项目的规定是：

- **公开仓库只放源码**，不放任何 APK 产物；
- 定制版 APK 一律**私下发放**（直接发文件给信得过的人），不走 GitHub Release；
- 公开版（`build.sh`，无预设）只作源码可复现的构建产物，同样不必上传。

> 背景：早期版本曾把 APK 发到公开 Release（`mobile-v1.0.0/1.0.1/1.0.2`）。
> 事后核查过这三个包**不含**任何服务器地址、账号、私钥或口令，
> 但「把可执行产物挂在公开渠道」这件事本身就是不该养成的习惯 —— 以后不再这么做。

- 内置账号在服务器上被限制成**只能转发到管理端口**：登不了 shell、连不了别的端口，
  所以即使 APK 泄露，损失面也只限于机器人管理，不涉及整台服务器。
- 构建脚本把「注入 → 构建 → 还原」做成原子流程（还原在 `trap` 里），
  公开仓库永远只有空模板；`build.sh` 另有一道闸：公开版里出现真实域名/定制版文案就拒绝产出。
- 公开版（`build.sh`）与定制版产物**路径不同**，互不覆盖。
- 密钥必须是 **RSA**：JSch 的 ed25519 实现要 Java 15+，安卓的 Ed25519 要 API 33+，
  用 ed25519 会一直 Auth fail（脚本会提前拦住）。

## 安全设计 / Security

- **App 里不存任何密钥。** SSH 密码、WebUI Token、QQ 密码、TOTP 动态码、
  **主聊天 API Key** 全部只进内存；保存的只有地址类非敏感字段
  （白名单在 `DeployConfig.PERSIST_KEYS`，聊天范围/接口地址/模型名在其中，API Key 不在）。
- **没有任何预置配置。** 不含任何人的服务器地址、端口、Token、路径、群号、QQ 号、API Key；
  仓库地址默认值是公开的源码镜像，用户可改。
- **密码只以 MD5 过网线**（NapCat `PasswordLogin` 协议要求），请求体里没有明文密码（有测试钉死）。
- **部署日志全部脱敏**后上屏（URL userinfo / token=... / 长度≥6 的敏感值）。
- **API Key 不进命令行**：只写在 SFTP 传上去的 600 权限文件里，脚本读完即删；
  写完**回读校验**（重新读文件核对），对不上就报错；改前自动备份原配置。
- **SSH 主机指纹**：首次连接需明确勾选接受；此后逐次校验，不一致直接拒绝。
- **只管理带 `.dafeiyu-managed` 标记的目录**，绝不会覆盖用户已有的部署。
- **权限只要两个**：联网、读网络状态。不读通讯录、不读存储、不要位置。
- **⚠️ 与 WebUI 之间是明文 HTTP**（NapCat 默认如此），SSH 本身是加密的。
  公共 Wi-Fi 下请意识到这一点。

## 协议依据 / Protocol notes

NapCat WebUI 协议按官方源码 `NapNeko/NapCatQQ` 的
`napcat-webui-backend`（`api/QQLogin.ts`、`api/Auth.ts`、`helper/SignToken.ts`）核对：

- 登录：`POST /api/auth/login`，body 为 `{"hash": sha256hex(token + ".napcat")}`，
  返回 `data.Credential`（1 小时有效），之后所有请求带 `Authorization: Bearer <Credential>`；
  响应外壳 `{"code","message","data"}`，`code != 0` 即失败，`message == "Unauthorized"`
  触发一次自动重登。
- 状态：`POST /api/QQLogin/CheckLoginStatus` → `isLogin/isOffline/loginPhase/qrcodeurl/loginError`；
  `qrcodeurl` 是**二维码内容**，本地渲染（官方网页前端同样做法）。
- 密码登录：`POST /api/QQLogin/PasswordLogin`，body `{"uin","passwordMd5"}`；
  需要验证码 / 新设备验证时返回 `needCaptcha` / `needNewDevice` —— 这两步是腾讯网页组件，
  App 提示用户切到内置网页登录页完成。
- 快速登录：`GetQuickLoginListNew`（新版，缺接口时回退 `GetQuickLoginList`）+ `SetQuickLogin`。

旧版 NapCat 若没有 `PasswordLogin` 接口，会收到 404 → App 明确提示「该版本不支持密码登录」，
扫码与快速登录不受影响。
