package com.dafeiyu.controller;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 动态配置（旋钮）的解析、校验与写回 —— 桌面控制台 knobs.py 的移植。
 *
 * 仓库里的 deploy/console-config.json 决定显示哪些配置项；控制台不写死控件，
 * 只认类型，遇到不认识的类型**跳过而不是报错**（以后加新控件不会让旧 App 崩）。
 *
 * env 文本按行编辑，保留注释、空行与顺序 —— 配置文件的注释比变量本身值钱。
 */
public final class Knobs {

    public static final Pattern LINE_RE = Pattern.compile("^([A-Z][A-Z0-9_]*)=(.*)$",
            Pattern.DOTALL);
    public static final int MAX_STR_LEN = 200;
    public static final String MANAGED_SECTION =
            "# ---- 控制台托管（下面这些是控制台加的，可以照常手改）----";
    private static final String[] KINDS = {"bool", "int", "float", "str", "enum"};
    private static final String[] TRUE_WORDS = {"1", "true", "yes", "on", "是", "开"};
    private static final String[] FALSE_WORDS = {"0", "false", "no", "off", "否", "关"};

    /** 配置项取值不合法；消息可直接展示。 */
    public static class KnobException extends Exception {
        public KnobException(String msg) {
            super(msg);
        }
    }

    public static final class Knob {
        public final String key;
        public final String label;
        public final String group;
        public final String kind;
        public final String dflt;
        public final Double min;
        public final Double max;
        public final List<String> options;
        public final String hint;
        public final boolean restart;
        public final boolean secret;

        Knob(String key, String label, String group, String kind, String dflt,
             Double min, Double max, List<String> options, String hint,
             boolean restart, boolean secret) {
            this.key = key;
            this.label = label;
            this.group = group;
            this.kind = kind;
            this.dflt = dflt;
            this.min = min;
            this.max = max;
            this.options = options;
            this.hint = hint;
            this.restart = restart;
            this.secret = secret;
        }
    }

    private static Double asDouble(Object v) {
        if (v == null || "".equals(v)) {
            return null;
        }
        if (v instanceof Number) {
            return ((Number) v).doubleValue();
        }
        try {
            return Double.parseDouble(String.valueOf(v));
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private static boolean in(String[] words, String v) {
        for (String w : words) {
            if (w.equals(v)) {
                return true;
            }
        }
        return false;
    }

    /** 把仓库下发的 JSON 解析成旋钮列表；非法项跳过，保证向前兼容。 */
    public static List<Knob> parse(Object raw) {
        Object items = raw;
        if (raw instanceof Map) {
            items = Json.raw(raw, "knobs");
        }
        List<Knob> out = new ArrayList<Knob>();
        if (!(items instanceof List)) {
            return out;
        }
        java.util.Set<String> seen = new java.util.HashSet<String>();
        for (Object o : (List<?>) items) {
            if (!(o instanceof Map)) {
                continue;
            }
            Map<?, ?> item = (Map<?, ?>) o;
            String key = String.valueOf(orEmpty(item.get("key"))).trim();
            if (key.isEmpty() || seen.contains(key)
                    || !LINE_RE.matcher(key + "=").matches()) {
                continue;
            }
            String kind = String.valueOf(orEmpty(item.get("type"))).trim().toLowerCase();
            if (!in(KINDS, kind)) {
                continue;
            }
            List<String> options = new ArrayList<String>();
            if ("enum".equals(kind)) {
                Object opts = item.get("options");
                if (!(opts instanceof List) || ((List<?>) opts).isEmpty()) {
                    continue;
                }
                for (Object opt : (List<?>) opts) {
                    options.add(String.valueOf(opt));
                }
            }
            seen.add(key);
            out.add(new Knob(
                    key,
                    orDefault(item.get("label"), key),
                    orDefault(item.get("group"), "常规"),
                    kind,
                    String.valueOf(item.get("default") == null ? "" : item.get("default")),
                    asDouble(item.get("min")),
                    asDouble(item.get("max")),
                    options,
                    orDefault(item.get("hint"), ""),
                    item.get("restart_required") == null || truthy(item.get("restart_required")),
                    truthy(item.get("secret"))));
        }
        return out;
    }

    private static String orEmpty(Object o) {
        return o == null ? "" : String.valueOf(o);
    }

    private static String orDefault(Object o, String dflt) {
        String s = orEmpty(o);
        return s.isEmpty() ? dflt : s;
    }

    private static boolean truthy(Object o) {
        if (o instanceof Boolean) {
            return (Boolean) o;
        }
        String s = orEmpty(o).toLowerCase();
        return in(TRUE_WORDS, s);
    }

    /** 分组按首次出现顺序。 */
    public static List<String> groupOrder(List<Knob> knobs) {
        List<String> order = new ArrayList<String>();
        for (Knob k : knobs) {
            if (!order.contains(k.group)) {
                order.add(k.group);
            }
        }
        return order;
    }

    public static Map<String, String> envValues(String text) {
        Map<String, String> out = new LinkedHashMap<String, String>();
        if (text == null) {
            return out;
        }
        for (String line : text.split("\n")) {
            Matcher m = LINE_RE.matcher(line);
            if (m.matches()) {
                out.put(m.group(1), m.group(2));
            }
        }
        return out;
    }

    /**
     * 给界面显示的值。注意区分两种情况：变量**不在文件里**（raw 为 null）用默认值；
     * 变量在文件里但写成空值必须原样显示成空 —— 插件里 `os.environ.get("X", "1")`
     * 拿到的是空串而不是默认值，把空值显示成默认值会误导用户。
     */
    public static String displayValue(Knob knob, String raw) {
        return raw == null ? knob.dflt : raw;
    }

    public static boolean isEnabled(Knob knob, String raw) {
        String value = displayValue(knob, raw).trim().toLowerCase();
        if (in(TRUE_WORDS, value)) {
            return true;
        }
        if (in(FALSE_WORDS, value)) {
            return false;
        }
        return !value.isEmpty();
    }

    /** 把界面输入校验并格式化成 env 行里的字符串。 */
    public static String coerceValue(Knob knob, String value) throws KnobException {
        String text = value == null ? "" : value;
        if ("bool".equals(knob.kind)) {
            String t = text.trim().toLowerCase();
            if (in(TRUE_WORDS, t)) {
                return "1";
            }
            if (in(FALSE_WORDS, t)) {
                return "0";
            }
            throw new KnobException(knob.label + "只能选开或关。");
        }
        if ("enum".equals(knob.kind)) {
            String t = text.trim();
            if (!knob.options.contains(t)) {
                throw new KnobException(knob.label + "只能从预设里选。");
            }
            return t;
        }
        if ("int".equals(knob.kind) || "float".equals(knob.kind)) {
            String t = text.trim();
            if (t.isEmpty()) {
                throw new KnobException(knob.label + "不能留空。");
            }
            double number;
            try {
                number = Double.parseDouble(t);
            } catch (NumberFormatException e) {
                throw new KnobException(knob.label + "必须是数字。");
            }
            if ("int".equals(knob.kind) && number != Math.rint(number)) {
                throw new KnobException(knob.label + "必须是整数。");
            }
            if (knob.min != null && number < knob.min) {
                throw new KnobException(knob.label + "不能小于 " + fmtNum(knob.min) + "。");
            }
            if (knob.max != null && number > knob.max) {
                throw new KnobException(knob.label + "不能大于 " + fmtNum(knob.max) + "。");
            }
            return "float".equals(knob.kind) ? fmtNum(number) : String.valueOf((long) number);
        }
        if (text.contains("\n") || text.contains("\r") || text.contains("\0")) {
            throw new KnobException(knob.label + "不能包含换行或空字符。");
        }
        text = text.trim();
        if (text.length() > MAX_STR_LEN) {
            throw new KnobException(knob.label + "太长（最多 " + MAX_STR_LEN + " 个字符）。");
        }
        if (!knob.options.isEmpty() && !knob.options.contains(text)) {
            throw new KnobException(knob.label + "只能从预设里选。");
        }
        return text;
    }

    public static String fmtNum(double number) {
        if (number == Math.rint(number) && Math.abs(number) < 1e15) {
            return String.valueOf((long) number);
        }
        double rounded = Math.round(number * 1e6) / 1e6;
        String s = String.valueOf(rounded);
        if (s.contains(".")) {
            s = s.replaceAll("0+$", "").replaceAll("\\.$", "");
        }
        return s;
    }

    public static final class ApplyResult {
        public final String text;
        /** key → [旧值(可能 null), 新值]，只含真改动的项。 */
        public final Map<String, String[]> changed;

        ApplyResult(String text, Map<String, String[]> changed) {
            this.text = text;
            this.changed = changed;
        }
    }

    /** 行级替换；不存在的变量追加到固定的托管小节。 */
    public static ApplyResult applyEnvText(String text, Map<String, String> updates)
            throws KnobException {
        for (Map.Entry<String, String> e : updates.entrySet()) {
            if (!LINE_RE.matcher(e.getKey() + "=").matches()) {
                throw new KnobException("变量名不合法：" + e.getKey());
            }
            if (e.getValue().contains("\n") || e.getValue().contains("\r")
                    || e.getValue().contains("\0")) {
                throw new KnobException("值里不能有换行或空字符。");
            }
        }
        List<String> lines = new ArrayList<String>();
        boolean trailingNewline = false;
        if (text != null && !text.isEmpty()) {
            for (String line : text.split("\n", -1)) {
                lines.add(line);
            }
            if (!lines.isEmpty() && lines.get(lines.size() - 1).isEmpty()) {
                lines.remove(lines.size() - 1);
                trailingNewline = true;
            }
        }
        java.util.Set<String> seen = new java.util.HashSet<String>();
        Map<String, String[]> changed = new LinkedHashMap<String, String[]>();
        for (int i = 0; i < lines.size(); i++) {
            String line = lines.get(i);
            String bare = line.endsWith("\r") ? line.substring(0, line.length() - 1) : line;
            Matcher m = LINE_RE.matcher(bare);
            if (!m.matches()) {
                continue;
            }
            String key = m.group(1);
            if (!updates.containsKey(key)) {
                continue;
            }
            seen.add(key);
            String old = m.group(2);
            String now = updates.get(key);
            if (old.equals(now)) {
                continue;
            }
            lines.set(i, key + "=" + now + (line.endsWith("\r") ? "\r" : ""));
            changed.put(key, new String[]{old, now});
        }
        List<String> missing = new ArrayList<String>();
        for (String key : updates.keySet()) {
            if (!seen.contains(key)) {
                missing.add(key);
            }
        }
        java.util.Collections.sort(missing);
        boolean appendedSection = false;
        if (!missing.isEmpty()) {
            // 与桌面版一致：全文件找托管小节头；找不到才在末尾补空行 + 小节头，
            // 新变量插在小节头的紧后面（已有小节时插在旧小节头后面）。
            int index = -1;
            for (int i = 0; i < lines.size(); i++) {
                String bare = lines.get(i).endsWith("\r")
                        ? lines.get(i).substring(0, lines.get(i).length() - 1) : lines.get(i);
                if (bare.trim().equals(MANAGED_SECTION)) {
                    index = i + 1;
                    break;
                }
            }
            if (index < 0) {
                lines.add("");
                lines.add(MANAGED_SECTION);
                appendedSection = true;
                index = lines.size();
            }
            for (String key : missing) {
                lines.add(index, key + "=" + updates.get(key));
                index++;
                changed.put(key, new String[]{null, updates.get(key)});
            }
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < lines.size(); i++) {
            if (i > 0) {
                sb.append('\n');
            }
            sb.append(lines.get(i));
        }
        if (trailingNewline || appendedSection) {
            sb.append('\n');
        }
        return new ApplyResult(sb.toString(), changed);
    }

    /** 敏感项只报「已设置 / 未设置」，绝不回显真实值。 */
    public static String secretState(Knob knob, String raw) {
        if (!knob.secret) {
            return displayValue(knob, raw);
        }
        return raw != null && !raw.isEmpty() ? "已设置" : "未设置";
    }
}
