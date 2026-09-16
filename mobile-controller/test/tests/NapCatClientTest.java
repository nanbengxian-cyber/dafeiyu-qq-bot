package tests;

import com.dafeiyu.controller.Json;
import com.dafeiyu.controller.NapCatClient;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 用假 HTTP 传输把 NapCat WebUI 协议整个测穿：
 * 登录换凭据 → 带凭据调用 → 凭据过期自动重登一次 → 密码登录分支 → 快速登录列表回退。
 * 假传输按「官方后端的行为」回话（协议核对自 NapCat 源码，不是凭空造的）。
 */
public final class NapCatClientTest {

    private static final String TOKEN = "tok123";
    private static final String CRED = "CRED-1";

    /** 可编排的假传输：按请求路径返回预设响应，并记录每次请求的 bearer。 */
    static class Fake implements NapCatClient.Transport {
        final List<String> paths = new ArrayList<String>();
        final List<String> bearers = new ArrayList<String>();
        final List<String> bodies = new ArrayList<String>();
        String loginBody = "";
        int loginCalls;
        boolean require2fa;
        boolean loginFailsOnce;
        boolean unauthorizedOnce = true;   // 第一次业务调用就过期，测自动重登
        String passwordLoginData = "null";
        boolean listNewMissing = true;

        public NapCatClient.Resp send(String method, String url, String bearer,
                                      String jsonBody, int timeoutMs) {
            String path = url.replaceFirst("^https?://[^/]+", "");
            paths.add(path);
            bearers.add(bearer == null ? "" : bearer);
            bodies.add(jsonBody == null ? "" : jsonBody);
            if (path.equals("/api/auth/login")) {
                loginCalls++;
                loginBody = jsonBody;
                if (loginFailsOnce && loginCalls == 1) {
                    return resp("{\"code\":-1,\"message\":\"token is invalid\"}");
                }
                if (require2fa) {
                    return resp("{\"code\":0,\"data\":{\"require2FA\":true,"
                            + "\"message\":\"Please enter your authenticator code\"}}");
                }
                return resp("{\"code\":0,\"data\":{\"Credential\":\"" + CRED + "\"}}");
            }
            if (!CRED.equals(bearer)) {
                return resp("{\"code\":-1,\"message\":\"Unauthorized\"}");
            }
            if (path.equals("/api/QQLogin/CheckLoginStatus")) {
                if (unauthorizedOnce) {
                    unauthorizedOnce = false;
                    return resp("{\"code\":-1,\"message\":\"Unauthorized\"}");
                }
                return resp("{\"code\":0,\"data\":{\"isLogin\":false,\"isOffline\":false,"
                        + "\"loginPhase\":\"waiting_qrcode\","
                        + "\"qrcodeurl\":\"https://ssl.ptlogin2.qq.com/jump?x=1\","
                        + "\"loginError\":\"\"}}");
            }
            if (path.equals("/api/QQLogin/RefreshQRcode")) {
                return resp("{\"code\":0,\"data\":{\"qrcodeurl\":\"https://x.test/jump?2\"}}");
            }
            if (path.equals("/api/QQLogin/GetQQLoginInfo")) {
                return resp("{\"code\":0,\"data\":{\"uin\":\"10001\",\"nick\":\"测试号\","
                        + "\"online\":true}}");
            }
            if (path.equals("/api/QQLogin/PasswordLogin")) {
                return resp("{\"code\":0,\"data\":" + passwordLoginData + "}");
            }
            if (path.equals("/api/QQLogin/SetQuickLogin")) {
                return resp("{\"code\":0,\"data\":null}");
            }
            if (path.equals("/api/QQLogin/GetQuickLoginListNew")) {
                if (listNewMissing) {
                    return new NapCatClient.Resp(404, "<html>404</html>");
                }
                return resp("{\"code\":0,\"data\":[{\"uin\":\"10001\",\"nick\":\"测试号\"}]}");
            }
            if (path.equals("/api/QQLogin/GetQuickLoginList")) {
                return resp("{\"code\":0,\"data\":[\"10001\"]}");
            }
            if (path.equals("/api/QQLogin/RestartNapCat")) {
                return resp("{\"code\":0,\"data\":null}");
            }
            return new NapCatClient.Resp(404, "<html>404</html>");
        }

        private static NapCatClient.Resp resp(String body) {
            return new NapCatClient.Resp(200, body);
        }
    }

    private static Map<String, Object> data(Map<String, Object> envelope) {
        return Json.obj(envelope, "data");
    }

    public static void run() {
        T.group("地址归一化");
        T.eq("补 scheme", "http://1.2.3.4:6099", NapCatClient.normalize("1.2.3.4:6099"));
        T.eq("去尾斜杠", "http://1.2.3.4:6099", NapCatClient.normalize("1.2.3.4:6099/"));
        T.eq("原样保留 https", "https://napcat.test", NapCatClient.normalize("https://napcat.test"));
        T.eq("空串", "", NapCatClient.normalize("  "));
        T.eq("null", "", NapCatClient.normalize(null));

        T.group("登录换凭据（sha256(token + \".napcat\")）");
        Fake f = new Fake();
        NapCatClient c = new NapCatClient(f);
        c.configure("napcat.test:6099", TOKEN, "");
        try {
            c.login();
        } catch (NapCatClient.ApiError e) {
            T.bad("登录应当成功", e.getMessage());
        }
        T.eq("登录请求打了对的路径", true, f.paths.get(0).endsWith("/api/auth/login"));
        Map<String, Object> sent = parse(f.loginBody);
        String wantHash = NapCatClient.sha256hex(TOKEN + ".napcat");
        T.eq("hash 是 sha256(token + \".napcat\")", wantHash, Json.str(sent, "hash"));
        T.eq("没开 2FA 不带动态码", false, sent.containsKey("totpCode"));
        T.eq("connected 状态翻转", true, c.connected());

        T.group("登录失败信息透传");
        Fake f2 = new Fake();
        f2.loginFailsOnce = true;
        NapCatClient c2 = new NapCatClient(f2);
        c2.configure("napcat.test:6099", "wrong", "");
        T.throwsWith("token 错误要原话报出来", "token is invalid", new Runnable() {
            public void run() {
                try {
                    c2.login();
                } catch (NapCatClient.ApiError e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });

        T.group("两步验证分支");
        Fake f3 = new Fake();
        f3.require2fa = true;
        NapCatClient c3 = new NapCatClient(f3);
        c3.configure("napcat.test", TOKEN, "");
        T.throwsWith("没填动态码要指路", "动态码", new Runnable() {
            public void run() {
                try {
                    c3.login();
                } catch (NapCatClient.ApiError e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });
        // 填了动态码 → 假传输第二遍发凭据
        Fake f4 = new Fake() {
            public NapCatClient.Resp send(String method, String url, String bearer,
                                          String jsonBody, int timeoutMs) {
                paths.add(url);
                if (url.endsWith("/api/auth/login") && jsonBody != null
                        && jsonBody.contains("totpCode")) {
                    return new NapCatClient.Resp(200,
                            "{\"code\":0,\"data\":{\"Credential\":\"" + CRED + "\"}}");
                }
                return new NapCatClient.Resp(200,
                        "{\"code\":0,\"data\":{\"require2FA\":true}}");
            }
        };
        NapCatClient c4 = new NapCatClient(f4);
        c4.configure("napcat.test", TOKEN, "654321");
        try {
            c4.login();
            T.ok("带动态码能拿到凭据");
        } catch (NapCatClient.ApiError e) {
            T.bad("带动态码应当成功", e.getMessage());
        }

        T.group("业务调用自动带凭据 + 过期自动重登一次");
        Fake f5 = new Fake();
        NapCatClient c5 = new NapCatClient(f5);
        c5.configure("napcat.test", TOKEN, "");
        Map<String, Object> st = null;
        try {
            st = data(c5.checkLoginStatus());
        } catch (NapCatClient.ApiError e) {
            T.bad("状态查询应当成功", e.getMessage());
        }
        T.eq("初始登录 + 过期重登共两次", 2, f5.loginCalls);
        T.eq("状态里的 phase", "waiting_qrcode", Json.str(st, "loginPhase"));
        T.eq("二维码内容拿到了", true,
                Json.str(st, "qrcodeurl").startsWith("https://ssl.ptlogin2.qq.com/"));
        T.eq("业务请求带凭据（Real 传输会拼成 Bearer 头）", CRED,
                f5.bearers.get(f5.bearers.size() - 1));
        // fake 里第一次 CheckLoginStatus 回 Unauthorized：客户端应该重登并重试成功
        int logins = f5.loginCalls;
        T.eq("Unauthorized 触发了重登", logins >= 2, true);

        T.group("刷新二维码 / 登录信息 / 重启");
        try {
            Map<String, Object> rf = data(c5.refreshQrcode());
            T.eq("刷新返回新码", "https://x.test/jump?2", Json.str(rf, "qrcodeurl"));
            Map<String, Object> info = data(c5.loginInfo());
            T.eq("uin", "10001", Json.str(info, "uin"));
            T.eq("nick", "测试号", Json.str(info, "nick"));
            c5.restartNapCat();
            T.ok("重启调用成功");
        } catch (NapCatClient.ApiError e) {
            T.bad("这三个调用不该失败", e.getMessage());
        }

        T.group("密码登录（MD5 + 安全验证分支）");
        try {
            f5.passwordLoginData = "{\"needCaptcha\":true,\"proofWaterUrl\":\"https://cap.test/x\"}";
            Map<String, Object> cap = data(c5.passwordLogin("10001", "pw12345"));
            T.eq("needCaptcha 透传", true, Json.bool(cap, "needCaptcha", false));
            T.eq("验证地址透传", "https://cap.test/x", Json.str(cap, "proofWaterUrl"));
            f5.passwordLoginData = "{\"needNewDevice\":true,\"jumpUrl\":\"https://nd.test/y\"}";
            Map<String, Object> nd = data(c5.passwordLogin("10001", "pw12345"));
            T.eq("needNewDevice 透传", true, Json.bool(nd, "needNewDevice", false));
            f5.passwordLoginData = "null";
            Map<String, Object> okData = data(c5.passwordLogin("10001", "pw12345"));
            T.eq("成功时 data 为空对象", 0, okData.size());
        } catch (NapCatClient.ApiError e) {
            T.bad("密码登录分支不该失败", e.getMessage());
        }
        // 请求体里的 passwordMd5 必须是密码的 MD5（最后一次 PasswordLogin 的 body）
        String lastPwBody = "";
        for (int i = 0; i < f5.paths.size(); i++) {
            if (f5.paths.get(i).endsWith("/api/QQLogin/PasswordLogin")) {
                lastPwBody = f5.bodies.get(i);
            }
        }
        Map<String, Object> pwBody = parse(lastPwBody);
        T.eq("uin 原样", "10001", Json.str(pwBody, "uin"));
        T.eq("密码只以 MD5 出现", NapCatClient.md5hex("pw12345"),
                Json.str(pwBody, "passwordMd5"));
        T.notContains("请求体里不能有明文密码", lastPwBody, "pw12345");
        T.throwsWith("空 uin 拦下", "QQ 号", new Runnable() {
            public void run() {
                try {
                    new NapCatClient(new Fake()).passwordLogin("  ", "x");
                } catch (NapCatClient.ApiError e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });
        T.throwsWith("空密码拦下", "密码", new Runnable() {
            public void run() {
                try {
                    new NapCatClient(new Fake()).passwordLogin("10001", "");
                } catch (NapCatClient.ApiError e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });

        T.group("快速登录：列表回退与设置");
        try {
            f5.listNewMissing = false;
            List<Object> fresh = c5.quickList();
            T.eq("新版列表优先", "10001", Json.str(fresh.get(0), "uin"));
            f5.listNewMissing = true;
            List<Object> legacy = c5.quickList();
            T.eq("旧版回退（纯 uin）", "10001", String.valueOf(legacy.get(0)));
            c5.setQuickLogin("10001");
            boolean sawSet = false;
            for (String p : f5.paths) {
                if (p.endsWith("/api/QQLogin/SetQuickLogin")) {
                    sawSet = true;
                }
            }
            T.eq("SetQuickLogin 调用到位", true, sawSet);
        } catch (NapCatClient.ApiError e) {
            T.bad("快速登录不该失败", e.getMessage());
        }

        T.group("杂项");
        T.eq("md5 已知向量", "e10adc3949ba59abbe56e057f20f883e",
                NapCatClient.md5hex("123456"));
        T.throwsWith("未配置就调用给人话错误", "还没填 WebUI 地址", new Runnable() {
            public void run() {
                try {
                    new NapCatClient(new Fake()).checkLoginStatus();
                } catch (NapCatClient.ApiError e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });
    }

    private static Map<String, Object> parse(String json) {
        try {
            return Json.parseObject(json);
        } catch (Json.JsonError e) {
            throw new IllegalStateException(e);
        }
    }
}
