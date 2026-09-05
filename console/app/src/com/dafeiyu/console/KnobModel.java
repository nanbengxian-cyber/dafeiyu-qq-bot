package com.dafeiyu.console;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 服务端 schema 的本地模型 —— 一个旋钮/模式/动作在手机上长什么样、
 * 用户改的值合不合法，全在这里判。
 *
 * 为什么要有它：界面是**按服务端下发的 schema 渲染**的，所以「这个旋钮该画成
 * 开关还是滑块」「滑块的档位有几个」「用户拖到的值合不合法」都不能写死在
 * Activity 里。抽出来还有个更实际的好处 —— 这些是最容易出错的算术
 * （float 的步长、int 的边界、进度条整数格与实值的来回换算），
 * 放在不碰 Android API 的类里才能在普通 JVM 上测。
 *
 * 值的表示：统一用 Object（Boolean / Double / Long / String），
 * 和 {@link Json} 解析出来的类型一致，这样「服务端给的当前值」可以直接塞进来，
 * 不用先猜类型再转换。
 */
public final class KnobModel {

    /** 一个配置旋钮。字段和 console_spec.py 的 KNOBS 一一对应。 */
    public static final class Knob {
        public final String path;
        public final String name;
        public final String group;
        /** bool / int / float / enum / str / csv */
        public final String type;
        public final String hint;
        public final String unit;
        public final boolean hot;
        public final Double min;
        public final Double max;
        public final Double step;
        /** enum 才有；其余为空表 */
        public final List<String> options;
        /** 密钥类：服务端只回「设了没设」，手机端不许改 */
        public final boolean secret;
        /** str 的长度上限 */
        public final int maxLen;

        Knob(String path, String name, String group, String type, String hint,
             String unit, boolean hot, Double min, Double max, Double step,
             List<String> options, boolean secret, int maxLen) {
            this.path = path;
            this.name = name;
            this.group = group;
            this.type = type;
            this.hint = hint;
            this.unit = unit;
            this.hot = hot;
            this.min = min;
            this.max = max;
            this.step = step;
            this.options = options;
            this.secret = secret;
            this.maxLen = maxLen;
        }

        public boolean isBool() {
            return "bool".equals(type);
        }

        public boolean isNumber() {
            return "int".equals(type) || "float".equals(type);
        }

        public boolean isEnum() {
            return "enum".equals(type);
        }

        /** 文本类：str 和 csv 都用输入框，区别只在提交时 csv 切成数组。 */
        public boolean isText() {
            return "str".equals(type) || "csv".equals(type);
        }

        public boolean isCsv() {
            return "csv".equals(type);
        }

        /**
         * 这个旋钮住在 imagegen.env 里（插件开关），不在 cmd_config.json。
         * 唯一区别体现在生效方式：env 的改完必须重启容器。
         */
        public boolean isEnv() {
            return path.startsWith("env:");
        }

        /** 密钥类只能在服务器上改，界面上显示成只读。 */
        public boolean readOnly() {
            return secret;
        }
    }

    public static final class Mode {
        public final String id;
        public final String name;
        public final String desc;
        public final boolean danger;

        Mode(String id, String name, String desc, boolean danger) {
            this.id = id;
            this.name = name;
            this.desc = desc;
            this.danger = danger;
        }
    }

    public static final class Action {
        public final String id;
        public final String name;
        public final String desc;
        public final boolean danger;

        Action(String id, String name, String desc, boolean danger) {
            this.id = id;
            this.name = name;
            this.desc = desc;
            this.danger = danger;
        }
    }

    /** 解析完的整份 schema。 */
    public static final class Schema {
        public final List<Knob> knobs = new ArrayList<Knob>();
        public final List<Mode> modes = new ArrayList<Mode>();
        public final List<Action> actions = new ArrayList<Action>();
        public final List<String> chatModels = new ArrayList<String>();
        /** 识图渠道：provider_id → 显示名 */
        public final Map<String, String> visionProviders = new LinkedHashMap<String, String>();
        /** 插件：name → 中文名 */
        public final Map<String, String> pluginLabels = new LinkedHashMap<String, String>();
        /** 插件分组的显示顺序，服务端给的 */
        public final List<String> pluginGroups = new ArrayList<String>();
        /** schema 结构版本。服务端加了新类型会升，用来判断该不该提示更新 APK */
        public int schemaVersion = 1;
        /** 「env 改完要重启」的文案，服务端下发，界面直接用 */
        public String envHint = "";

        public Knob knob(String path) {
            for (Knob k : knobs) {
                if (k.path.equals(path)) {
                    return k;
                }
            }
            return null;
        }

        /** 按 group 归组，顺序照 knobs 里第一次出现的顺序（服务端已排好）。 */
        public List<String> groups() {
            List<String> out = new ArrayList<String>();
            for (Knob k : knobs) {
                String g = k.group == null || k.group.isEmpty() ? "其他" : k.group;
                if (!out.contains(g)) {
                    out.add(g);
                }
            }
            return out;
        }

        public List<Knob> inGroup(String group) {
            List<Knob> out = new ArrayList<Knob>();
            for (Knob k : knobs) {
                String g = k.group == null || k.group.isEmpty() ? "其他" : k.group;
                if (g.equals(group)) {
                    out.add(k);
                }
            }
            return out;
        }
    }

    private KnobModel() {
    }

    /**
     * 认得的旋钮类型。不在这个集合里的一律跳过 ——
     * 这是「服务端先加功能、APK 后跟上」能安全并存的关键：
     * 老包遇到新类型只是少显示一项，不会整页打不开。
     */
    private static final java.util.Set<String> KNOWN_TYPES =
            new java.util.HashSet<String>(java.util.Arrays.asList(
                    "bool", "int", "float", "enum", "str", "csv"));

    // ---------------------------------------------------------------- 解析

    /**
     * 从 /api/console/schema 的响应建模型。
     *
     * 刻意宽容：将来服务端多给字段（比如新加一种 type）不该让手机崩。
     * 认不出的旋钮**跳过**而不是报错 —— 老 APK 遇到新旋钮时少显示一项，
     * 比整页打不开好得多。这正是「schema 驱动」要的向前兼容，
     * 也是为什么服务端加开关不用重装 APK。
     */
    public static Schema parse(Map<String, Object> root) {
        Schema s = new Schema();
        Long schemaVersion = Json.lng(root, "version");
        s.schemaVersion = schemaVersion == null ? 1 : schemaVersion.intValue();
        s.envHint = Json.str(root, "env_hint");
        for (Object o : Json.arr(root, "knobs")) {
            String path = Json.str(o, "path");
            String type = Json.str(o, "type");
            if (path.isEmpty() || type.isEmpty()) {
                continue;
            }
            if (!KNOWN_TYPES.contains(type)) {
                continue; // 不认识的类型，跳过而不是崩
            }
            List<String> options = new ArrayList<String>();
            for (Object op : Json.arr(o, "options")) {
                if (op == null) {
                    continue;
                }
                // 选项可以是裸值，也可以是 {"value":..,"label":..}。
                // 服务端两种都在用，这里都认。
                if (op instanceof Map) {
                    String v = Json.str(op, "value");
                    if (!v.isEmpty()) {
                        options.add(v);
                    }
                } else {
                    options.add(String.valueOf(op));
                }
            }
            s.knobs.add(new Knob(
                    path,
                    Json.str(o, "name", path),
                    Json.str(o, "group", "其他"),
                    type,
                    Json.str(o, "hint"),
                    Json.str(o, "unit"),
                    Json.bool(o, "hot", true),
                    Json.dbl(o, "min"),
                    Json.dbl(o, "max"),
                    Json.dbl(o, "step"),
                    options,
                    Json.bool(o, "secret", false),
                    (Json.lng(o, "max_len") == null ? 200 : Json.lng(o, "max_len").intValue())));
        }
        for (Object o : Json.arr(root, "modes")) {
            String id = Json.str(o, "id");
            if (id.isEmpty()) {
                continue;
            }
            s.modes.add(new Mode(id, Json.str(o, "name", id), Json.str(o, "desc"),
                    Json.bool(o, "danger", false)));
        }
        for (Object o : Json.arr(root, "actions")) {
            String id = Json.str(o, "id");
            if (id.isEmpty()) {
                continue;
            }
            s.actions.add(new Action(id, Json.str(o, "name", id), Json.str(o, "desc"),
                    Json.bool(o, "danger", false)));
        }
        for (Object o : Json.arr(root, "chat_models")) {
            if (o != null) {
                s.chatModels.add(String.valueOf(o));
            }
        }
        for (Object o : Json.arr(root, "vision_providers")) {
            String id = Json.str(o, "id");
            if (!id.isEmpty()) {
                // 服务端给的是 {"id","model"}（build_schema 里从 provider[] 里挑
                // modalities 含 image 的那几条），字段名是 model 而不是 name。
                // 之前写成 name 时这里静默回落成 id，界面上就只显示
                // "zhipu-vision" 而不是模型名 —— 单测能过、真机看着也像对的。
                s.visionProviders.put(id, Json.str(o, "model", Json.str(o, "name", id)));
            }
        }
        Object labels = Json.raw(root, "plugin_labels");
        if (labels instanceof Map) {
            for (Map.Entry<?, ?> e : ((Map<?, ?>) labels).entrySet()) {
                s.pluginLabels.put(String.valueOf(e.getKey()), String.valueOf(e.getValue()));
            }
        }
        for (Object o : Json.arr(root, "plugin_groups")) {
            if (o != null) {
                s.pluginGroups.add(String.valueOf(o));
            }
        }
        return s;
    }

    // ---------------------------------------------------------------- 显示

    /** 把值显示成人话。滑块旁边和「已改动」提示都用它，保证两处口径一致。 */
    public static String show(Knob k, Object value) {
        if (k.secret) {
            // 服务端对密钥类只回 true/false（值一个字节都不出服务器）
            return truthy(value) ? "已设置" : "未设置";
        }
        if (value == null) {
            return "—";
        }
        if (k.isCsv()) {
            String joined = csvText(value);
            return joined.isEmpty() ? "（空）" : joined;
        }
        if ("str".equals(k.type)) {
            String text = String.valueOf(value);
            return text.isEmpty() ? "（空）" : text;
        }
        if (k.isBool()) {
            return truthy(value) ? "开" : "关";
        }
        if (k.isEnum()) {
            return String.valueOf(value);
        }
        String text;
        if ("int".equals(k.type)) {
            text = String.valueOf(asLong(value, 0L));
        } else {
            double d = asDouble(value, 0d);
            // 概率这类小数，两位足够；末尾的 0 去掉（0.30 → 0.3）
            text = trimZero(String.format("%.2f", d));
        }
        if (k.unit != null && !k.unit.isEmpty()) {
            text += " " + k.unit;
        }
        return text;
    }

    private static String trimZero(String s) {
        if (s.indexOf('.') < 0) {
            return s;
        }
        int end = s.length();
        while (end > 0 && s.charAt(end - 1) == '0') {
            end--;
        }
        if (end > 0 && s.charAt(end - 1) == '.') {
            end--;
        }
        return s.substring(0, end);
    }

    /**
     * csv 值 → 输入框里的文本。服务端给的是数组，界面上要显示成
     * 「100000001, 123456」这种一眼能改的形式。
     */
    public static String csvText(Object value) {
        if (value == null) {
            return "";
        }
        if (value instanceof List) {
            StringBuilder sb = new StringBuilder();
            for (Object o : (List<?>) value) {
                if (o == null) {
                    continue;
                }
                String s = String.valueOf(o).trim();
                if (s.isEmpty()) {
                    continue;
                }
                if (sb.length() > 0) {
                    sb.append(", ");
                }
                sb.append(s);
            }
            return sb.toString();
        }
        return String.valueOf(value).trim();
    }

    /** 输入框里的文本 → csv 数组。空段丢掉，两端空白去掉。 */
    public static List<String> csvParse(String text) {
        List<String> out = new ArrayList<String>();
        if (text == null) {
            return out;
        }
        // 中文输入法下逗号常常打成「，」，这里一并认 —— 否则用户会得到一个
        // 「格式不对」而完全看不出哪里不对。
        for (String part : text.replace('，', ',').split(",")) {
            String s = part.trim();
            if (!s.isEmpty()) {
                out.add(s);
            }
        }
        return out;
    }

    // ---------------------------------------------------------------- 数值换算
    //
    // 安卓的 SeekBar 只认整数格。所以要在「实值」和「第几格」之间来回换。
    // 这段算术是这个类存在的主要理由：写错了表现为「滑块拖不到最大值」或
    // 「概率 0.25 显示成 0.24」，肉眼很难发现，但单测一测就现。

    /** 这个旋钮的步长。没给就按类型兜底：int 走 1，float 走 0.05。 */
    public static double stepOf(Knob k) {
        if (k.step != null && k.step > 0) {
            return k.step;
        }
        return "int".equals(k.type) ? 1d : 0.05d;
    }

    public static double minOf(Knob k) {
        return k.min != null ? k.min : 0d;
    }

    public static double maxOf(Knob k) {
        if (k.max != null) {
            return k.max;
        }
        return "int".equals(k.type) ? 100d : 1d;
    }

    /** 一共几格（含两端）。 */
    public static int ticks(Knob k) {
        double span = maxOf(k) - minOf(k);
        double st = stepOf(k);
        if (span <= 0 || st <= 0) {
            return 1;
        }
        return (int) Math.round(span / st) + 1;
    }

    /** 实值 → 第几格。越界钳到两端（用户从服务端拿到的值可能在范围外，比如手改过配置）。 */
    public static int toTick(Knob k, Object value) {
        double v = asDouble(value, minOf(k));
        double st = stepOf(k);
        int t = (int) Math.round((v - minOf(k)) / st);
        if (t < 0) {
            return 0;
        }
        int max = ticks(k) - 1;
        return t > max ? max : t;
    }

    /**
     * 第几格 → 实值。
     *
     * float 必须**按步长四舍五入**再回值，否则 0.05 累加 5 次会得到
     * 0.25000000000000006，服务端要么校验失败要么把这串写进配置文件。
     */
    public static Object fromTick(Knob k, int tick) {
        double st = stepOf(k);
        double v = minOf(k) + tick * st;
        double lo = minOf(k);
        double hi = maxOf(k);
        if (v < lo) {
            v = lo;
        }
        if (v > hi) {
            v = hi;
        }
        if ("int".equals(k.type)) {
            return Long.valueOf(Math.round(v));
        }
        // 按步长的小数位数定精度：步长 0.05 → 保留 2 位
        int digits = decimals(st);
        double scale = Math.pow(10, digits);
        return Double.valueOf(Math.round(v * scale) / scale);
    }

    /**
     * 步长的小数位数 —— fromTick 靠它决定四舍五入到几位。
     * 公开是为了能单测：这个函数错一位，所有 float 旋钮都会带浮点噪声。
     */
    public static int decimals(double step) {
        String s = trimZero(String.format("%.6f", step));
        int dot = s.indexOf('.');
        return dot < 0 ? 0 : s.length() - dot - 1;
    }

    // ---------------------------------------------------------------- 校验

    /**
     * 校验用户要提交的值。返回 null 表示合法，否则返回给人看的错误话。
     *
     * 手机上先校验一遍不是为了替代服务端 —— 服务端照样会拦（越界直接 400，
     * 不做静默钳制）。这里挡一道是为了**不让人白等一个来回**，
     * 尤其在服务器要几十秒才回的时候。
     */
    public static String validate(Knob k, Object value) {
        if (k == null) {
            return "这个配置项服务器不认";
        }
        if (k.readOnly()) {
            return k.name + "是密钥，只能在服务器上改";
        }
        if (k.isCsv()) {
            // csv 一律合法（空表也允许 —— 比如「限定群」清空就是「所有群」）。
            // 每一项的格式服务端用 item_pattern 校验，这里不重复一份正则：
            // 两边各写一份迟早会不一致，而不一致时用户看到的是「手机说行、服务器说不行」。
            return null;
        }
        if ("str".equals(k.type)) {
            String text = value == null ? "" : String.valueOf(value);
            if (text.length() > k.maxLen) {
                return k.name + "太长了（上限 " + k.maxLen + " 字）";
            }
            return null;
        }
        if (k.isBool()) {
            if (value instanceof Boolean) {
                return null;
            }
            return k.name + "只能是开或关";
        }
        if (k.isEnum()) {
            String v = String.valueOf(value);
            if (k.options.isEmpty() || k.options.contains(v)) {
                return null;
            }
            return k.name + "只能是：" + join(k.options, "、");
        }
        Double d = numOrNull(value);
        if (d == null) {
            return k.name + "要填数字";
        }
        if ("int".equals(k.type) && Math.abs(d - Math.rint(d)) > 1e-9) {
            return k.name + "要填整数";
        }
        if (k.min != null && d < k.min - 1e-9) {
            return k.name + "不能小于 " + trimZero(String.format("%.2f", k.min));
        }
        if (k.max != null && d > k.max + 1e-9) {
            return k.name + "不能大于 " + trimZero(String.format("%.2f", k.max));
        }
        return null;
    }

    /** 提交前把值规范成服务端期望的类型（int 给整数、float 给小数、csv 给数组）。 */
    public static Object normalize(Knob k, Object value) {
        if (k.isCsv()) {
            if (value instanceof List) {
                return value;
            }
            return csvParse(value == null ? "" : String.valueOf(value));
        }
        if ("str".equals(k.type)) {
            return value == null ? "" : String.valueOf(value);
        }
        if (k.isBool()) {
            return Boolean.valueOf(truthy(value));
        }
        if (k.isEnum()) {
            return String.valueOf(value);
        }
        Double d = numOrNull(value);
        if (d == null) {
            return value;
        }
        if ("int".equals(k.type)) {
            return Long.valueOf(Math.round(d.doubleValue()));
        }
        int digits = decimals(stepOf(k));
        double scale = Math.pow(10, digits);
        return Double.valueOf(Math.round(d.doubleValue() * scale) / scale);
    }

    /**
     * 算出「哪些值和服务端当前值不一样」—— 只提交这些。
     *
     * 为什么不整份提交：服务端每次写都是「读全量配置 → 改 → 写回」，
     * 提交越少、被撞上并发冲突（409）的窗口越小。而且 hot=false 的旋钮
     * 改了要重启容器，不该因为「顺手一起提交」而白重启一次。
     */
    public static Map<String, Object> diff(Schema schema,
                                           Map<String, Object> current,
                                           Map<String, Object> edited) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        for (Map.Entry<String, Object> e : edited.entrySet()) {
            Knob k = schema.knob(e.getKey());
            if (k == null) {
                continue;
            }
            Object want = normalize(k, e.getValue());
            Object have = current.get(e.getKey());
            if (!same(k, have, want)) {
                out.put(e.getKey(), want);
            }
        }
        return out;
    }

    /** 值相等判断。数值按类型比，别拿 Double.equals 去比 1 和 1.0。 */
    public static boolean same(Knob k, Object a, Object b) {
        if (a == null || b == null) {
            return a == b;
        }
        if (k.isBool()) {
            return truthy(a) == truthy(b);
        }
        if (k.isEnum()) {
            return String.valueOf(a).equals(String.valueOf(b));
        }
        Double x = numOrNull(a);
        Double y = numOrNull(b);
        if (x == null || y == null) {
            return String.valueOf(a).equals(String.valueOf(b));
        }
        return Math.abs(x - y) < 1e-9;
    }

    /** 改动里有没有「要重启才生效」的。有的话界面要提醒，别让人以为已经生效了。 */
    public static boolean needsRestart(Schema schema, Map<String, Object> changed) {
        for (String path : changed.keySet()) {
            Knob k = schema.knob(path);
            if (k != null && !k.hot) {
                return true;
            }
        }
        return false;
    }

    // ---------------------------------------------------------------- 小工具

    public static boolean truthy(Object v) {
        if (v instanceof Boolean) {
            return ((Boolean) v).booleanValue();
        }
        if (v instanceof Number) {
            return ((Number) v).doubleValue() != 0;
        }
        String s = String.valueOf(v);
        return "true".equalsIgnoreCase(s) || "1".equals(s) || "开".equals(s);
    }

    static Double numOrNull(Object v) {
        if (v instanceof Number) {
            return Double.valueOf(((Number) v).doubleValue());
        }
        if (v instanceof Boolean) {
            return null;
        }
        try {
            return Double.valueOf(Double.parseDouble(String.valueOf(v).trim()));
        } catch (RuntimeException exc) {
            return null;
        }
    }

    public static double asDouble(Object v, double dflt) {
        Double d = numOrNull(v);
        return d == null ? dflt : d.doubleValue();
    }

    public static long asLong(Object v, long dflt) {
        Double d = numOrNull(v);
        return d == null ? dflt : Math.round(d.doubleValue());
    }

    static String join(List<String> list, String sep) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < list.size(); i++) {
            if (i > 0) {
                sb.append(sep);
            }
            sb.append(list.get(i));
        }
        return sb.toString();
    }
}
