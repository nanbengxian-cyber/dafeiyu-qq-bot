package com.dafeiyu.controller;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * 把 NapCat 的请求经管理服务转发 —— NapCatClient.Transport 的实现。
 *
 * 为什么要这一层：NapCat 的 WebUI 只监听服务器本机（127.0.0.1:实例端口），
 * 手机根本连不上。管理服务在服务器上，能连到它，于是让管理服务替我们转发。
 * 这样 App 只需要一条 SSH 隧道，不用给每个实例开端口转发。
 *
 * NapCat 的凭据走 Authorization 头，管理口令走 X-Dafeiyu-Token ——
 * 两个令牌分开走不同的头，不然会互相覆盖（这个坑踩过）。
 */
public final class ProxyTransport implements NapCatClient.Transport {

    private final String localBase;   // http://127.0.0.1:<隧道端口>
    private final String managerToken;
    private final String instance;

    public ProxyTransport(int tunnelPort, String managerToken, String instance) {
        this.localBase = "http://127.0.0.1:" + tunnelPort;
        this.managerToken = managerToken;
        this.instance = instance;
    }

    @Override
    public NapCatClient.Resp send(String method, String url, String bearer,
                                  String jsonBody, int timeoutMs) throws IOException {
        // NapCatClient 给的是完整 URL（base + path）。这里只取路径部分，
        // 换成走管理服务的 /proxy/<实例>/<路径>。
        String path = pathOf(url);
        String full = localBase + "/proxy/" + enc(instance) + "/" + path;

        HttpURLConnection conn = (HttpURLConnection) new URL(full).openConnection();
        try {
            conn.setRequestMethod(method);
            conn.setConnectTimeout(Math.min(timeoutMs, 15000));
            conn.setReadTimeout(timeoutMs);
            conn.setInstanceFollowRedirects(false);
            conn.setRequestProperty("Accept", "application/json");
            // 管理口令：用专用头，避免和 NapCat 的 Authorization 打架
            conn.setRequestProperty("X-Dafeiyu-Token", managerToken);
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
            return new NapCatClient.Resp(code, text);
        } finally {
            conn.disconnect();
        }
    }

    /** 从完整 URL 里取路径（含查询串）。公开是为了能被单测直接验证 ——
     * 这段拼错了会让请求打到错的接口上，表现是「登录没反应」，很难查。 */
    public static String pathOf(String url) {
        if (url == null) {
            return "";
        }
        int i = url.indexOf("://");
        if (i < 0) {
            return url;
        }
        int slash = url.indexOf('/', i + 3);
        return slash < 0 ? "" : url.substring(slash + 1);
    }

    private static String enc(String s) {
        try {
            return java.net.URLEncoder.encode(s == null ? "" : s, "UTF-8");
        } catch (java.io.UnsupportedEncodingException e) {
            return s == null ? "" : s;
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
