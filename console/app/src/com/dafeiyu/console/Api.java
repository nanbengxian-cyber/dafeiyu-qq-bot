package com.dafeiyu.console;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 和服务器说话的那一层。刻意不引用任何 android.* —— 这样能在普通 JVM 上
 * 起一个假服务器把它测穿（登录、cookie、错误分支都测得到）。
 *
 * 协议是 8088 那套现成的：
 *   1. POST /login  body: password=xxx    → 302/303 + Set-Cookie: dsh_qr=...
 *   2. 之后每个请求带上这个 Cookie
 *   3. cookie 无效时控制台端点回 **401 JSON**（页面路由是 303 跳登录页，
 *      但 /api/console/* 特意改成 401，因为跟着 303 会拿到一页 HTML 却以为成功）
 *
 * 三个刻意的选择：
 *   · {@code setInstanceFollowRedirects(false)} —— 必须自己看 302，否则
 *     HttpURLConnection 会跟过去把 Set-Cookie 丢掉；
 *   · 超时分两档：状态/配置读 12 秒，写和动作 100 秒（重启容器真的要几十秒）；
 *   · 错误响应也读 body。服务端把原因放在 {"error": "..."} 里，不读就只剩
 *     一个「HTTP 400」，等于没有任何线索。
 */
public class Api {

    /** 服务器地址，例如 http://your-server.example.com:8088 */
    private String base;
    private String cookie;
    private final Transport transport;

    /** 抽出来是为了单测能塞一个假的；生产用 Real。 */
    public interface Transport {
        Resp send(String method, String url, String cookie, String body, int timeoutMs)
                throws IOException;
    }

    public static final class Resp {
        public final int code;
        public final String body;
        public final String setCookie;

        public Resp(int code, String body, String setCookie) {
            this.code = code;
            this.body = body;
            this.setCookie = setCookie;
        }
    }

    /** 业务异常：message 直接显示给人看，所以都是中文人话。 */
    public static class ApiException extends Exception {
        public final int code;

        public ApiException(String msg, int code) {
            super(msg);
            this.code = code;
        }
    }

    public Api(String base, String cookie) {
        this(base, cookie, new Real());
    }

    public Api(String base, String cookie, Transport transport) {
        this.base = normalize(base);
        this.cookie = cookie == null ? "" : cookie;
        this.transport = transport;
    }

    /** 用户只会输 IP 或 IP:端口，帮他补全，末尾斜杠也去掉。 */
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
        // 没写端口就补 8088（安全组只放行了这个口）
        String rest = s.substring(s.indexOf("://") + 3);
        if (!rest.contains(":") && !s.startsWith("https://")) {
            s = s + ":8088";
        }
        return s;
    }

    public String base() {
        return base;
    }

    public String cookie() {
        return cookie;
    }

    public boolean hasCookie() {
        return cookie != null && !cookie.isEmpty();
    }

    // ---------------------------------------------------------------- 登录

    /**
     * 登录换 cookie。成功后 cookie 存在本实例里，调用方负责持久化。
     *
     * 服务端有 IP 指数退避锁定（连错 5 次锁 30 秒起、翻倍到 1 小时），
     * 所以密码错了要把「等多久」讲清楚，否则用户会一直点，越点锁越久。
     */
    public void login(String password) throws ApiException {
        if (password == null || password.isEmpty()) {
            throw new ApiException("密码不能填空", 0);
        }
        String body = "password=" + urlEncode(password);
        Resp r;
        try {
            r = transport.send("POST", base + "/login", "", body, 15000);
        } catch (IOException e) {
            throw new ApiException("连不上服务器：" + friendly(e), 0);
        }
        if (r.setCookie != null && !r.setCookie.isEmpty()) {
            String c = extractCookie(r.setCookie);
            if (c != null && !c.isEmpty()) {
                // Max-Age=0 是登出用的空 cookie，别当成登录成功
                if (!r.setCookie.contains("Max-Age=0")) {
                    cookie = c;
                    return;
                }
            }
        }
        if (r.code == 429 || (r.body != null && r.body.contains("试太多次"))) {
            throw new ApiException("密码错太多次被锁了，等一会再试（每次翻倍，最多 1 小时）", r.code);
        }
        if (r.code == 200 && r.body != null && r.body.contains("密码不对")) {
            throw new ApiException("密码不对", 401);
        }
        throw new ApiException("登录失败（HTTP " + r.code + "），密码可能不对", r.code);
    }

    /** 从 Set-Cookie 里抠出 {@code dsh_qr=值}。 */
    public static String extractCookie(String setCookie) {
        if (setCookie == null) {
            return null;
        }
        for (String part : setCookie.split(";")) {
            String p = part.trim();
            if (p.startsWith("dsh_qr=")) {
                return p;
            }
        }
        return null;
    }

    // ---------------------------------------------------------------- 端点

    public Map<String, Object> status() throws ApiException {
        return get("/api/console/status", 12000);
    }

    public Map<String, Object> schema() throws ApiException {
        return get("/api/console/schema", 45000);
    }

    public Map<String, Object> config() throws ApiException {
        return get("/api/console/config", 12000);
    }

    public Map<String, Object> setKnobs(Map<String, Object> values) throws ApiException {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("values", values);
        return post("/api/console/config", body, 60000);
    }

    public Map<String, Object> setChatModel(String model) throws ApiException {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("chat", model);
        return post("/api/console/model", body, 60000);
    }

    public Map<String, Object> setVision(String providerId) throws ApiException {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("vision", providerId);
        return post("/api/console/model", body, 60000);
    }

    public Map<String, Object> applyMode(String id) throws ApiException {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("id", id);
        return post("/api/console/mode", body, 100000);
    }

    public Map<String, Object> setPlugin(String name, boolean enabled) throws ApiException {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("name", name);
        body.put("enabled", Boolean.valueOf(enabled));
        return post("/api/console/plugin", body, 60000);
    }

    public Map<String, Object> action(String id) throws ApiException {
        Map<String, Object> body = new LinkedHashMap<String, Object>();
        body.put("id", id);
        return post("/api/console/action", body, 200000);
    }

    // ---------------------------------------------------------------- 底层

    private Map<String, Object> get(String path, int timeout) throws ApiException {
        return call("GET", path, null, timeout);
    }

    private Map<String, Object> post(String path, Map<String, Object> body, int timeout)
            throws ApiException {
        return call("POST", path, Json.write(body), timeout);
    }

    private Map<String, Object> call(String method, String path, String body, int timeout)
            throws ApiException {
        if (base.isEmpty()) {
            throw new ApiException("还没设置服务器地址", 0);
        }
        Resp r;
        try {
            r = transport.send(method, base + path, cookie, body, timeout);
        } catch (IOException e) {
            throw new ApiException("连不上服务器：" + friendly(e), 0);
        }
        if (r.code == 401 || r.code == 403) {
            // 403 也算掉登录：cookie 过期（7 天）和被拒的表现对用户没区别
            throw new ApiException("登录过期了，请重新输密码", 401);
        }
        if (r.code == 503) {
            throw new ApiException(errorOf(r.body, "服务器上的控制台后端没起来"), 503);
        }
        String text = r.body == null ? "" : r.body.trim();
        if (text.isEmpty()) {
            throw new ApiException("服务器回了空响应（HTTP " + r.code + "）", r.code);
        }
        if (text.startsWith("<")) {
            // 回了 HTML 说明请求被页面路由接走了 —— 服务端补丁没生效
            throw new ApiException("服务器返回的是网页而不是数据，控制台补丁可能没装上", r.code);
        }
        Map<String, Object> obj;
        try {
            obj = Json.parseObject(text);
        } catch (Json.JsonError e) {
            throw new ApiException("响应看不懂：" + e.getMessage(), r.code);
        }
        if (r.code >= 400) {
            throw new ApiException(errorOf(text, "服务器拒绝了（HTTP " + r.code + "）"), r.code);
        }
        return obj;
    }

    private static String errorOf(String body, String dflt) {
        if (body == null) {
            return dflt;
        }
        try {
            Map<String, Object> o = Json.parseObject(body.trim());
            String e = Json.str(o, "error", "");
            return e.isEmpty() ? dflt : e;
        } catch (Json.JsonError ex) {
            return dflt;
        }
    }

    /** 网络异常的原始 message 对普通人毫无意义，翻译成能行动的说法。 */
    static String friendly(IOException e) {
        String n = e.getClass().getSimpleName();
        if (n.contains("UnknownHost")) {
            return "地址解析不了，检查服务器地址";
        }
        if (n.contains("SocketTimeout") || n.contains("ConnectTimeout")) {
            return "超时了，可能没连网或服务器忙";
        }
        if (n.contains("ConnectException")) {
            return "端口连不上，检查 8088 是否在跑";
        }
        String m = e.getMessage();
        return m == null || m.isEmpty() ? n : m;
    }

    static String urlEncode(String s) {
        try {
            return URLEncoder.encode(s, "UTF-8");
        } catch (java.io.UnsupportedEncodingException e) {
            return s;   // UTF-8 一定存在，走不到这
        }
    }

    /** 真实网络实现。 */
    public static final class Real implements Transport {
        @Override
        public Resp send(String method, String url, String cookie, String body, int timeoutMs)
                throws IOException {
            HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
            try {
                conn.setRequestMethod(method);
                conn.setConnectTimeout(Math.min(timeoutMs, 15000));
                conn.setReadTimeout(timeoutMs);
                // 必须自己处理跳转，否则 302 会被跟掉、Set-Cookie 拿不到
                conn.setInstanceFollowRedirects(false);
                conn.setRequestProperty("Accept", "application/json");
                if (cookie != null && !cookie.isEmpty()) {
                    conn.setRequestProperty("Cookie", cookie);
                }
                if (body != null) {
                    boolean form = url.endsWith("/login");
                    conn.setRequestProperty("Content-Type", form
                            ? "application/x-www-form-urlencoded"
                            : "application/json; charset=utf-8");
                    byte[] raw = body.getBytes("UTF-8");
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
                String sc = conn.getHeaderField("Set-Cookie");
                return new Resp(code, text, sc);
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
                // 上限 2MB：正常响应几十 KB，设个盖子防手机被打爆
                while ((n = in.read(buf)) > 0 && out.size() < 2 * 1024 * 1024) {
                    out.write(buf, 0, n);
                }
                return new String(out.toByteArray(), "UTF-8");
            } finally {
                in.close();
            }
        }
    }

    // ---------------------------------------------------------------- 结果解读

    /**
     * 把一键模式的返回整理成一句话。
     * 部分失败必须说清是哪一步 —— 只说「失败」会让人重复点，越点越乱。
     */
    public static String describeMode(Map<String, Object> out) {
        String name = Json.str(out, "name", Json.str(out, "mode", "模式"));
        boolean ok = Json.bool(out, "ok", false);
        List<Object> steps = Json.arr(out, "steps");
        if (ok) {
            int changed = 0;
            for (Object s : steps) {
                Long c = Json.lng(s, "changed");
                if (c != null) {
                    changed += c.intValue();
                }
            }
            return "已切到「" + name + "」" + (changed > 0 ? "，改了 " + changed + " 项" : "");
        }
        StringBuilder sb = new StringBuilder("「" + name + "」只切了一部分：");
        boolean first = true;
        for (Object s : steps) {
            if (!Json.bool(s, "ok", true)) {
                if (!first) {
                    sb.append("；");
                }
                first = false;
                sb.append(Json.str(s, "step")).append(" 失败（")
                        .append(Json.str(s, "error", "原因不明")).append("）");
            }
        }
        return sb.toString();
    }
}
