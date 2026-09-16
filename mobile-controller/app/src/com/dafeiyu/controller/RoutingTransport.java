package com.dafeiyu.controller;

import java.io.IOException;

/**
 * 会自己选路的 NapCat 传输层。
 *
 * 两种情况都要能用：
 *   ① 连了服务器（Session 里有隧道）→ 走管理服务代理，实例名从当前选中的机器人来。
 *      这是内部版的正常路径，用户什么都不用填。
 *   ② 没连服务器（公开版，或用户想直接连自己已有的 NapCat）→ 直连。
 *
 * 为什么不让 LoginView 自己判断：登录页要处理二维码、轮询、快捷登录好几处请求，
 * 每处都写一遍 if 判断迟早会漏一处，然后表现为「某个按钮连不上」——
 * 这种 bug 很难查。集中在一个类里，判断只有一份。
 */
public final class RoutingTransport implements NapCatClient.Transport {

    private final NapCatClient.Transport direct = new NapCatClient.Real();

    /** 当前选中的实例名；空表示没选，走直连。 */
    private static volatile String activeInstance = "";

    public static void setActiveInstance(String name) {
        activeInstance = name == null ? "" : name;
    }

    public static String activeInstance() {
        return activeInstance;
    }

    /** 是否处于「经服务器」模式。 */
    public static boolean viaServer() {
        return Session.connected() && !activeInstance.isEmpty();
    }

    public NapCatClient.Resp send(String method, String url, String bearer,
                                  String jsonBody, int timeoutMs)
            throws IOException {
        if (viaServer()) {
            ProxyTransport p = new ProxyTransport(
                    Session.tunnel().port(), Session.client().token(), activeInstance);
            return p.send(method, url, bearer, jsonBody, timeoutMs);
        }
        return direct.send(method, url, bearer, jsonBody, timeoutMs);
    }
}
