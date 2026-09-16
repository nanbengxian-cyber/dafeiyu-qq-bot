package com.dafeiyu.controller;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * 一次部署的全部参数 + 校验规则 —— 桌面控制台 config_schema.py / deployer.py
 * DeployConfig 的逐条移植，规则一条不少：
 *
 * - 用户输入进命令行前全部 shell quote，所以这里只做「早死早超己」的格式校验；
 * - 部署目录不能是根目录/主目录，控制台只管理带专用标记的目录；
 * - 仓库地址不允许内嵌账号/Token，HTTPS 之外只认 git@host:owner/repo.git。
 */
public final class DeployConfig {

    public String host = "";
    public int port = 22;
    public String username = "";
    public String password = "";
    public boolean trustNewHost = false;

    public String repoUrl = "";
    public String repoRef = "main";
    public String deployDir = "~/dafeiyu-bot";
    public boolean installDeps = true;

    public int napcatPort = 3001;
    public int astrbotPort = 6185;
    public int astrbotApiPort = 6186;
    public String bindAddress = "0.0.0.0";

    public String napcatImage = "mlikiowa/napcat-docker:latest";
    public String astrbotImage = "soulter/astrbot:latest";

    /** 构造时的原始输入（safeProfile 要取聊天范围/API 的非密钥字段）。 */
    private Map<String, String> rawValues = new LinkedHashMap<String, String>();

    /** 允许持久化的字段白名单（密码永远不在其中）。 */
    public static final String[] PERSIST_KEYS = {
            "host", "port", "username", "trust_new_host",
            "repo_url", "repo_ref", "deploy_dir", "install_dependencies",
            "napcat_port", "astrbot_port", "astrbot_api_port", "bind_address",
            "napcat_image", "astrbot_image",
            // 聊天范围与主聊天 API 的**非密钥**部分：API Key 不在白名单里，
            // 和 SSH 密码一个待遇 —— 只进内存，进程一死就没了。
            "chat_groups", "chat_friends", "api_base", "api_model",
    };

    public static final String DEFAULT_REPO = "https://github.com/nanbengxian-cyber/dafeiyu-qq-bot.git";

    /** 从界面/存储的字符串表构造并校验；失败抛 DeployException（消息可直接展示）。 */
    public static DeployConfig from(Map<String, String> v) throws Deployer.DeployException {
        DeployConfig c = new DeployConfig();
        c.host = text(v, "host");
        c.port = intOf(v, "port", "SSH 端口", 22, 1, 65535);
        c.username = text(v, "username");
        c.password = v.get("password") == null ? "" : v.get("password");
        c.trustNewHost = "1".equals(text(v, "trust_new_host"))
                || Boolean.parseBoolean(text(v, "trust_new_host"));
        c.repoUrl = text(v, "repo_url");
        c.repoRef = text(v, "repo_ref").isEmpty() ? "main" : text(v, "repo_ref");
        c.deployDir = text(v, "deploy_dir").isEmpty() ? "~/dafeiyu-bot" : text(v, "deploy_dir");
        c.installDeps = !"0".equals(text(v, "install_dependencies"))
                && !"false".equalsIgnoreCase(text(v, "install_dependencies"));
        c.napcatPort = intOf(v, "napcat_port", "NapCat 端口", 3001, 1, 65535);
        c.astrbotPort = intOf(v, "astrbot_port", "AstrBot 端口", 6185, 1, 65535);
        c.astrbotApiPort = intOf(v, "astrbot_api_port", "AstrBot API 端口", 6186, 1, 65535);
        c.bindAddress = text(v, "bind_address").isEmpty() ? "0.0.0.0" : text(v, "bind_address");
        c.napcatImage = text(v, "napcat_image").isEmpty()
                ? "mlikiowa/napcat-docker:latest" : text(v, "napcat_image");
        c.astrbotImage = text(v, "astrbot_image").isEmpty()
                ? "soulter/astrbot:latest" : text(v, "astrbot_image");
        c.rawValues = new LinkedHashMap<String, String>(v);
        c.validate();
        return c;
    }

    private static String text(Map<String, String> v, String key) {
        String s = v.get(key);
        return s == null ? "" : s.trim();
    }

    private static int intOf(Map<String, String> v, String key, String label,
                             int dflt, int lo, int hi) throws Deployer.DeployException {
        String raw = text(v, key);
        if (raw.isEmpty()) {
            return dflt;
        }
        int value;
        try {
            value = Integer.parseInt(raw);
        } catch (NumberFormatException e) {
            throw new Deployer.DeployException(label + "必须是数字。");
        }
        if (value < lo || value > hi) {
            throw new Deployer.DeployException(label + "必须在 " + lo + " 到 " + hi + " 之间。");
        }
        return value;
    }

    private void validate() throws Deployer.DeployException {
        if (host.isEmpty() || hasSpace(host)) {
            throw new Deployer.DeployException("请填写正确的服务器地址。");
        }
        if (!Pattern.matches("[A-Za-z0-9._-]+", username)) {
            throw new Deployer.DeployException("SSH 用户名格式不正确。");
        }
        if (password.isEmpty()) {
            throw new Deployer.DeployException("请输入 SSH 密码。");
        }
        validateRepoUrl(repoUrl);
        if (!Pattern.matches("[A-Za-z0-9._/+-]+", repoRef)) {
            throw new Deployer.DeployException("分支或标签格式不正确。");
        }
        if (deployDir.isEmpty() || deployDir.contains("\r") || deployDir.contains("\n")
                || deployDir.contains("\0")) {
            throw new Deployer.DeployException("部署目录格式不正确。");
        }
        String trimmed = deployDir.replaceAll("/+$", "");
        if (trimmed.isEmpty() || trimmed.equals("/") || trimmed.equals("~")
                || trimmed.equals("$HOME") || trimmed.equals(".") || trimmed.equals("..")) {
            throw new Deployer.DeployException("部署目录不能是根目录或主目录本身。");
        }
        if (napcatPort == astrbotPort || napcatPort == astrbotApiPort
                || astrbotPort == astrbotApiPort) {
            throw new Deployer.DeployException("三个服务端口不能重复。");
        }
        if (!Pattern.matches("[0-9a-fA-F.:]+", bindAddress)) {
            throw new Deployer.DeployException("监听地址必须是合法 IP。");
        }
        for (String[] item : new String[][]{{"NapCat", napcatImage}, {"AstrBot", astrbotImage}}) {
            if (!Pattern.matches("[A-Za-z0-9._/:@-]+", item[1])) {
                throw new Deployer.DeployException(item[0] + " 镜像名称格式不正确。");
            }
        }
    }

    private static boolean hasSpace(String s) {
        for (int i = 0; i < s.length(); i++) {
            if (Character.isWhitespace(s.charAt(i))) {
                return true;
            }
        }
        return false;
    }

    public static void validateRepoUrl(String url) throws Deployer.DeployException {
        if (url == null || url.isEmpty()) {
            throw new Deployer.DeployException("请填写源码仓库地址。");
        }
        if (url.startsWith("https://") || url.startsWith("http://")) {
            String rest = url.substring(url.indexOf("://") + 3);
            if (rest.isEmpty() || rest.contains("@") || rest.contains("?") || rest.contains("#")) {
                throw new Deployer.DeployException("仓库地址不能内嵌账号、密码、Token、参数或片段。");
            }
            if (url.startsWith("http://")) {
                throw new Deployer.DeployException("请使用 HTTPS 仓库地址；HTTP 会泄露源码。");
            }
            return;
        }
        if (Pattern.matches("git@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+(\\.git)?", url)) {
            return;
        }
        throw new Deployer.DeployException("仓库地址只支持 HTTPS 或 git@主机:owner/repo.git。");
    }

    /** 落盘（Store.profile）只带这份数据；密码永远进不去。 */
    public Map<String, String> safeProfile() {
        Map<String, String> out = new LinkedHashMap<String, String>();
        out.put("host", host);
        out.put("port", String.valueOf(port));
        out.put("username", username);
        out.put("trust_new_host", trustNewHost ? "1" : "0");
        out.put("repo_url", repoUrl);
        out.put("repo_ref", repoRef);
        out.put("deploy_dir", deployDir);
        out.put("install_dependencies", installDeps ? "1" : "0");
        out.put("napcat_port", String.valueOf(napcatPort));
        out.put("astrbot_port", String.valueOf(astrbotPort));
        out.put("astrbot_api_port", String.valueOf(astrbotApiPort));
        out.put("bind_address", bindAddress);
        out.put("napcat_image", napcatImage);
        out.put("astrbot_image", astrbotImage);
        // 聊天范围与主聊天 API 的非密钥部分（群号/私聊 QQ/接口地址/模型名）。
        // **API Key 不在这里** —— 它和 SSH 密码一样只进内存，见 PERSIST_KEYS 的注释。
        out.putAll(ChatSetup.persistable(v_get("chat_groups"), v_get("chat_friends"),
                v_get("api_base"), v_get("api_model")));
        return out;
    }

    /** 读取原始输入表里的一个值（safeProfile 需要，构造时没存这些字段）。 */
    private String v_get(String key) {
        String s = rawValues.get(key);
        return s == null ? "" : s;
    }
}
