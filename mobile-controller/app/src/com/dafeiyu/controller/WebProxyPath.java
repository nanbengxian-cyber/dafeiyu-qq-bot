package com.dafeiyu.controller;

/**
 * 把 WebView 发出的请求路径改写成「经管理服务代理」的地址。
 *
 * 单独拎出来是为了能单测：这里错了的表现是**网页白屏或一直转圈**
 * （HTML 能出来，但样式和 JS 全 404），从现象几乎不可能反推出
 * 「少了个代理前缀」，所以必须用测试钉死。
 *
 * 这个类刻意不碰 android.*（见 test/run-tests.sh 的反向检查）——
 * 逻辑可测，界面不可测，就别把逻辑写在界面里。
 */
public final class WebProxyPath {

    private WebProxyPath() {
    }

    /**
     * 映射成经代理的地址。返回 null 表示「不处理，交回 WebView 自己走」。
     *
     * @param tunnelPort 隧道本地端口（&lt;=0 表示不用代理）
     * @param instance   实例名（空表示不用代理）
     * @param url        WebView 想请求的地址
     */
    public static String proxyUrlFor(int tunnelPort, String instance, String url) {
        if (tunnelPort <= 0 || instance == null || instance.isEmpty()) {
            return null;
        }
        String rest = pathAndQuery(url);
        if (rest == null) {
            return null;
        }
        // 已经是代理地址了就别再套一层，否则会变成 /proxy/x/proxy/x/...
        if (rest.startsWith("/proxy/")) {
            return null;
        }
        return "http://127.0.0.1:" + tunnelPort + "/proxy/" + enc(instance) + rest;
    }

    /**
     * 生成注入页面的改写脚本 —— 包住 XMLHttpRequest / fetch / EventSource，
     * 把地址换成代理地址。
     *
     * **为什么非要有这一层**（只靠 HTTP 层拦截会静默坏掉登录）：
     * WebView 的 shouldInterceptRequest 拿不到 POST body（安卓公开 API 的限制，
     * 官方 issue tracker 明确写着 "There is nothing to read the body of this
     * request"）。而 NapCat 的登录是 `POST /api/auth/login` 带 JSON body
     * `{hash, totpCode}`。硬在 HTTP 层拦，请求会变成空 body ——
     * 页面能显示、但一登录就失败，看起来一切正常，最难查。
     *
     * 所以在页面 JS 里改地址：body 原样交给真正的请求，我们只换 URL。
     * 注入时机必须是文档开始加载时（早于页面脚本执行）。
     */
    public static String shimJs(int tunnelPort, String instance) {
        if (tunnelPort <= 0 || instance == null || instance.isEmpty()) {
            return "";
        }
        String prefix = "http://127.0.0.1:" + tunnelPort + "/proxy/" + enc(instance);
        StringBuilder sb = new StringBuilder();
        sb.append("(function(){");
        sb.append("var P=").append(jsStr(prefix)).append(";");
        sb.append("function fix(u){");
        sb.append("  try{");
        sb.append("    if(typeof u!=='string'||u.length===0)return u;");
        // 只处理同源的站内路径（/webui/... 和 /api/...）。
        // 绝对外部地址不动，否则会把外链也代理了。
        sb.append("    if(u.indexOf('://')>=0){");
        sb.append("      if(u.indexOf('127.0.0.1')<0&&u.indexOf('localhost')<0)return u;");
        sb.append("      var i=u.indexOf('/',u.indexOf('://')+3);");
        sb.append("      if(i<0)return u;");
        sb.append("      u=u.substring(i);");
        sb.append("    }");
        sb.append("    if(u.charAt(0)!=='/')return u;");
        sb.append("    if(u.indexOf('/proxy/')===0)return u;");   // 别重复套前缀
        sb.append("    return P+u;");
        sb.append("  }catch(e){return u;}");
        sb.append("}");
        // ① fetch
        sb.append("var _f=window.fetch;");
        sb.append("if(_f){window.fetch=function(a,b){");
        sb.append("  try{");
        sb.append("    if(typeof a==='string'){a=fix(a);}");
        sb.append("    else if(a&&a.url){a=new Request(fix(a.url),a);}");
        sb.append("  }catch(e){}");
        sb.append("  return _f.call(this,a,b);");
        sb.append("};}");
        // ② XMLHttpRequest（axios 在浏览器里走这条）
        sb.append("var _o=XMLHttpRequest.prototype.open;");
        sb.append("XMLHttpRequest.prototype.open=function(m,u){");
        sb.append("  try{u=fix(u);}catch(e){}");
        sb.append("  var a=[].slice.call(arguments);a[1]=u;");
        sb.append("  return _o.apply(this,a);");
        sb.append("};");
        // ③ EventSource（NapCat 用 EventSourcePolyfill 拉实时日志）
        sb.append("var _E=window.EventSource;");
        sb.append("if(_E){var W=function(u,c){return new _E(fix(u),c);};");
        sb.append("W.prototype=_E.prototype;window.EventSource=W;}");
        sb.append("})();");
        return sb.toString();
    }

    /**
     * 把改写脚本插到 HTML 最前面，保证它**先于页面自己的任何脚本**执行。
     *
     * 插在 &lt;head ...&gt; 之后；没有 head 就插在文档最开头。
     *
     * 为什么不用 WebView 的 evaluateJavascript 就完事：那是异步的，
     * 可能在页面脚本之后才执行 —— 而页面脚本一执行就把
     * XMLHttpRequest / fetch 的原始引用拿走了，再包也没用。
     * 插进 HTML 是确定性的。
     */
    public static String injectIntoHtml(String html, String js) {
        if (html == null || js == null || js.isEmpty()) {
            return html;
        }
        String tag = "<script>" + js + "</script>";
        int at = indexOfIgnoreCase(html, "<head");
        if (at >= 0) {
            int gt = html.indexOf('>', at);
            at = gt >= 0 ? gt + 1 : at;
        } else {
            at = 0;
        }
        return html.substring(0, at) + tag + html.substring(at);
    }

    private static int indexOfIgnoreCase(String s, String needle) {
        return s.toLowerCase().indexOf(needle.toLowerCase());
    }

    /** 把字符串安全地嵌进 JS 单引号字面量（防注入、防把脚本拼坏）。 */
    public static String jsStr(String s) {
        StringBuilder sb = new StringBuilder("'");
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '\\' || c == '\'') {
                sb.append('\\').append(c);
            } else if (c == '\n') {
                sb.append("\\n");
            } else if (c == '\r') {
                sb.append("\\r");
            } else if (c == '<') {
                // 防止拼出 </script> 之类把宿主文档截断
                sb.append("\\u003c");
            } else {
                sb.append(c);
            }
        }
        return sb.append('\'').toString();
    }

    /**
     * 给 WebUI 地址带上 `?token=`，让网页**自动登录**，不用用户手输 Token。
     *
     * ── 为什么需要这个（用户报过「网页让我输入 token 是什么情况」）──────────
     *
     * NapCat 的 WebUI 有它自己的一套登录：页面没凭据时会被路由守卫踢到
     * `/web_login`，那儿有个输入框写着「请输入token」。用户当然不知道这是什么 ——
     * 那是 NapCat WebUI 的访问口令，不是 QQ 密码，App 里也从没让他填过。
     *
     * 关键发现：NapCat 的登录页支持从地址栏取 token 并**自动提交**
     * （`web_login` 的 `useEffect`：`if(j){C(!1),m();return}`，j 就是
     * `new URLSearchParams(location.search).get("token")`）。
     * 而路由守卫在跳转时会把当前地址上的 token 原样带过去：
     * `o&&(a+=\`?token=${o}\`)`。
     *
     * 所以只要打开 `/webui/?token=xxx`，守卫就会转到
     * `/web_login?token=xxx`，登录页拿到 token 自动登录 —— 全程无需用户输入。
     *
     * 注意：`token` 要 URL 编码（口令里可能有 + / = 等字符，不编码会被截断）。
     * 管理服务的日志会把 `token=***` 打码，所以不会明文留在 journald 里。
     *
     * @param url   已经拼好的地址（如 .../proxy/qq1/webui/）
     * @param token WebUI 口令；空则原样返回（不硬塞空参数，免得页面报错）
     */
    public static String withToken(String url, String token) {
        if (url == null || token == null || token.isEmpty()) {
            return url;
        }
        String sep = url.indexOf('?') >= 0 ? "&" : "?";
        return url + sep + "token=" + enc(token);
    }

    /**
     * 从完整 URL 里取出「/路径?查询串」。返回 null 表示这不是一个该代理的 HTTP 地址。
     *
     * 这是改写的核心：主机部分一律丢掉 —— WebView 请求的主机（隧道回环地址）
     * 不是真正要去的地方，真正要去的是服务器上那个实例（由代理前缀表达）。
     */
    public static String pathAndQuery(String url) {
        if (url == null) {
            return null;
        }
        String s = url.trim();
        if (s.isEmpty()) {
            return null;
        }
        int i = s.indexOf("://");
        if (i >= 0) {
            String scheme = s.substring(0, i).toLowerCase();
            // 只代理 http/https：data:/blob:/about:/javascript: 这些交给 WebView。
            if (!scheme.equals("http") && !scheme.equals("https")) {
                return null;
            }
            int slash = s.indexOf('/', i + 3);
            if (slash < 0) {
                // 只有主机名（如 http://127.0.0.1:1234）→ 就是根路径
                int q = s.indexOf('?', i + 3);
                return q < 0 ? "/" : "/" + s.substring(q);
            }
            s = s.substring(slash);
        }
        // 没有 scheme 的相对地址必须从 / 开始；否则（如 "foo.js"）交给 WebView。
        if (!s.startsWith("/")) {
            return null;
        }
        return s;
    }

    private static String enc(String s) {
        try {
            // 注意：URLEncoder 是**表单编码**，空格会变成 "+"。
            // 但这里是 URL **路径**段，"+" 在路径里是字面加号、不是空格 ——
            // 拼错了会让实例名里的空格变成 %2B 或解析错，所以手工换成 %20。
            return java.net.URLEncoder.encode(s == null ? "" : s, "UTF-8")
                    .replace("+", "%20");
        } catch (java.io.UnsupportedEncodingException e) {
            return s == null ? "" : s;
        }
    }
}
