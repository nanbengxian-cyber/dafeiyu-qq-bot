package com.dafeiyu.controller;

import java.util.ArrayList;
import java.util.List;

/**
 * 机器人列表的「按名字搜索」过滤逻辑。
 *
 * 为什么要单独抽一个类（而不是直接写在 RobotsView 里）：
 * ① RobotsView 依赖 android.*，进不了单测；过滤逻辑是这个功能的**全部**
 *    智力所在，必须能被测试钉住。
 * ② 用户反馈原话：「机器人列表那里，是公开的，不是说不好，是不能定向搜索
 *    机器人名字」—— 这是明确的功能缺失，得有用例守着它不再退化。
 *
 * 刻意不碰 android.*：这样它能进 test/run-tests.sh。
 */
public final class RobotFilter {

    private RobotFilter() {
    }

    /**
     * 按关键字筛出名字匹配的项。
     *
     * 规则（都按用户直觉来）：
     *   * 空关键字 / 只有空白 → 全部返回（清空搜索框就恢复全部，这是默认预期）；
     *   * 忽略大小写 —— 用户输入 qq1 时要能搜到 QQ1，反之亦然；
     *   * **子串匹配**而不是前缀匹配：用户常记得名字中间那段
     *     （比如「test-01」只记得「01」），前缀匹配会让他搜不到；
     *   * 两端空白自动去掉（手机键盘很容易多打一个空格）。
     *
     * @param names 全部名字（保持原顺序）
     * @param query 关键字
     * @return 匹配到的下标，顺序与输入一致
     */
    public static List<Integer> match(List<String> names, String query) {
        List<Integer> out = new ArrayList<Integer>();
        if (names == null) {
            return out;
        }
        String q = query == null ? "" : query.trim().toLowerCase();
        for (int i = 0; i < names.size(); i++) {
            String n = names.get(i);
            if (q.isEmpty()) {
                out.add(i);
                continue;
            }
            if (n != null && n.toLowerCase().contains(q)) {
                out.add(i);
            }
        }
        return out;
    }

    /**
     * 搜索结果的提示语。
     *
     * ★ 关键：搜不到时**必须说清「是没搜到，不是没有机器人」**。
     * 否则用户会以为机器人被删了，转头去重新建一个 —— 而原来的还在，
     * 于是越建越乱。这是「空结果」这类交互最典型的坑。
     */
    public static String statusText(String query, int shown, int total) {
        String q = query == null ? "" : query.trim();
        if (total == 0) {
            return "还没有机器人 —— 在上面起个名字，点「新建」。";
        }
        if (q.isEmpty()) {
            return "共 " + total + " 个机器人。点「登录这个 QQ」去扫码。";
        }
        if (shown == 0) {
            return "没有名字含「" + q + "」的机器人（一共 " + total
                    + " 个）。清空搜索框看全部。";
        }
        return "搜到 " + shown + " 个（共 " + total + " 个）。";
    }
}
