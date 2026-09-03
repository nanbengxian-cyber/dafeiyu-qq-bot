package com.dafeiyu.console;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 界面文案与状态页装配 —— 依然不碰任何 android.* API。
 *
 * 为什么把这些也抽出来：状态页是「一堆 Row」，模式/动作的确认文案是纯字符串拼接，
 * 这些恰恰是最容易出错又最难在真机上验证的部分（要造出「看门狗停摆」「代理挂了」
 * 这些状态，在真机上几乎没法复现）。放在这里，单测可以随手喂一份假 JSON 把
 * 每种异常状态都走一遍。
 *
 * 字段名一律以 console_api.py 的 build_status / build_schema 为准，
 * 不凭记忆写 —— 拼错一个键的表现是「那一行永远显示 —」，很难发现。
 */
public final class Ui {

    private Ui() {
    }

    /**
     * 状态页要显示的全部行，顺序即显示顺序。
     *
     * 排序原则：**越可能出事的排越前**。QQ 在线状态第一，二维码第二（只在掉线时出现），
     * 然后是两个容器，再是模型与工具能力，最后才是主机资源 —— 内存百分比很少是
     * 你打开这个 App 想看的第一件事。
     */
    public static List<StatusFmt.Row> statusRows(Map<String, Object> status,
                                                 List<Object> modes) {
        List<StatusFmt.Row> rows = new ArrayList<StatusFmt.Row>();
        add(rows, StatusFmt.qqRow(status));
        add(rows, StatusFmt.qrRow(status));
        for (Object c : Json.arr(status, "containers")) {
            if (c instanceof Map) {
                @SuppressWarnings("unchecked")
                Map<String, Object> cm = (Map<String, Object>) c;
                add(rows, StatusFmt.containerRow(cm));
            }
        }
        add(rows, StatusFmt.chatModelRow(status));
        add(rows, StatusFmt.visionRow(status));
        add(rows, StatusFmt.toolRow(status));
        add(rows, StatusFmt.modeRow(status, modes));
        add(rows, StatusFmt.talkRow(status));
        add(rows, StatusFmt.limitRow(status));
        add(rows, StatusFmt.ctxRow(status));
        add(rows, StatusFmt.pluginRow(status));
        add(rows, StatusFmt.proxyRow(status));
        add(rows, StatusFmt.memRow(status));
        add(rows, StatusFmt.diskRow(status));
        add(rows, StatusFmt.loadRow(status));
        return rows;
    }

    private static void add(List<StatusFmt.Row> rows, StatusFmt.Row r) {
        if (r != null) {
            rows.add(r);
        }
    }

    /**
     * 状态页顶部那句总结。
     *
     * 规则：**只报最坏的那一件事**。同时列「QQ 掉线」和「内存 80%」只会让人
     * 抓不住重点 —— 掉线时内存多少根本不重要。
     */
    public static StatusFmt.Row headline(Map<String, Object> status) {
        Object qq = Json.raw(status, "qq");
        String state = Json.str(qq, "state", "");
        boolean stale = Json.bool(qq, "watchdog_stale", false);

        if (state.isEmpty()) {
            return new StatusFmt.Row("总览", "读不到状态 —— 看门狗没在写 status.json", 2);
        }
        if (stale) {
            return new StatusFmt.Row("总览", "状态不明 —— 看门狗 "
                    + StatusFmt.dur(Json.lng(qq, "watchdog_age_seconds"))
                    + "没更新，下面的数据是旧的", 2);
        }
        if (!"online".equals(state)) {
            Boolean qr = Json.boolOrNull(qq, "qr_available");
            String tail = (qr != null && qr.booleanValue())
                    ? "，二维码已就绪，去扫码页登录" : "，二维码还没出来，稍等";
            return new StatusFmt.Row("总览", "机器人掉线了" + tail, 3);
        }
        // 在线时，按严重程度往下找问题
        Object rt = Json.raw(status, "runtime");
        if (Json.boolOrNull(rt, "tool_use_ok") != null && !Json.bool(rt, "tool_use_ok", true)) {
            return new StatusFmt.Row("总览", "在线，但工具能力异常 —— 会「嘴上答应却不发图」", 3);
        }
        for (Object c : Json.arr(status, "containers")) {
            if (!"running".equals(Json.str(c, "state", ""))) {
                return new StatusFmt.Row("总览",
                        "在线，但容器 " + Json.str(c, "name") + " 不在运行", 3);
            }
        }
        Long turns = Json.lng(rt, "context_turns");
        if (turns != null && turns < 0) {
            return new StatusFmt.Row("总览", "在线，但上下文没有上限 —— 曾导致答非所问", 3);
        }
        Object host = Json.raw(status, "host");
        Long memTotal = Json.lng(host, "mem_total");
        Long memAvail = Json.lng(host, "mem_available");
        if (memTotal != null && memAvail != null && memTotal > 0
                && memAvail * 100 / memTotal < 8) {
            return new StatusFmt.Row("总览", "在线，但内存快满了（可用 "
                    + StatusFmt.bytes(memAvail) + "）", 2);
        }
        return new StatusFmt.Row("总览", "一切正常", 1);
    }

    // ---------------------------------------------------------------- 确认文案

    /**
     * 危险动作的二次确认文案。
     *
     * 一定要把**后果**写出来而不是只问「确定吗」。尤其重启 QQ 客户端会掉线
     * 并大概率要重新扫码 —— 不写清楚，人在群里聊得正好的时候一按就出事。
     */
    public static String confirmAction(KnobModel.Action a) {
        StringBuilder sb = new StringBuilder();
        if (a.desc != null && !a.desc.isEmpty()) {
            sb.append(stripMd(a.desc));
        }
        if (a.danger) {
            if (sb.length() > 0) {
                sb.append("\n\n");
            }
            sb.append("这个操作会打断正在进行的对话。确定继续？");
        }
        return sb.toString();
    }

    public static String confirmMode(KnobModel.Mode m) {
        StringBuilder sb = new StringBuilder();
        sb.append("切到「").append(m.name).append("」");
        if (m.desc != null && !m.desc.isEmpty()) {
            sb.append("\n\n").append(stripMd(m.desc));
        }
        return sb.toString();
    }

    /**
     * 提交配置前的确认摘要 —— 把「哪几项从什么变成什么」列清楚。
     *
     * 服务端的写入是读改写，提交后就生效了，没有撤销。所以宁可多一次确认，
     * 也不要让人在滑块上手滑之后才发现机器人变成了话痨。
     */
    public static String confirmChanges(KnobModel.Schema schema,
                                        Map<String, Object> current,
                                        Map<String, Object> changed) {
        StringBuilder sb = new StringBuilder();
        sb.append("要改这 ").append(changed.size()).append(" 项：\n");
        for (Map.Entry<String, Object> e : changed.entrySet()) {
            KnobModel.Knob k = schema.knob(e.getKey());
            if (k == null) {
                continue;
            }
            sb.append("\n· ").append(k.name).append("：")
                    .append(KnobModel.show(k, current.get(e.getKey())))
                    .append(" → ").append(KnobModel.show(k, e.getValue()));
            if (!k.hot) {
                sb.append("（要重启才生效）");
            }
        }
        if (KnobModel.needsRestart(schema, changed)) {
            sb.append("\n\n其中有项目需要重启机器人容器，重启期间群里不回话。");
        }
        return sb.toString();
    }

    /** 服务端的 hint 里带 Markdown 强调（`**x**`），手机上直接显示会很难看。 */
    public static String stripMd(String s) {
        if (s == null) {
            return "";
        }
        return s.replace("**", "").replace("`", "");
    }

    // ---------------------------------------------------------------- 模型列表

    /**
     * 模型选择列表：当前用的那个排第一并标注，其余保持服务端给的顺序。
     *
     * 为什么要重排：列表可能有十几项，人一打开最想确认的是「现在用的是哪个」。
     */
    public static List<String> modelChoices(List<String> models, String current) {
        List<String> out = new ArrayList<String>();
        if (current != null && !current.isEmpty()) {
            out.add(current + "（当前）");
        }
        for (String m : models) {
            if (m == null || m.isEmpty()) {
                continue;
            }
            if (current != null && m.equals(current)) {
                continue;
            }
            out.add(m);
        }
        if (out.isEmpty()) {
            out.add("（服务器没给模型列表）");
        }
        return out;
    }

    /** 把上面那个带「（当前）」后缀的选项还原成真模型名。 */
    public static String modelOf(String choice) {
        if (choice == null) {
            return "";
        }
        int i = choice.indexOf("（当前）");
        return i < 0 ? choice : choice.substring(0, i);
    }

    /**
     * 识图渠道的可选项。
     *
     * 服务端只给 {id, model}，所以显示名要在这里拼。两条链的差别很大
     * （opus 链贵但准、智谱直连便宜快），不标注的话没人知道该选哪个。
     */
    public static List<String> visionChoices(KnobModel.Schema schema, String current) {
        List<String> out = new ArrayList<String>();
        for (Map.Entry<String, String> e : schema.visionProviders.entrySet()) {
            out.add(visionLabel(e.getKey(), e.getValue(), e.getKey().equals(current)));
        }
        if (out.isEmpty()) {
            out.add("（服务器没给识图渠道）");
        }
        return out;
    }

    static String visionLabel(String id, String model, boolean isCurrent) {
        String note = "vision-opus5".equals(id) ? " · 三档故障转移，准但慢"
                : ("zhipu-vision".equals(id) ? " · 直连，便宜快" : "");
        String base = (model == null || model.isEmpty() ? id : model) + note;
        return isCurrent ? base + "（当前）" : base;
    }

    /** 从上面那个显示串还原出 provider_id。 */
    public static String visionIdOf(KnobModel.Schema schema, String choice) {
        for (Map.Entry<String, String> e : schema.visionProviders.entrySet()) {
            if (choice != null
                    && (choice.equals(visionLabel(e.getKey(), e.getValue(), false))
                        || choice.equals(visionLabel(e.getKey(), e.getValue(), true)))) {
                return e.getKey();
            }
        }
        return "";
    }

    // ---------------------------------------------------------------- 插件

    public static final class PluginItem {
        public final String name;
        public final String label;
        public final boolean enabled;
        public final String version;

        PluginItem(String name, String label, boolean enabled, String version) {
            this.name = name;
            this.label = label;
            this.enabled = enabled;
            this.version = version;
        }
    }

    /**
     * 插件列表。服务端已经只回 dsh-*（框架自带的关掉会出事，写操作也拒绝 403），
     * 这里再挡一道纯属保险 —— 万一以后服务端放宽了，手机上也不该显示能关的假象。
     */
    public static List<PluginItem> plugins(Map<String, Object> status) {
        List<PluginItem> out = new ArrayList<PluginItem>();
        for (Object o : Json.arr(status, "plugins")) {
            String name = Json.str(o, "name");
            if (name.isEmpty() || !name.startsWith("dsh-")) {
                continue;
            }
            out.add(new PluginItem(name, Json.str(o, "label", name),
                    Json.bool(o, "enabled", false), Json.str(o, "version")));
        }
        return out;
    }

    // ---------------------------------------------------------------- 错误话术

    /**
     * 把异常翻成「人能照着做点什么」的话。
     *
     * 直接显示 `java.net.SocketTimeoutException` 对用户零帮助。每条都要带
     * 「可能是什么原因」或「该怎么办」。
     */
    public static String explain(Throwable exc) {
        if (exc == null) {
            return "不知道哪里出错了";
        }
        if (exc instanceof Api.ApiException) {
            Api.ApiException ae = (Api.ApiException) exc;
            String msg = ae.getMessage() == null ? "" : ae.getMessage();
            if (ae.code == 401) {
                return "登录过期了，重新输一次密码";
            }
            if (ae.code == 403) {
                return msg.isEmpty() ? "这一项服务器不允许改" : msg;
            }
            if (ae.code == 409) {
                return "有人同时在网页后台改了配置，下拉刷新后再改一次";
            }
            if (ae.code == 429) {
                return msg.isEmpty() ? "密码试太多次被锁了，等一会儿" : msg;
            }
            if (ae.code == 503) {
                return "服务器上的控制台后端没装载：" + msg;
            }
            return msg.isEmpty() ? ("服务器回了 " + ae.code) : msg;
        }
        if (exc instanceof Json.JsonError) {
            return "服务器回的内容看不懂（" + exc.getMessage() + "）";
        }
        String cls = exc.getClass().getName();
        if (cls.contains("UnknownHost")) {
            return "找不到服务器地址，检查填的 IP 对不对";
        }
        if (cls.contains("Timeout") || cls.contains("timedout")) {
            return "连接超时。要么手机没网，要么服务器忙 —— 重启容器那类操作本来就慢，稍等再试";
        }
        if (cls.contains("ConnectException") || cls.contains("NoRouteToHost")) {
            return "连不上服务器。检查 8088 端口是否还开着、服务器是否在跑";
        }
        if (cls.contains("SSL") || cls.contains("Ssl")) {
            return "TLS 出错。8088 是明文 HTTP，地址别写成 https://";
        }
        String m = exc.getMessage();
        return (m == null || m.isEmpty()) ? cls : m;
    }

    // ---------------------------------------------------------------- 一览摘要

    /**
     * 配置页顶部的一行摘要。
     * 服务端已经反推过模式（status.mode），这里只负责在推不出时给一句有用的话。
     */
    public static String configSummary(Map<String, Object> status,
                                       List<Object> modes,
                                       Map<String, Object> values) {
        String id = Json.str(status, "mode", "");
        if (!id.isEmpty()) {
            for (Object m : modes) {
                if (id.equals(Json.str(m, "id"))) {
                    return "当前匹配「" + Json.str(m, "name", id) + "」";
                }
            }
            return "当前匹配「" + id + "」";
        }
        Object on = values.get("provider_ltm_settings.active_reply.enable");
        if (on != null && !KnobModel.truthy(on)) {
            return "自定义：不主动插话";
        }
        Object p = values.get("provider_ltm_settings.active_reply.possibility_reply");
        if (p != null) {
            long pct = Math.round(KnobModel.asDouble(p, 0) * 100);
            return "自定义：插话概率 " + pct + "%";
        }
        return "自定义配置";
    }

    /**
     * 状态页最后一行：这份数据是什么时候的。
     * 服务端给的是 server_time 字符串（本地时区），直接显示，不做时区换算 ——
     * 手机和服务器时区不同时，自己算只会算错。
     */
    public static String freshness(Map<String, Object> status) {
        String t = Json.str(status, "server_time", "");
        return t.isEmpty() ? "下拉刷新" : ("服务器时间 " + t + " · 下拉刷新");
    }

    /** 给 Map 排个稳定顺序，界面刷新时行不会跳。 */
    public static Map<String, Object> stable(Map<String, Object> in) {
        return in == null ? new LinkedHashMap<String, Object>()
                : new LinkedHashMap<String, Object>(in);
    }
}
