package com.dafeiyu.controller;

import android.content.Context;
import android.view.View;
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
 * 第三块虚拟屏 —— 内置网页验证（短信 / 验证码 / 两步验证兜底）。
 *
 * 它是「三大验证」里最后一块：二维码 → {@link QrScreen}、密码 →
 * {@link PwScreen}、剩下的网页验证 → 本屏。三块都遵循 VirtualScreen
 * 的「用完即弃」约定：点开才构建，关掉立刻 destroy WebView，平时零占用。
 *
 * ── 为什么这一块也必须虚拟屏化（2026-09-19 用户要求）──────────────
 *
 * 原来它是个独立 Activity（WebLoginActivity）：点「短信 / 网页验证」
 * 会跳出一整页独立的网页界面 —— 用户看到的就是「之前的网页」。
 * 而另两块已经是悬浮屏了，体验被割裂：二维码/密码是盖在主界面上的
 * 悬浮窗，验证码却整个跳出去。用户要求三大验证统一成虚拟屏。
 *
 * 于是把这页从 Activity 改成 VirtualScreen：同样是那套内置 WebView +
 * 两层代理改写，但由 ScreenHost 托管，盖在主界面上，返回键直接回到 App。
 *
 * ── 两层改写缺一不可（这是老功能，坑记录保留）───────────────────
 *
 * 实例的 WebUI 只监听**服务器**的 127.0.0.1:实例端口，手机连不上，
 * 必须经管理服务的 /proxy/&lt;实例名&gt;/ 转发。NapCat 网页是 React 应用，
 * 资源用绝对路径（&lt;script src="/webui/assets/..."&gt;），所以：
 *
 *   ① HTTP 层（shouldInterceptRequest）：改写浏览器自己解析的静态资源。
 *      GET、无 body，在 HTTP 层改地址即可。
 *   ② JS 层（注入 shim）：页面 JS 自己发的 API 调用（如 POST
 *      /api/auth/login 带 JSON body）。shouldInterceptRequest 拿不到
 *      POST body（安卓公开 API 限制），硬拦会把登录请求变成空 body ——
 *      页面能显示、一登录就失败，看起来一切正常，最难查。
 *      所以必须在页面脚本执行前注入 shim，把 XHR/fetch 的地址换成代理
 *      前缀，body 原样交给真正的请求。
 *
 * 注入时机：onPageStarted 每次导航都注入（新文档 = 新 window）；
 * shouldInterceptRequest 命中 HTML 时直接把 shim 插进文档（确定性最高）。
 */
public final class WebScreen implements VirtualScreen {

    private final String base;             // 直连模式地址（经代理时也能用）
    private final int tunnelPort;          // ≤0 = 不用代理
    private final String instance;
    private final String managerToken;
    private final String webuiToken;
    private final boolean viaProxy;

    private WebView webView;
    private boolean loaded;
    private Context ctxForLoad;

    /**
     * @param base        直连模式下的 WebUI 地址（经代理时传 127.0.0.1 即可）
     * @param tunnelPort  隧道本地端口；&lt;=0 表示直连
     * @param instance    实例名；经代理时必填
     * @param managerToken 管理口令（走 X-Dafeiyu-Token 头）
     * @param webuiToken  实例 WebUI 口令；有值时网页自动登录
     */
    public WebScreen(String base, int tunnelPort, String instance,
                     String managerToken, String webuiToken) {
        this.base = base == null ? "" : base;
        this.tunnelPort = tunnelPort;
        this.instance = instance == null ? "" : instance;
        this.managerToken = managerToken == null ? "" : managerToken;
        this.webuiToken = webuiToken == null ? "" : webuiToken;
        this.viaProxy = tunnelPort > 0 && !this.instance.isEmpty()
                && !this.managerToken.isEmpty();
    }

    @Override
    public String title() {
        return "短信 / 网页验证";
    }

    @Override
    public View build(Context ctx) {
        LinearLayout root = new LinearLayout(ctx);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Theme.BG);

        TextView hint = UiKit.text(ctx,
                viaProxy
                        ? (webuiToken.isEmpty()
                            // 拿不到口令时如实说明，并告诉用户去哪找，
                            // 而不是让他对着「请输入token」发愣。
                            ? "下面是你服务器上这个机器人的 NapCat 网页。"
                              + "没能自动登录 —— 请在机器人页确认它是「已解锁」状态再进来。"
                            : "下面是你服务器上这个机器人的 NapCat 网页，已自动登录；"
                              + "登录后可扫码或密码登录。验证完成按返回键回到 App。")
                        : "下面是你填的那个 NapCat 网页：输 Token 登录后可扫码或密码登录。"
                          + "完成后按返回键回到 App。",
                12, Theme.DIM);
        hint.setPadding(Theme.dp(ctx, 12), Theme.dp(ctx, 8), Theme.dp(ctx, 12),
                Theme.dp(ctx, 8));
        root.addView(hint);

        webView = new WebView(ctx);
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);

        if (viaProxy) {
            // 两层改写（见类注释），缺一不可。
            webView.setWebViewClient(new ProxyClient());
        }

        webView.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        root.addView(webView);
        ctxForLoad = ctx;
        return root;
    }

    @Override
    public void onEnter() {
        // 视图已可见再加载，避免「屏还没显示就已经在请求」。
        if (!loaded && webView != null) {
            loaded = true;
            webView.loadUrl(viaProxy
                    ? WebProxyPath.withToken(
                            WebProxyPath.proxyUrlFor(tunnelPort, instance, "/webui/"),
                            webuiToken)
                    : NapCatClient.normalize(base) + "/");
        }
    }

    @Override
    public boolean onBack() {
        // 网页内部有历史：先回退，别一按返回键就把整屏关掉。
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return true;
        }
        return false;
    }

    @Override
    public void dispose() {
        // WebView 不 destroy 的话，它的渲染线程和 native 资源会一直留着
        // （这和二维码位图不 recycle 是同一类泄漏）。关屏即毁。
        if (webView != null) {
            try {
                webView.stopLoading();
            } catch (Throwable ignore) {
                // 已经卸载等极端情况下 stopLoading 也可能抛，关屏不能被卡住
            }
            try {
                webView.destroy();
            } catch (Throwable ignore) {
                // destroy 抛异常也要继续往下 —— 屏必须能关掉
            }
            webView = null;
        }
        ctxForLoad = null;
        loaded = false;
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

        /** 文档开始加载时也注入一次（双保险）。脚本幂等，重复注入无害。 */
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

        /** 老版本回调（API 21 起就有）。某些 ROM 可能走这条，行为保持一致。 */
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
                // ★ 主文档（HTML）要把改写脚本**插进去**再交给 WebView
                //   （比 onPageStarted 注入更确定：在页面任何脚本之前执行）。
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

    /** 把改写脚本插到 HTML 最前面（&lt;head&gt; 之后，或文档最开头）。 */
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
}