package com.dafeiyu.controller;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 管理服务的客户端 —— App 与服务器之间唯一的业务通道。
 *
 * 所有请求都打到 127.0.0.1:<隧道本地端口>，也就是 SSH 隧道的那一头，
 * 公网上不存在这个服务。这样 App 里不需要写任何服务器公网端口，
 * 也不需要在服务器上开放管理端口。
 *
 * 认证是双层的：
 *   ① SSH 密钥（隧道本身）—— 证明「这个 App 被服务器允许连进来」；
 *   ② 管理口令（Bearer token）—— 证明「这次调用是管理服务发的」。
 * 口令由服务器生成，App 通过隧道读一次，只放在内存里。
 */
public final class ManagerClient {

    /** 管理服务的默认端口（服务器上只监听 127.0.0.1）。 */
    public static final int REMOTE_PORT = 6199;

    private final String base;      // 形如 http://127.0.0.1:38421
    private final String token;

    public ManagerClient(int localPort, String token) {
        this.base = "http://127.0.0.1:" + localPort;
        this.token = token == null ? "" : token;
    }

    // ------------------------------------------------------------ 基础请求

    private Map<String, Object> request(String method, String path, String body)
            throws Deployer.DeployException {
        return request(method, path, body, "");
    }

    /**
     * 发请求。unlockPassword 非空时放进 X-Dafeiyu-Unlock 头。
     *
     * ★ 为什么不放查询串：服务器（BaseHTTPRequestHandler）会把**整条请求行**
     * 写进系统日志，?password=xxx 就明文留在 journald 里了。
     * 走请求头只记录路径，密码不会落进日志。
     */
    private Map<String, Object> request(String method, String path, String body,
                                        String unlockPassword)
            throws Deployer.DeployException {
        HttpURLConnection c = null;
        try {
            URL u = new URL(base + path);
            c = (HttpURLConnection) u.openConnection();
            c.setRequestMethod(method);
            c.setConnectTimeout(10000);
            // 读超时给足：创建实例、重启容器都可能花十几秒
            c.setReadTimeout(120000);
            c.setRequestProperty("Authorization", "Bearer " + token);
            c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            if (unlockPassword != null && !unlockPassword.isEmpty()) {
                c.setRequestProperty("X-Dafeiyu-Unlock", unlockPassword);
            }
            if (body != null) {
                c.setDoOutput(true);
                byte[] raw = body.getBytes("UTF-8");
                c.setFixedLengthStreamingMode(raw.length);
                OutputStream os = c.getOutputStream();
                os.write(raw);
                os.close();
            }
            int code = c.getResponseCode();
            String text = readAll(code >= 400 ? c.getErrorStream() : c.getInputStream());

            Map<String, Object> obj;
            try {
                obj = Json.parseObject(text);
            } catch (Json.JsonError e) {
                // 隧道通了但对面不是管理服务（比如连到了别的程序）——说清楚
                throw new Deployer.DeployException(
                        "服务器的管理服务返回了看不懂的内容（HTTP " + code + "）。"
                                + "可能是这个 App 版本和服务器不匹配。");
            }
            if (code == 401) {
                throw new Deployer.DeployException("管理口令不对。请重新连接服务器。");
            }
            if (code >= 400) {
                String err = Json.str(obj, "error", "服务器报错（HTTP " + code + "）");
                throw new Deployer.DeployException(err);
            }
            return obj;
        } catch (Deployer.DeployException e) {
            throw e;
        } catch (java.io.IOException e) {
            throw new Deployer.DeployException(
                    "连不上服务器上的管理服务，隧道可能断了。请回到「服务器」页重新连接。");
        } finally {
            if (c != null) {
                c.disconnect();
            }
        }
    }

    private static String readAll(InputStream in) throws java.io.IOException {
        if (in == null) {
            return "";
        }
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) {
            bos.write(buf, 0, n);
        }
        in.close();
        return new String(bos.toByteArray(), "UTF-8");
    }

    // ------------------------------------------------------------ 接口

    /** 探活：确认隧道通、口令对。 */
    public void health() throws Deployer.DeployException {
        request("GET", "/health", null);
    }

    /** 实例列表。 */
    public List<Instance> list() throws Deployer.DeployException {
        Map<String, Object> r = request("GET", "/instances", null);
        List<Instance> out = new ArrayList<Instance>();
        for (Object o : Json.arr(r, "instances")) {
            out.add(Instance.from(o));
        }
        return out;
    }

    /** 单实例详情（含配置回读）。 */
    public Map<String, Object> detail(String name) throws Deployer.DeployException {
        return request("GET", "/instance/" + enc(name), null);
    }

    /**
     * 单实例详情（私密实例带解锁密码）。
     *
     * ★ 服务器**只认密码**，不认「App 说自己解锁过了」——
     * 所以每次读配置都要把密码带上，不能只靠解锁那一次的调用。
     */
    public Map<String, Object> detail(String name, String unlockPassword)
            throws Deployer.DeployException {
        return request("GET", "/instance/" + enc(name), null,
                unlockPassword == null ? "" : unlockPassword);
    }

    public Instance create(String name) throws Deployer.DeployException {
        Map<String, Object> b = new HashMap<String, Object>();
        b.put("name", name);
        Map<String, Object> r = request("POST", "/instance/create", Json.write(b));
        return Instance.from(Json.obj(r, "meta"));
    }

    public void start(String name) throws Deployer.DeployException {
        post("start", name);
    }

    public void stop(String name) throws Deployer.DeployException {
        post("stop", name);
    }

    public void destroy(String name) throws Deployer.DeployException {
        post("destroy", name);
    }

    private void post(String action, String name) throws Deployer.DeployException {
        Map<String, Object> b = new HashMap<String, Object>();
        b.put("name", name);
        request("POST", "/instance/" + action, Json.write(b));
    }

    /**
     * 写入三配置。返回服务器给的「改了哪几块」说明。
     *
     * apiKey 传空串表示「不改 API 那块」——这样用户可以只改群号或只改人格，
     * 不必每次都把 Key 重新输一遍（App 也不回显 Key）。
     */
    public List<String> applyConfig(String name, String groups, String friends,
                                    String apiBase, String apiKey, String apiModel,
                                    String persona) throws Deployer.DeployException {
        return applyConfig(name, groups, friends, apiBase, apiKey, apiModel,
                persona, "");
    }

    /**
     * 写入三配置（私密实例要带解锁密码）。
     *
     * lockPassword 是给「设了私密的机器人」用的：服务器那边没密码就不让改，
     * 免得锁只挡住了「看」却没挡住「改」。
     */
    public List<String> applyConfig(String name, String groups, String friends,
                                    String apiBase, String apiKey, String apiModel,
                                    String persona, String lockPassword)
            throws Deployer.DeployException {
        Map<String, Object> b = new HashMap<String, Object>();
        b.put("name", name);
        b.put("groups", groups == null ? "" : groups);
        b.put("friends", friends == null ? "" : friends);
        b.put("api_base", apiBase == null ? "" : apiBase);
        b.put("api_key", apiKey == null ? "" : apiKey);
        b.put("api_model", apiModel == null ? "" : apiModel);
        b.put("persona", persona == null ? "" : persona);
        if (lockPassword != null && !lockPassword.isEmpty()) {
            b.put("lock_password", lockPassword);
        }
        Map<String, Object> r = request("POST", "/instance/config", Json.write(b));
        List<String> out = new ArrayList<String>();
        for (Object o : Json.arr(r, "changed")) {
            out.add(String.valueOf(o));
        }
        return out;
    }

    /**
     * 设为私密 / 取消私密 / 改密码。
     *
     * 已经锁着的实例要改，必须给 oldPassword —— 否则任何拿到管理口令的人
     * 都能把别人的锁直接改掉，等于没锁。
     */
    public Map<String, Object> setLock(String name, boolean enabled, String password,
                                       String oldPassword)
            throws Deployer.DeployException {
        Map<String, Object> b = new HashMap<String, Object>();
        b.put("name", name);
        b.put("enabled", enabled);
        b.put("password", password == null ? "" : password);
        b.put("old_password", oldPassword == null ? "" : oldPassword);
        return request("POST", "/instance/lock", Json.write(b));
    }

    /** 凭密码解锁，拿回配置。密码不对会抛异常。 */
    public Map<String, Object> unlock(String name, String password)
            throws Deployer.DeployException {
        Map<String, Object> b = new HashMap<String, Object>();
        b.put("name", name);
        b.put("password", password == null ? "" : password);
        return request("POST", "/instance/unlock", Json.write(b));
    }

    /** 管理口令。RoutingTransport 构造代理时需要它。 */
    public String token() {
        return token;
    }

    /**
     * 测主聊天 API 通不通（**在服务器上**测）。
     *
     * 为什么不在手机上调这个 API：真正要用它的是服务器上的 AstrBot。
     * 手机能连通而服务器连不上（境外 API、服务器没网、DNS 不同）很常见，
     * 在手机上测会给出「通的」这个错误结论，用户就再也查不出机器人为什么不回话。
     *
     * 返回原始结果（reachable / auth_ok / model_ok / models / message），
     * 由界面决定怎么展示。
     */
    public Map<String, Object> testApi(String name, String apiBase, String apiKey,
                                       String apiModel) throws Deployer.DeployException {
        Map<String, Object> b = new HashMap<String, Object>();
        b.put("name", name);
        b.put("api_base", apiBase == null ? "" : apiBase);
        b.put("api_key", apiKey == null ? "" : apiKey);
        b.put("api_model", apiModel == null ? "" : apiModel);
        return request("POST", "/instance/api/test", Json.write(b));
    }

    /**
     * 拉取这个 API 支持的模型名列表。
     *
     * base/key 可以传空 —— 那时用实例里已保存的。也支持「还没保存就先看看
     * 有哪些模型」：用户常常是「先拿到模型名才敢保存」。
     */
    public List<String> listModels(String name, String apiBase, String apiKey)
            throws Deployer.DeployException {
        StringBuilder q = new StringBuilder("/instance/api/models?name=");
        q.append(enc(name));
        if (apiBase != null && !apiBase.isEmpty()) {
            q.append("&base=").append(enc(apiBase));
        }
        if (apiKey != null && !apiKey.isEmpty()) {
            q.append("&key=").append(enc(apiKey));
        }
        Map<String, Object> r = request("GET", q.toString(), null);
        List<String> out = new ArrayList<String>();
        for (Object o : Json.arr(r, "models")) {
            out.add(String.valueOf(o));
        }
        return out;
    }

    /** 读实例的 WebUI token（App 拿它去登 NapCat 网页）。 */
    public String webuiToken(String name) throws Deployer.DeployException {
        return webuiToken(name, "");
    }

    /**
     * 带解锁口令的版本。
     *
     * 私密机器人锁上之后，服务器对**没有口令**的详情请求只回 lock 状态、
     * 不回 webui_token（免得口令形同虚设）。所以登录私密机器人时必须把
     * 用户刚输的密码带上，否则这里会拿到空串，界面报「这个机器人还没跑起来，
     * 先回机器人页点启动」—— 明明跑着，提示却是错的，很难查。
     */
    public String webuiToken(String name, String unlockPassword)
            throws Deployer.DeployException {
        Map<String, Object> r = detail(name, unlockPassword);
        return Json.str(r, "webui_token", "");
    }

    /**
     * 把 WebUI 请求经管理服务转发。
     *
     * 路径形如 proxy/<实例名>/api/auth/login。这样 App 只需要一条隧道，
     * 不用为每个实例单独开端口转发（服务器上那个受限账号也只放行了 6199）。
     *
     * 注意认证头用 X-Dafeiyu-Token 而不是 Authorization：
     * NapCat 自己的凭据就放在 Authorization 里，我们要把它原样透传给 NapCat，
     * 所以管理口令必须走另一个头，否则两个令牌会互相覆盖。
     */
    public String proxy(String name, String webuiPath, String napcatBearer, String jsonBody)
            throws Deployer.DeployException {
        HttpURLConnection c = null;
        try {
            URL u = new URL(base + "/proxy/" + enc(name) + "/" + webuiPath);
            c = (HttpURLConnection) u.openConnection();
            c.setRequestMethod(jsonBody == null ? "GET" : "POST");
            c.setConnectTimeout(10000);
            c.setReadTimeout(30000);
            c.setRequestProperty("X-Dafeiyu-Token", token);
            c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            if (napcatBearer != null && !napcatBearer.isEmpty()) {
                // 这个头会被服务器原样转给 NapCat
                c.setRequestProperty("Authorization", "Bearer " + napcatBearer);
            }
            if (jsonBody != null) {
                c.setDoOutput(true);
                byte[] raw = jsonBody.getBytes("UTF-8");
                c.setFixedLengthStreamingMode(raw.length);
                OutputStream os = c.getOutputStream();
                os.write(raw);
                os.close();
            }
            int code = c.getResponseCode();
            String text = readAll(code >= 400 ? c.getErrorStream() : c.getInputStream());
            if (code >= 400 && text.isEmpty()) {
                throw new Deployer.DeployException("NapCat 返回 HTTP " + code);
            }
            return text;
        } catch (Deployer.DeployException e) {
            throw e;
        } catch (java.io.IOException e) {
            throw new Deployer.DeployException("访问实例的 WebUI 失败，隧道或容器可能断了。");
        } finally {
            if (c != null) {
                c.disconnect();
            }
        }
    }

    /** 便捷版：不带 NapCat 凭据。 */
    public String proxy(String name, String webuiPath, String jsonBody)
            throws Deployer.DeployException {
        return proxy(name, webuiPath, null, jsonBody);
    }

    private static String enc(String s) {
        try {
            return java.net.URLEncoder.encode(s == null ? "" : s, "UTF-8");
        } catch (java.io.UnsupportedEncodingException e) {
            return s == null ? "" : s;
        }
    }

    // ------------------------------------------------------------ 数据结构

    /** 一个实例（一套 QQ 号 + 机器人的组合）。 */
    public static final class Instance {
        public final String name;
        public final int webuiPort;
        public final int onebotPort;
        public final int panelPort;
        public final String napcat;   // 容器实际状态
        public final String astrbot;
        /** 是否设成了私密（要看/要改都得先输密码）。 */
        public final boolean locked;

        private Instance(String name, int webuiPort, int onebotPort, int panelPort,
                         String napcat, String astrbot, boolean locked) {
            this.name = name;
            this.webuiPort = webuiPort;
            this.onebotPort = onebotPort;
            this.panelPort = panelPort;
            this.napcat = napcat;
            this.astrbot = astrbot;
            this.locked = locked;
        }

        public static Instance from(Object node) {
            Map<String, Object> m = node instanceof Map
                    ? castMap(node) : new HashMap<String, Object>();
            Map<String, Object> cs = Json.obj(m, "containers");
            Map<String, Object> lk = Json.obj(m, "lock");
            return new Instance(
                    Json.str(m, "name", ""),
                    (int) numOr(Json.lng(m, "webui_port"), 0),
                    (int) numOr(Json.lng(m, "onebot_port"), 0),
                    (int) numOr(Json.lng(m, "panel_port"), 0),
                    cs == null ? "absent" : Json.str(cs, "napcat", "absent"),
                    cs == null ? "absent" : Json.str(cs, "astrbot", "absent"),
                    lk != null && Json.bool(lk, "locked", false));
        }

        @SuppressWarnings("unchecked")
        private static Map<String, Object> castMap(Object o) {
            return (Map<String, Object>) o;
        }

        private static long numOr(Long v, long dflt) {
            return v == null ? dflt : v.longValue();
        }

        /** 机器人是否真的在跑（两个容器都 running）。 */
        public boolean running() {
            return "running".equals(napcat) && "running".equals(astrbot);
        }

        /** 给界面看的短状态。 */
        public String stateText() {
            if (running()) {
                return "运行中";
            }
            if ("absent".equals(napcat) && "absent".equals(astrbot)) {
                return "未启动";
            }
            if ("exited".equals(napcat) && "exited".equals(astrbot)) {
                return "已停止";
            }
            return "启动中 / 异常（napcat=" + napcat + ", astrbot=" + astrbot + "）";
        }
    }
}
