package com.dafeiyu.controller;

import android.app.Activity;
import android.os.Bundle;
import android.view.ViewGroup;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 内置网页登录页 —— 把服务器上这个实例的 NapCat WebUI 装进 App 的 WebView。
 *
 * 什么时候用它：
 * - 密码登录触发了腾讯的验证码 / 新设备验证 —— 那套验证组件是网页 JS，
 *   官方网页登录页能直接跑完，App 原生页面做不了；
 * - WebUI 开了两步验证但手边没有验证器；
 * - 或者就是想看官方页面的完整信息。
 *
 * ── 为什么要改写请求（这个坑踩过，症状是「网页老是连不上」）──────────────
 *
 * 实例的 WebUI 只监听**服务器**的 127.0.0.1:实例端口，手机连不上，
 * 必须经管理服务的 /proxy/&lt;实例名&gt;/ 转发。但 NapCat 的网页是 React 应用，
 * 它引用资源用的是**绝对路径**：
 *
 *     &lt;script src="/webui/assets/index-CaeQ89K8.js"&gt;
 *
 * 于是 WebView 会去请求 http://127.0.0.1:&lt;隧道端口&gt;/webui/assets/...，
 * 而管理服务上那条路径是 404（它只认 /proxy/&lt;实例名&gt;/webui/...）。
 * 结果：HTML 能出来、样式和 JS 全 404，页面白屏或一直转圈 —— 看起来像
 * 「连不上」，其实只是每个子请求都少了个前缀。
 * 实测：页面引用的 8 个资源，未改写全部 404、改写后全部 200。
 *
 * ── 为什么需要**两层**改写（只做一层会静默坏掉登录）────────────────────
 *
 * 页面发出的请求分两类，必须分别处理：
 *
 * ① **浏览器自己解析的静态资源**（HTML 里的 src/href）：GET、没有 body。
 *    → 用 shouldInterceptRequest 在 HTTP 层改写即可。
 *
 * ② **页面 JS 自己发的 API 调用**：NapCat 用 axios（baseURL="/api"）和
 *    fetch("/api"+e)，登录是 `POST /api/auth/login` 带 JSON body
 *    {hash, totpCode}。
 *    → **不能在 HTTP 层改写**：WebView 的 shouldInterceptRequest
 *      拿不到 POST body（安卓公开 API 的限制，官方 issue tracker 确认
 *      "There is nothing to read the body of this request"）。
 *      硬拦会让登录请求变成空 body，页面能显示、但**一登录就失败** ——
 *      比白屏更难查，因为看起来一切正常。
 *    → 必须在**页面 JS 发起请求之前**改写 URL：注入一小段脚本，
 *      包装 XMLHttpRequest 和 fetch，把地址加上代理前缀。
 *      body 原样传给真正的请求，我们只是换了地址。
 *
 * 注入时机用 onPageStarted（文档开始加载时），早于页面里任何脚本执行；
 * 每次导航都重新注入（新文档 = 新的 window）。
 */

public final class WebLoginActivity extends Activity {

    /** 隧道本地端口；&lt;=0 表示直连模式（不用代理）。 */
    public static final String EXTRA_TUNNEL_PORT = "tunnel_port";
    /** 实例名；经代理时必填。 */
    public static final String EXTRA_INSTANCE = "instance";
    /** 管理口令（走 X-Dafeiyu-Token 头）。 */
    public static final String EXTRA_MANAGER_TOKEN = "manager_token";

    private WebView webView;
    private int tunnelPort;
    private String instance = "";
    private String managerToken = "";
    private boolean viaProxy;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        String base = getIntent().getStringExtra("base");
        if (base == null || base.isEmpty()) {
            finish();
            return;
        }
        tunnelPort = getIntent().getIntExtra(EXTRA_TUNNEL_PORT, 0);
        instance = str(getIntent().getStringExtra(EXTRA_INSTANCE));
        managerToken = str(getIntent().getStringExtra(EXTRA_MANAGER_TOKEN));
        viaProxy = tunnelPort > 0 && !instance.isEmpty() && !managerToken.isEmpty();

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Theme.BG);

        TextView hint = UiKit.text(this,
                viaProxy
                        ? "下面是你服务器上这个机器人的 NapCat 网页。登录后可扫码或密码登录；"
                          + "完成后按返回键回到 App。"
                        : "下面是你填的那个 NapCat 网页：输 Token 登录后可扫码或密码登录。"
                          + "完成后按返回键回到 App。",
                12, Theme.DIM);
        hint.setPadding(Theme.dp(this, 12), Theme.dp(this, 8), Theme.dp(this, 12),
                Theme.dp(this, 8));
        root.addView(hint);

        webView = new WebView(this);
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);

        if (viaProxy) {
            // 两层改写，缺一不可（见类注释）：
            //  ① HTTP 层：拦浏览器自己解析的静态资源（GET，无 body）
            //  ② JS 层：包 XHR/fetch，改页面 JS 发的 API 调用（POST 带 body）
            // 只做 ① 的话页面能显示，但一登录就失败（POST body 被丢掉），
            // 而且看起来一切正常 —— 这种「静默坏掉」最难查。
            webView.setWebViewClient(new ProxyClient());
        }

        webView.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        root.addView(webView);
        setContentView(root);

        // 经代理时直接加载代理地址下的 /webui/ —— 不要加载 base 的根路径。
        // 根路径在管理服务上没有对应路由（会 404），而且 WebUI 的正确入口
        // 就是实例的 /webui/（实测 /proxy/<实例>/webui/ 返回完整页面）。
        webView.loadUrl(viaProxy
                ? WebProxyPath.proxyUrlFor(tunnelPort, instance, "/webui/")
                : NapCatClient.normalize(base) + "/");
    }

    // ------------------------------------------------------------ 请求改写

    /**
     * 把 WebView 的请求经管理服务代理出去。
     *
     * 只改**路径**：WebView 无论请求什么地址，最终都落到
     * http://127.0.0.1:&lt;隧道端口&gt;/proxy/&lt;实例名&gt;/&lt;原始路径&gt;。
     * 查询串原样保留（NapCat 有些接口靠它传参）。
     */
    private final class ProxyClient extends WebViewClient {

        /**
         * 文档开始加载时也注入一次（双保险）。
         *
         * 主路径是下面 shouldInterceptRequest 里把脚本**直接插进 HTML**（确定性最高）；
         * 这里再补一次，因为 onPageStarted 对「页面内跳转」等场景仍然有效。
         * 脚本是幂等的（已带 /proxy/ 前缀的地址会被原样放过），重复注入无害。
         */
        @Override
        public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
            super.onPageStarted(view, url, favicon);
            view.evaluateJavascript(WebProxyPath.shimJs(tunnelPort, instance), null);
        }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
            String url = req.getUrl() == null ? "" : req.getUrl().toString();
            return handle(url, req.getMethod(), req.getRequestHeaders());
        }

        /**
         * 老版本回调（API 21 起就有）。
         *
         * minSdk 是 26，走的是上面那个带 WebResourceRequest 的重载；
         * 但这个也覆写掉 —— 万一在某些 ROM 上走了这条，行为保持一致，
         * 而不是悄悄退化成「不代理、全部 404」。
         */
        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, String url) {
            return handle(url, "GET", null);
        }

        private WebResourceResponse handle(String url, String method,
                                           Map<String, String> headers) {
            String target = WebProxyPath.proxyUrlFor(tunnelPort, instance, url);
            if (target == null) {
                return null;             // 不处理：交回 WebView 自己走
            }
            try {
                WebResourceResponse r = fetch(target, method, headers);
                // ★ 主文档（HTML）要**把改写脚本插进去**再交给 WebView。
                //
                // 为什么不用 onPageStarted + evaluateJavascript 就完事：
                // 那是异步的，可能在页面自己的脚本之后才执行 —— 而页面脚本
                // 一执行就把 XMLHttpRequest / fetch 的原始引用拿走了，
                // 再包也没用（登录 POST 会变成空 body，静默坏掉）。
                // 插进 HTML 里则是**确定性**的：它在页面任何脚本之前执行。
                String ct = r.getMimeType() == null ? "" : r.getMimeType();
                if (ct.contains("html") && r.getData() != null) {
                    return injectShim(r, WebProxyPath.shimJs(tunnelPort, instance));
                }
                return r;
            } catch (IOException e) {
                // 单个子资源失败不要拖垮整页：回空响应，其余部分照常渲染。
                return new WebResourceResponse("text/plain", "utf-8",
                        new ByteArrayInputStream(new byte[0]));
            }
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
            return false;                // 页面内跳转照常；子请求仍会被上面改写
        }
    }

    /**
     * 把改写脚本插到 HTML 最前面（&lt;head&gt; 之后，或文档最开头）。
     *
     * 放在最前面是为了保证它**先于页面自己的任何脚本**执行。
     * 插不进就原样返回 —— 宁可少一次改写，也不能把页面弄坏。
     */
    private static WebResourceResponse injectShim(WebResourceResponse r, String js) {
        if (js == null || js.isEmpty()) {
            return r;
        }
        try {
            byte[] raw = readAll(r.getData());
            String out = WebProxyPath.injectIntoHtml(new String(raw, "UTF-8"), js);
            Map<String, String> h = r.getResponseHeaders() == null
                    ? new HashMap<String, String>() : r.getResponseHeaders();
            // 长度变了，必须去掉原来那个 content-length，否则页面会被截断。
            h.remove("Content-Length");
            h.remove("content-length");
            return new WebResourceResponse(r.getMimeType(), r.getEncoding(),
                    r.getStatusCode(), r.getReasonPhrase(), h,
                    new ByteArrayInputStream(out.getBytes("UTF-8")));
        } catch (Exception e) {
            return r;
        }
    }

    private static byte[] readAll(InputStream in) throws IOException {
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) {
            bos.write(buf, 0, n);
        }
        return bos.toByteArray();
    }

    /** 真正发请求。headers 来自 WebView（可能是 null）。 */
    private WebResourceResponse fetch(String url, String method,
                                      Map<String, String> headers) throws IOException {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setRequestMethod(method == null || method.isEmpty() ? "GET" : method);
        conn.setConnectTimeout(15000);
        conn.setReadTimeout(30000);
        conn.setInstanceFollowRedirects(false);
        // 管理口令走专用头：Authorization 要留给 NapCat 自己的凭据。
        conn.setRequestProperty("X-Dafeiyu-Token", managerToken);
        if (headers != null) {
            for (Map.Entry<String, String> e : headers.entrySet()) {
                String k = e.getKey();
                if (k == null) {
                    continue;
                }
                String lk = k.toLowerCase();
                // 这些头由 HttpURLConnection 自己管，手动设置会出错；
                // X-Dafeiyu-Token 也不能被页面里的值覆盖。
                if (lk.equals("host") || lk.equals("content-length")
                        || lk.equals("connection") || lk.equals("accept-encoding")
                        || lk.equals("x-dafeiyu-token")) {
                    continue;
                }
                conn.setRequestProperty(k, e.getValue());
            }
        }
        int code = conn.getResponseCode();
        InputStream in = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
        if (in == null) {
            in = new ByteArrayInputStream(new byte[0]);
        }
        String mime = "text/plain";
        String charset = "utf-8";
        String ctype = conn.getContentType();
        if (ctype != null && !ctype.isEmpty()) {
            int semi = ctype.indexOf(';');
            mime = (semi < 0 ? ctype : ctype.substring(0, semi)).trim();
            int cs = ctype.toLowerCase().indexOf("charset=");
            if (cs >= 0) {
                charset = ctype.substring(cs + 8).trim();
            }
        }
        Map<String, String> rh = new HashMap<String, String>();
        for (Map.Entry<String, List<String>> e : conn.getHeaderFields().entrySet()) {
            if (e.getKey() == null || e.getValue() == null || e.getValue().isEmpty()) {
                continue;
            }
            String lk = e.getKey().toLowerCase();
            if (lk.equals("content-length") || lk.equals("content-encoding")
                    || lk.equals("transfer-encoding") || lk.equals("connection")) {
                continue;
            }
            rh.put(e.getKey(), e.getValue().get(0));
        }
        // 不 disconnect：返回的 InputStream 还要被 WebView 读，读完后连接会自己回收。
        return new WebResourceResponse(mime, charset, code,
                code >= 200 && code < 300 ? "OK" : "HTTP " + code, rh, in);
    }

    private static String str(String s) {
        return s == null ? "" : s;
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
