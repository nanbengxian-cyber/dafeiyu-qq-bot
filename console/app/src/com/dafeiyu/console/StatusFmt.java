package com.dafeiyu.console;

import java.util.List;
import java.util.Map;

/**
 * 把服务端的状态 JSON 变成界面上的一行行文字。
 *
 * 单独一个类、不碰任何 Android API，是为了能在普通 JVM 上单测 ——
 * 状态页最容易出的不是崩溃，是「显示得不对」（比如秒数换算错、
 * 看门狗停摆了还显示在线）。这种错只有把格式化逻辑隔离出来才测得动。
 */
public final class StatusFmt {

    private StatusFmt() {
    }

    /** 一行状态：标签、值、色调。 */
    public static final class Row {
        public final String label;
        public final String value;
        /** 0 普通 / 1 好 / 2 警告 / 3 坏 */
        public final int tone;

        public Row(String label, String value, int tone) {
            this.label = label;
            this.value = value;
            this.tone = tone;
        }

        @Override
        public String toString() {
            return label + "=" + value + "#" + tone;
        }
    }

    /** 秒 → 「3天5小时」这种人话。 */
    public static String dur(Long seconds) {
        if (seconds == null || seconds < 0) {
            return "—";
        }
        long s = seconds.longValue();
        if (s < 60) {
            return s + " 秒";
        }
        long m = s / 60;
        if (m < 60) {
            return m + " 分钟";
        }
        long h = m / 60;
        if (h < 24) {
            long rm = m % 60;
            return rm > 0 ? h + " 小时 " + rm + " 分" : h + " 小时";
        }
        long d = h / 24;
        long rh = h % 24;
        return rh > 0 ? d + " 天 " + rh + " 小时" : d + " 天";
    }

    /** 字节 → MB/GB。内存和磁盘都用它，保证两处口径一致。 */
    public static String bytes(Long n) {
        if (n == null || n < 0) {
            return "—";
        }
        double v = n.doubleValue();
        if (v < 1024) {
            return (long) v + " B";
        }
        String[] units = {"KB", "MB", "GB", "TB"};
        int i = -1;
        while (v >= 1024 && i < units.length - 1) {
            v /= 1024;
            i++;
        }
        return (v >= 100 ? String.valueOf(Math.round(v)) : String.format("%.1f", v)) + " " + units[i];
    }

    // ---------------------------------------------------------------- QQ

    /**
     * QQ 状态行。
     *
     * 这里有个必须守住的规则：**看门狗写的 status.json 过期了，就不许把里面的
     * state 当真**。看门狗每 10 秒写一次，超过 60 秒没动说明它自己挂了，
     * 那份「在线」是化石。之前 v1 判定就吃过这个亏：QQ 静默掉线时 NapCat 什么
     * 日志都不打，页面绿了整整一小时。所以过期时状态显示成「不确定」。
     */
    public static Row qqRow(Map<String, Object> status) {
        Object qq = Json.raw(status, "qq");
        String state = Json.str(qq, "state", "");
        String msg = Json.str(qq, "message", "");
        boolean stale = Json.bool(qq, "watchdog_stale", false);
        Long age = Json.lng(qq, "watchdog_age_seconds");

        if (state.isEmpty()) {
            return new Row("QQ", "读不到状态（看门狗没在写？）", 2);
        }
        if (stale) {
            return new Row("QQ", "不确定 · 看门狗 " + dur(age) + " 没更新", 2);
        }
        boolean online = "online".equals(state);
        String text = !msg.isEmpty() ? msg : (online ? "在线" : state);
        Long seen = Json.lng(qq, "last_seen_seconds");
        if (online && seen != null) {
            text += " · " + dur(seen) + "前有消息";
        }
        return new Row("QQ", text, online ? 1 : 3);
    }

    /** 掉线时才提二维码：在线状态下说「码还剩 xx 秒」纯属噪音。 */
    public static Row qrRow(Map<String, Object> status) {
        Object qq = Json.raw(status, "qq");
        if ("online".equals(Json.str(qq, "state", "")) && !Json.bool(qq, "watchdog_stale", false)) {
            return null;
        }
        Boolean has = Json.boolOrNull(qq, "qr_available");
        if (has == null || !has.booleanValue()) {
            return new Row("二维码", "还没生成，稍等或重启 QQ 客户端", 2);
        }
        Long age = Json.lng(qq, "qr_age_seconds");
        return new Row("二维码", "可扫 · 已生成 " + dur(age) + "（看门狗超 100 秒会自动换码）", 1);
    }

    // ---------------------------------------------------------------- 容器

    public static Row containerRow(Map<String, Object> c) {
        String name = Json.str(c, "name");
        String label = "napcat".equals(name) ? "QQ 客户端" : ("astrbot".equals(name) ? "机器人" : name);
        String state = Json.str(c, "state", "unknown");
        if (!"running".equals(state)) {
            return new Row(label, "missing".equals(state) ? "容器不存在" : state, 3);
        }
        StringBuilder sb = new StringBuilder("运行 " + dur(Json.lng(c, "up_seconds")));
        Long mem = Json.lng(c, "mem_bytes");
        if (mem != null) {
            sb.append(" · 内存 ").append(bytes(mem));
        }
        Long re = Json.lng(c, "restarts");
        int tone = 1;
        if (re != null && re > 0) {
            sb.append(" · 重启过 ").append(re).append(" 次");
            tone = 2;
        }
        return new Row(label, sb.toString(), tone);
    }

    // ---------------------------------------------------------------- 主机

    public static Row memRow(Map<String, Object> status) {
        Object h = Json.raw(status, "host");
        Long total = Json.lng(h, "mem_total");
        Long avail = Json.lng(h, "mem_available");
        if (total == null || avail == null) {
            return new Row("内存", "—", 0);
        }
        long used = total - avail;
        int pct = (int) Math.round(used * 100.0 / total);
        // 这台机器只有 1.9G，内存是最先出事的资源，所以 90% 就标红。
        int tone = pct >= 90 ? 3 : (pct >= 75 ? 2 : 1);
        return new Row("内存", bytes(used) + " / " + bytes(total) + "（" + pct + "%）", tone);
    }

    public static Row diskRow(Map<String, Object> status) {
        Object h = Json.raw(status, "host");
        Long total = Json.lng(h, "disk_total");
        Long free = Json.lng(h, "disk_free");
        if (total == null || free == null) {
            return new Row("磁盘", "—", 0);
        }
        int pct = (int) Math.round((total - free) * 100.0 / total);
        int tone = pct >= 90 ? 3 : (pct >= 80 ? 2 : 1);
        return new Row("磁盘", "剩 " + bytes(free) + "（已用 " + pct + "%）", tone);
    }

    public static Row loadRow(Map<String, Object> status) {
        Object h = Json.raw(status, "host");
        Double l1 = Json.dbl(h, "load1");
        if (l1 == null) {
            return new Row("负载", "—", 0);
        }
        // 2 核，load 超 2 就是排队了
        int tone = l1 >= 3 ? 3 : (l1 >= 2 ? 2 : 1);
        return new Row("负载", Json.num(l1) + "（2 核）", tone);
    }

    // ---------------------------------------------------------------- 代理

    public static Row proxyRow(Map<String, Object> status) {
        Object p = Json.raw(status, "proxy");
        Boolean active = Json.boolOrNull(p, "proxy_active");
        if (active == null) {
            active = Json.boolOrNull(p, "active");
        }
        Boolean tunnel = Json.boolOrNull(p, "tunnel_healthy");
        String ip = Json.str(p, "egress_ip", "");
        if (active == null && tunnel == null) {
            return new Row("大陆代理", "—", 0);
        }
        if (active != null && active.booleanValue()) {
            return new Row("大陆代理", "已启用 · 出口 " + (ip.isEmpty() ? "?" : ip), 1);
        }
        // fail-open 回直连是**设计如此**（黑洞比被踢更糟），所以这不算错误，只是提示。
        return new Row("大陆代理", tunnel != null && tunnel.booleanValue()
                ? "隧道通但规则没挂" : "已回落直连（隧道不通时自动摘规则）", 2);
    }

    // ---------------------------------------------------------------- 模型

    public static Row chatModelRow(Map<String, Object> status) {
        Object m = Json.raw(status, "models");
        String model = Json.str(m, "chat_model", "");
        return new Row("聊天模型", model.isEmpty() ? "—" : model, model.isEmpty() ? 2 : 0);
    }

    public static Row visionRow(Map<String, Object> status) {
        Object m = Json.raw(status, "models");
        String pid = Json.str(m, "vision_provider", "");
        String model = Json.str(m, "vision_model", "");
        if (pid.isEmpty()) {
            return new Row("识图", "—", 2);
        }
        String extra = "vision-opus5".equals(pid) ? "（三档故障转移链）"
                : ("zhipu-vision".equals(pid) ? "（直连，便宜快）" : "");
        return new Row("识图", (model.isEmpty() ? pid : model) + extra, 0);
    }

    /**
     * 工具能力行 —— 这是全屏最该看的一行。
     *
     * modalities 里少了 tool_use，框架会**静默**丢掉整个工具集（只打 debug 日志，
     * 线上 INFO 级看不见），表现是机器人嘴上答应「好的马上画」然后什么都不发。
     * 所以这里不光看配置，还看最近一小时真的调过没有：配置对但一小时没调过，
     * 那就是可疑而不是正常。
     */
    public static Row toolRow(Map<String, Object> status) {
        Object rt = Json.raw(status, "runtime");
        boolean ok = Json.bool(rt, "tool_use_ok", false);
        if (!ok) {
            return new Row("工具能力", "⚠ modalities 缺 tool_use，出图/语音全会失效", 3);
        }
        Object tools = Json.raw(status, "tools");
        Long n = Json.lng(tools, "count");
        if (n == null) {
            return new Row("工具能力", "配置正常", 1);
        }
        if (n == 0) {
            return new Row("工具能力", "配置正常 · 最近 1 小时没调用过", 2);
        }
        List<Object> names = Json.arr(tools, "tools");
        StringBuilder sb = new StringBuilder("正常 · 1 小时内调了 " + n + " 次");
        if (!names.isEmpty()) {
            sb.append("（").append(names.get(names.size() - 1)).append("…）");
        }
        return new Row("工具能力", sb.toString(), 1);
    }

    // ---------------------------------------------------------------- 行为

    public static Row talkRow(Map<String, Object> status) {
        Object rt = Json.raw(status, "runtime");
        Boolean active = Json.boolOrNull(rt, "active_reply");
        Double p = Json.dbl(rt, "possibility");
        if (active == null) {
            return new Row("主动插话", "—", 0);
        }
        if (!active.booleanValue()) {
            return new Row("主动插话", "关闭 · 只在被叫到时回", 0);
        }
        String pct = p == null ? "?" : Json.num(Math.round(p * 100)) + "%";
        return new Row("主动插话", "开启 · 概率 " + pct, 0);
    }

    public static Row limitRow(Map<String, Object> status) {
        Object rt = Json.raw(status, "runtime");
        String v = Json.str(rt, "rate_limit", "—");
        return new Row("限流", v, 0);
    }

    public static Row ctxRow(Map<String, Object> status) {
        Object rt = Json.raw(status, "runtime");
        Long n = Json.lng(rt, "context_turns");
        if (n == null) {
            return new Row("上下文", "—", 0);
        }
        // -1 = 不截断，正是之前答非所问的第一病因，必须标红
        if (n < 0) {
            return new Row("上下文", "不限（危险：曾导致答非所问）", 3);
        }
        return new Row("上下文", "保留 " + n + " 轮", 0);
    }

    public static Row modeRow(Map<String, Object> status, List<Object> modes) {
        String id = Json.str(status, "mode", "");
        if (id.isEmpty()) {
            return new Row("当前模式", "自定义", 0);
        }
        for (Object m : modes) {
            if (id.equals(Json.str(m, "id"))) {
                return new Row("当前模式", Json.str(m, "emoji") + " " + Json.str(m, "name"), 1);
            }
        }
        return new Row("当前模式", id, 0);
    }

    /** 插件摘要：开了几个、关了哪几个。全开时不必列名字。 */
    public static Row pluginRow(Map<String, Object> status) {
        Object v = Json.raw(status, "plugins");
        if (!(v instanceof List)) {
            return new Row("插件", "读不到", 2);
        }
        List<?> list = (List<?>) v;
        if (list.isEmpty()) {
            return new Row("插件", "一个都没装？", 3);
        }
        int on = 0;
        StringBuilder off = new StringBuilder();
        for (Object o : list) {
            if (Json.bool(o, "enabled", false)) {
                on++;
            } else {
                if (off.length() > 0) {
                    off.append("、");
                }
                off.append(Json.str(o, "label", Json.str(o, "name")));
            }
        }
        if (off.length() == 0) {
            return new Row("插件", on + " 个全部启用", 1);
        }
        return new Row("插件", on + " 开 / 已关：" + off, 2);
    }
}
