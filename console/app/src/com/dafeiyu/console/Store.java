package com.dafeiyu.console;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * 本地持久化 —— 服务器地址、登录 cookie、上次看到的模式。
 *
 * 存什么、不存什么，是这个类唯一值得说的事：
 *   **存**：服务器地址（省得每次输）、会话 cookie（7 天有效，省得每次输密码）。
 *   **不存**：密码。一次都不存。
 *
 * 为什么不存密码：8088 是明文 HTTP，密码在网络上已经够危险了，再在手机上落一份
 * 纯属白送。cookie 是服务端签的、7 天自动失效、丢了顶多别人能看你机器人的状态，
 * 拿不到密码就改不了密码、也进不了别的地方。风险面小得多。
 *
 * SharedPreferences 用 MODE_PRIVATE，其他 App 读不到（未 root 的前提下）。
 * 这不是加密存储 —— 手机被 root 或被物理接触时 cookie 是能捞出来的。
 * 要真正的安全得上 TLS + Keystore，那超出这个工具的范围，
 * 所以文档里明说了这一点，而不是假装它很安全。
 */
public final class Store {

    private static final String FILE = "console";
    private static final String K_BASE = "base";
    private static final String K_COOKIE = "cookie";
    private static final String K_LAST_MODE = "last_mode";
    private static final String K_TAB = "tab";

    private final SharedPreferences sp;

    public Store(Context ctx) {
        this.sp = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    public String base() {
        return sp.getString(K_BASE, "");
    }

    public void setBase(String v) {
        sp.edit().putString(K_BASE, v == null ? "" : v).apply();
    }

    public String cookie() {
        return sp.getString(K_COOKIE, "");
    }

    public void setCookie(String v) {
        sp.edit().putString(K_COOKIE, v == null ? "" : v).apply();
    }

    /** 退出登录：只清 cookie，留着地址 —— 下次登录不用重新输 IP。 */
    public void clearCookie() {
        sp.edit().remove(K_COOKIE).apply();
    }

    public boolean loggedIn() {
        return !base().isEmpty() && !cookie().isEmpty();
    }

    public String lastMode() {
        return sp.getString(K_LAST_MODE, "");
    }

    public void setLastMode(String v) {
        sp.edit().putString(K_LAST_MODE, v == null ? "" : v).apply();
    }

    /** 记住上次看的页签，回到 App 时停在原处。 */
    public int tab() {
        return sp.getInt(K_TAB, 0);
    }

    public void setTab(int v) {
        sp.edit().putInt(K_TAB, v).apply();
    }

    public Api api() {
        return new Api(base(), cookie());
    }
}
