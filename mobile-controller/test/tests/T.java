package tests;


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

    public static void isNull(String what, Object v) {
        if (v == null) {
            ok(what);
        } else {
            bad(what, "期望 null，得到 [" + v + "]");
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
}
