package tests;

import com.dafeiyu.console.Json;
import com.dafeiyu.console.KnobModel;
import com.dafeiyu.console.StatusFmt;
import com.dafeiyu.console.Ui;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 手写的极简断言框架 + 造假数据的工具。
 *
 * 为什么不用 JUnit：要下 jar、要管 classpath，而这个项目的构建是手写命令链，
 * 多一个外部依赖就多一处「换台机器就跑不起来」的风险。断言只需要
 * eq / isTrue / throws 三种，二十行就够。
 *
 * 退出码即结论：0 全过，1 有失败 —— auto/verify.sh 靠这个判。
 */
public final class T {

    static int pass = 0;
    static int fail = 0;
    static final List<String> failures = new ArrayList<String>();
    static String group = "";

    public static void group(String name) {
        group = name;
        System.out.println("\n\u001b[1m" + name + "\u001b[0m");
    }

    public static void ok(String what) {
        pass++;
        System.out.println("  \u001b[32m✓\u001b[0m " + what);
    }

    public static void bad(String what, String detail) {
        fail++;
        failures.add(group + " / " + what + "：" + detail);
        System.out.println("  \u001b[31m✗\u001b[0m " + what + "  " + detail);
    }

    public static void eq(String what, Object want, Object got) {
        if (want == null ? got == null : want.equals(got)) {
            ok(what);
        } else {
            bad(what, "想要 [" + want + "]，得到 [" + got + "]");
        }
    }

    public static void near(String what, double want, double got) {
        if (Math.abs(want - got) < 1e-9) {
            ok(what);
        } else {
            bad(what, "想要 " + want + "，得到 " + got);
        }
    }

    public static void isTrue(String what, boolean cond) {
        if (cond) {
            ok(what);
        } else {
            bad(what, "期望为真");
        }
    }

    public static void isFalse(String what, boolean cond) {
        if (!cond) {
            ok(what);
        } else {
            bad(what, "期望为假");
        }
    }

    public static void contains(String what, String haystack, String needle) {
        if (haystack != null && haystack.contains(needle)) {
            ok(what);
        } else {
            bad(what, "[" + haystack + "] 里没有 [" + needle + "]");
        }
    }

    public static void notContains(String what, String haystack, String needle) {
        if (haystack == null || !haystack.contains(needle)) {
            ok(what);
        } else {
            bad(what, "[" + haystack + "] 里不该有 [" + needle + "]");
        }
    }

    /** 断言这段代码抛异常，且异常消息包含某个片段。 */
    public static void throwsWith(String what, String needle, Runnable body) {
        try {
            body.run();
            bad(what, "没有抛异常");
        } catch (RuntimeException e) {
            String m = e.getMessage() == null ? "" : e.getMessage();
            // 包装过的异常，看一层 cause
            if (!m.contains(needle) && e.getCause() != null
                    && e.getCause().getMessage() != null) {
                m = e.getCause().getMessage();
            }
            if (m.contains(needle)) {
                ok(what);
            } else {
                bad(what, "异常消息是 [" + m + "]，不含 [" + needle + "]");
            }
        }
    }

    public static int report() {
        System.out.println();
        if (fail > 0) {
            System.out.println("未通过的项：");
            for (String f : failures) {
                System.out.println("  · " + f);
            }
        }
        System.out.println(String.format("通过 %d，失败 %d", pass, fail));
        return fail > 0 ? 1 : 0;
    }

    // ---------------------------------------------------------------- 造数据

    public static Map<String, Object> map(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        for (int i = 0; i + 1 < kv.length; i += 2) {
            m.put(String.valueOf(kv[i]), kv[i + 1]);
        }
        return m;
    }

    public static List<Object> list(Object... items) {
        List<Object> l = new ArrayList<Object>();
        for (Object o : items) {
            l.add(o);
        }
        return l;
    }

    /**
     * 一份「一切正常」的状态。字段名严格照 console_api.py 的 build_status()，
     * 拼错了单测会绿但真机会显示 —— 所以这份数据本身就是契约的一部分。
     */
    public static Map<String, Object> healthyStatus() {
        return map(
                "server_time", "2026-09-03 02:30:00",
                "qq", map(
                        "state", "online",
                        "message", "在线",
                        "account", "3889000001",
                        "reason", "探针 online=true",
                        "last_seen_seconds", Long.valueOf(12),
                        "qr_available", Boolean.FALSE,
                        "qr_age_seconds", null,
                        "watchdog_age_seconds", Long.valueOf(8),
                        "watchdog_stale", Boolean.FALSE),
                "proxy", map(
                        "active", Boolean.TRUE,
                        "tunnel_healthy", Boolean.TRUE,
                        "egress_ip", "203.0.113.10"),
                "containers", list(
                        map("name", "astrbot", "state", "running",
                                "up_seconds", Long.valueOf(3600),
                                "mem_bytes", Long.valueOf(320L * 1024 * 1024),
                                "restarts", Long.valueOf(0)),
                        map("name", "napcat", "state", "running",
                                "up_seconds", Long.valueOf(28800),
                                "mem_bytes", Long.valueOf(410L * 1024 * 1024),
                                "restarts", Long.valueOf(2))),
                "host", map(
                        "mem_total", Long.valueOf(2L * 1024 * 1024 * 1024),
                        "mem_available", Long.valueOf(900L * 1024 * 1024),
                        "load1", Double.valueOf(0.42),
                        "disk_total", Long.valueOf(40L * 1024 * 1024 * 1024),
                        "disk_free", Long.valueOf(22L * 1024 * 1024 * 1024),
                        "uptime", Long.valueOf(500000)),
                "models", map(
                        "chat_provider", "relay-a",
                        "chat_model", "deepseek-v4-flash-0731",
                        "vision_provider", "zhipu-vision",
                        "vision_model", "glm-4.6v",
                        "persona", "大肥鱼"),
                "runtime", map(
                        "active_reply", Boolean.TRUE,
                        "possibility", Double.valueOf(0.25),
                        "rate_limit", "30 条 / 60 秒",
                        "context_turns", Long.valueOf(40),
                        "tool_use_ok", Boolean.TRUE),
                "tools", map("window_minutes", Long.valueOf(60),
                        "count", Long.valueOf(7),
                        "tools", list("生成图片", "搜索")),
                "plugins", list(
                        map("name", "dsh-imagegen", "label", "出图", "enabled", Boolean.TRUE,
                                "version", "1.4.0"),
                        map("name", "dsh-ctxclean", "label", "上下文清理",
                                "enabled", Boolean.TRUE, "version", "1.0.0"),
                        map("name", "dsh-welcome", "label", "入群欢迎",
                                "enabled", Boolean.FALSE, "version", "1.1.0")),
                "mode", "daily");
    }

    /** 一份 schema，覆盖 bool / int / float / enum 四种旋钮。 */
    public static Map<String, Object> schemaJson() {
        return map(
                "version", Long.valueOf(1),
                "knobs", list(
                        map("path", "provider_ltm_settings.active_reply.enable",
                                "name", "主动插话", "group", "说话风格", "type", "bool",
                                "hint", "关掉就**只在被叫时**回话", "hot", Boolean.TRUE),
                        map("path", "provider_ltm_settings.active_reply.possibility_reply",
                                "name", "插话概率", "group", "说话风格", "type", "float",
                                "min", Double.valueOf(0), "max", Double.valueOf(1),
                                "step", Double.valueOf(0.05), "hot", Boolean.TRUE,
                                "hint", "0.25 大约四条插一句"),
                        map("path", "provider_settings.max_context_length",
                                "name", "保留轮数", "group", "记忆", "type", "int",
                                "min", Double.valueOf(10), "max", Double.valueOf(100),
                                "step", Double.valueOf(5), "unit", "轮",
                                "hot", Boolean.TRUE),
                        map("path", "platform_settings.rate_limit.count",
                                "name", "限流条数", "group", "限流", "type", "int",
                                "min", Double.valueOf(1), "max", Double.valueOf(60),
                                "hot", Boolean.FALSE),
                        map("path", "provider_settings.default_personality",
                                "name", "人格", "group", "记忆", "type", "enum",
                                "options", list("大肥鱼", "默认"), "hot", Boolean.TRUE),
                        // 故意放一个认不出的类型，验证向前兼容
                        map("path", "future.knob", "name", "未来旋钮", "type", "matrix")),
                "modes", list(
                        map("id", "quiet", "name", "安静", "emoji", "🤫",
                                "desc", "只在被叫时说话"),
                        map("id", "daily", "name", "日常", "emoji", "🙂",
                                "desc", "默认状态"),
                        map("id", "shutup", "name", "闭嘴", "emoji", "🔇",
                                "desc", "完全不说话", "danger", Boolean.TRUE)),
                "actions", list(
                        map("id", "restart_bot", "name", "重启机器人",
                                "desc", "重启 astrbot 容器，约 20 秒",
                                "danger", Boolean.TRUE),
                        map("id", "clear_ctx", "name", "清空上下文",
                                "desc", "忘掉当前对话历史")),
                "chat_models", list("deepseek-v4-flash-0731", "glm-5.3", "kimi-k2.6"),
                "vision_providers", list(
                        map("id", "zhipu-vision", "model", "glm-4.6v"),
                        map("id", "vision-opus5", "model", "opus-5-vision")),
                "plugin_labels", map("dsh-imagegen", "出图"),
                "chat_provider", "relay-a");
    }

    public static KnobModel.Schema schema() {
        return KnobModel.parse(schemaJson());
    }

    /** 找某个 label 的行；找不到返回 null。 */
    public static StatusFmt.Row row(List<StatusFmt.Row> rows, String label) {
        for (StatusFmt.Row r : rows) {
            if (r.label.equals(label)) {
                return r;
            }
        }
        return null;
    }

    public static List<Object> modesOf(Map<String, Object> schemaJson) {
        return Json.arr(schemaJson, "modes");
    }

    /** 深拷一份状态再改一处，避免测试之间互相污染。 */
    @SuppressWarnings("unchecked")
    public static Map<String, Object> tweak(Map<String, Object> base, String path,
                                            Object value) {
        Map<String, Object> copy = deepCopy(base);
        String[] parts = path.split("\\.");
        Map<String, Object> cur = copy;
        for (int i = 0; i < parts.length - 1; i++) {
            Object next = cur.get(parts[i]);
            if (!(next instanceof Map)) {
                next = new LinkedHashMap<String, Object>();
                cur.put(parts[i], next);
            }
            cur = (Map<String, Object>) next;
        }
        cur.put(parts[parts.length - 1], value);
        return copy;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> deepCopy(Map<String, Object> in) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        for (Map.Entry<String, Object> e : in.entrySet()) {
            Object v = e.getValue();
            if (v instanceof Map) {
                out.put(e.getKey(), deepCopy((Map<String, Object>) v));
            } else if (v instanceof List) {
                List<Object> l = new ArrayList<Object>();
                for (Object o : (List<Object>) v) {
                    l.add(o instanceof Map ? deepCopy((Map<String, Object>) o) : o);
                }
                out.put(e.getKey(), l);
            } else {
                out.put(e.getKey(), v);
            }
        }
        return out;
    }

    /** Ui.statusRows 的简写。 */
    public static List<StatusFmt.Row> rows(Map<String, Object> status) {
        return Ui.statusRows(status, modesOf(schemaJson()));
    }
}
