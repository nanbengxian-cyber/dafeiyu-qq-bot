package tests;

import com.dafeiyu.console.Feed;

import java.util.List;
import java.util.Map;

/**
 * Feed（观察页纯逻辑层）的测试。
 *
 * 主线是**降级与口径**：服务端少给一段、给 null、给老字段名，页面不能空、
 * 不能崩、不能把「机器人消息」标成「群友消息」。这些在真机上要造半截子
 * 服务端响应才能复现，在这里就是一行 map()。
 */
public final class FeedTest {

    @SuppressWarnings("unchecked")
    public static void run() {
        T.group("Feed：群列表");
        Map<String, Object> live = T.map(
                "groups", java.util.Arrays.asList(
                        T.map("id", "476", "label", "群 476", "user_msgs", Long.valueOf(240),
                                "bot_msgs", Long.valueOf(5803), "last_age_s", Long.valueOf(12124)),
                        T.map("id", "106", "label", "群 106", "user_msgs", Long.valueOf(142),
                                "bot_msgs", Long.valueOf(4), "last_age_s", Long.valueOf(175473))),
                "group", "476",
                "dialog", java.util.Arrays.asList(
                        T.map("ts", Long.valueOf(1000), "who", "甲", "qq", "u1",
                                "dir", "in", "text", "你好", "src", "memory"),
                        T.map("ts", Long.valueOf(2000), "who", "机器人", "qq", "",
                                "dir", "out", "text", "在的", "src", "effect",
                                "reactions", Long.valueOf(2))),
                "more", Boolean.TRUE);
        List<Feed.Group> gs = Feed.groups(live);
        T.eq("群数量", 2, Integer.valueOf(gs.size()));
        T.eq("群 id", "476", gs.get(0).id);
        T.eq("群消息数", 240, Integer.valueOf(gs.get(0).userMsgs));
        T.eq("群机消息数", 5803, Integer.valueOf(gs.get(0).botMsgs));
        T.eq("当前群回显", "476", Feed.currentGroup(live, ""));
        T.eq("null live 回退选群", "999", Feed.currentGroup(null, "999"));
        // groups 为空数组时无群可选：回退到调用方给的 fallback
        T.eq("空 groups 回退 fallback", "x", Feed.currentGroup(T.map("groups", java.util.Arrays.asList()), "x"));

        T.group("Feed：对话消息");
        // 服务端契约：dialog 新的在前（desc）。feed 里 2000(机器人) 排在 1000(群友) 前。
        List<Feed.Msg> msgs = Feed.messages(T.map(
                "dialog", java.util.Arrays.asList(
                        T.map("ts", Long.valueOf(2000), "who", "机器人", "qq", "",
                                "dir", "out", "text", "在的", "src", "effect",
                                "reactions", Long.valueOf(2)),
                        T.map("ts", Long.valueOf(1000), "who", "甲", "qq", "u1",
                                "dir", "in", "text", "你好", "src", "memory"))));
        T.eq("消息数量", 2, Integer.valueOf(msgs.size()));
        T.eq("新在前（服务端序透传）", Long.valueOf(2000), Long.valueOf(msgs.get(0).ts));
        T.isTrue("机器人侧标记", msgs.get(0).bot);
        T.eq("机器人名字", "机器人", msgs.get(0).who);
        T.eq("reactions 带出", Integer.valueOf(2), msgs.get(0).reactions);
        T.isFalse("群友侧不是机器人", msgs.get(1).bot);
        T.isNull("群友消息无 reactions", msgs.get(1).reactions);
        T.eq("文本", "你好", msgs.get(1).text);
        T.eq("null live 不崩", 0, Integer.valueOf(Feed.messages(null).size()));

        T.group("Feed：残缺响应降级");
        List<Feed.Msg> holes = Feed.messages(T.map(
                "dialog", java.util.Arrays.asList(
                        T.map("dir", "out"),          // 没 ts/who/text
                        "不是map的元素",              // 混进来的字符串
                        null)));                       // 混进来的 null
        T.eq("残缺行不崩且保留", 1, Integer.valueOf(holes.size()));
        T.isTrue("残缺机器人标记仍对", holes.get(0).bot);
        T.eq("缺文本给空串", "", holes.get(0).text);

        T.group("Feed：心智页");
        Map<String, Object> mind = T.map(
                "honesty_note", "dsh-mind 的状态块，不是原始思维链",
                "mind", T.map("recent", java.util.Arrays.asList(
                        T.map("age_s", Long.valueOf(90), "islands",
                                java.util.Arrays.asList("好奇(2)·指向我", "连接 100"),
                                "notes", java.util.Arrays.asList("今日馋夜宵")))),
                "effect", java.util.Arrays.asList(
                        T.map("age_s", Long.valueOf(250), "text", "在的",
                                "reactions", Long.valueOf(2), "why", "群里跟着玩了")),
                "selfaware", T.map("revisions", java.util.Arrays.asList(
                        T.map("ts", Long.valueOf(System.currentTimeMillis() / 1000 - 60),
                                "subject", "vision", "old", "degraded",
                                "new", "available", "reason", "imgctx 修复"))),
                "dynamics", T.map("n_user_msg", Long.valueOf(5398),
                        "n_bot_msg", Long.valueOf(1095), "p3_share", Double.valueOf(0.192),
                        "p1_miss_n", Long.valueOf(5), "generated", "2026-09-13 19:30"),
                "live_plugins", java.util.Arrays.asList(
                        T.map("live_status", "running"),
                        T.map("live_status", "crashed"),
                        T.map("live_status", "idle"),
                        T.map("live_status", "disabled")));
        Feed.MindPage page = Feed.mindPage(mind);
        T.isTrue("有数据", page.hasData);
        T.eq("诚实标注透传", "dsh-mind 的状态块，不是原始思维链", page.honesty);
        T.eq("状态行 1 条", 1, Integer.valueOf(page.rows.size()));
        T.contains("islands 拼进文案", page.rows.get(0).text, "好奇(2)·指向我");
        T.contains("notes 拼进文案", page.rows.get(0).text, "今日馋夜宵");
        T.contains("相对时间", page.rows.get(0).text, "前");
        T.eq("效应行 1 条", 1, Integer.valueOf(page.effects.size()));
        T.contains("效应带反应数", page.effects.get(0).text, "反应 2 条");
        T.contains("效应带判词", page.effects.get(0).text, "群里跟着玩了");
        T.eq("能力变化 1 条", 1, Integer.valueOf(page.revisions.size()));
        T.contains("迁移文案", page.revisions.get(0).text, "degraded → available");
        T.contains("插件摘要：有错误", page.pluginSummary, "有错误");
        T.contains("记分卡：参与度", page.dynamics, "19%");
        T.contains("记分卡：被@未回", page.dynamics, "被@未回 5 次");

        T.group("Feed：心智页降级");
        Feed.MindPage empty = Feed.mindPage(null);
        T.isFalse("null 无数据", empty.hasData);
        T.eq("null 无插件摘要", "", empty.pluginSummary);
        Feed.MindPage sparse = Feed.mindPage(T.map());
        T.isFalse("空 map 无数据", sparse.hasData);
        // 老服务端响应（mind.recent 平铺在顶层）也要能读
        Feed.MindPage legacy = Feed.mindPage(T.map(
                "recent", java.util.Arrays.asList(
                        T.map("age_s", Long.valueOf(30), "islands", java.util.Arrays.asList("累")))));
        T.eq("老字段名兼容", 1, Integer.valueOf(legacy.rows.size()));

        T.group("Feed：日志行");
        Map<String, Object> log = T.map(
                "lines", java.util.Arrays.asList(
                        T.map("ts", "2026-09-14 10:00:00", "level", "INFO",
                                "tag", "dsh-voice", "text", "[voice] 已加载"),
                        T.map("ts", "2026-09-14 10:05:00", "level", "ERRO",
                                "tag", "dsh-crash", "text", "[crash] 炸了"),
                        T.map("ts", "2026-09-14 10:06:00", "level", "WARN",
                                "tag", "dsh-web", "text", "[web] 审核超时"),
                        T.map("ts", "2026-09-14 10:07:00", "level", "?",
                                "tag", "", "text", "Traceback (most recent call last)")));
        List<Feed.LogLine> lines = Feed.logLines(log);
        T.eq("行数", 4, Integer.valueOf(lines.size()));
        T.eq("INFO 普通色调", 0, Integer.valueOf(lines.get(0).tone));
        T.eq("ERRO 坏色调", 3, Integer.valueOf(lines.get(1).tone));
        T.eq("WARN 警告色调", 2, Integer.valueOf(lines.get(2).tone));
        T.eq("Traceback 也算坏", 3, Integer.valueOf(lines.get(3).tone));
        T.eq("无 tag 给空串（界面上显示为无插件名）", "", lines.get(3).tag);
        T.eq("null 不崩", 0, Integer.valueOf(Feed.logLines(null).size()));
    }
}
