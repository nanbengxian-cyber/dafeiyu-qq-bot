package tests;

import com.dafeiyu.controller.Knobs;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class KnobsTest {

    private static final String SCHEMA_JSON = "{"
            + "\"knobs\":["
            + "{\"key\":\"DSH_A\",\"label\":\"甲\",\"group\":\"分组一\",\"type\":\"int\","
            + "\"min\":1,\"max\":10,\"default\":\"5\",\"hint\":\"提示甲\",\"restart_required\":true},"
            + "{\"key\":\"DSH_B\",\"label\":\"乙\",\"group\":\"分组一\",\"type\":\"bool\","
            + "\"default\":\"1\",\"restart_required\":false},"
            + "{\"key\":\"DSH_C\",\"label\":\"丙\",\"group\":\"分组二\",\"type\":\"enum\","
            + "\"options\":[\"fast\",\"slow\"],\"default\":\"fast\"},"
            + "{\"key\":\"DSH_SECRET\",\"label\":\"密\",\"type\":\"str\",\"secret\":true},"
            + "{\"key\":\"DSH_BAD\",\"type\":\"matrix\"},"          // 不认识的类型 → 跳过
            + "{\"key\":\"DSH_A\",\"type\":\"str\"},"                // 重复 → 跳过
            + "{\"key\":\"lower\",\"type\":\"str\"},"                // 变量名不合法 → 跳过
            + "{\"key\":\"DSH_NOOPT\",\"type\":\"enum\"}"            // 枚举没选项 → 跳过
            + "]}";

    public static void run() {
        T.group("Knobs.parse");
        List<Knobs.Knob> knobs = Knobs.parse(parse(SCHEMA_JSON));
        T.eq("合法项数量（非法全跳过）", 4, knobs.size());
        T.eq("key 顺序", "DSH_A", knobs.get(0).key);
        T.eq("int 下限", 1.0, knobs.get(0).min);
        T.eq("restart 缺省为 true", true, knobs.get(0).restart);
        T.eq("restart 可显式关", false, knobs.get(1).restart);
        T.eq("enum 选项", "slow", knobs.get(2).options.get(1));
        T.eq("secret 标记", true, knobs.get(3).secret);
        T.eq("分组顺序", "分组一", Knobs.groupOrder(knobs).get(0));
        T.eq("分组按首现顺序且不重复", 3, Knobs.groupOrder(knobs).size());
        T.eq("裸数组也能解析", 4, Knobs.parse(parse(itemsJson())).size());
        T.eq("完全不是数组 → 空", 0, Knobs.parse("abc").size());

        T.group("Knobs.envValues / displayValue");
        String env = "# 注释\nDSH_A=3\n\nDSH_C=slow\nDSH_SECRET=abc123\n";
        Map<String, String> values = Knobs.envValues(env);
        T.eq("注释不进值表", false, values.containsKey("# 注释"));
        T.eq("读值", "3", values.get("DSH_A"));
        T.eq("不在文件里 → 用默认值", "5", Knobs.displayValue(knobs.get(0), null));
        T.eq("在文件里但是空值 → 原样空", "", Knobs.displayValue(knobs.get(0), ""));

        T.group("Knobs.isEnabled");
        T.eq("1 为开", true, Knobs.isEnabled(knobs.get(1), "1"));
        T.eq("0 为关", false, Knobs.isEnabled(knobs.get(1), "0"));
        T.eq("true 为开", true, Knobs.isEnabled(knobs.get(1), "true"));
        T.eq("空为关", false, Knobs.isEnabled(knobs.get(1), ""));
        T.eq("其它非空为开", true, Knobs.isEnabled(knobs.get(1), "abc"));

        T.group("Knobs.coerceValue");
        T.eq("bool 中文", "1", coerce(knobs.get(1), "开"));
        T.eq("bool 英文", "0", coerce(knobs.get(1), "off"));
        T.throwsWith("bool 非法", "只能选开或关", new Runnable() {
            public void run() {
                try {
                    Knobs.coerceValue(knobs.get(1), "maybe");
                } catch (Knobs.KnobException e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });
        T.eq("int 合法", "7", coerce(knobs.get(0), "7"));
        T.throwsWith("int 越上界", "不能大于 10", new Runnable() {
            public void run() {
                try {
                    Knobs.coerceValue(knobs.get(0), "11");
                } catch (Knobs.KnobException e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });
        T.throwsWith("int 小数", "必须是整数", new Runnable() {
            public void run() {
                try {
                    Knobs.coerceValue(knobs.get(0), "1.5");
                } catch (Knobs.KnobException e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });
        T.eq("enum 合法", "fast", coerce(knobs.get(2), "fast"));
        T.throwsWith("enum 非法", "只能从预设里选", new Runnable() {
            public void run() {
                try {
                    Knobs.coerceValue(knobs.get(2), "quick");
                } catch (Knobs.KnobException e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });
        T.eq("secret 照常校验", "xyz", coerce(knobs.get(3), " xyz "));
        T.eq("数字格式化：整数不带小数点", "2", Knobs.fmtNum(2.0));
        T.eq("数字格式化：小数去尾零", "2.5", Knobs.fmtNum(2.5000000001));

        T.group("Knobs.secretState");
        T.eq("已设置", "已设置", Knobs.secretState(knobs.get(3), "abc123"));
        T.eq("未设置", "未设置", Knobs.secretState(knobs.get(3), null));
        T.eq("敏感值绝不回显", false,
                Knobs.secretState(knobs.get(3), "abc123").contains("abc123"));

        T.group("Knobs.applyEnvText：行级替换");
        Map<String, String> updates = new LinkedHashMap<String, String>();
        updates.put("DSH_A", "8");
        Knobs.ApplyResult r = apply(env, updates);
        T.eq("替换成功", "DSH_A=8", lineOf(r.text, "DSH_A"));
        T.eq("注释保留", true, r.text.contains("# 注释"));
        T.eq("空行保留", true, r.text.contains("\n\n"));
        T.eq("其它行不动", true, r.text.contains("DSH_C=slow"));
        T.eq("改动记录", 1, r.changed.size());
        T.eq("旧值记录", "3", r.changed.get("DSH_A")[0]);

        T.group("Knobs.applyEnvText：值没变就不算改动");
        Map<String, String> same = new LinkedHashMap<String, String>();
        same.put("DSH_A", "3");
        T.eq("零改动", 0, apply(env, same).changed.size());

        T.group("Knobs.applyEnvText：缺失变量进托管小节");
        Map<String, String> add = new LinkedHashMap<String, String>();
        add.put("DSH_NEW", "1");
        Knobs.ApplyResult added = apply(env, add);
        T.eq("补了小节头", true,
                added.text.contains(Knobs.MANAGED_SECTION));
        T.eq("新变量在小节头后面", true, added.text
                .contains(Knobs.MANAGED_SECTION + "\nDSH_NEW=1\n"));
        T.eq("旧变量仍在", true, added.text.contains("DSH_A=3"));
        // 第二次追加：小节已存在，新变量插在小节头紧后面（与桌面版一致，后来的排前面）
        Map<String, String> add2 = new LinkedHashMap<String, String>();
        add2.put("DSH_NEW2", "2");
        Knobs.ApplyResult added2 = apply(added.text, add2);
        T.eq("小节头只出现一次", 1, count(added2.text, Knobs.MANAGED_SECTION));
        T.eq("第二次的变量也在小节里", true, added2.text
                .contains(Knobs.MANAGED_SECTION + "\nDSH_NEW2=2\nDSH_NEW=1"));

        T.group("Knobs.applyEnvText：非法输入");
        T.throwsWith("变量名不合法", "变量名不合法", new Runnable() {
            public void run() {
                try {
                    Map<String, String> bad = new LinkedHashMap<String, String>();
                    bad.put("bad name", "1");
                    Knobs.applyEnvText("", bad);
                } catch (Knobs.KnobException e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });
        T.throwsWith("值里有换行", "换行", new Runnable() {
            public void run() {
                try {
                    Map<String, String> bad = new LinkedHashMap<String, String>();
                    bad.put("DSH_X", "a\nb");
                    Knobs.applyEnvText("", bad);
                } catch (Knobs.KnobException e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });
    }

    private static Object parse(String json) {
        try {
            return com.dafeiyu.controller.Json.parse(json);
        } catch (com.dafeiyu.controller.Json.JsonError e) {
            throw new IllegalStateException(e);
        }
    }

    /** 同一份清单的裸数组形态（去外壳）。 */
    private static String itemsJson() {
        return SCHEMA_JSON.substring(SCHEMA_JSON.indexOf(':') + 1,
                SCHEMA_JSON.length() - 1);
    }

    private static String coerce(Knobs.Knob knob, String value) {
        try {
            return Knobs.coerceValue(knob, value);
        } catch (Knobs.KnobException e) {
            throw new IllegalStateException(e);
        }
    }

    private static Knobs.ApplyResult apply(String env, Map<String, String> updates) {
        try {
            return Knobs.applyEnvText(env, updates);
        } catch (Knobs.KnobException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String lineOf(String text, String key) {
        for (String line : text.split("\n")) {
            if (line.startsWith(key + "=")) {
                return line;
            }
        }
        return "";
    }

    private static int count(String text, String needle) {
        int n = 0;
        int i = 0;
        while ((i = text.indexOf(needle, i)) >= 0) {
            n++;
            i += needle.length();
        }
        return n;
    }
}
