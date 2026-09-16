package com.dafeiyu.controller;

import android.app.Activity;
import android.os.Bundle;
import android.view.ViewGroup;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * 内置网页登录页 —— 把服务器上的 NapCat WebUI 装进 App 的 WebView。
 *
 * 什么时候用它：
 * - 密码登录触发了腾讯的验证码 / 新设备验证 —— 那套验证组件是网页 JS，
 *   官方网页登录页能直接跑完，App 原生页面做不了；
 * - WebUI 开了两步验证但手边没有验证器；
 * - 或者就是想看官方页面的完整信息。
 *
 * 明文 HTTP 由 usesCleartextTraffic 放行（文档里如实写了风险）。
 * 不注入任何脚本、不拦截任何请求 —— 它就是一个原样的浏览器窗口。
 */
public final class WebLoginActivity extends Activity {

    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        String base = getIntent().getStringExtra("base");
        if (base == null || base.isEmpty()) {
            finish();
            return;
        }

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Theme.BG);

        TextView hint = UiKit.text(this,
                "下面是你服务器上的 NapCat 网页：输 Token 登录后可扫码或密码登录。"
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
        webView.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        root.addView(webView);
        setContentView(root);

        webView.loadUrl(NapCatClient.normalize(base) + "/");
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
