package tests;

import com.dafeiyu.console.Json;

import java.util.List;
import java.util.Map;

/**
 * Json 的测试。重点不在「能不能解析正常数据」，而在**畸形输入会不会静默出错**：
 * 截断的响应、错类型、深嵌套，这些在真机上表现为「界面莫名少东西」，
 * 是最难查的一类问题。
 */
public final class JsonTest {

    public static void run() {
        T.group("Json：正常解析");
        try {
            Map<String, Object> o = Json.parseObject(
                    "{\"a\":1,\"b\":\"x\",\"c\":true,\"d\":null,\"e\":[1,2],\"f\":{\"g\":0.5}}");
            T.eq("整数", "1", Json.str(o, "a"));
            T.eq("字符串", "x", Json.str(o, "b"));
            T.eq("布尔", Boolean.TRUE, Json.boolOrNull(o, "c"));
            T.eq("null 当缺失", null, Json.boolOrNull(o, "d"));
            T.eq("数组长度", 2, Json.arr(o, "e").size());
            T.near("嵌套取值", 0.5, Json.dbl(Json.raw(o, "f"), "g").doubleValue());
        } catch (Json.JsonError e) {
            T.bad("正常 JSON 解析失败", e.getMessage());
        }

        T.group("Json：数字格式");
        T.eq("整数不带小数点", "3", Json.num(3.0));
        T.eq("小数保留", "0.25", Json.num(0.25));
        T.eq("浮点噪声抹平", "0.3", Json.num(0.30000000000000004));
        T.eq("负数", "-7", Json.num(-7.0));
        T.eq("零", "0", Json.num(0.0));

        T.group("Json：转义");
        try {
            Map<String, Object> o = Json.parseObject(
                    "{\"s\":\"a\\nb\\t\\\"c\\\"\\\\d\",\"u\":\"\\u4e2d\\u6587\"}");
            T.eq("控制字符与引号", "a\nb\t\"c\"\\d", Json.str(o, "s"));
            T.eq("Unicode 转义", "中文", Json.str(o, "u"));
        } catch (Json.JsonError e) {
            T.bad("转义解析失败", e.getMessage());
        }

        T.group("Json：中文原样");
        try {
            Map<String, Object> o = Json.parseObject("{\"m\":\"机器人掉线了\"}");
            T.eq("UTF-8 直出", "机器人掉线了", Json.str(o, "m"));
        } catch (Json.JsonError e) {
            T.bad("中文解析失败", e.getMessage());
        }

        T.group("Json：畸形输入必须炸");
        badJson("截断的对象", "{\"a\":1");
        badJson("截断的字符串", "{\"a\":\"abc");
        badJson("尾部有残留", "{\"a\":1}garbage");
        badJson("两个顶层对象", "{\"a\":1}{\"b\":2}");
        badJson("单引号", "{'a':1}");
        badJson("尾逗号", "{\"a\":1,}");
        badJson("数组尾逗号", "{\"a\":[1,2,]}");
        badJson("裸键", "{a:1}");
        badJson("空串", "");
        badJson("NaN", "{\"a\":NaN}");
        badJson("键不是字符串", "{1:2}");
        badJson("缺冒号", "{\"a\" 1}");

        T.group("Json：顶层必须是对象");
        try {
            Json.parseObject("[1,2,3]");
            T.bad("数组当顶层应报错", "没报错");
        } catch (Json.JsonError e) {
            T.contains("数组当顶层报错", e.getMessage(), "不是 JSON 对象");
        }

        T.group("Json：取值容错");
        Map<String, Object> empty = T.map();
        T.eq("缺字段回默认", "兜底", Json.str(empty, "nope", "兜底"));
        T.eq("缺字段回空串", "", Json.str(empty, "nope"));
        T.eq("缺字段的数组是空表", 0, Json.arr(empty, "nope").size());
        T.eq("缺字段的对象非 null", 0, Json.obj(empty, "nope").size());
        T.eq("null 节点取值不崩", "", Json.str(null, "any"));
        T.eq("字符串节点取值不崩", "", Json.str("不是对象", "any"));
        T.eq("bool 默认值", Boolean.TRUE, Boolean.valueOf(Json.bool(empty, "x", true)));

        T.group("Json：类型不符时不硬转");
        Map<String, Object> mixed = T.map("n", "12", "b", "true", "arr", "不是数组");
        T.eq("字符串数字能转", Long.valueOf(12), Json.lng(mixed, "n"));
        T.eq("字符串 true 不当布尔", null, Json.boolOrNull(mixed, "b"));
        T.eq("字符串当数组回空表", 0, Json.arr(mixed, "arr").size());
        T.eq("非数字字符串回 null", null, Json.dbl(T.map("x", "abc"), "x"));

        T.group("Json：手工 Map 的整数也认");
        // 这是真踩过的坑：测试里塞 Long，而 dbl() 只认 Double，
        // 结果所有数值行静默显示「—」，单测却全绿。
        Map<String, Object> handmade = T.map("v", Long.valueOf(42),
                "i", Integer.valueOf(7));
        T.eq("Long 能读出", Long.valueOf(42), Json.lng(handmade, "v"));
        T.eq("Integer 能读出", Long.valueOf(7), Json.lng(handmade, "i"));
        T.eq("Long 转字符串不带小数", "42", Json.str(handmade, "v"));

        T.group("Json：序列化");
        T.eq("对象", "{\"a\":1,\"b\":\"x\"}",
                Json.write(T.map("a", Long.valueOf(1), "b", "x")));
        T.eq("布尔与 null", "{\"t\":true,\"n\":null}",
                Json.write(T.map("t", Boolean.TRUE, "n", null)));
        T.eq("嵌套", "{\"o\":{\"k\":[1,2]}}",
                Json.write(T.map("o", T.map("k",
                        T.list(Long.valueOf(1), Long.valueOf(2))))));
        T.eq("小数不带尾零", "{\"p\":0.25}", Json.write(T.map("p", Double.valueOf(0.25))));
        T.eq("整数型 double 不带 .0", "{\"n\":40}",
                Json.write(T.map("n", Double.valueOf(40.0))));

        T.group("Json：序列化的转义");
        String w = Json.write(T.map("s", "引\"号\n换行\\反斜杠"));
        T.contains("引号转义", w, "\\\"");
        T.contains("换行转义", w, "\\n");
        T.contains("反斜杠转义", w, "\\\\");
        T.eq("中文不转 \\u（服务端 UTF-8 收得下）", "{\"s\":\"中文\"}",
                Json.write(T.map("s", "中文")));

        T.group("Json：写完能读回来（往返）");
        Map<String, Object> src = T.map(
                "values", T.map(
                        "provider_ltm_settings.active_reply.possibility_reply",
                        Double.valueOf(0.35),
                        "provider_settings.max_context_length", Long.valueOf(40),
                        "enable", Boolean.FALSE),
                "note", "带\"引号\"和\n换行的中文");
        try {
            Map<String, Object> back = Json.parseObject(Json.write(src));
            Object vals = Json.raw(back, "values");
            T.near("小数往返", 0.35,
                    Json.dbl(vals, "provider_ltm_settings.active_reply.possibility_reply")
                            .doubleValue());
            T.eq("整数往返", Long.valueOf(40),
                    Json.lng(vals, "provider_settings.max_context_length"));
            T.eq("布尔往返", Boolean.FALSE, Json.boolOrNull(vals, "enable"));
            T.eq("含引号换行的中文往返", "带\"引号\"和\n换行的中文", Json.str(back, "note"));
        } catch (Json.JsonError e) {
            T.bad("往返失败", e.getMessage());
        }

        T.group("Json：深层与大数据");
        StringBuilder deep = new StringBuilder();
        for (int i = 0; i < 40; i++) {
            deep.append("{\"a\":");
        }
        deep.append("1");
        for (int i = 0; i < 40; i++) {
            deep.append("}");
        }
        try {
            Json.parseObject(deep.toString());
            T.ok("40 层嵌套能解析");
        } catch (Json.JsonError e) {
            T.bad("深嵌套解析失败", e.getMessage());
        }
        StringBuilder big = new StringBuilder("{\"list\":[");
        for (int i = 0; i < 2000; i++) {
            if (i > 0) {
                big.append(",");
            }
            big.append("{\"i\":").append(i).append("}");
        }
        big.append("]}");
        try {
            List<Object> l = Json.arr(Json.parseObject(big.toString()), "list");
            T.eq("2000 项数组", 2000, l.size());
            T.eq("末项正确", Long.valueOf(1999), Json.lng(l.get(1999), "i"));
        } catch (Json.JsonError e) {
            T.bad("大数组解析失败", e.getMessage());
        }
    }

    private static void badJson(String what, final String text) {
        try {
            Json.parse(text);
            T.bad(what + " 应该报错", "却解析成功了");
        } catch (Json.JsonError e) {
            T.ok(what + " 报错了");
        }
    }
}
