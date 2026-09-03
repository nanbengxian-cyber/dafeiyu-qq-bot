package com.dafeiyu.console;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极小 JSON 解析/序列化。
 *
 * 为什么不用 org.json：它在 android.jar 里只是**桩**（每个方法体都是
 * {@code throw new RuntimeException("Stub!")}），真机上有实现、但在 JVM 上跑不了。
 * 而这个 App 最该被测的恰恰是「服务端返回的 JSON 怎么变成界面」这段逻辑。
 * 自己写一个就能在普通 JVM 上把解析测穿，不必开模拟器。
 *
 * 只支持标准 JSON，够用且刻意不宽容：
 *   · 不接受尾逗号、单引号、注释、NaN/Infinity —— 服务端是 Python json.dumps，不会产出这些；
 *   · 数字统一存 double，取整时再转（配置里的 int 都在安全范围内）；
 *   · 解析完必须**恰好**到字符串末尾，尾部有残留就报错（截断的响应要炸得明显，
 *     否则会拿半份数据去渲染，看起来像「服务器少给了几个字段」，极难查）。
 */
public final class Json {

    private Json() {
    }

    // ---------------------------------------------------------------- 解析

    public static Object parse(String text) throws JsonError {
        if (text == null) {
            throw new JsonError("空响应");
        }
        Parser p = new Parser(text);
        p.skipWs();
        Object value = p.value();
        p.skipWs();
        if (p.pos != text.length()) {
            throw new JsonError("第 " + p.pos + " 字符后还有多余内容（响应被截断？）");
        }
        return value;
    }

    /** 顶层必须是对象，否则报错。服务端所有端点都回对象。 */
    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) throws JsonError {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonError("顶层不是 JSON 对象");
        }
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        private final String s;
        private int pos;

        Parser(String s) {
            this.s = s;
        }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        char peek() throws JsonError {
            if (pos >= s.length()) {
                throw new JsonError("内容提前结束");
            }
            return s.charAt(pos);
        }

        Object value() throws JsonError {
            char c = peek();
            switch (c) {
                case '{':
                    return object();
                case '[':
                    return array();
                case '"':
                    return string();
                case 't':
                    lit("true");
                    return Boolean.TRUE;
                case 'f':
                    lit("false");
                    return Boolean.FALSE;
                case 'n':
                    lit("null");
                    return null;
                default:
                    return number();
            }
        }

        void lit(String want) throws JsonError {
            if (!s.startsWith(want, pos)) {
                throw new JsonError("第 " + pos + " 字符处期望 " + want);
            }
            pos += want.length();
        }

        Map<String, Object> object() throws JsonError {
            pos++; // {
            Map<String, Object> out = new LinkedHashMap<String, Object>();
            skipWs();
            if (peek() == '}') {
                pos++;
                return out;
            }
            while (true) {
                skipWs();
                if (peek() != '"') {
                    throw new JsonError("第 " + pos + " 字符处对象的键必须是字符串");
                }
                String key = string();
                skipWs();
                if (peek() != ':') {
                    throw new JsonError("第 " + pos + " 字符处缺少冒号");
                }
                pos++;
                skipWs();
                out.put(key, value());
                skipWs();
                char c = peek();
                if (c == ',') {
                    pos++;
                    continue;
                }
                if (c == '}') {
                    pos++;
                    return out;
                }
                throw new JsonError("第 " + pos + " 字符处对象里期望 , 或 }");
            }
        }

        List<Object> array() throws JsonError {
            pos++; // [
            List<Object> out = new ArrayList<Object>();
            skipWs();
            if (peek() == ']') {
                pos++;
                return out;
            }
            while (true) {
                skipWs();
                out.add(value());
                skipWs();
                char c = peek();
                if (c == ',') {
                    pos++;
                    continue;
                }
                if (c == ']') {
                    pos++;
                    return out;
                }
                throw new JsonError("第 " + pos + " 字符处数组里期望 , 或 ]");
            }
        }

        String string() throws JsonError {
            pos++; // "
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (pos >= s.length()) {
                    throw new JsonError("字符串没有结束引号");
                }
                char c = s.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c != '\\') {
                    sb.append(c);
                    continue;
                }
                if (pos >= s.length()) {
                    throw new JsonError("转义符后内容结束");
                }
                char e = s.charAt(pos++);
                switch (e) {
                    case '"': sb.append('"'); break;
                    case '\\': sb.append('\\'); break;
                    case '/': sb.append('/'); break;
                    case 'b': sb.append('\b'); break;
                    case 'f': sb.append('\f'); break;
                    case 'n': sb.append('\n'); break;
                    case 'r': sb.append('\r'); break;
                    case 't': sb.append('\t'); break;
                    case 'u':
                        if (pos + 4 > s.length()) {
                            throw new JsonError("\\u 转义不完整");
                        }
                        String hex = s.substring(pos, pos + 4);
                        pos += 4;
                        try {
                            sb.append((char) Integer.parseInt(hex, 16));
                        } catch (NumberFormatException ex) {
                            throw new JsonError("\\u 后不是十六进制：" + hex);
                        }
                        break;
                    default:
                        throw new JsonError("未知转义 \\" + e);
                }
            }
        }

        Double number() throws JsonError {
            int start = pos;
            if (pos < s.length() && (s.charAt(pos) == '-' || s.charAt(pos) == '+')) {
                pos++;
            }
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E'
                        || c == '+' || c == '-') {
                    pos++;
                } else {
                    break;
                }
            }
            String raw = s.substring(start, pos);
            if (raw.isEmpty()) {
                throw new JsonError("第 " + start + " 字符处不是合法值");
            }
            try {
                return Double.valueOf(raw);
            } catch (NumberFormatException ex) {
                throw new JsonError("不是合法数字：" + raw);
            }
        }
    }

    // ---------------------------------------------------------------- 取值
    //
    // 一律「取不到就给默认值」，不抛异常：界面上少显示一个字段可以接受，
    // 但因为某个字段缺失整页白屏不可接受。**唯一例外是 error 字段**，
    // 那个必须原样显示出来。

    @SuppressWarnings("unchecked")
    public static Map<String, Object> obj(Object node, String key) {
        if (node instanceof Map) {
            Object v = ((Map<String, Object>) node).get(key);
            if (v instanceof Map) {
                return (Map<String, Object>) v;
            }
        }
        return new LinkedHashMap<String, Object>();
    }

    @SuppressWarnings("unchecked")
    public static List<Object> arr(Object node, String key) {
        if (node instanceof Map) {
            Object v = ((Map<String, Object>) node).get(key);
            if (v instanceof List) {
                return (List<Object>) v;
            }
        }
        return new ArrayList<Object>();
    }

    @SuppressWarnings("unchecked")
    public static Object raw(Object node, String key) {
        return node instanceof Map ? ((Map<String, Object>) node).get(key) : null;
    }

    public static String str(Object node, String key, String dflt) {
        Object v = raw(node, key);
        if (v == null) {
            return dflt;
        }
        if (v instanceof String) {
            return (String) v;
        }
        if (v instanceof Number) {
            return num(((Number) v).doubleValue());
        }
        return String.valueOf(v);
    }

    public static String str(Object node, String key) {
        return str(node, key, "");
    }

    /** null / 缺失 都回 dflt —— 服务端用 null 表示「测不到」，不能显示成 false。 */
    public static Boolean boolOrNull(Object node, String key) {
        Object v = raw(node, key);
        return v instanceof Boolean ? (Boolean) v : null;
    }

    public static boolean bool(Object node, String key, boolean dflt) {
        Boolean v = boolOrNull(node, key);
        return v == null ? dflt : v.booleanValue();
    }

    public static Double dbl(Object node, String key) {
        Object v = raw(node, key);
        // 解析器产出的数字一律是 Double，但取 Number 而不是死盯 Double：
        // 手工构造的 Map（测试、以后可能的本地缓存）会塞 Long/Integer，
        // 那时候死盯 Double 会静默回 null，表现为「这一行永远显示 —」。
        if (v instanceof Number) {
            return Double.valueOf(((Number) v).doubleValue());
        }
        if (v instanceof String) {
            try {
                return Double.valueOf((String) v);
            } catch (NumberFormatException ex) {
                return null;
            }
        }
        return null;
    }

    public static Long lng(Object node, String key) {
        Double d = dbl(node, key);
        return d == null ? null : Long.valueOf(d.longValue());
    }

    /** 3.0 显示成 "3" 而不是 "3.0"；0.25 保持 "0.25"。 */
    public static String num(double d) {
        if (d == Math.rint(d) && !Double.isInfinite(d) && Math.abs(d) < 1e15) {
            return String.valueOf((long) d);
        }
        String s = String.valueOf(d);
        if (s.contains("E") || s.contains("e")) {
            return s;
        }
        // 去掉浮点噪声：0.30000000000000004 → 0.3
        java.math.BigDecimal bd = new java.math.BigDecimal(s)
                .setScale(4, java.math.RoundingMode.HALF_UP).stripTrailingZeros();
        return bd.toPlainString();
    }

    // ---------------------------------------------------------------- 序列化

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, value);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeTo(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof Boolean || v instanceof Integer || v instanceof Long) {
            sb.append(v);
        } else if (v instanceof Double || v instanceof Float) {
            sb.append(num(((Number) v).doubleValue()));
        } else if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : ((Map<String, Object>) v).entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                quote(sb, e.getKey());
                sb.append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof List) {
            sb.append('[');
            boolean first = true;
            for (Object o : (List<Object>) v) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeTo(sb, o);
            }
            sb.append(']');
        } else {
            quote(sb, String.valueOf(v));
        }
    }

    private static void quote(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }

    /** 解析失败用受检异常：调用方必须处理，不能忘。 */
    public static class JsonError extends Exception {
        public JsonError(String msg) {
            super(msg);
        }
    }
}
