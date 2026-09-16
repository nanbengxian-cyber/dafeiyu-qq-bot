package com.dafeiyu.controller;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 部署编排 —— 桌面控制台 deployer.py 的移植。
 *
 * 传输层抽象成 {@link Transport}（安卓上由 JSch 实现，测试用假传输）；
 * 远程脚本由本地生成、用户输入一律 shell quote；部署目录必须带
 * .dafeiyu-managed 专用标记，绝不覆盖用户自己的目录。日志全部脱敏。
 */
public final class Deployer {

    public static final String APP_MARKER = ".dafeiyu-managed";
    public static final String ENV_REL = "deploy/robot.env";
    public static final String ENV_EXAMPLE_REL = "deploy/robot.env.example";
    public static final String SCHEMA_REL = "deploy/console-config.json";
    public static final String COMPOSE_NAME = "docker-compose.generated.yml";

    /** 业务异常：消息已脱敏，可直接展示。 */
    public static class DeployException extends Exception {
        public DeployException(String msg) {
            super(msg);
        }
    }

    // ------------------------------------------------------------ 传输接口

    /** 远程执行与文件读写的最小接口。 */
    public interface Transport {
        RunResult run(String command, int timeoutMs, LineSink onLine);
        void putText(String path, String text) throws DeployException;
        String getText(String path, int limit) throws DeployException;
        void close();
    }

    public interface LineSink {
        void line(String s);
    }

    public static final class RunResult {
        public final int exit;
        public final String stdout;
        public final String stderr;

        public RunResult(int exit, String stdout, String stderr) {
            this.exit = exit;
            this.stdout = stdout == null ? "" : stdout;
            this.stderr = stderr == null ? "" : stderr;
        }
    }

    // ------------------------------------------------------------ 脱敏

    private static final Pattern URL_USERINFO =
            Pattern.compile("(https?://)[^/@\\s]+@", Pattern.CASE_INSENSITIVE);
    private static final Pattern SECRET_KV = Pattern.compile(
            "(?i)\\b(token|password|passphrase|secret|authorization|api[_-]?key)\\b\\s*[=:]\\s*\\S+");

    /** 日志兜底脱敏：固定模式 + 只替换足够长的敏感值（避免把普通数字打成乱码）。 */
    public static String redact(String text, String[] secrets) {
        if (text == null) {
            return "";
        }
        text = URL_USERINFO.matcher(text).replaceAll("$1<已隐藏>@");
        text = SECRET_KV.matcher(text).replaceAll("$1=<已隐藏>");
        if (secrets != null) {
            for (String secret : secrets) {
                if (secret != null && secret.length() >= 6) {
                    text = text.replace(secret, "<已隐藏>");
                }
            }
        }
        return text;
    }

    // ------------------------------------------------------------ 远程脚本

    /** shlex.quote 等价：单引号包起来，内部单引号按 '\''.join 拆。 */
    public static String shq(String value) {
        if (value == null) {
            return "''";
        }
        return "'" + value.replace("'", "'\\''") + "'";
    }

    /** 生成 compose 覆盖文件：不依赖仓库里是否已有 compose，端口与镜像可配。 */
    public static String buildComposeOverride(DeployConfig cfg) {
        return "services:\n"
                + "  napcat:\n"
                + "    image: " + cfg.napcatImage + "\n"
                + "    container_name: dafeiyu-napcat\n"
                + "    restart: unless-stopped\n"
                + "    ports:\n"
                + "      - \"" + cfg.bindAddress + ":" + cfg.napcatPort + ":3001\"\n"
                + "    volumes:\n"
                + "      - ./data/napcat:/app/config\n"
                + "    networks: [dafeiyu-net]\n"
                + "\n"
                + "  astrbot:\n"
                + "    image: " + cfg.astrbotImage + "\n"
                + "    container_name: dafeiyu-astrbot\n"
                + "    restart: unless-stopped\n"
                + "    env_file:\n"
                + "      - ./" + ENV_REL + "\n"
                + "    ports:\n"
                + "      - \"" + cfg.bindAddress + ":" + cfg.astrbotPort + ":6185\"\n"
                + "      - \"" + cfg.bindAddress + ":" + cfg.astrbotApiPort + ":6186\"\n"
                + "    volumes:\n"
                + "      - ./data/astrbot:/AstrBot/data\n"
                + "      - ./plugins:/AstrBot/data/plugins\n"
                + "    depends_on: [napcat]\n"
                + "    networks: [dafeiyu-net]\n"
                + "\n"
                + "networks:\n"
                + "  dafeiyu-net:\n"
                + "    driver: bridge\n";
    }

    private static final String SCRIPT_TEMPLATE = ""
            + "#!/bin/sh\n"
            + "set -eu\n"
            + "REPO=__REPO__\n"
            + "REF=__REF__\n"
            + "TARGET=__TARGET__\n"
            + "INSTALL=__INSTALL__\n"
            + "MARKER=__MARKER__\n"
            + "\n"
            + "log() { printf 'DSH_PROGRESS:%s\\n' \"$1\"; }\n"
            + "fail() { printf 'DSH_ERROR:%s\\n' \"$1\" >&2; exit 1; }\n"
            + "\n"
            + "case \"$TARGET\" in\n"
            + "  '~/'*) TARGET=\"$HOME/${TARGET#'~/'}\" ;;\n"
            + "  '~') TARGET=\"$HOME\" ;;\n"
            + "esac\n"
            + "\n"
            + "[ \"$(uname -s)\" = \"Linux\" ] || fail \"只支持 Linux 服务器\"\n"
            + "[ -n \"$TARGET\" ] || fail \"部署目录为空\"\n"
            + "case \"$TARGET\" in /|\"$HOME\"|.) fail \"不安全的部署目录\" ;; esac\n"
            + "\n"
            + "if [ \"$INSTALL\" = \"1\" ]; then\n"
            + "  if ! command -v git >/dev/null 2>&1 || ! command -v docker >/dev/null 2>&1; then\n"
            + "    log \"安装系统依赖\"\n"
            + "    if command -v apt-get >/dev/null 2>&1; then\n"
            + "      SUDO=\"\"; [ \"$(id -u)\" = \"0\" ] || SUDO=\"sudo\"\n"
            + "      $SUDO apt-get update -y >/dev/null\n"
            + "      $SUDO apt-get install -y git docker.io docker-compose-plugin >/dev/null\n"
            + "      $SUDO systemctl enable --now docker >/dev/null 2>&1 || true\n"
            + "    else\n"
            + "      fail \"缺少 Git/Docker，且系统不是 Debian/Ubuntu\"\n"
            + "    fi\n"
            + "  fi\n"
            + "fi\n"
            + "\n"
            + "command -v git >/dev/null 2>&1 || fail \"服务器未安装 Git\"\n"
            + "command -v docker >/dev/null 2>&1 || fail \"服务器未安装 Docker\"\n"
            + "DOCKER=docker\n"
            + "docker info >/dev/null 2>&1 || DOCKER=\"sudo docker\"\n"
            + "$DOCKER info >/dev/null 2>&1 || fail \"当前账号无权访问 Docker，请用 root 或加入 docker 组\"\n"
            + "$DOCKER compose version >/dev/null 2>&1 || fail \"缺少 Docker Compose 插件\"\n"
            + "\n"
            + "if [ -e \"$TARGET\" ] && [ ! -f \"$TARGET/$MARKER\" ]; then\n"
            + "  fail \"部署目录已存在且不是本控制台创建的，已拒绝覆盖\"\n"
            + "fi\n"
            + "if [ -f \"$TARGET/$MARKER\" ] && [ ! -d \"$TARGET/.git\" ]; then\n"
            + "  log \"重新下载源码\"\n"
            + "  rm -rf \"$TARGET\"\n"
            + "fi\n"
            + "\n"
            + "if [ ! -d \"$TARGET/.git\" ]; then\n"
            + "  log \"从仓库下载源码\"\n"
            + "  mkdir -p \"$(dirname \"$TARGET\")\"\n"
            + "  if ! git clone --branch \"$REF\" --single-branch \"$REPO\" \"$TARGET\"; then\n"
            + "    rm -rf \"$TARGET\"\n"
            + "    fail \"源码下载失败，请检查仓库地址、分支和服务器网络\"\n"
            + "  fi\n"
            + "else\n"
            + "  log \"更新仓库源码\"\n"
            + "  git -C \"$TARGET\" remote set-url origin \"$REPO\"\n"
            + "  git -C \"$TARGET\" fetch --depth=1 origin \"$REF\" || fail \"获取指定分支或标签失败\"\n"
            + "  git -C \"$TARGET\" checkout --detach FETCH_HEAD >/dev/null 2>&1 || fail \"切换代码版本失败\"\n"
            + "fi\n"
            + ": > \"$TARGET/$MARKER\"\n"
            + "\n"
            + "log \"准备目录与配置文件\"\n"
            + "mkdir -p \"$TARGET/data/napcat\" \"$TARGET/data/astrbot\" \"$TARGET/plugins\" \"$TARGET/deploy\"\n"
            + "if [ ! -f \"$TARGET/" + ENV_REL + "\" ]; then\n"
            + "  if [ -f \"$TARGET/" + ENV_EXAMPLE_REL + "\" ]; then\n"
            + "    cp \"$TARGET/" + ENV_EXAMPLE_REL + "\" \"$TARGET/" + ENV_REL + "\"\n"
            + "  else\n"
            + "    : > \"$TARGET/" + ENV_REL + "\"\n"
            + "  fi\n"
            + "fi\n"
            + "chmod 600 \"$TARGET/" + ENV_REL + "\" 2>/dev/null || true\n"
            + "\n"
            + "cat > \"$TARGET/__COMPOSE_NAME__\" <<'DSH_COMPOSE_EOF'\n"
            + "__COMPOSE__\n"
            + "DSH_COMPOSE_EOF\n"
            + "\n"
            + "cd \"$TARGET\"\n"
            + "log \"拉取容器镜像\"\n"
            + "$DOCKER compose -f __COMPOSE_NAME__ pull || fail \"镜像拉取失败，请检查服务器网络\"\n"
            + "log \"启动机器人服务\"\n"
            + "$DOCKER compose -f __COMPOSE_NAME__ up -d --remove-orphans || fail \"服务启动失败\"\n"
            + "log \"读取运行状态\"\n"
            + "$DOCKER compose -f __COMPOSE_NAME__ ps || true\n"
            + "log \"部署完成\"\n";

    /** 生成幂等远程脚本。用户输入全部 shell quote，compose 走 heredoc。 */
    public static String buildRemoteScript(DeployConfig cfg) throws DeployException {
        return buildRemoteScript(cfg, buildComposeOverride(cfg));
    }

    public static String buildRemoteScript(DeployConfig cfg, String composeText)
            throws DeployException {
        String compose = composeText == null ? "" : composeText.trim();
        if (compose.contains("DSH_COMPOSE_EOF")) {
            throw new DeployException("生成的 compose 内容包含保留标记，已中止。");
        }
        String script = SCRIPT_TEMPLATE;
        script = script.replace("__REPO__", shq(cfg.repoUrl));
        script = script.replace("__REF__", shq(cfg.repoRef));
        script = script.replace("__TARGET__", shq(cfg.deployDir));
        script = script.replace("__INSTALL__", cfg.installDeps ? "1" : "0");
        script = script.replace("__MARKER__", shq(APP_MARKER));
        script = script.replace("__COMPOSE_NAME__", shq(COMPOSE_NAME));
        script = script.replace("__COMPOSE__", compose);
        return script;
    }

    /** 在远程 shell 里解析部署目录（含 ~ 展开）的前缀。 */
    public static String targetPrefix(DeployConfig cfg) {
        return "TARGET=" + shq(cfg.deployDir)
                + "; case \"$TARGET\" in '~/'*) TARGET=\"$HOME/${TARGET#'~/'}\" ;; "
                + "'~') TARGET=\"$HOME\" ;; esac; ";
    }

    /** 构造一条在部署目录里执行的 docker compose 命令（自动处理 sudo）。 */
    public static String composeShell(DeployConfig cfg, String args) {
        return targetPrefix(cfg)
                + "cd \"$TARGET\" && { DOCKER=docker; docker info >/dev/null 2>&1 || DOCKER=\"sudo docker\"; "
                + "$DOCKER compose -f " + shq(COMPOSE_NAME) + " " + args + "; }";
    }

    public static final String PREFLIGHT_SCRIPT = ""
            + "echo \"OS=$(uname -s 2>/dev/null || echo unknown)\"\n"
            + "echo \"ARCH=$(uname -m 2>/dev/null || echo unknown)\"\n"
            + "echo \"GIT=$(command -v git >/dev/null 2>&1 && echo yes || echo no)\"\n"
            + "echo \"DOCKER=$(command -v docker >/dev/null 2>&1 && echo yes || echo no)\"\n"
            + "echo \"COMPOSE=$(docker compose version >/dev/null 2>&1 && echo yes || echo no)\"\n"
            + "if [ \"$(id -u)\" = \"0\" ]; then\n"
            + "  echo \"PRIVILEGE=root\"\n"
            + "elif sudo -n true 2>/dev/null; then\n"
            + "  echo \"PRIVILEGE=passwordless-sudo\"\n"
            + "else\n"
            + "  echo \"PRIVILEGE=limited\"\n"
            + "fi\n"
            + "echo \"HOME=$HOME\"\n"
            + "echo \"DISK_FREE_MB=$(df -Pm \"$HOME\" 2>/dev/null | awk 'NR==2{print $4}')\"\n";

    // ------------------------------------------------------------ 会话

    private final DeployConfig cfg;
    private final String[] secrets;
    private final TransportFactory factory;
    private Transport transport;
    private String remoteDir;
    private final LineSink progressSink;

    public interface TransportFactory {
        Transport open(DeployConfig cfg) throws DeployException;
    }

    public Deployer(DeployConfig cfg, LineSink progress, TransportFactory factory) {
        this.cfg = cfg;
        this.secrets = new String[]{cfg.password};
        this.progressSink = progress == null ? NOOP : progress;
        this.factory = factory;
    }

    private static final LineSink NOOP = new LineSink() {
        public void line(String s) {
        }
    };

    /** 进度回调（对应桌面版 session.progress(...)）。 */
    public void progress(String text) {
        progressSink.line(text);
    }

    public void open() throws DeployException {
        if (transport == null) {
            progressSink.line("正在连接服务器…");
            transport = factory.open(cfg);
            progressSink.line("SSH 连接成功");
        }
    }

    public void close() {
        if (transport != null) {
            transport.close();
            transport = null;
        }
    }

    private Transport require() throws DeployException {
        if (transport == null) {
            throw new DeployException("尚未连接服务器。");
        }
        return transport;
    }

    /** 部署目录的绝对路径。SFTP 不展开 ~，写文件前必须先解析成绝对路径。 */
    public String remoteDir() throws DeployException {
        if (remoteDir == null) {
            Transport t = require();
            RunResult r = t.run(targetPrefix(cfg) + "printf %s \"$TARGET\"", 60000, null);
            String value = r.stdout.trim();
            if (r.exit != 0 || !value.startsWith("/")) {
                throw new DeployException("无法确定服务器上的部署目录。");
            }
            remoteDir = value;
        }
        return remoteDir;
    }

    // ------------------------------------------------------------ 操作

    /** 执行部署。不关闭连接 —— 调用方通常部署后还要读配置与状态。 */
    public void deploy() throws DeployException {
        open();
        Transport t = require();
        String script;
        try {
            script = buildRemoteScript(cfg);
        } catch (DeployException e) {
            throw e;
        }
        RunResult r = t.run(script, 1800000, new LineSink() {
            public void line(String line) {
                if (line.startsWith("DSH_PROGRESS:")) {
                    progressSink.line(line.substring("DSH_PROGRESS:".length()));
                }
            }
        });
        if (r.exit != 0) {
            throw new DeployException(extractError(r.stdout, r.stderr));
        }
    }

    private String extractError(String out, String err) {
        String combined = out + "\n" + err;
        for (String line : combined.split("\n")) {
            if (line.startsWith("DSH_ERROR:")) {
                return trunc(redact(line.substring("DSH_ERROR:".length()), secrets), 300);
            }
        }
        String tail = null;
        for (String line : combined.split("\n")) {
            if (!line.trim().isEmpty() && !line.startsWith("DSH_PROGRESS:")) {
                tail = line;
            }
        }
        if (tail != null) {
            return trunc(redact(tail, secrets), 300);
        }
        return "自动部署失败，请检查服务器网络与权限。";
    }

    private static String trunc(String s, int n) {
        return s.length() <= n ? s : s.substring(0, n);
    }

    /** 只读预检：确认能登录、系统可用、Git/Docker 是否就绪。不改服务器任何东西。 */
    public Map<String, String> checkEnvironment() throws DeployException {
        Transport t = require();
        RunResult r = t.run(PREFLIGHT_SCRIPT, 120000, null);
        if (r.exit != 0) {
            throw new DeployException("环境检查失败：" + redact(r.stderr, secrets));
        }
        return parseKeyValues(r.stdout);
    }

    public List<Map<String, Object>> status() throws DeployException {
        Transport t = require();
        RunResult r = t.run(composeShell(cfg, "ps --format json"), 120000, null);
        if (r.exit != 0) {
            throw new DeployException("读取服务状态失败：" + redact(r.stderr, secrets));
        }
        return parseComposePs(r.stdout);
    }

    public Object readKnobSchema() throws DeployException {
        Transport t = require();
        RunResult r = t.run(targetPrefix(cfg) + "cat \"$TARGET/" + SCHEMA_REL
                + "\" 2>/dev/null || true", 60000, null);
        String text = r.stdout.trim();
        if (text.isEmpty()) {
            return null;
        }
        try {
            return Json.parse(text);
        } catch (Json.JsonError e) {
            throw new DeployException("仓库里的配置清单不是合法 JSON：" + SCHEMA_REL);
        }
    }

    public String readEnv() throws DeployException {
        Transport t = require();
        RunResult r = t.run(targetPrefix(cfg) + "cat \"$TARGET/" + ENV_REL
                + "\" 2>/dev/null || true", 60000, null);
        return r.stdout;
    }

    public void writeEnv(String text) throws DeployException {
        Transport t = require();
        String tmp = ENV_REL + ".tmp";
        t.putText(remoteDir() + "/" + tmp, text);
        RunResult r = t.run(targetPrefix(cfg) + "chmod 600 \"$TARGET/" + tmp
                + "\" && mv \"$TARGET/" + tmp + "\" \"$TARGET/" + ENV_REL + "\"", 60000, null);
        if (r.exit != 0) {
            throw new DeployException("保存远程配置失败：" + redact(r.stderr, secrets));
        }
    }

    public void recreate(String... services) throws DeployException {
        if (services.length == 0) {
            return;
        }
        StringBuilder names = new StringBuilder();
        for (String s : services) {
            if (names.length() > 0) {
                names.append(' ');
            }
            names.append(shq(s));
        }
        RunResult r = require().run(composeShell(cfg,
                "up -d --force-recreate --no-deps " + names), 600000, null);
        if (r.exit != 0) {
            throw new DeployException("重启服务失败：" + redact(r.stderr, secrets));
        }
    }

    // ------------------------------------------------------------ 聊天范围与主聊天 API

    /**
     * 把「聊天范围 + 主聊天 API」写进服务器上的 AstrBot 配置。
     *
     * 三条刻意的设计：
     * - **密钥不进命令行**：载荷（含 key）用 SFTP 传成 600 的文件，脚本读完即删；
     *   命令行里只有路径，服务器 ps/历史里看不到密钥。
     * - **只改对应字段**：脚本按 id 找 provider_sources/provider 条目做 upsert，
     *   不动别人的 provider；改前 copy 一份带时间戳的备份。
     * - **改完不自动重启**：由调用方决定要不要 recreate astrbot（白名单改动需重启生效）。
     *
     * @return 脚本回传的结果（changed 列表 + 备份名）
     */
    public Map<String, Object> applyChatSetup(ChatSetup.Request request) throws DeployException {
        open();
        Transport t = require();
        String dir = remoteDir();
        String scriptPath = dir + "/" + ChatSetup.SCRIPT_REL;
        String payloadPath = dir + "/" + ChatSetup.PAYLOAD_REL;

        progressSink.line("上传设置脚本");
        t.putText(scriptPath, ChatSetup.script());
        progressSink.line("上传本次要写的内容");
        t.putText(payloadPath, ChatSetup.payload(request));

        String cmd = "set -e; TARGET=" + shq(cfg.deployDir)
                + "; case \"$TARGET\" in '~/'*) TARGET=\"$HOME/${TARGET#'~/'}\" ;; "
                + "'~') TARGET=\"$HOME\" ;; esac; "
                + "chmod 600 " + shq(ChatSetup.PAYLOAD_REL) + " 2>/dev/null || true; "
                + "PY=python3; command -v python3 >/dev/null 2>&1 || PY=python; "
                + "cd \"$TARGET\" && $PY " + shq(ChatSetup.SCRIPT_REL)
                + " " + shq(ChatSetup.CFG_REL) + " " + shq(ChatSetup.PAYLOAD_REL)
                + "; RC=$?; rm -f " + shq(ChatSetup.PAYLOAD_REL)
                + "; exit $RC";

        RunResult r;
        try {
            r = t.run("cd " + shq(dir) + " && " + cmd, 300000, new LineSink() {
                public void line(String line) {
                    if (line.startsWith("DSH_PROGRESS:")) {
                        progressSink.line(line.substring("DSH_PROGRESS:".length()));
                    }
                }
            });
        } finally {
            // 载荷里有密钥：无论成败都不留在服务器上
            try {
                t.run("rm -f " + shq(payloadPath), 60000, null);
            } catch (RuntimeException ignored) {
                // 尽力而为
            }
        }
        if (r.exit != 0) {
            throw new DeployException(extractError(r.stdout, r.stderr));
        }
        try {
            return ChatSetup.parseResult(r.stdout);
        } catch (ChatSetup.SetupException e) {
            throw new DeployException(e.getMessage());
        }
    }

    // ------------------------------------------------------------ 解析与格式化

    public static Map<String, String> parseKeyValues(String text) {
        Map<String, String> out = new LinkedHashMap<String, String>();
        if (text == null) {
            return out;
        }
        for (String line : text.split("\n")) {
            int i = line.indexOf('=');
            if (i > 0) {
                out.put(line.substring(0, i).trim(), line.substring(i + 1).trim());
            }
        }
        return out;
    }

    /** 把预检结果整理成新手能看懂的一段话。 */
    public static String formatEnvironment(Map<String, String> info) {
        if (info == null || info.isEmpty()) {
            return "没有读取到服务器信息。";
        }
        StringBuilder sb = new StringBuilder();
        sb.append("系统：").append(or(info.get("OS"), "未知"))
                .append("（").append(or(info.get("ARCH"), "未知")).append("）\n");
        sb.append("Git：").append("yes".equals(info.get("GIT")) ? "已安装" : "未安装").append('\n');
        sb.append("Docker：").append("yes".equals(info.get("DOCKER")) ? "已安装" : "未安装").append('\n');
        sb.append("Compose：").append("yes".equals(info.get("COMPOSE")) ? "已安装" : "未安装").append('\n');
        String privilege = info.get("PRIVILEGE");
        if ("root".equals(privilege)) {
            sb.append("权限：root（可自动安装依赖）\n");
        } else if ("passwordless-sudo".equals(privilege)) {
            sb.append("权限：普通用户 + 免密 sudo（可自动安装依赖）\n");
        } else {
            sb.append("权限：普通用户（无法自动安装依赖，请先装好 Git/Docker）\n");
        }
        String disk = info.get("DISK_FREE_MB");
        if (disk != null && !disk.isEmpty()) {
            sb.append("可用磁盘：约 ").append(disk).append(" MB");
        }
        return sb.toString().replaceFirst("\n$", "");
    }

    /** 解析 docker compose ps --format json，兼容逐行 JSON 与 JSON 数组两种输出。 */
    public static List<Map<String, Object>> parseComposePs(String text) {
        List<Map<String, Object>> items = new ArrayList<Map<String, Object>>();
        String t = text == null ? "" : text.trim();
        if (t.isEmpty()) {
            return items;
        }
        Object data = null;
        try {
            data = Json.parse(t);
        } catch (Json.JsonError e) {
            data = null;
        }
        if (data instanceof List) {
            for (Object o : (List<?>) data) {
                if (o instanceof Map) {
                    items.add((Map<String, Object>) o);
                }
            }
            return items;
        }
        if (data instanceof Map) {
            items.add((Map<String, Object>) data);
            return items;
        }
        for (String line : t.split("\n")) {
            line = line.trim();
            if (line.isEmpty()) {
                continue;
            }
            try {
                Object parsed = Json.parse(line);
                if (parsed instanceof Map) {
                    items.add((Map<String, Object>) parsed);
                }
            } catch (Json.JsonError ignored) {
            }
        }
        return items;
    }

    /** 把容器状态整理成给人看的中文行；只保留必要字段。 */
    public static String formatContainers(List<Map<String, Object>> containers) {
        if (containers == null || containers.isEmpty()) {
            return "还没有读取到运行状态，请点「刷新状态」。";
        }
        StringBuilder sb = new StringBuilder();
        for (Map<String, Object> item : containers) {
            String name = or(Json.str(item, "Service", null),
                    or(Json.str(item, "Name", null), "服务"));
            String state = or(Json.str(item, "State", null),
                    or(Json.str(item, "Status", null), "未知"));
            String health = Json.str(item, "Health", "");
            sb.append(name).append("：").append(state);
            if (!health.isEmpty()) {
                sb.append("（").append(health).append("）");
            }
            List<Object> ports = Json.arr(item, "Publishers");
            if (!ports.isEmpty()) {
                StringBuilder pairs = new StringBuilder();
                for (Object p : ports) {
                    if (p instanceof Map) {
                        Long pub = Json.lng(p, "PublishedPort");
                        Long tgt = Json.lng(p, "TargetPort");
                        if (pub != null && pub != 0) {
                            if (pairs.length() > 0) {
                                pairs.append("，");
                            }
                            pairs.append(pub).append("→").append(tgt == null ? "?" : tgt);
                        }
                    }
                }
                if (pairs.length() > 0) {
                    sb.append("  端口：").append(pairs);
                }
            }
            sb.append('\n');
        }
        return sb.toString().replaceFirst("\n$", "");
    }

    private static String or(String a, String b) {
        return a == null || a.isEmpty() ? b : a;
    }
}
