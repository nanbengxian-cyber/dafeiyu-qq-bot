package com.dafeiyu.controller;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * NapCat WebUI 客户端 —— 「开放式登录」的底层（纯 JVM，可用假传输单测）。
 *
 * 协议（按 NapCat 官方源码 packages/napcat-webui-backend 核实，不是猜的）：
 *   1. POST /api/auth/login  body: {"hash": sha256hex(token + ".napcat")}
 *      → {"code":0,"data":{"Credential":"base64..."}}
 *      （开了两步验证时 data 里是 require2FA:true，要再带 totpCode）
 *   2. 之后每个请求带 Authorization: Bearer &lt;Credential&gt;（1 小时有效，过期会回
 *      code!=0 + message "Unauthorized"，这里自动重登一次再试）
 *   3. 响应统一外壳 {"code":0,"message":"...","data":...}，code!=0 即业务失败
 *   4. CheckLoginStatus 的 qrcodeurl 是**二维码内容**（跳转 URL），不是图片 ——
 *      图要本地画（见 QrPainter），官方网页前端也是这么干的
 *
 * 密码登录：POST /api/QQLogin/PasswordLogin {"uin","passwordMd5"}（密码的 MD5），
 * 返回里可能要求安全验证（needCaptcha/needNewDevice）——那两步是腾讯的网页验证，
 * v1 不在本机做，提示用户切到内置网页登录页完成。
 */
public class NapCatClient {

    /** 抽出来是为了单测能塞一个假的；生产用 Real。 */
    public interface Transport {
        Resp send(String method, String url, String bearer, String jsonBody, int timeoutMs)
                throws IOException;
    }

    public static final class Resp {
        public final int code;
        public final String body;

        public Resp(int code, String body) {
            this.code = code;
            this.body = body;
        }
    }

    /** 业务异常：message 直接显示给人看。 */
    public static class ApiError extends Exception {
        public ApiError(String msg) {
            super(msg);
        }
    }

    private String base = "";
    private String token = "";
    private String totp = "";
    private String credential = "";
    private final Transport transport;

    public NapCatClient(Transport transport) {
        this.transport = transport;
    }

    /** 用户只会输 IP 或 IP:端口，帮他补全 scheme，末尾斜杠去掉。不猜端口。 */
    public static String normalize(String raw) {
        if (raw == null) {
            return "";
        }
        String s = raw.trim();
        if (s.isEmpty()) {
            return "";
        }
        if (!s.startsWith("http://") && !s.startsWith("https://")) {
            s = "http://" + s;
        }
        while (s.endsWith("/")) {
            s = s.substring(0, s.length() - 1);
        }
        return s;
    }

    public void configure(String base, String token, String totp) {
        this.base = normalize(base);
        this.token = token == null ? "" : token;
        this.totp = totp == null ? "" : totp;
        this.credential = "";
    }

    public String base() {
        return base;
    }

    public boolean connected() {
        return credential != null && !credential.isEmpty();
    }

    public void disconnect() {
        credential = "";
    }

    // ---------------------------------------------------------------- 登录

    /** WebUI 登录换凭据。token 存在内存里（不落盘），过期自动重登。 */
    public synchronized void login() throws ApiError {
        if (base.isEmpty()) {
            throw new ApiError("还没填 WebUI 地址");
        }
        if (token.isEmpty()) {
            throw new ApiError("请填 WebUI Token（在服务器 webui.json 或启动日志里）");
        }
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("hash", sha256hex(token + ".napcat"));
        if (!totp.isEmpty()) {
            body.put("totpCode", totp);
        }
        Map<String, Object> resp;
        try {
            resp = rawCall("POST", "/api/auth/login", body, 15000, "");
        } catch (ApiError e) {
            throw e;
        }
        Map<String, Object> data = Json.obj(resp, "data");
        String cred = Json.str(data, "Credential", "");
        if (cred.isEmpty()) {
            if (Json.bool(data, "require2FA", false)) {
                throw new ApiError("这台 WebUI 开启了两步验证：请在「动态码」里填"
                        + "验证器 App 当前的 6 位数字");
            }
            throw new ApiError("登录响应里没有凭据");
        }
        credential = cred;
    }

    // ---------------------------------------------------------------- 端点

    /**
     * 当前登录状态。字段宽容解析：旧版 WebUI 没有 loginPhase 也能用。
     *
     * 必须剥掉 data 外壳再返回 —— 这几个接口的响应是
     * {"code":0,"data":{"qrcodeurl":…},"message":"success"}，
     * 真正的字段在 data 里面。曾经这里直接把整个外壳返回，调用方却按
     * 「qrcodeurl 就在顶层」去读，于是永远读到空串：**二维码一个都不显示**，
     * 而且 loginPhase/loginError 也一起丢了，界面只剩「等待扫码」四个字。
     * 单测当时用 data(...) 自己剥了一层，正好把这个错掩掉了（见回归测试）。
     */
    public Map<String, Object> checkLoginStatus() throws ApiError {
        return Json.obj(call("POST", "/api/QQLogin/CheckLoginStatus", null, 12000), "data");
    }

    /** 刷新二维码，返回新的 qrcodeurl（可能为空串 + restarting=true）。 */
    public Map<String, Object> refreshQrcode() throws ApiError {
        return Json.obj(call("POST", "/api/QQLogin/RefreshQRcode", null, 20000), "data");
    }

    /** 登录账号信息（uin / 昵称 / 头像 / online）。 */
    public Map<String, Object> loginInfo() throws ApiError {
        return Json.obj(call("POST", "/api/QQLogin/GetQQLoginInfo", null, 12000), "data");
    }

    /**
     * 快速登录列表：优先新版（带昵称），旧版没有这个接口时退回老接口（纯 uin 列表）。
     */
    public List<Object> quickList() throws ApiError {
        try {
            Map<String, Object> resp = call("POST", "/api/QQLogin/GetQuickLoginListNew",
                    null, 12000);
            List<Object> items = Json.arr(resp, "data");
            if (items != null && !items.isEmpty()) {
                return items;
            }
        } catch (ApiError ignored) {
        }
        Map<String, Object> resp = call("POST", "/api/QQLogin/GetQuickLoginList", null, 12000);
        List<Object> items = Json.arr(resp, "data");
        if (items == null) {
            return new java.util.ArrayList<Object>();
        }
        return items;
    }

    public void setQuickLogin(String uin) throws ApiError {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("uin", uin);
        call("POST", "/api/QQLogin/SetQuickLogin", body, 20000);
    }

    /**
     * 密码登录。返回 data（可能含 needCaptcha/needNewDevice 标记，见类注释）。
     */
    public Map<String, Object> passwordLogin(String uin, String password) throws ApiError {
        if (uin == null || uin.trim().isEmpty()) {
            throw new ApiError("请填 QQ 号");
        }
        if (password == null || password.isEmpty()) {
            throw new ApiError("请填密码");
        }
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("uin", uin.trim());
        body.put("passwordMd5", md5hex(password));
        // 同样要剥 data 外壳：needCaptcha / needNewDevice 这些标记都在 data 里。
        // data 为 null（登录请求已受理）时 Json.obj 回空 Map，调用方按「没有标记」处理。
        return Json.obj(call("POST", "/api/QQLogin/PasswordLogin", body, 30000), "data");
    }

    public void restartNapCat() throws ApiError {
        call("POST", "/api/QQLogin/RestartNapCat", null, 60000);
    }

    // ---------------------------------------------------------------- 底层

    /** 带自动重登的调用：凭据过期（Unauthorized）时重登一次再试一次。 */
    private Map<String, Object> call(String method, String path, Map<String, Object> body,
                                     int timeoutMs) throws ApiError {
        if (credential.isEmpty()) {
            login();
        }
        try {
            return rawCall(method, path, body, timeoutMs, credential);
        } catch (ApiError first) {
            if (!isUnauthorized(first)) {
                throw first;
            }
            credential = "";
            login();
            return rawCall(method, path, body, timeoutMs, credential);
        }
    }

    private static boolean isUnauthorized(ApiError e) {
        String m = e.getMessage();
        return m != null && m.contains("Unauthorized");
    }

    /** 发一个请求并解开 {code,message,data} 外壳；code!=0 → ApiError(message)。 */
    private Map<String, Object> rawCall(String method, String path, Map<String, Object> body,
                                        int timeoutMs, String bearer) throws ApiError {
        if (base.isEmpty()) {
            throw new ApiError("还没填 WebUI 地址");
        }
        // 官方前端所有请求都是带 JSON 的 POST；空请求体补 "{}"，避免 HttpURLConnection
        // 对无 DoOutput 的 POST 发出没有 Content-Length 的怪请求。
        String payload = body == null ? "{}" : Json.write(body);
        Resp r;
        try {
            r = transport.send(method, base + path, bearer, payload, timeoutMs);
        } catch (IOException e) {
            throw new ApiError("连不上 WebUI：" + friendly(e));
        }
        String text = r.body == null ? "" : r.body.trim();
        if (text.isEmpty()) {
            throw new ApiError("服务器回了空响应（HTTP " + r.code + "）");
        }
        if (text.startsWith("<")) {
            if (r.code == 404) {
                throw new ApiError("这个接口不存在（NapCat 版本可能较旧）");
            }
            throw new ApiError("服务器返回的是网页而不是数据，地址可能填错了");
        }
        Map<String, Object> obj;
        try {
            obj = Json.parseObject(text);
        } catch (Json.JsonError e) {
            throw new ApiError("响应看不懂：" + e.getMessage());
        }
        long code = codeOf(obj);
        String message = Json.str(obj, "message", "");
        if (code != 0) {
            throw new ApiError(message.isEmpty() ? "请求失败（HTTP " + r.code + "）" : message);
        }
        return obj;
    }

    private static long codeOf(Map<String, Object> obj) {
        Number n = (Number) obj.get("code");
        return n == null ? -1 : n.longValue();
    }

    static String friendly(IOException e) {
        String n = e.getClass().getSimpleName();
        if (n.contains("UnknownHost")) {
            return "地址解析不了，检查 WebUI 地址";
        }
        if (n.contains("SocketTimeout") || n.contains("ConnectTimeout")) {
            return "超时了，可能没连网或服务器忙";
        }
        if (n.contains("ConnectException")) {
            return "端口连不上，检查地址和端口";
        }
        String m = e.getMessage();
        return m == null || m.isEmpty() ? n : m;
    }

    // ---------------------------------------------------------------- 哈希

    public static String sha256hex(String s) {
        return digest("SHA-256", s);
    }

    public static String md5hex(String s) {
        return digest("MD5", s);
    }

    private static String digest(String algo, String s) {
        try {
            MessageDigest md = MessageDigest.getInstance(algo);
            byte[] out = md.digest(s.getBytes("UTF-8"));
            StringBuilder sb = new StringBuilder();
            for (byte b : out) {
                sb.append(Character.forDigit((b >> 4) & 0xF, 16));
                sb.append(Character.forDigit(b & 0xF, 16));
            }
            return sb.toString();
        } catch (java.security.GeneralSecurityException | java.io.UnsupportedEncodingException e) {
            throw new IllegalStateException(e);
        }
    }

    // ---------------------------------------------------------------- 真实网络

    public static final class Real implements Transport {
        @Override
        public Resp send(String method, String url, String bearer, String jsonBody,
                         int timeoutMs) throws IOException {
            HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
            try {
                conn.setRequestMethod(method);
                conn.setConnectTimeout(Math.min(timeoutMs, 15000));
                conn.setReadTimeout(timeoutMs);
                conn.setInstanceFollowRedirects(false);
                conn.setRequestProperty("Accept", "application/json");
                if (bearer != null && !bearer.isEmpty()) {
                    conn.setRequestProperty("Authorization", "Bearer " + bearer);
                }
                if (jsonBody != null) {
                    conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                    byte[] raw = jsonBody.getBytes("UTF-8");
                    conn.setFixedLengthStreamingMode(raw.length);
                    conn.setDoOutput(true);
                    OutputStream os = conn.getOutputStream();
                    try {
                        os.write(raw);
                        os.flush();
                    } finally {
                        os.close();
                    }
                }
                int code = conn.getResponseCode();
                String text = read(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
                return new Resp(code, text);
            } finally {
                conn.disconnect();
            }
        }

        private static String read(InputStream in) throws IOException {
            if (in == null) {
                return "";
            }
            try {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buf = new byte[4096];
                int n;
                while ((n = in.read(buf)) > 0 && out.size() < 2 * 1024 * 1024) {
                    out.write(buf, 0, n);
                }
                return new String(out.toByteArray(), "UTF-8");
            } finally {
                in.close();
            }
        }
    }
}
