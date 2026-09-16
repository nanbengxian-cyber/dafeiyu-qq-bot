package com.dafeiyu.controller;

/**
 * 当前连接状态 —— 隧道和管理服务客户端的全局持有者。
 *
 * 为什么要有这个类：几个页面（服务器页、机器人页）都要用同一条隧道。
 * 如果各自建一条，服务器上会开一堆连接，而且状态会不一致
 * （一个页面显示已连、另一个显示没连，用户会懵）。
 * 这里放一份，大家共用。
 *
 * 注意：管理口令只放在内存里，不落盘。App 退出就没了 ——
 * 反正重新连一次只需要点一下按钮，而落盘就等于把服务器管理权限
 * 写在了手机上。
 */
public final class Session {

    private static Tunnel tunnel;
    private static ManagerClient client;

    private Session() {
    }

    /** 连接建立后登记（由服务器页调用）。 */
    public static synchronized void set(Tunnel t, ManagerClient c) {
        close();
        tunnel = t;
        client = c;
    }

    /** 当前是否可用（隧道活着 + 有客户端）。 */
    public static synchronized boolean connected() {
        return tunnel != null && tunnel.alive() && client != null;
    }

    /** 取客户端；没连时返回 null。 */
    public static synchronized ManagerClient client() {
        return connected() ? client : null;
    }

    /** 当前隧道；没连时返回 null。 */
    public static synchronized Tunnel tunnel() {
        return connected() ? tunnel : null;
    }

    /** 断开并清空。 */
    public static synchronized void close() {
        if (tunnel != null) {
            tunnel.close();
        }
        tunnel = null;
        client = null;
    }
}
