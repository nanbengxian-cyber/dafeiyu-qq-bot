package tests;

import com.dafeiyu.controller.ApiGuide;
import com.dafeiyu.controller.WebProxyPath;

import java.util.List;

/**
 * WebView 请求改写（WebProxyPath）+ API 申请引导（ApiGuide）的测试。
 *
 * 这两块都出过「症状离原因很远」的问题，所以专门钉住：
 *
 * ① WebProxyPath：错了的表现是**网页白屏 / 一直转圈**。
 *    NapCat 的网页是 React 应用，用绝对路径引资源（/webui/assets/x.js）。
 *    经隧道时这些请求会打到管理服务的 /webui/... 而不是 /proxy/<实例>/webui/...，
 *    结果 HTML 出来了、JS 和 CSS 全 404 —— 用户看到的就是「网页老是连不上」。
 *    从现象几乎不可能反推出「少了个前缀」，所以必须用测试锁死。
 *
 * ② ApiGuide：里面是**要发给用户照着做的地址**。写错一个字母，
 *    用户就白跑一趟（注册、实名、充值，然后发现页面打不开）。
 *    这里断言结构完整性（每家的地址都齐、格式对），
 *    真实可达性由 test/verify-api-guide.sh 用 curl 实测。
 */
public final class WebProxyPathTest {

    public static void run() {
        T.group("WebView 请求改写（修「网页老是连不上」）");

        // ── 核心：绝对路径必须补上代理前缀 ──────────────────────────────
        T.eq("★ 绝对路径资源补上 /proxy/<实例>/ 前缀",
                "http://127.0.0.1:41000/proxy/qq1/webui/assets/index-abc.js",
                WebProxyPath.proxyUrlFor(41000, "qq1",
                        "http://127.0.0.1:41000/webui/assets/index-abc.js"));

        T.eq("★ 页面里的 API 请求同样补前缀",
                "http://127.0.0.1:41000/proxy/qq1/api/auth/login",
                WebProxyPath.proxyUrlFor(41000, "qq1",
                        "http://127.0.0.1:41000/api/auth/login"));

        // 不带端口的地址（相对地址被 WebView 解析后可能长这样）
        T.eq("不带端口的主机也能处理",
                "http://127.0.0.1:41000/proxy/qq1/webui/favicon.ico",
                WebProxyPath.proxyUrlFor(41000, "qq1", "http://localhost/webui/favicon.ico"));

        // ── 查询串必须原样保留（NapCat 有接口靠它传参）─────────────────
        T.eq("★ 查询串原样保留",
                "http://127.0.0.1:41000/proxy/qq1/api/x?a=1&b=2",
                WebProxyPath.proxyUrlFor(41000, "qq1",
                        "http://127.0.0.1:41000/api/x?a=1&b=2"));

        // ── 只有主机名（根路径）────────────────────────────────────────
        T.eq("只有主机名 → 补成根路径",
                "http://127.0.0.1:41000/proxy/qq1/",
                WebProxyPath.proxyUrlFor(41000, "qq1", "http://127.0.0.1:41000"));
        T.eq("主机名带查询串",
                "http://127.0.0.1:41000/proxy/qq1/?t=1",
                WebProxyPath.proxyUrlFor(41000, "qq1", "http://127.0.0.1:41000?t=1"));

        // ── 不能重复套前缀（否则 /proxy/x/proxy/x/... 无限套）───────────
        T.isNull("★ 已经是代理地址就不再套一层（防无限嵌套）",
                WebProxyPath.proxyUrlFor(41000, "qq1",
                        "http://127.0.0.1:41000/proxy/qq1/webui/x.js"));

        // ── 非 HTTP 地址交给 WebView（别去代理 data:/blob:）─────────────
        T.isNull("data: 地址不处理", WebProxyPath.proxyUrlFor(41000, "qq1",
                "data:image/png;base64,iVBORw0KGgo="));
        T.isNull("blob: 地址不处理", WebProxyPath.proxyUrlFor(41000, "qq1",
                "blob:http://127.0.0.1:41000/abc-123"));
        T.isNull("about:blank 不处理", WebProxyPath.proxyUrlFor(41000, "qq1",
                "about:blank"));
        T.isNull("javascript: 不处理", WebProxyPath.proxyUrlFor(41000, "qq1",
                "javascript:void(0)"));
        T.isNull("相对地址（不以 / 开头）不处理", WebProxyPath.proxyUrlFor(41000, "qq1",
                "foo.js"));

        // ── 直连模式（没有隧道）必须完全不改 ────────────────────────────
        T.isNull("★ 隧道端口为 0（直连模式）时不改写",
                WebProxyPath.proxyUrlFor(0, "qq1", "http://1.2.3.4:6099/webui/x.js"));
        T.isNull("实例名为空时不改写",
                WebProxyPath.proxyUrlFor(41000, "", "http://1.2.3.4:6099/webui/x.js"));
        T.isNull("实例名为 null 时不改写",
                WebProxyPath.proxyUrlFor(41000, null, "http://1.2.3.4:6099/x"));
        T.isNull("url 为 null 时不改写", WebProxyPath.proxyUrlFor(41000, "qq1", null));

        // ── 实例名要转义（虽然校验只允许 [a-z0-9-]，但别依赖上游）───────
        T.eq("实例名做 URL 转义",
                "http://127.0.0.1:41000/proxy/a%20b/x",
                WebProxyPath.proxyUrlFor(41000, "a b", "http://h/x"));

        withToken();

        // ── pathAndQuery 单独测（它是核心，且被两处用到）────────────────
        T.eq("pathAndQuery：完整地址", "/a/b?c=1",
                WebProxyPath.pathAndQuery("https://h:1/a/b?c=1"));
        T.eq("pathAndQuery：https 也认", "/x",
                WebProxyPath.pathAndQuery("https://h/x"));
        T.eq("pathAndQuery：无路径 → /",
                "/", WebProxyPath.pathAndQuery("http://h:1234"));
        T.isNull("pathAndQuery：null", WebProxyPath.pathAndQuery(null));
        T.isNull("pathAndQuery：空串", WebProxyPath.pathAndQuery("   "));
        T.isNull("pathAndQuery：ftp 不处理",
                WebProxyPath.pathAndQuery("ftp://h/x"));
        T.isNull("pathAndQuery：file 不处理",
                WebProxyPath.pathAndQuery("file:///etc/passwd"));

        // 大小写不敏感（WebView 给的 scheme 大小写不保证）
        T.eq("pathAndQuery：scheme 大小写不敏感", "/x",
                WebProxyPath.pathAndQuery("HTTP://H/x"));

        shim();
        apiGuide();
    }

    /**
     * 注入脚本（第二层改写）—— **只做 HTTP 层拦截会静默坏掉登录**。
     *
     * 背景：WebView 的 shouldInterceptRequest 拿不到 POST body
     * （安卓公开 API 限制，官方 issue tracker 确认），而 NapCat 的登录是
     * `POST /api/auth/login` 带 JSON body {hash, totpCode}。
     * 硬拦会让登录变成空 body：页面能显示、但一登录就失败，看起来一切正常。
     *
     * 所以必须在页面 JS 里改地址（body 原样交给真正的请求）。
     * 这里把脚本的行为钉死；脚本本身还会在真 node 里跑一遍（见 run-tests.sh）。
     */
    private static void shim() {
        T.group("注入脚本（修「页面能开但一登录就失败」）");

        String js = WebProxyPath.shimJs(41000, "qq1");
        T.isTrue("生成了脚本", js != null && js.length() > 100);
        T.contains("★ 脚本里带代理前缀", js, "127.0.0.1:41000/proxy/qq1");
        // 三个都要包：axios 走 XHR、有的代码走 fetch、实时日志走 EventSource
        T.contains("★ 包了 XMLHttpRequest（axios 走这条）", js, "XMLHttpRequest");
        T.contains("★ 包了 fetch", js, "window.fetch");
        T.contains("★ 包了 EventSource（实时日志）", js, "EventSource");
        // 不能重复套前缀
        T.contains("★ 脚本里有防重复套前缀的判断", js, "'/proxy/'");
        // 外部地址不能被动
        T.contains("★ 只改同源地址（外链不动）", js, "127.0.0.1");
        // 脚本要能独立执行（IIFE，不污染全局）
        T.isTrue("脚本是 IIFE（不污染全局）",
                js.trim().startsWith("(function(){") && js.trim().endsWith("})();"));

        // 直连模式不该注入任何脚本
        T.eq("直连模式（端口 0）不生成脚本",
                "", WebProxyPath.shimJs(0, "qq1"));
        T.eq("实例名为空不生成脚本",
                "", WebProxyPath.shimJs(41000, ""));

        // ── 字符串转义：实例名里的引号不能把脚本拼坏（也不会造成注入）──
        // 注意这里是**两层**防护，所以断言要按实际生效的那层写：
        //   ① 实例名先过 enc()（URL 编码）—— 引号变成 %27、反斜杠变成 %5C，
        //      根本到不了脚本字面量里；
        //   ② jsStr() 再做一次 JS 字面量转义（纵深防御）。
        String evil = WebProxyPath.shimJs(41000, "a'b\\c");
        T.contains("★ 实例名里的引号被 URL 编码（到不了脚本里）", evil, "a%27b%5Cc");
        T.notContains("★ 脚本里不出现裸的单引号实例名", evil, "a'b");
        // jsStr 单独测：即使绕过 enc() 直接调用，也要转义干净
        T.eq("★ jsStr 转义单引号与反斜杠",
                "'a\\'b\\\\c\\u003c'", WebProxyPath.jsStr("a'b\\c<"));
        T.contains("★ 尖括号被转义（防截断宿主文档）",
                WebProxyPath.jsStr("<script>"), "\\u003c");
        T.notContains("不出现裸的 <", WebProxyPath.jsStr("<script>"), "<");
        T.eq("jsStr 转义换行", "'a\\nb'", WebProxyPath.jsStr("a\nb"));

        // ── HTML 注入位置：必须在页面自己的脚本之前 ──────────────────────
        T.group("脚本插入位置（必须早于页面脚本）");

        String html = "<!doctype html><html lang=\"zh\"><head><meta charset=\"UTF-8\">"
                + "<script src=\"/webui/assets/index.js\"></script></head><body></body></html>";
        String out = WebProxyPath.injectIntoHtml(html, "SHIM();");
        int posShim = out.indexOf("SHIM();");
        int posPage = out.indexOf("/webui/assets/index.js");
        T.isTrue("★ 注入的脚本在页面脚本之前执行", posShim >= 0 && posShim < posPage);
        T.contains("注入内容包在 <script> 里", out, "<script>SHIM();</script>");
        // 插在 head 开标签之后（不是 head 之前，避免破坏文档结构）
        T.isTrue("插在 <head> 之后",
                out.indexOf("<head>") < posShim);

        // 带属性的 head
        String h2 = WebProxyPath.injectIntoHtml(
                "<html><head profile=\"x\"><title>t</title></head>", "S();");
        T.isTrue("head 带属性也能插对", h2.indexOf("S();") > h2.indexOf("<head profile=\"x\">"));

        // 没有 head 就插最前面（不能崩）
        String h3 = WebProxyPath.injectIntoHtml("<html><body>x</body></html>", "S();");
        T.isTrue("没有 head 时插在文档最前", h3.startsWith("<script>S();</script>"));

        // 大写 HEAD 也要认（真实页面大小写不保证）
        String h4 = WebProxyPath.injectIntoHtml("<HTML><HEAD><TITLE>t</TITLE>", "S();");
        T.isTrue("大写 HEAD 也能插对", h4.indexOf("S();") > h4.indexOf("<HEAD>"));

        // 边界：null / 空脚本原样返回（不能崩，也不能把页面弄坏）
        T.eq("html 为 null 时原样返回", null,
                WebProxyPath.injectIntoHtml(null, "S();"));
        T.eq("脚本为空时原样返回", html,
                WebProxyPath.injectIntoHtml(html, ""));
        T.eq("脚本为 null 时原样返回", html,
                WebProxyPath.injectIntoHtml(html, null));
    }

    /**
     * 网页自动登录（修「网页让我输入 token 是什么情况」）。
     *
     * 用户打开内置网页登录页时撞上了一个写着「请输入token」的输入框 ——
     * 那是 **NapCat WebUI 自己的访问口令**，不是 QQ 密码，App 里也从没让他填过，
     * 所以他当然不知道这是什么。
     *
     * 实测确认的机制（对着线上 NapCat 的 webui 前端 bundle 读出来的）：
     *  - 路由守卫：`if(!isAuth){const o=new URLSearchParams(location.search)
     *    .get("token"); let a="/web_login"; o&&(a+=\`?token=${o}\`); navigate(a)}`
     *    → 地址栏上的 token 会被**原样带**到登录页；
     *  - 登录页：`useEffect(()=>{if(j){C(!1),m();return} ...},[])`，其中
     *    `j=new URLSearchParams(location.search).get("token")`、`m()` 是提交函数
     *    → 带 token 进来自动提交，**不需要用户点任何东西**。
     *
     * 所以修复就是拼一个 `?token=`。这里把拼接规则钉死：
     * 少一个 & 或多一层 ? 都会让页面取不到 token，退回「请输入token」，
     * 而症状跟没修一模一样 —— 属于不看测试根本发现不了的那类。
     */
    private static void withToken() {
        T.group("网页自动登录（修「网页让我输入 token」）");

        // 核心：代理地址 + token
        T.eq("★ 拼上 ?token=",
                "http://127.0.0.1:41000/proxy/qq1/webui/?token=abc123",
                WebProxyPath.withToken(
                        "http://127.0.0.1:41000/proxy/qq1/webui/", "abc123"));

        // 已经有查询串时必须用 & 接（用 ? 会拼出两个 ?，页面取不到 token）
        T.eq("★ 已有查询串时用 & 接（不能用第二个 ?）",
                "http://127.0.0.1:41000/proxy/qq1/webui/?a=1&token=abc",
                WebProxyPath.withToken(
                        "http://127.0.0.1:41000/proxy/qq1/webui/?a=1", "abc"));

        // token 必须 URL 编码：NapCat 的口令可能含 + / = 等字符，
        // 不编码会被当成别的含义（+ 变空格）或直接截断。
        T.eq("★ token 里的特殊字符被编码（+ 不能当空格）",
                "http://h/x?token=a%2Bb",
                WebProxyPath.withToken("http://h/x", "a+b"));
        T.eq("★ token 里的 / 被编码（否则路径就断了）",
                "http://h/x?token=a%2Fb",
                WebProxyPath.withToken("http://h/x", "a/b"));
        T.eq("★ token 里的 = 被编码（否则查询串被切错）",
                "http://h/x?token=a%3Db",
                WebProxyPath.withToken("http://h/x", "a=b"));
        T.eq("token 里的空格编码成 %20（不是 +）",
                "http://h/x?token=a%20b",
                WebProxyPath.withToken("http://h/x", "a b"));

        // 没有 token 时**不能**硬塞一个空参数：
        // `?token=` 会让登录页拿到空串，反而可能覆盖掉用户手填的值。
        T.eq("★ token 为空时不加参数", "http://h/x",
                WebProxyPath.withToken("http://h/x", ""));
        T.eq("★ token 为 null 时不加参数", "http://h/x",
                WebProxyPath.withToken("http://h/x", null));
        T.eq("url 为 null 时返回 null（不崩）", null,
                WebProxyPath.withToken(null, "abc"));

        // 真实形状：从实例详情拿到的 token 是 12 位字母数字（线上实测三个实例一致）
        T.eq("★ 真实形状（12 位字母数字）拼出来可读",
                "http://127.0.0.1:41000/proxy/666/webui/?token=Ab3xY9zQ7wEr",
                WebProxyPath.withToken(
                        "http://127.0.0.1:41000/proxy/666/webui/", "Ab3xY9zQ7wEr"));
    }

    private static void apiGuide() {
        T.group("API 申请引导（地址必须齐全、格式必须对）");

        List<ApiGuide.Provider> ps = ApiGuide.providers();
        T.isTrue("★ 收录了多家服务商（至少 6 家）", ps.size() >= 6);

        for (ApiGuide.Provider p : ps) {
            String tag = "[" + p.name + "] ";
            T.isTrue(tag + "有申请地址", !p.keyUrl.isEmpty());
            T.isTrue(tag + "申请地址是 https", p.keyUrl.startsWith("https://"));
            T.isTrue(tag + "有接口地址", !p.baseUrl.isEmpty());
            // 接口地址必须是 http(s)，否则填进去直接报「要以 http 开头」
            T.isTrue(tag + "接口地址是 http(s)",
                    p.baseUrl.startsWith("http://") || p.baseUrl.startsWith("https://"));
            // 不能以 / 结尾：保存时会拼 /models，双斜杠有些网关不认
            T.isTrue(tag + "接口地址不以 / 结尾", !p.baseUrl.endsWith("/"));
            T.isTrue(tag + "有模型名示例", !p.models.isEmpty());
            T.isTrue(tag + "模型名示例非空", !p.firstModel().trim().isEmpty());
        }

        // 推荐顺序：DeepSeek 必须排第一（引导语里点名推荐它）
        T.eq("★ 第一家是 DeepSeek（引导语推荐它）",
                "DeepSeek 深度求索", ps.get(0).name);

        // 关键：不能收录已停用的模型名 —— 用户照着填会直接报错。
        // deepseek-chat / deepseek-reasoner 已于 2026-07-24 官方停用。
        StringBuilder all = new StringBuilder();
        for (ApiGuide.Provider p : ps) {
            for (String m : p.models) {
                all.append(m).append(" ");
            }
            all.append(p.warn).append(" ");
        }
        String joined = all.toString();
        // 「deepseek-chat」这个词只能出现在警告文字里（说明它已停用），
        // 绝不能作为可填的示例模型名。
        for (ApiGuide.Provider p : ps) {
            for (String m : p.models) {
                T.isTrue("[" + p.name + "] 不含已停用的模型名 " + m,
                        !m.equals("deepseek-chat") && !m.equals("deepseek-reasoner"));
            }
        }
        T.contains("★ DeepSeek 的提示里说明了旧模型名已停用",
                joined, "deepseek-chat");
        T.contains("★ 说明了停用日期", joined, "2026-07-24");

        // 境外服务必须带警告（服务器在国内时连不上，不说就是坑人）
        ApiGuide.Provider or = ApiGuide.byName("OpenRouter（聚合，境外）");
        T.isTrue("找得到 OpenRouter", or != null);
        if (or != null) {
            T.contains("★ 境外服务带「可能连不上」警告", or.warn, "境外");
        }

        // 有坑的服务商必须写清坑（火山方舟要接入点 ID）
        ApiGuide.Provider ark = ApiGuide.byName("火山方舟（豆包）");
        T.isTrue("找得到火山方舟", ark != null);
        if (ark != null) {
            T.contains("★ 火山方舟提示了接入点 ID 的坑", ark.warn, "接入点");
        }

        // 智谱的地址结尾是 /v4（写 /v1 会不通）—— 必须如实写出来
        ApiGuide.Provider glm = ApiGuide.byName("智谱 AI（GLM）");
        T.isTrue("找得到智谱", glm != null);
        if (glm != null) {
            T.isTrue("★ 智谱接口地址以 /v4 结尾",
                    glm.baseUrl.endsWith("/api/paas/v4"));
            T.contains("★ 智谱提示了 /v4 不是 /v1", glm.warn, "/v4");
        }

        // 阿里百炼必须有 compatible-mode（少了就不是 OpenAI 兼容接口）
        ApiGuide.Provider ali = ApiGuide.byName("阿里云百炼（通义千问）");
        T.isTrue("找得到百炼", ali != null);
        if (ali != null) {
            T.contains("★ 百炼地址含 compatible-mode",
                    ali.baseUrl, "compatible-mode");
        }

        // 引导语要说清四步 + 推荐哪家
        String intro = ApiGuide.intro();
        T.contains("引导语提到 API Key", intro, "API Key");
        T.contains("引导语推荐 DeepSeek", intro, "DeepSeek");
        T.contains("★ 引导语让用户用「获取可用模型」而不是死记模型名",
                intro, "获取可用模型");
        // 诚实说明 Key 的风险（云厂商官方警告过别在手机里用长期 Key）
        String sec = ApiGuide.securityNote();
        T.contains("★ 如实说明 Key 不会存在手机上", sec, "不会存在手机上");
        T.contains("★ 提示为机器人单独建 Key 以便随时吊销", sec, "删掉");
        T.contains("★ 提示设置消费上限", sec, "上限");

        // byName 找不到时返回 null，不能抛异常
        T.isNull("byName 找不到返回 null", ApiGuide.byName("不存在的服务商"));
        T.isNull("byName(null) 返回 null", ApiGuide.byName(null));

        // ── 教程（用户：「加，并且加上对应的教程」）─────────────────────
        //
        // 光给网址不够：新手到了官网还是不知道点哪。教程必须写到
        // 「哪一步、点什么、复制什么」这个颗粒度，并提前说出坑。
        T.group("API 申请教程（修「新手不知道在哪里获取」）");

        String tut = ApiGuide.tutorial("DeepSeek 深度求索");
        T.contains("★ 教程含申请地址（不用自己去搜）", tut,
                "https://platform.deepseek.com/api_keys");
        T.contains("★ 教程含接口地址（可直接填）", tut,
                "https://api.deepseek.com/v1");
        T.contains("★ 提醒要实名（没实名建不了 Key）", tut, "实名");
        T.contains("★ 提醒要充值并给了大概金额", tut, "¥10");
        T.contains("★ 提醒 Key 只显示一次（最容易踩的坑）", tut, "只显示一次");
        T.contains("★ 提醒 Key 以 sk- 开头（用户好认）", tut, "sk-");
        T.contains("★ 告诉用户填完之后点测试连接", tut, "测试连接");
        T.contains("★ 让用户用「获取可用模型」而不是死记模型名", tut, "获取可用模型");
        T.isTrue("★ 教程分了步骤（不是一大段话）", tut.contains("第 1 步")
                && tut.contains("第 6 步"));

        // 每家都能生成教程，且都带上自己的地址（不能张冠李戴）
        for (ApiGuide.Provider p : ps) {
            String t = ApiGuide.tutorial(p.name);
            T.contains("[" + p.name + "] 教程含自己的申请地址", t, p.keyUrl);
            T.contains("[" + p.name + "] 教程含自己的接口地址", t, p.baseUrl);
        }
        // 认不出的名字要退回通用教程，不能抛异常
        T.contains("★ 未知服务商退回通用教程", ApiGuide.tutorial("不存在"), "第 1 步");
        T.contains("★ tutorial(null) 不抛异常", ApiGuide.tutorial(null), "第 1 步");
        T.contains("★ 通用教程也含关键提醒", ApiGuide.genericTutorial(), "只显示一次");
    }
}
