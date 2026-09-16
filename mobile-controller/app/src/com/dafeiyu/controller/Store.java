package com.dafeiyu.controller;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 本地持久化 —— 只存**非敏感**字段，这条边界是这个类唯一值得说的事：
 *
 * 存：服务器地址/端口/用户名、仓库地址、部署目录、端口偏好、
 *     WebUI 地址（不含 token）、上次填的 QQ 号、SSH 主机指纹（首次连接后记录）。
 * 不存：SSH 密码、WebUI token、TOTP 动态码、QQ 密码。一次都不存，
 *     进程一死就没了 —— 和桌面控制台同一套约定（safe_profile 只挑安全字段）。
 *
 * SharedPreferences MODE_PRIVATE，未 root 的手机上其他 App 读不到。
 * 这不是加密存储，文档里如实写明，不假装它很安全。
 */
public final class Store {

    private static final String FILE = "controller";
    private static final String K_TAB = "tab";

    private final SharedPreferences sp;

    public Store(Context ctx) {
        this.sp = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    public int tab() {
        return sp.getInt(K_TAB, 0);
    }

    public void setTab(int v) {
        sp.edit().putInt(K_TAB, v).apply();
    }

    /** 读写都是白名单字段，密码永远进不来（键名压根不出现）。 */
    public Map<String, String> profile() {
        Map<String, String> out = new LinkedHashMap<String, String>();
        for (String key : DeployConfig.PERSIST_KEYS) {
            out.put(key, sp.getString(key, ""));
        }
        return out;
    }

    public void saveProfile(Map<String, String> values) {
        SharedPreferences.Editor ed = sp.edit();
        for (String key : DeployConfig.PERSIST_KEYS) {
            String v = values.get(key);
            ed.putString(key, v == null ? "" : v);
        }
        ed.apply();
    }

    /** 上次「密码登录」填的 QQ 号（不是密码）。 */
    public String lastUin() {
        return sp.getString("last_uin", "");
    }

    public void setLastUin(String uin) {
        sp.edit().putString("last_uin", uin == null ? "" : uin).apply();
    }

    /** 上次连接的 WebUI 地址（不含 token）。 */
    public String webuiBase() {
        return sp.getString("webui_base", "");
    }

    public void setWebuiBase(String base) {
        sp.edit().putString("webui_base", base == null ? "" : base).apply();
    }

    // ------------------------------------------------ SSH 主机指纹

    private static String keyOf(String host, int port) {
        return "hostkey_" + host + "_" + port;
    }

    /** 已记录的指纹（type|base64），没连过返回 ""。 */
    public String hostKey(String host, int port) {
        return sp.getString(keyOf(host, port), "");
    }

    public void rememberHostKey(String host, int port, String type, String base64) {
        sp.edit().putString(keyOf(host, port), type + "|" + base64).apply();
    }
}
