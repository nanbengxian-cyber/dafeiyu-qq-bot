package com.dafeiyu.controller;

import java.io.File;
import java.io.FileInputStream;
import java.security.MessageDigest;
import java.util.Map;

/**
 * 公告 + 更新检查的**纯逻辑**（刻意不碰 android.*，见 run-tests.sh 对
 * PURE 类的注释）。
 *
 * 服务器 GET /app/update 返回的结构（见服务端 app_update_info）：
 *   {"latest_code": 2, "latest_name": "1.2.0",
 *    "announcement": "…公告正文…", "sha256": "abc…"}
 *
 * App 只在两种情况下提醒用户：
 *   ① latest_code > 本地 versionCode —— 有新版本，询问是否下载安装；
 *   ② announcement 非空 —— 只有公告没有新版本，纯展示。
 *
 * 两个字段互不依赖：可能「有新版没公告」也可能「只有公告没新版」。
 */
public final class AppUpdate {

    /** 服务器返回的字段名。App 与服务器约定（两侧各自有测试钉死）。 */
    public static final String K_CODE = "latest_code";
    public static final String K_NAME = "latest_name";
    public static final String K_ANNOUNCEMENT = "announcement";
    public static final String K_SHA256 = "sha256";

    public final long latestCode;       // ≤0 表示服务器没配更新
    public final String latestName;
    public final String announcement;
    public final String sha256;

    private AppUpdate(long code, String name, String ann, String sha) {
        this.latestCode = code <= 0 ? 0 : code;
        this.latestName = name == null ? "" : name;
        this.announcement = ann == null ? "" : ann;
        this.sha256 = sha == null ? "" : sha;
    }

    /** 服务器返回的空壳（没配置过）：latestCode=0、公告为空。 */
    public static AppUpdate empty() {
        return new AppUpdate(0, "", "", "");
    }

    /**
     * 从服务器 GET /app/update 的响应 Map 解析。
     * ★ 全程 get(...) + 判空，不索引 —— 服务器少个字段时返回空壳，
     *   绝不能让 App 直接崩（那会把「没配公告」变成「App 打不开」）。
     */
    @SuppressWarnings("unchecked")
    public static AppUpdate from(Map<String, Object> m) {
        if (m == null) {
            return empty();
        }
        long code = 0;
        String name = "";
        String ann = "";
        String sha = "";
        Object c = m.get(K_CODE);
        if (c instanceof Long) {
            code = ((Long) c).longValue();
        } else if (c instanceof Number) {
            code = ((Number) c).longValue();
        }
        Object n = m.get(K_NAME);
        if (n instanceof String) {
            name = (String) n;
        }
        Object a = m.get(K_ANNOUNCEMENT);
        if (a instanceof String) {
            ann = (String) a;
        }
        Object s = m.get(K_SHA256);
        if (s instanceof String) {
            sha = ((String) s).trim().toLowerCase();
        }
        return new AppUpdate(code, name, ann, sha);
    }

    /** 有没有新版本（服务器版本号 > 本地 versionCode）。 */
    public boolean hasUpdate(long localCode) {
        return latestCode > 0 && latestCode > localCode;
    }

    /** 有没有要展示的公告。 */
    public boolean hasAnnouncement() {
        return announcement != null && !announcement.trim().isEmpty();
    }

    /**
     * 校验下载好的 APK 的 sha256 是否和服务器声明的一致。
     *
     * ★ 为什么必须校验：APK 从服务器经隧道直传，防的是**下载截断/传坏**这个
     *   正常风险（几 MB 文件在弱网下最常见的坏法就是中途断掉，装一个半截
     *   的 APK 要么装不上、要么装上是坏的）。**不要**把 sha256 当成安全边界：
     *   隧道本身已有双重认证（SSH 隧道 + Bearer token），下载通道是可信的，
     *   这里只是完整性校验，不是鉴权。
     *
     * 服务器没给 sha256、或给的是空串 → 返回 true（无凭据可验，放行？不——
     * 见调用方：没 sha256 时 ManagerClient 已经用 Content-Length 兜底，
     * 这里空凭据直接返回「无法判断」，由上层决定怎么处理）。
     */
    public boolean sha256Matches(File apk) {
        if (sha256 == null || sha256.isEmpty() || apk == null || !apk.exists()) {
            return false;
        }
        try {
            String actual = sha256File(apk);
            return sha256.equals(actual);
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * 文件 sha256（小写十六进制）。纯逻辑，测试可直接算。
     */
    public static String sha256File(File f) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        FileInputStream in = new FileInputStream(f);
        byte[] buf = new byte[65536];
        int n;
        try {
            while ((n = in.read(buf)) > 0) {
                md.update(buf, 0, n);
            }
        } finally {
            in.close();
        }
        return hex(md.digest());
    }

    /** 字节数组 sha256（小写十六进制）—— 测试和校验都用同一个实现。 */
    public static String sha256(byte[] data) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        return hex(md.digest(data));
    }

    private static String hex(byte[] digest) {
        StringBuilder sb = new StringBuilder();
        for (byte b : digest) {
            sb.append(String.format("%02x", b & 0xff));
        }
        return sb.toString();
    }
}