package tests;

import com.dafeiyu.console.Api;
import com.dafeiyu.console.Json;
import com.dafeiyu.console.KnobModel;
import com.dafeiyu.console.Ui;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Api 的测试 —— 用假 Transport 顶掉真网络。
 *
 * 这是整个测试里最值钱的一部分：它验证「服务器给出各种奇怪回应时，App 会不会
 * 误判成功」。最危险的一种是**服务端补丁没装上、请求被页面路由接走，回了一个
 * HTML 登录页**：如果不检查就当成功，用户会看到一个空界面还以为是自己网差。
 */
public final class ApiTest {

    /** 假服务器：按脚本回应，并记录收到的请求。 */
    static final class Fake implements Api.Transport {
        final List<String> seen = new ArrayList<String>();
        final List<String> bodies = new ArrayList<String>();
        int code = 200;
        String body = "{}";
        String setCookie;
        IOException boom;

        @Override
        public Api.Resp send(String method, String url, String cookie, String body,
                             int timeout) throws IOException {
            seen.add(method + " " + url + " cookie=[" + (cookie == null ? "" : cookie)
                    + "] timeout=" + timeout);
            bodies.add(body == null ? "" : body);
            if (boom != null) {
                throw boom;
            }
            return new Api.Resp(code, this.body, setCookie);
        }

        String lastBody() {
            return bodies.isEmpty() ? "" : bodies.get(bodies.size() - 1);
        }

        String lastReq() {
            return seen.isEmpty() ? "" : seen.get(seen.size() - 1);
        }
    }

    public static void run() {
        T.group("Api：地址规范化");
        T.eq("补协议和默认端口", "http://your-server.example.com:8088",
                new Api("your-server.example.com", "").base());
        T.eq("带端口就不改", "http://your-server.example.com:8088",
                new Api("your-server.example.com:8088", "").base());
        T.eq("带协议保留", "http://1.2.3.4:9000",
                new Api("http://1.2.3.4:9000", "").base());
        T.eq("去掉尾斜杠", "http://1.2.3.4:8088",
                new Api("http://1.2.3.4:8088/", "").base());
        T.eq("去掉多余空格", "http://1.2.3.4:8088",
                new Api("  1.2.3.4  ", "").base());
        T.eq("https 保留（虽然服务端没有）", "https://x.com:8088",
                new Api("https://x.com:8088", "").base());
        T.eq("空地址就是空", "", new Api("", "").base());

        T.group("Api：cookie 提取");
        T.eq("标准 Set-Cookie", "dsh_qr=abc.def",
                Api.extractCookie("dsh_qr=abc.def; Path=/; HttpOnly; Max-Age=604800"));
        T.eq("有别的 cookie 也能挑对", "dsh_qr=xyz",
                Api.extractCookie("other=1; dsh_qr=xyz; Path=/"));
        T.eq("没有目标 cookie 回 null", null,
                Api.extractCookie("other=1; Path=/"));
        T.eq("null 输入不崩", null, Api.extractCookie(null));

        T.group("Api：登录");
        Fake f = new Fake();
        f.setCookie = "dsh_qr=tok123; Path=/; HttpOnly; Max-Age=604800";
        Api api = new Api("1.2.3.4", "", f);
        try {
            api.login("好密码");
            T.eq("拿到 cookie", "dsh_qr=tok123", api.cookie());
            T.contains("打到 /login", f.lastReq(), "POST http://1.2.3.4:8088/login");
            T.contains("表单编码提交", f.lastBody(), "password=");
            T.notContains("密码不出现在 URL", f.lastReq(), "好密码");
        } catch (Api.ApiException e) {
            T.bad("正常登录失败", e.getMessage());
        }

        T.group("Api：登录失败的各种情形");
        f = new Fake();
        f.setCookie = null;
        f.body = "<html>密码不对</html>";
        final Api wrong = new Api("1.2.3.4", "", f);
        try {
            wrong.login("坏密码");
            T.bad("密码错该报错", "却成功了");
        } catch (Api.ApiException e) {
            T.contains("说密码不对", e.getMessage(), "密码");
            T.eq("空 cookie", "", wrong.cookie());
        }

        f = new Fake();
        f.code = 429;
        f.body = "{\"error\":\"试太多次了\"}";
        try {
            new Api("1.2.3.4", "", f).login("x");
            T.bad("被锁该报错", "却成功了");
        } catch (Api.ApiException e) {
            T.contains("解释锁定与翻倍", e.getMessage(), "锁");
            T.eq("保留 429", 429, e.code);
        }

        // 登出时服务端也回 Set-Cookie，但是 Max-Age=0 的空 cookie。
        // 把它当成登录成功会得到一个立刻失效的凭据，症状是「登录后马上又被踢」。
        f = new Fake();
        f.setCookie = "dsh_qr=; Path=/; Max-Age=0";
        try {
            new Api("1.2.3.4", "", f).login("x");
            T.bad("Max-Age=0 该当失败", "却成功了");
        } catch (Api.ApiException e) {
            T.ok("Max-Age=0 的空 cookie 不算登录成功");
        }

        try {
            new Api("1.2.3.4", "", new Fake()).login("");
            T.bad("空密码该被本地拦住", "却发出去了");
        } catch (Api.ApiException e) {
            T.contains("本地就拦住空密码", e.getMessage(), "空");
        }

        f = new Fake();
        f.boom = new java.net.SocketTimeoutException("timed out");
        try {
            new Api("1.2.3.4", "", f).login("x");
            T.bad("超时该报错", "却成功了");
        } catch (Api.ApiException e) {
            T.contains("超时说人话", e.getMessage(), "超时");
        }

        T.group("Api：读端点");
        f = new Fake();
        f.body = "{\"qq\":{\"state\":\"online\"}}";
        api = new Api("1.2.3.4", "dsh_qr=tok", f);
        try {
            Map<String, Object> st = api.status();
            T.eq("解析出内容", "online", Json.str(Json.raw(st, "qq"), "state"));
            T.contains("带上 cookie", f.lastReq(), "cookie=[dsh_qr=tok]");
            T.contains("打到 /api/console/status", f.lastReq(), "/api/console/status");
        } catch (Api.ApiException e) {
            T.bad("读状态失败", e.getMessage());
        }

        T.group("Api：写端点的请求体");
        f = new Fake();
        f.body = "{\"ok\":true,\"changed\":[\"a\"]}";
        api = new Api("1.2.3.4", "dsh_qr=tok", f);
        try {
            Map<String, Object> vals = new LinkedHashMap<String, Object>();
            vals.put("provider_settings.max_context_length", Long.valueOf(40));
            api.setKnobs(vals);
            T.contains("包成 values", f.lastBody(), "\"values\"");
            T.contains("路径带上了", f.lastBody(), "max_context_length");
            T.contains("是 POST", f.lastReq(), "POST");

            api.setChatModel("glm-5.3");
            T.contains("模型放 chat 字段", f.lastBody(), "\"chat\":\"glm-5.3\"");

            api.setVision("zhipu-vision");
            T.contains("识图放 vision 字段", f.lastBody(),
                    "\"vision\":\"zhipu-vision\"");

            api.applyMode("quiet");
            T.contains("模式放 id", f.lastBody(), "\"id\":\"quiet\"");

            api.setPlugin("dsh-welcome", true);
            T.contains("插件名", f.lastBody(), "dsh-welcome");
            T.contains("开关是布尔", f.lastBody(), "\"enabled\":true");

            api.action("restart_bot");
            T.contains("动作放 id", f.lastBody(), "\"id\":\"restart_bot\"");
        } catch (Api.ApiException e) {
            T.bad("写操作失败", e.getMessage());
        }

        T.group("Api：写操作超时要比读长得多");
        // 重启容器要几十秒，用读的 12 秒超时会在服务端还在干活时就报错，
        // 用户看到「超时」以为没生效，再点一次 —— 于是重启两遍。
        f = new Fake();
        api = new Api("1.2.3.4", "c", f);
        try {
            api.status();
            String readReq = f.lastReq();
            api.action("restart_bot");
            String writeReq = f.lastReq();
            int readT = timeoutOf(readReq);
            int writeT = timeoutOf(writeReq);
            T.isTrue("读超时 ≤ 15 秒（" + readT + "ms）", readT <= 15000);
            T.isTrue("动作超时 ≥ 100 秒（" + writeT + "ms）", writeT >= 100000);
        } catch (Api.ApiException e) {
            T.bad("超时对比失败", e.getMessage());
        }

        T.group("Api：错误响应");
        checkErr("401 当掉登录", 401, "{\"error\":\"未登录\"}", 401, "登录");
        checkErr("403 也当掉登录", 403, "{\"error\":\"拒绝\"}", 401, "登录");
        checkErr("503 说后端没装载", 503,
                "{\"error\":\"console_api 导入失败\"}", 503, "console_api");
        checkErr("409 原样透出", 409,
                "{\"error\":\"配置被别人改过，请刷新\"}", 409, "刷新");
        checkErr("400 原样透出", 400,
                "{\"error\":\"插话概率不能大于 1\"}", 400, "不能大于");
        checkErr("500 有兜底话", 500, "{}", 500, "");

        T.group("Api：畸形响应必须报错，不许当成功");
        // 这条最重要：补丁没装上时，/api/console/* 会被页面路由接走回 HTML。
        f = new Fake();
        f.body = "<!DOCTYPE html><html><body>请先登录</body></html>";
        try {
            new Api("1.2.3.4", "c", f).status();
            T.bad("HTML 响应该报错", "却当成功了");
        } catch (Api.ApiException e) {
            T.contains("点明补丁没装上", e.getMessage(), "补丁");
        }

        f = new Fake();
        f.body = "";
        try {
            new Api("1.2.3.4", "c", f).status();
            T.bad("空响应该报错", "却当成功了");
        } catch (Api.ApiException e) {
            T.contains("说空响应", e.getMessage(), "空");
        }

        f = new Fake();
        f.body = "{\"qq\":{\"state\":\"onl";
        try {
            new Api("1.2.3.4", "c", f).status();
            T.bad("截断的 JSON 该报错", "却当成功了");
        } catch (Api.ApiException e) {
            T.contains("说看不懂", e.getMessage(), "看不懂");
        }

        f = new Fake();
        f.body = "[1,2,3]";
        try {
            new Api("1.2.3.4", "c", f).status();
            T.bad("数组顶层该报错", "却当成功了");
        } catch (Api.ApiException e) {
            T.ok("数组顶层被拒");
        }

        T.group("Api：没设地址时本地就拦住");
        try {
            new Api("", "c", new Fake()).status();
            T.bad("空地址该被拦", "却发出去了");
        } catch (Api.ApiException e) {
            T.contains("提示先设地址", e.getMessage(), "地址");
        }

        T.group("Api：模式结果解读");
        Map<String, Object> allOk = T.map("ok", Boolean.TRUE, "name", "安静",
                "steps", T.list(
                        T.map("step", "配置", "ok", Boolean.TRUE,
                                "changed", Long.valueOf(3)),
                        T.map("step", "插件", "ok", Boolean.TRUE,
                                "changed", Long.valueOf(1))));
        String msg = Api.describeMode(allOk);
        T.contains("说切成了", msg, "已切到");
        T.contains("说改了几项", msg, "4 项");

        Map<String, Object> partial = T.map("ok", Boolean.FALSE, "name", "省钱",
                "steps", T.list(
                        T.map("step", "配置", "ok", Boolean.TRUE,
                                "changed", Long.valueOf(2)),
                        T.map("step", "换模型", "ok", Boolean.FALSE,
                                "error", "渠道方 502")));
        msg = Api.describeMode(partial);
        T.contains("说只切了一部分", msg, "一部分");
        T.contains("点名失败的步骤", msg, "换模型");
        T.contains("带上原因", msg, "502");
        T.notContains("不能只说失败了完事", msg, "已切到");

        T.group("Api：错误话术（Ui.explain）");
        T.contains("401", Ui.explain(new Api.ApiException("x", 401)), "重新输");
        T.contains("409 教人怎么办", Ui.explain(new Api.ApiException("x", 409)),
                "下拉刷新");
        T.contains("503 点明后端", Ui.explain(new Api.ApiException("没装载", 503)),
                "控制台后端");
        T.contains("超时说可能原因",
                Ui.explain(new java.net.SocketTimeoutException("t")), "超时");
        T.contains("找不到主机", Ui.explain(new java.net.UnknownHostException("h")),
                "IP");
        T.contains("连不上", Ui.explain(new java.net.ConnectException("c")), "8088");
        T.contains("null 不崩", Ui.explain(null), "不知道");
    }

    private static int timeoutOf(String req) {
        int i = req.indexOf("timeout=");
        return i < 0 ? -1 : Integer.parseInt(req.substring(i + 8).trim());
    }

    private static void checkErr(String what, int code, String body,
                                 int wantCode, String needle) {
        Fake f = new Fake();
        f.code = code;
        f.body = body;
        try {
            new Api("1.2.3.4", "c", f).status();
            T.bad(what, "没报错");
        } catch (Api.ApiException e) {
            boolean codeOk = e.code == wantCode;
            boolean msgOk = needle.isEmpty()
                    || (e.getMessage() != null && e.getMessage().contains(needle));
            if (codeOk && msgOk) {
                T.ok(what);
            } else {
                T.bad(what, "code=" + e.code + " msg=" + e.getMessage());
            }
        }
    }
}
