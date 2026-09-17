package tests;

import com.dafeiyu.controller.ManagerClient;
import com.dafeiyu.controller.ProxyTransport;
import com.dafeiyu.controller.Tunnel;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.HashMap;
import java.util.Map;

/**
 * 隧道 / 管理服务 / 代理 的单测 —— 也就是「手机怎么连上服务器」这一层。
 *
 * 用项目自己的 T 断言框架（不是另起一套），这样失败会进统一汇总、
 * 退出码也能被 auto/verify.sh 判到。
 *
 * 重点覆盖「错了会很难查」的地方：
 *   · 指纹算法（错了会导致连不上任何服务器，且报错看着像密钥问题）
 *   · 代理路径解析（错了请求打到错接口，表现是「登录没反应」）
 *   · 两个令牌走不同的头（错了 NapCat 凭据会被管理口令覆盖）
 *   · 错误信息是不是人话（这是给非技术用户用的 App）
 */
public final class TunnelManagerTest {

    public static void run() {
        T.group("隧道与管理服务");
        fingerprint();
        proxyPaths();
        instanceModel();
        try {
            clientAgainstFakeServer();
            headerSeparation();
        } catch (Exception e) {
            T.bad("端到端测试", "崩溃 " + e);
        }
    }

    // ---------------------------------------------------------------- 指纹

    private static void fingerprint() {
        // ★ 已知答案测试：这是从真实服务器上抓下来的 ECDSA 主机密钥 blob，
        //   以及 `ssh-keyscan | ssh-keygen -lf -` 给出的官方指纹。
        //   两者必须对得上 —— 如果对不上，App 内置的指纹校验会永远失败，
        //   表现是「连不上服务器」，而报错看着像密钥问题，极难查。
        String realBlob =
                "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBK+4f2F8" +
                "5XJDMHGrHN0vPGlfZQK5wrveg0huy0DAzVcSDfr3NkDc9cLVIlVNyNWs8CWh" +
                "12SbYGgCYFgnCjJ7KYA=";
        T.eq("★ 真实主机密钥算出的指纹与 ssh-keygen 完全一致",
                "SHA256:Q5JEe2Eqf+SwmVIW/Uis34U1NlQraJi3UERxDK/hmZY",
                Tunnel.sha256Fingerprint(realBlob));

        String fp = Tunnel.sha256Fingerprint(realBlob);
        T.isTrue("指纹带 SHA256: 前缀", fp.startsWith("SHA256:"));
        T.isTrue("指纹长度合理", fp.length() > 20);
        T.isFalse("指纹尾部不带 =（与 ssh-keygen 一致）", fp.endsWith("="));
        T.eq("指纹计算是确定性的", fp, Tunnel.sha256Fingerprint(realBlob));
        T.isTrue("不同密钥得到不同指纹",
                !Tunnel.sha256Fingerprint(realBlob.substring(0, realBlob.length() - 4) + "AAAA")
                        .equals(fp));

        // ★ 空输入必须返回空串：返回一个「合法的哈希」会让调用方以为指纹有效，
        //   从而跳过校验 —— 那就等于没有防中间人。
        T.eq("★ 空输入返回空串（不能返回一个像样的哈希）", "",
                Tunnel.sha256Fingerprint(""));
        T.eq("★ 非法 base64 返回空串（不能误判成匹配）", "",
                Tunnel.sha256Fingerprint("这不是base64!!!"));
    }

    // ---------------------------------------------------------------- 代理路径

    private static void proxyPaths() {
        T.eq("普通路径", "api/auth/login",
                ProxyTransport.pathOf("http://127.0.0.1:6099/api/auth/login"));
        T.eq("带查询串", "api/x?a=1",
                ProxyTransport.pathOf("http://127.0.0.1:6099/api/x?a=1"));
        T.eq("只有根斜杠", "", ProxyTransport.pathOf("https://example.com/"));
        T.eq("没有路径", "", ProxyTransport.pathOf("https://example.com"));
        T.eq("空串", "", ProxyTransport.pathOf(""));
        T.eq("null", "", ProxyTransport.pathOf(null));
        T.eq("带端口的地址不把端口当路径", "api/QQLogin/CheckLoginStatus",
                ProxyTransport.pathOf("http://1.2.3.4:6099/api/QQLogin/CheckLoginStatus"));
    }

    // ---------------------------------------------------------------- 实例模型

    private static void instanceModel() {
        Map<String, Object> m = new HashMap<String, Object>();
        m.put("name", "qq1");
        m.put("webui_port", 16000L);
        m.put("onebot_port", 16001L);
        m.put("panel_port", 16002L);
        Map<String, Object> cs = new HashMap<String, Object>();
        cs.put("napcat", "running");
        cs.put("astrbot", "running");
        m.put("containers", cs);

        ManagerClient.Instance it = ManagerClient.Instance.from(m);
        T.eq("名字解析", "qq1", it.name);
        T.eq("WebUI 端口解析", 16000, it.webuiPort);
        T.isTrue("两个容器都 running → running()", it.running());
        T.eq("状态文案", "运行中", it.stateText());

        // ★ 半死不活：napcat 活着但 astrbot 挂了。绝不能报「运行中」，
        //   否则用户以为一切正常，实际机器人根本不理人。
        cs.put("astrbot", "exited");
        ManagerClient.Instance half = ManagerClient.Instance.from(m);
        T.isFalse("★ 一个容器挂了就不能算运行中", half.running());
        T.isTrue("★ 半死状态要提示异常/启动中，不能谎报正常",
                half.stateText().contains("异常") || half.stateText().contains("启动中"));

        cs.put("napcat", "absent");
        cs.put("astrbot", "absent");
        T.eq("未启动文案", "未启动", ManagerClient.Instance.from(m).stateText());

        // 缺字段不能崩（服务器版本不同时可能出现）
        Map<String, Object> bare = new HashMap<String, Object>();
        bare.put("name", "x");
        ManagerClient.Instance b = ManagerClient.Instance.from(bare);
        T.eq("缺字段时名字仍可用", "x", b.name);
        T.eq("缺端口时为 0（不崩）", 0, b.webuiPort);
        T.isFalse("缺字段不算运行中", b.running());
        // 老服务器没有 lock 字段 → 当成不锁，不能崩
        T.isFalse("★ 没有 lock 字段时按「不锁」处理（兼容老服务器）", b.locked);

        // ── 私密锁的解析（对应「可以设为私密的机器人配置」这条反馈）────
        Map<String, Object> lm = new HashMap<String, Object>();
        lm.put("name", "secret1");
        Map<String, Object> lk = new HashMap<String, Object>();
        lk.put("locked", true);
        lm.put("lock", lk);
        ManagerClient.Instance locked = ManagerClient.Instance.from(lm);
        T.isTrue("★ locked=true 被正确解析（界面才能画锁图标）", locked.locked);
        T.eq("锁着的实例名字仍可读", "secret1", locked.name);

        lk.put("locked", false);
        T.isFalse("★ locked=false 解析为不锁",
                ManagerClient.Instance.from(lm).locked);

        // lock 存在但不是对象 / 为 null 时都不能崩
        lm.put("lock", null);
        T.isFalse("lock=null 时按不锁处理", ManagerClient.Instance.from(lm).locked);
        lm.put("lock", "垃圾数据");
        T.isFalse("★ lock 是垃圾数据时不崩、按不锁处理",
                ManagerClient.Instance.from(lm).locked);
    }

    // ---------------------------------------------------------------- 端到端

    private static void clientAgainstFakeServer() throws Exception {
        // 正常响应
        FakeServer s1 = new FakeServer(200, "{\"ok\":true,\"version\":1}");
        try {
            new ManagerClient(s1.port(), "tok").health();
            T.ok("健康检查通过");
            T.eq("★ 管理口令走 Authorization 头", "Bearer tok", s1.lastAuthHeader);
            T.eq("请求路径正确", "/health", s1.lastPath);
        } finally {
            s1.close();
        }

        // 口令不对 → 提示要说人话
        FakeServer s2 = new FakeServer(401, "{\"error\":\"未授权。\"}");
        try {
            T.throwsWith("★ 401 提示「口令不对」", "口令", new Runnable() {
                public void run() {
                    try {
                        new ManagerClient(s2.port(), "wrong").health();
                    } catch (Exception e) {
                        throw new RuntimeException(e);
                    }
                }
            });
        } finally {
            s2.close();
        }

        // 服务器业务错 → 原话透传
        final FakeServer s3 = new FakeServer(400,
                "{\"error\":\"实例名只能用 小写字母/数字/短横线。\"}");
        try {
            T.throwsWith("★ 服务器的人话原样透传", "小写字母", new Runnable() {
                public void run() {
                    try {
                        new ManagerClient(s3.port(), "tok").create("BAD_NAME");
                    } catch (Exception e) {
                        throw new RuntimeException(e);
                    }
                }
            });
        } finally {
            s3.close();
        }

        // 对面不是管理服务（HTML）→ 不能显示乱码
        final FakeServer s4 = new FakeServer(200, "<html>我是别的服务</html>");
        try {
            T.throwsWith("★ 非 JSON 给可行动提示", "不匹配", new Runnable() {
                public void run() {
                    try {
                        new ManagerClient(s4.port(), "tok").health();
                    } catch (Exception e) {
                        throw new RuntimeException(e);
                    }
                }
            });
        } finally {
            s4.close();
        }

        // 端口上没服务 → 提示隧道断了
        ServerSocket tmp = new ServerSocket(0);
        final int deadPort = tmp.getLocalPort();
        tmp.close();
        T.throwsWith("★ 连不上时提示隧道断了", "隧道", new Runnable() {
            public void run() {
                try {
                    new ManagerClient(deadPort, "tok").health();
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            }
        });
    }

    private static void headerSeparation() throws Exception {
        FakeServer s = new FakeServer(200, "{\"code\":0,\"data\":{}}");
        try {
            ProxyTransport t = new ProxyTransport(s.port(), "MANAGER_TOK", "qq1");
            t.send("POST", "http://127.0.0.1:6099/api/auth/login",
                    "NAPCAT_CRED", "{}", 5000);
            T.eq("★ 管理口令走 X-Dafeiyu-Token", "MANAGER_TOK", s.lastDafeiyuToken);
            T.eq("★ NapCat 凭据走 Authorization（两者不打架）",
                    "Bearer NAPCAT_CRED", s.lastAuthHeader);
            T.eq("路径带实例名", "/proxy/qq1/api/auth/login", s.lastPath);
        } finally {
            s.close();
        }
    }

    // ---------------------------------------------------------------- 假服务器

    /** 极简 HTTP 服务：固定响应，并记下收到的头供断言。 */
    static final class FakeServer {
        volatile String lastAuthHeader = "";
        volatile String lastDafeiyuToken = "";
        volatile String lastPath = "";
        private final ServerSocket server;
        private volatile boolean stop;

        FakeServer(final int code, final String body) throws Exception {
            server = new ServerSocket(0);
            Thread t = new Thread(new Runnable() {
                public void run() {
                    while (!stop) {
                        try {
                            Socket s = server.accept();
                            handle(s, code, body);
                        } catch (Exception e) {
                            // 关闭时的竞态，忽略
                        }
                    }
                }
            });
            t.setDaemon(true);
            t.start();
        }

        private void handle(Socket s, int code, String body) throws Exception {
            InputStream in = s.getInputStream();
            // 读 HTTP 头。注意判据是「连续两个 CRLF」（\r\n\r\n）——
            // 一开始我写成 prev=='\n' && cur=='\n'，因为中间夹着 \r 而永远不成立，
            // 结果服务器读不到请求、客户端读超时，看着像「连不上服务器」。
            ByteArrayOutputStream head = new ByteArrayOutputStream();
            int a = -1, b = -1, c = -1, cur;
            while ((cur = in.read()) >= 0) {
                head.write(cur);
                if (a == '\r' && b == '\n' && c == '\r' && cur == '\n') {
                    break;
                }
                a = b;
                b = c;
                c = cur;
            }
            String[] lines = new String(head.toByteArray(), "UTF-8").split("\r\n");
            int len = 0;
            for (String ln : lines) {
                String low = ln.toLowerCase();
                if (low.startsWith("authorization:")) {
                    lastAuthHeader = ln.substring(ln.indexOf(':') + 1).trim();
                } else if (low.startsWith("x-dafeiyu-token:")) {
                    lastDafeiyuToken = ln.substring(ln.indexOf(':') + 1).trim();
                } else if (ln.startsWith("GET ") || ln.startsWith("POST ")) {
                    lastPath = ln.split(" ")[1];
                } else if (low.startsWith("content-length:")) {
                    try {
                        len = Integer.parseInt(ln.substring(ln.indexOf(':') + 1).trim());
                    } catch (NumberFormatException ignored) {
                        len = 0;
                    }
                }
            }
            for (int i = 0; i < len; i++) {
                in.read();
            }
            byte[] payload = body.getBytes("UTF-8");
            String resp = "HTTP/1.1 " + code + " X\r\n"
                    + "Content-Type: application/json\r\n"
                    + "Content-Length: " + payload.length + "\r\n"
                    + "Connection: close\r\n\r\n";
            OutputStream out = s.getOutputStream();
            out.write(resp.getBytes("UTF-8"));
            out.write(payload);
            out.flush();
            s.close();
        }

        int port() {
            return server.getLocalPort();
        }

        void close() {
            stop = true;
            try {
                server.close();
            } catch (Exception ignored) {
                // 无所谓
            }
        }
    }
}
