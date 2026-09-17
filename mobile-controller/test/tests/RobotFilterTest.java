package tests;

import com.dafeiyu.controller.RobotFilter;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * 机器人列表「按名字搜索」的测试。
 *
 * 对应用户反馈：「机器人列表那里，是公开的，不是说不好，是不能定向搜索
 * 机器人名字」。
 *
 * 这里钉住两类东西：
 *   ① 匹配规则本身（大小写、子串、空格）；
 *   ② **空结果的提示语** —— 这是最容易被写错、后果又最误导人的一处：
 *      搜不到时必须让人明白「是搜索没匹配上」，而不是「机器人没了」。
 *      说不清的话，用户会去重新建一个，于是列表越建越乱。
 */
public final class RobotFilterTest {

    public static void run() {
        T.group("机器人列表：按名字搜索（修「不能定向搜索机器人名字」）");

        List<String> names = Arrays.asList(
                "qq1", "qq2", "QQ3", "test-01", "my-bot", "大肥鱼");

        // ── 空关键字 = 全部（清空搜索框就该恢复全部）──────────────────
        T.eq("★ 空关键字返回全部", 6, RobotFilter.match(names, "").size());
        T.eq("★ null 关键字返回全部", 6, RobotFilter.match(names, null).size());
        T.eq("★ 只有空格也返回全部", 6, RobotFilter.match(names, "   ").size());

        // ── 子串匹配（用户常只记得中间那段）────────────────────────────
        T.eq("★ 子串匹配 qq → 3 个", 3, RobotFilter.match(names, "qq").size());
        T.eq("★ 子串匹配 01（不是前缀）→ 1 个",
                1, RobotFilter.match(names, "01").size());
        T.eq("★ 子串匹配 bot（不是前缀）→ 1 个",
                1, RobotFilter.match(names, "bot").size());
        T.eq("子串匹配 test → 1 个", 1, RobotFilter.match(names, "test").size());

        // ── 忽略大小写（两个方向都要）─────────────────────────────────
        T.eq("★ 小写 qq 能搜到大写 QQ3", 3, RobotFilter.match(names, "qq").size());
        T.eq("★ 大写 QQ 也能搜到小写 qq1", 3, RobotFilter.match(names, "QQ").size());
        T.eq("★ 大小写混写同样有效", 3, RobotFilter.match(names, "Qq").size());

        // ── 两端空格自动去掉（手机键盘很容易多打一个空格）──────────────
        T.eq("★ 关键字两端空格被忽略", 3, RobotFilter.match(names, "  qq  ").size());

        // ── 中文名字（用户真会这么起名）───────────────────────────────
        T.eq("★ 中文名字能搜到", 1, RobotFilter.match(names, "大肥").size());
        T.eq("★ 中文全名能搜到", 1, RobotFilter.match(names, "大肥鱼").size());

        // ── 匹配不上 / 边界 ────────────────────────────────────────────
        T.eq("★ 匹配不上时返回空（不是抛异常）",
                0, RobotFilter.match(names, "zzzz").size());
        T.eq("空列表返回空", 0, RobotFilter.match(new ArrayList<String>(), "qq").size());
        T.eq("null 列表返回空（不抛异常）", 0, RobotFilter.match(null, "qq").size());

        // ── 结果顺序必须与输入一致（否则列表会乱跳）────────────────────
        List<Integer> idx = RobotFilter.match(names, "qq");
        T.eq("★ 结果顺序与输入一致", "[0, 1, 2]", idx.toString());

        // ── 名字里有 null 也不能崩（数据异常时界面不该白屏）────────────
        List<String> withNull = new ArrayList<String>();
        withNull.add(null);
        withNull.add("qq1");
        T.eq("★ 名字为 null 时跳过而不崩", 1,
                RobotFilter.match(withNull, "qq").size());
        T.eq("★ 名字为 null 时空关键字仍返回它", 2,
                RobotFilter.match(withNull, "").size());

        // ── 提示语 ────────────────────────────────────────────────────
        T.group("机器人列表：搜索提示语（空结果不能让人误会）");

        String none = RobotFilter.statusText("zzz", 0, 6);
        T.contains("★ 搜不到时说明是「没有名字含…的」", none, "没有名字含");
        T.contains("★ 搜不到时告诉总数（证明机器人还在）", none, "一共 6 个");
        T.contains("★ 搜不到时告诉怎么办（清空搜索框）", none, "清空搜索框");

        String some = RobotFilter.statusText("qq", 3, 6);
        T.contains("★ 有结果时报告命中数", some, "搜到 3 个");
        T.contains("★ 有结果时也报总数", some, "共 6 个");

        String all = RobotFilter.statusText("", 6, 6);
        T.contains("★ 无关键字时报告总数", all, "共 6 个");
        T.contains("★ 无关键字时提示下一步（登录这个 QQ）", all, "登录这个 QQ");

        String empty = RobotFilter.statusText("", 0, 0);
        T.contains("★ 一个机器人都没有时提示去新建", empty, "新建");
        T.isTrue("★ 没有机器人时不提「搜索」二字（免得误导）",
                !empty.contains("搜索") && !empty.contains("搜到"));
    }
}
