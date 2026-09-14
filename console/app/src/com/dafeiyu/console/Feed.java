package com.dafeiyu.console;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 观察页（实时/心智/日志）的纯逻辑 —— 不碰任何 android.* API，
 * 和 StatusFmt/Ui 一样能在普通 JVM 上被单测穿。
 *
 * 字段名一律以 console_api.py 的 /live /mind /log /rawconfig 输出为准，
 * 不凭记忆写 —— 拼错一个键的表现是「那一行永远空」，最难发现。
 */
public final class Feed {

    private Feed() {
    }

    /** 一条对话消息的展示条目。 */
    public static final class Msg {
        public final long ts;
        public final String who;
        public final boolean bot;
        public final String text;
        public final Integer reactions;

        Msg(long ts, String who, boolean bot, String text, Integer reactions) {
            this.ts = ts;
            this.who = who;
            this.bot = bot;
            this.text = text;
            this.reactions = reactions;
        }
    }

    /** 一个群的可选列表项。 */
    public static final class Group {
        public final String id;
        public final String label;
        public final int userMsgs;
        public final int botMsgs;
        public final long lastAgeS;

        Group(String id, String label, int userMsgs, int botMsgs, long lastAgeS) {
            this.id = id;
            this.label = label;
            this.userMsgs = userMsgs;
            this.botMsgs = botMsgs;
            this.lastAgeS = lastAgeS;
        }
    }

    // ---------------------------------------------------------------- 群聊流

    /** /live 的 groups 数组 → 可选列表。空数组给一条「暂无群」。 */
    public static List<Group> groups(Map<String, Object> live) {
        List<Group> out = new ArrayList<Group>();
        if (live == null) {
            return out;
        }
        for (Object o : Json.arr(live, "groups")) {
            if (!(o instanceof Map)) {
                continue;
            }
            Map<?, ?> g = (Map<?, ?>) o;
            String id = Json.str(g, "id", "");
            if (id.isEmpty()) {
                continue;
            }
            Long la = Json.lng(g, "last_age_s");
            out.add(new Group(
                    id,
                    Json.str(g, "label", "群 " + id),
                    lngOr(g, "user_msgs"),
                    lngOr(g, "bot_msgs"),
                    la == null ? -1 : la.longValue()));
        }
        return out;
    }

    /** /live 的 dialog 数组 → 消息条目（新的在前，界面上倒着渲染成「最新在底」）。 */
    public static List<Msg> messages(Map<String, Object> live) {
        List<Msg> out = new ArrayList<Msg>();
        if (live == null) {
            return out;
        }
        for (Object o : Json.arr(live, "dialog")) {
            if (!(o instanceof Map)) {
                continue;
            }
            Map<?, ?> m = (Map<?, ?>) o;
            String dir = Json.str(m, "dir", "in");
            Long ts = Json.lng(m, "ts");
            Long reactions = Json.lng(m, "reactions");
            out.add(new Msg(
                    ts == null ? 0 : ts.longValue(),
                    Json.str(m, "who", ""),
                    "out".equals(dir),
                    Json.str(m, "text", ""),
                    reactions == null ? null : Integer.valueOf((int) reactions.longValue())));
        }
        return out;
    }

    /** 当前选中的群 id；live 没给就回第一个群。 */
    public static String currentGroup(Map<String, Object> live, String fallback) {
        String g = Json.str(live, "group", "");
        if (!g.isEmpty()) {
            return g;
        }
        List<Group> gs = groups(live);
        if (!gs.isEmpty()) {
            return gs.get(0).id;
        }
        return fallback == null ? "" : fallback;
    }

    // ---------------------------------------------------------------- 心智页

    /** 一个内在状态块（dsh-mind 每轮注入的 islands/notes）。 */
    public static final class MindRow {
        public final long ageS;
        public final String text;

        MindRow(long ageS, String text) {
            this.ageS = ageS;
            this.text = text;
        }
    }

    /** 心智页整页的展示条目。 */
    public static final class MindPage {
        public final String honesty;
        public final List<MindRow> rows;      // 「内在状态」区
        public final List<MindRow> effects;   // 「发出去的话 & 反应」区
        public final List<MindRow> revisions; // 「能力变化」区
        public final String dynamics;         // 记分卡摘要文案
        public final String pluginSummary;    // 插件活性一句话
        public final boolean hasData;

        MindPage(String honesty, List<MindRow> rows, List<MindRow> effects,
                 List<MindRow> revisions, String dynamics, String pluginSummary,
                 boolean hasData) {
            this.honesty = honesty;
            this.rows = rows;
            this.effects = effects;
            this.revisions = revisions;
            this.dynamics = dynamics;
            this.pluginSummary = pluginSummary;
            this.hasData = hasData;
        }
    }

    /** 把 /mind 响应整理成心智页。任何一段缺了都降级成一句话，不让整页空。 */
    public static MindPage mindPage(Map<String, Object> mind) {
        if (mind == null) {
            return new MindPage("", new ArrayList<MindRow>(),
                    new ArrayList<MindRow>(), new ArrayList<MindRow>(),
                    "", "", false);
        }
        List<MindRow> rows = new ArrayList<MindRow>();
        List<Object> recent = Json.arr(mind, "recent");  // 兼容老响应名
        Object mindObj = mind.get("mind");
        if (mindObj instanceof Map) {
            recent = Json.arr((Map<String, Object>) mindObj, "recent");
        }
        for (Object o : recent) {
            if (!(o instanceof Map)) {
                continue;
            }
            Map<?, ?> r = (Map<?, ?>) o;
            Long age = Json.lng(r, "age_s");
            String head = age == null ? "" : StatusFmt.dur(age) + "前";
            StringBuilder sb = new StringBuilder();
            List<?> islands = Json.arr(r, "islands");
            List<?> notes = Json.arr(r, "notes");
            for (Object is : islands) {
                if (sb.length() > 0) {
                    sb.append(" ｜ ");
                }
                sb.append(String.valueOf(is));
            }
            for (Object n : notes) {
                if (sb.length() > 0) {
                    sb.append(" ｜ ");
                }
                sb.append(String.valueOf(n));
            }
            if (sb.length() == 0) {
                sb.append("（无状态块注入）");
            }
            rows.add(new MindRow(age == null ? -1 : age.longValue(),
                    head.isEmpty() ? sb.toString() : head + " " + sb.toString()));
        }

        List<MindRow> effects = new ArrayList<MindRow>();
        List<Object> effArr = Json.arr(mind, "effect");
        for (Object o : effArr) {
            if (!(o instanceof Map)) {
                continue;
            }
            Map<?, ?> e = (Map<?, ?>) o;
            Long age = Json.lng(e, "age_s");
            Long reac = Json.lng(e, "reactions");
            StringBuilder sb = new StringBuilder();
            if (age != null) {
                sb.append(StatusFmt.dur(age)).append("前 · ");
            }
            sb.append(Json.str(e, "text", ""));
            if (reac != null) {
                sb.append("（反应 ").append(reac).append(" 条）");
            }
            String why = Json.str(e, "why", "");
            if (!why.isEmpty() && !"None".equals(why)) {
                sb.append(" · ").append(why);
            }
            effects.add(new MindRow(age == null ? -1 : age.longValue(), sb.toString()));
        }

        List<MindRow> revisions = new ArrayList<MindRow>();
        Object sa = mind.get("selfaware");
        if (sa instanceof Map) {
            List<?> revs = Json.arr((Map<String, Object>) sa, "revisions");
            for (Object o : revs) {
                if (!(o instanceof Map)) {
                    continue;
                }
                Map<?, ?> r = (Map<?, ?>) o;
                Long age = Json.lng(r, "ts");
                String reason = Json.str(r, "reason", "");
                String subj = Json.str(r, "subject", "");
                String oldV = Json.str(r, "old", "");
                String newV = Json.str(r, "new", "");
                StringBuilder sb = new StringBuilder();
                if (age != null) {
                    long ago = (System.currentTimeMillis() / 1000) - age.longValue();
                    sb.append(StatusFmt.dur(ago < 0 ? 0 : ago)).append("前 · ");
                }
                sb.append(subj).append("：").append(oldV).append(" → ").append(newV);
                if (!reason.isEmpty() && !"None".equals(reason)) {
                    sb.append("（").append(reason).append("）");
                }
                revisions.add(new MindRow(-1, sb.toString()));
            }
        }

        String dynamics = "";
        Object dyn = mind.get("dynamics");
        if (dyn instanceof Map) {
            dynamics = dynamicsText((Map<?, ?>) dyn);
        }
        String pluginSummary = livePluginSummary(Json.arr(mind, "live_plugins"));
        String honesty = Json.str(mind, "honesty_note", "");
        boolean hasData = !rows.isEmpty() || !effects.isEmpty() || !revisions.isEmpty();
        return new MindPage(honesty, rows, effects, revisions, dynamics,
                pluginSummary, hasData);
    }

    /** 记分卡 → 一两行摘要文案。给不出就空串（页面上隐藏该区）。 */
    static String dynamicsText(Map<?, ?> d) {
        if (d == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder("记分卡");
        Long user = Json.lng(d, "n_user_msg");
        Long bot = Json.lng(d, "n_bot_msg");
        Double share = Json.dbl(d, "p3_share");
        if (user != null && bot != null) {
            sb.append("：机器人 ").append(bot).append(" / 群 ").append(user)
                    .append(" 条");
        }
        if (share != null) {
            sb.append("，参与度 ").append(Math.round(share.doubleValue() * 100)).append("%");
        }
        Long p1 = Json.lng(d, "p1_miss_n");
        if (p1 != null && p1.longValue() > 0) {
            sb.append("，被@未回 ").append(p1).append(" 次");
        }
        Long p6 = Json.lng(d, "p6_total");
        if (p6 == null && d.get("p6_fails") instanceof Map) {
            p6 = Long.valueOf(((Map<?, ?>) d.get("p6_fails")).size());
        }
        if (p6 != null && p6.longValue() > 0) {
            sb.append("，功能失败 ").append(p6).append(" 类");
        }
        String gen = Json.str(d, "generated", "");
        if (!gen.isEmpty()) {
            sb.append("（").append(gen).append("）");
        }
        return sb.toString();
    }

    /** 插件活性 → 一句话：X 运行 / Y 出错 / Z 关闭。 */
    static String livePluginSummary(List<?> plugins) {
        if (plugins == null || plugins.isEmpty()) {
            return "插件活性：读不到";
        }
        int running = 0, errored = 0, disabled = 0, other = 0;
        for (Object o : plugins) {
            if (!(o instanceof Map)) {
                continue;
            }
            Map<?, ?> p = (Map<?, ?>) o;
            String st = Json.str(p, "live_status", "");
            if ("running".equals(st) || "active".equals(st)) {
                running++;
            } else if ("crashed".equals(st) || "error".equals(st)) {
                errored++;
            } else if ("disabled".equals(st)) {
                disabled++;
            } else {
                other++;
            }
        }
        StringBuilder sb = new StringBuilder("插件：").append(running).append(" 在运行");
        if (errored > 0) {
            sb.append("，").append(errored).append(" 有错误！");
        } else if (disabled > 0) {
            sb.append("，").append(disabled).append(" 已关闭");
        } else if (other > 0) {
            sb.append("，").append(other).append(" 待观察");
        }
        return sb.toString();
    }

    // ---------------------------------------------------------------- 日志页

    /** 一条日志行的展示条目。 */
    public static final class LogLine {
        public final String ts;
        public final String tag;
        public final String text;
        public final int tone;  // 0 普通 / 1 好 / 2 警告 / 3 坏

        LogLine(String ts, String tag, String text, int tone) {
            this.ts = ts;
            this.tag = tag;
            this.text = text;
            this.tone = tone;
        }
    }

    public static List<LogLine> logLines(Map<String, Object> log) {
        List<LogLine> out = new ArrayList<LogLine>();
        if (log == null) {
            return out;
        }
        for (Object o : Json.arr(log, "lines")) {
            if (!(o instanceof Map)) {
                continue;
            }
            Map<?, ?> l = (Map<?, ?>) o;
            String lvl = Json.str(l, "level", "");
            int tone = 0;
            if ("ERRO".equals(lvl) || "CRIT".equals(lvl)
                    || (Json.str(l, "text", "") != null
                        && Json.str(l, "text", "").contains("Traceback"))) {
                tone = 3;
            } else if ("WARN".equals(lvl)) {
                tone = 2;
            }
            out.add(new LogLine(
                    Json.str(l, "ts", ""),
                    Json.str(l, "tag", ""),
                    Json.str(l, "text", ""),
                    tone));
        }
        return out;
    }

    // ---------------------------------------------------------------- 小工具

    private static int lngOr(Map<?, ?> m, String key) {
        Long v = Json.lng(m, key);
        return v == null ? 0 : v.intValue();
    }
}
