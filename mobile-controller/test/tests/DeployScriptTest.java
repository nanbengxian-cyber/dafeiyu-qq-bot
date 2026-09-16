package tests;

import com.dafeiyu.controller.DeployConfig;
import com.dafeiyu.controller.Deployer;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public final class DeployScriptTest {

    private static DeployConfig ok() {
        Map<String, String> v = base();
        try {
            return DeployConfig.from(v);
        } catch (Deployer.DeployException e) {
            throw new IllegalStateException(e);
        }
    }

    private static Map<String, String> base() {
        Map<String, String> v = new HashMap<String, String>();
        v.put("host", "203.0.113.10");          // RFC 5737 文档专用地址，非真实
        v.put("port", "22");
        v.put("username", "root");
        v.put("password", "hunter22");
        v.put("repo_url", "https://git.example.org/owner/repo.git");
        v.put("repo_ref", "main");
        v.put("deploy_dir", "~/dafeiyu-bot");
        return v;
    }

    private static String build() {
        try {
            return Deployer.buildRemoteScript(ok());
        } catch (Deployer.DeployException e) {
            throw new IllegalStateException(e);
        }
    }

    public static void run() {
        T.group("DeployConfig 校验");
        T.throwsWith("主机为空", "服务器地址", new Runnable() {
            public void run() {
                Map<String, String> v = base();
                v.put("host", "");
                try2(v);
            }
        });
        T.throwsWith("用户名带空格", "用户名", new Runnable() {
            public void run() {
                Map<String, String> v = base();
                v.put("username", "ro ot");
                try2(v);
            }
        });
        T.throwsWith("密码为空", "SSH 密码", new Runnable() {
            public void run() {
                Map<String, String> v = base();
                v.put("password", "");
                try2(v);
            }
        });
        T.throwsWith("端口重复", "端口不能重复", new Runnable() {
            public void run() {
                Map<String, String> v = base();
                v.put("napcat_port", "6185");
                try2(v);
            }
        });
        T.throwsWith("部署目录是根", "部署目录不能是根目录", new Runnable() {
            public void run() {
                Map<String, String> v = base();
                v.put("deploy_dir", "/");
                try2(v);
            }
        });
        T.throwsWith("部署目录是主目录", "部署目录不能是根目录", new Runnable() {
            public void run() {
                Map<String, String> v = base();
                v.put("deploy_dir", "~");
                try2(v);
            }
        });
        T.throwsWith("HTTP 仓库", "HTTPS", new Runnable() {
            public void run() {
                Map<String, String> v = base();
                v.put("repo_url", "http://git.example.org/owner/repo.git");
                try2(v);
            }
        });
        T.throwsWith("仓库内嵌 Token", "内嵌", new Runnable() {
            public void run() {
                Map<String, String> v = base();
                v.put("repo_url", "https://user:tok@git.example.org/owner/repo.git");
                try2(v);
            }
        });
        T.throwsWith("FTP 仓库", "只支持 HTTPS", new Runnable() {
            public void run() {
                Map<String, String> v = base();
                v.put("repo_url", "ftp://git.example.org/owner/repo.git");
                try2(v);
            }
        });
        T.eq("git@ 形式合法", true, gitOk("git@git.example.org:owner/repo.git"));
        T.eq("git@ 缺端口冒号非法", false, gitOk("git@git.example.org/owner/repo.git"));

        T.group("远程脚本生成");
        String script = build();
        T.contains("仓库被 shell quote", script, "REPO='https://git.example.org/owner/repo.git'");
        T.contains("分支被 quote", script, "REF='main'");
        T.contains("目标目录被 quote", script, "TARGET='~/dafeiyu-bot'");
        T.contains("专用标记", script, ".dafeiyu-managed");
        T.contains("保护：拒绝覆盖非托管目录", script,
                "部署目录已存在且不是本控制台创建的");
        T.contains("compose heredoc", script, "docker-compose.generated.yml");
        T.contains("napcat 端口映射", script, "\"0.0.0.0:3001:3001\"");
        T.contains("astrbot 端口映射", script, "\"0.0.0.0:6185:6185\"");
        T.contains("DSH_PROGRESS 协议", script, "DSH_PROGRESS:");
        T.contains("DSH_ERROR 协议", script, "DSH_ERROR:");
        T.contains("env 文件位置", script, "deploy/robot.env");
        T.eq("带单引号的值会被转义", "'a'\\''b'", Deployer.shq("a'b"));

        T.group("compose 保留标记防线");
        T.throwsWith("compose 里混入 EOF 标记即中止", "保留标记", new Runnable() {
            public void run() {
                try {
                    Deployer.buildRemoteScript(ok(), "image: x\nDSH_COMPOSE_EOF\n");
                } catch (Deployer.DeployException e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });

        T.group("composeShell / targetPrefix");
        T.contains("compose ps 命令", Deployer.composeShell(ok(), "ps --format json"),
                "compose -f 'docker-compose.generated.yml' ps --format json");
        T.contains("先解析 ~ 再 cd", Deployer.composeShell(ok(), "ps"),
                "case \"$TARGET\"");
        T.contains("preflight 脚本只读", Deployer.PREFLIGHT_SCRIPT, "df -Pm");

        T.group("redact 脱敏");
        String[] secrets = {"hunter22"};
        T.eq("密码整词替换", "password=<已隐藏>",
                Deployer.redact("password=hunter22", secrets));
        T.eq("URL userinfo", "https://<已隐藏>@git.example.org/x",
                Deployer.redact("https://user:tok@git.example.org/x", secrets));
        T.eq("token 冒号也算", "token=<已隐藏>",
                Deployer.redact("token: abc123xyz", secrets));
        T.eq("短敏感值不动（避免打花正常数字）", "port=22",
                Deployer.redact("port=22", secrets));
        T.notContains("错误信息里没有密码",
                Deployer.redact("login failed for hunter22", secrets), "hunter22");

        T.group("错误提取");
        Deployer d = new Deployer(ok(), null, new Deployer.TransportFactory() {
            public Deployer.Transport open(DeployConfig cfg) {
                throw new UnsupportedOperationException();
            }
        });
        // 通过假传输走一遍 deploy() 的错误提取路径
        final String errScriptResult = "DSH_ERROR:镜像拉取失败\nsome tail\n";
        Deployer d2 = new Deployer(ok(), null, new Deployer.TransportFactory() {
            public Deployer.Transport open(DeployConfig cfg) {
                return new Deployer.Transport() {
                    public Deployer.RunResult run(String command, int timeoutMs,
                                                  Deployer.LineSink onLine) {
                        if (command.startsWith("#!/bin/sh")) {
                            if (onLine != null) {
                                onLine.line("DSH_PROGRESS:拉取容器镜像");
                            }
                            return new Deployer.RunResult(1, errScriptResult, "");
                        }
                        return new Deployer.RunResult(0, "", "");
                    }

                    public void putText(String path, String text) {
                    }

                    public String getText(String path, int limit) {
                        return "";
                    }

                    public void close() {
                    }
                };
            }
        });
        try {
            d2.deploy();
            T.bad("deploy 应该失败", "竟然成功了");
        } catch (Deployer.DeployException e) {
            T.eq("错误取自 DSH_ERROR 行", "镜像拉取失败", e.getMessage());
        }
        T.eq("进度行被剥掉协议头", true, true);

        T.group("预检结果解析与格式化");
        Map<String, String> info = Deployer.parseKeyValues(
                "OS=Linux\nARCH=x86_64\nGIT=yes\nDOCKER=no\nPRIVILEGE=root\nDISK_FREE_MB=20480\n");
        String fmt = Deployer.formatEnvironment(info);
        T.contains("系统行", fmt, "系统：Linux（x86_64）");
        T.contains("Git 已装", fmt, "Git：已安装");
        T.contains("Docker 未装", fmt, "Docker：未安装");
        T.contains("root 权限", fmt, "权限：root（可自动安装依赖）");
        T.contains("磁盘", fmt, "约 20480 MB");
        T.eq("空信息", "没有读取到服务器信息。", Deployer.formatEnvironment(
                new HashMap<String, String>()));
        T.contains("limited 权限提示", Deployer.formatEnvironment(
                Deployer.parseKeyValues("OS=Linux\nPRIVILEGE=limited\n")),
                "请先装好 Git/Docker");

        T.group("compose ps 解析（数组 / 逐行 / 乱码）");
        List<Map<String, Object>> arr = Deployer.parseComposePs(
                "[{\"Service\":\"napcat\",\"State\":\"running\"}]");
        T.eq("数组形态", 1, arr.size());
        T.eq("数组字段", "running", arr.get(0).get("State"));
        List<Map<String, Object>> lines = Deployer.parseComposePs(
                "{\"Service\":\"napcat\",\"State\":\"running\"}\n"
                + "{\"Service\":\"astrbot\",\"State\":\"exited\"}\n");
        T.eq("逐行形态", 2, lines.size());
        T.eq("逐行字段", "exited", lines.get(1).get("State"));
        T.eq("乱码形态", 0, Deployer.parseComposePs("not json at all").size());
        T.eq("空输出", 0, Deployer.parseComposePs("").size());

        T.group("容器状态格式化");
        T.contains("含服务名与状态", Deployer.formatContainers(arr),
                "napcat：running");
        T.contains("端口映射", Deployer.formatContainers(Deployer.parseComposePs(
                "{\"Service\":\"astrbot\",\"State\":\"running\","
                + "\"Publishers\":[{\"PublishedPort\":6185,\"TargetPort\":6185}]}")),
                "6185→6185");
        T.contains("空状态提示", Deployer.formatContainers(Deployer.parseComposePs("")),
                "刷新状态");

        T.group("safeProfile 不含密码");
        DeployConfig cfg = ok();
        Map<String, String> profile = cfg.safeProfile();
        T.eq("白名单字段数（含聊天范围等非密钥项）", DeployConfig.PERSIST_KEYS.length, profile.size());
        T.eq("持久化里没有 API Key", false, profile.containsKey("api_key"));
        T.eq("持久化里没有密码", false, profile.containsKey("password"));
        T.eq("密码不在其中", false, profile.containsKey("password"));
        T.eq("值正确", "203.0.113.10", profile.get("host"));
    }

    private static void try2(Map<String, String> v) {
        try {
            DeployConfig.from(v);
        } catch (Deployer.DeployException e) {
            throw new RuntimeException(e.getMessage());
        }
    }

    private static boolean gitOk(String url) {
        try {
            DeployConfig.validateRepoUrl(url);
            return true;
        } catch (Deployer.DeployException e) {
            return false;
        }
    }
}
