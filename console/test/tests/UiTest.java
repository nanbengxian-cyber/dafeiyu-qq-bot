package tests;

import com.dafeiyu.console.KnobModel;
import com.dafeiyu.console.StatusFmt;
import com.dafeiyu.console.Ui;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Ui 的测试 —— 状态页装配、危险动作确认文案、错误话术。
 *
 * 重点是两条：
 *   · 横幅只报最坏的一件事（同时列「掉线」和「内存高」会让人抓不住重点）；
 *   · 危险动作的确认必须写明**后果**，不能只问「确定吗」。
 */
public final class UiTest {

    public static void run() {
        Map<String, Object> healthy = T.healthyStatus();
        KnobModel.Schema s = T.schema();
        List<Object> modes = T.modesOf(T.schemaJson());

        T.group("Ui：状态页行装配");
        List<StatusFmt.Row> rows = Ui.statusRows(healthy, modes);
        T.isTrue("行数够（" + rows.size() + "）", rows.size() >= 12);
        T.eq("第一行是 QQ", "QQ", rows.get(0).label);
        T.isTrue("在线时没有二维码行", T.row(rows, "二维码") == null);
        T.isTrue("有机器人容器行", T.row(rows, "机器人") != null);
        T.isTrue("有 QQ 客户端行", T.row(rows, "QQ 客户端") != null);
        T.isTrue("有工具能力行", T.row(rows, "工具能力") != null);
        T.isTrue("有插件行", T.row(rows, "插件") != null);
        T.isTrue("有内存行", T.row(rows, "内存") != null);
        for (StatusFmt.Row r : rows) {
            if (r.value == null || r.value.isEmpty()) {
                T.bad("有空值的行", r.label);
                break;
            }
        }
        T.ok("每一行都有值");

        // 掉线时二维码行要插到很靠前 —— 那时人就是要去扫码
        Map<String, Object> off = T.tweak(healthy, "qq.state", "offline");
        off = T.tweak(off, "qq.qr_available", Boolean.TRUE);
        off = T.tweak(off, "qq.qr_age_seconds", Long.valueOf(20));
        rows = Ui.statusRows(off, modes);
        T.eq("二维码行紧跟 QQ 行", "二维码", rows.get(1).label);

        T.group("Ui：横幅只报最坏的一件事");
        StatusFmt.Row head = Ui.headline(healthy);
        T.contains("一切正常", head.value, "一切正常");
        T.eq("好色调", 1, head.tone);

        head = Ui.headline(off);
        T.contains("掉线优先", head.value, "掉线");
        T.contains("提示去扫码", head.value, "扫码");
        T.eq("坏色调", 3, head.tone);
        T.notContains("不提无关的内存", head.value, "内存");

        // 掉线 + 内存也高：只报掉线
        Map<String, Object> both = T.tweak(off, "host.mem_available",
                Long.valueOf(100L * 1024 * 1024));
        T.contains("同时有多个问题时报掉线", Ui.headline(both).value, "掉线");

        // 看门狗停摆压过一切：那时候「在线」本身不可信
        Map<String, Object> stale = T.tweak(healthy, "qq.watchdog_stale",
                Boolean.TRUE);
        stale = T.tweak(stale, "qq.watchdog_age_seconds", Long.valueOf(600));
        head = Ui.headline(stale);
        T.contains("说状态不明", head.value, "状态不明");
        T.contains("说数据是旧的", head.value, "旧的");
        T.notContains("不说一切正常", head.value, "一切正常");

        // 在线但 tool_use 丢了 —— 这是「答应了却不发图」的根因
        head = Ui.headline(T.tweak(healthy, "runtime.tool_use_ok", Boolean.FALSE));
        T.contains("点出工具能力异常", head.value, "工具能力");
        T.contains("描述症状", head.value, "嘴上答应");
        T.eq("标红", 3, head.tone);

        // 在线但容器挂了
        Map<String, Object> deadBox = T.deepCopy(healthy);
        @SuppressWarnings("unchecked")
        Map<String, Object> c0 = (Map<String, Object>)
                com.dafeiyu.console.Json.arr(deadBox, "containers").get(0);
        c0.put("state", "exited");
        head = Ui.headline(deadBox);
        T.contains("点名容器", head.value, "astrbot");
        T.eq("标红", 3, head.tone);

        // 在线但上下文没上限
        head = Ui.headline(T.tweak(healthy, "runtime.context_turns",
                Long.valueOf(-1)));
        T.contains("提上下文", head.value, "上下文");
        T.contains("提后果", head.value, "答非所问");

        // 在线但内存快满
        head = Ui.headline(T.tweak(healthy, "host.mem_available",
                Long.valueOf(100L * 1024 * 1024)));
        T.contains("提内存", head.value, "内存");
        T.eq("警告色", 2, head.tone);

        head = Ui.headline(T.map());
        T.contains("完全没数据时说读不到", head.value, "读不到");
        T.isTrue("不是好色调", head.tone != 1);

        T.group("Ui：危险动作的确认要写后果");
        KnobModel.Action restart = s.actions.get(0);
        String text = Ui.confirmAction(restart);
        T.contains("带上服务端的描述", text, "20 秒");
        T.contains("说会打断对话", text, "打断");
        T.contains("要求确认", text, "确定");
        KnobModel.Action safe = s.actions.get(1);
        text = Ui.confirmAction(safe);
        T.notContains("非危险动作不加恐吓", text, "打断");
        T.contains("仍然说明做什么", text, "忘掉");

        T.group("Ui：模式确认");
        text = Ui.confirmMode(s.modes.get(0));
        T.contains("带模式名", text, "安静");
        T.contains("带说明", text, "只在被叫时说话");

        T.group("Ui：Markdown 去掉");
        T.eq("去掉星号", "只在被叫时回话", Ui.stripMd("只在**被叫时**回话"));
        T.eq("去掉反引号", "值是 40", Ui.stripMd("值是 `40`"));
        T.eq("null 不崩", "", Ui.stripMd(null));

        T.group("Ui：改动摘要");
        Map<String, Object> current = new LinkedHashMap<String, Object>();
        current.put("provider_ltm_settings.active_reply.possibility_reply",
                Double.valueOf(0.25));
        current.put("provider_settings.max_context_length", Long.valueOf(40));
        current.put("platform_settings.rate_limit.count", Long.valueOf(30));
        Map<String, Object> changed = new LinkedHashMap<String, Object>();
        changed.put("provider_ltm_settings.active_reply.possibility_reply",
                Double.valueOf(0.5));
        text = Ui.confirmChanges(s, current, changed);
        T.contains("说改几项", text, "1 项");
        T.contains("显示旧值", text, "0.25");
        T.contains("显示新值", text, "0.5");
        T.contains("箭头", text, "→");
        T.notContains("热更新项不提重启", text, "重启机器人容器");

        changed.put("platform_settings.rate_limit.count", Long.valueOf(10));
        text = Ui.confirmChanges(s, current, changed);
        T.contains("冷更新项标注", text, "要重启才生效");
        T.contains("末尾提醒重启后果", text, "群里不回话");

        T.group("Ui：模型列表");
        List<String> choices = Ui.modelChoices(s.chatModels,
                "deepseek-v4-flash-0731");
        T.eq("当前的排第一", "deepseek-v4-flash-0731（当前）", choices.get(0));
        T.eq("不重复列出", 3, choices.size());
        T.eq("能还原真名", "deepseek-v4-flash-0731", Ui.modelOf(choices.get(0)));
        T.eq("没后缀的原样", "glm-5.3", Ui.modelOf("glm-5.3"));
        T.eq("null 不崩", "", Ui.modelOf(null));
        choices = Ui.modelChoices(new java.util.ArrayList<String>(), "");
        T.contains("空列表有提示", choices.get(0), "没给模型列表");

        T.group("Ui：识图渠道列表");
        choices = Ui.visionChoices(s, "zhipu-vision");
        T.eq("两个渠道", 2, choices.size());
        T.contains("当前的标注出来", choices.get(0), "当前");
        T.contains("标注直连便宜", choices.get(0), "便宜快");
        T.contains("标注故障转移", choices.get(1), "故障转移");
        T.eq("能还原 id", "zhipu-vision", Ui.visionIdOf(s, choices.get(0)));
        T.eq("能还原第二个", "vision-opus5", Ui.visionIdOf(s, choices.get(1)));
        T.eq("认不出就空", "", Ui.visionIdOf(s, "乱写的"));

        T.group("Ui：插件列表只列可控的");
        List<Ui.PluginItem> plugins = Ui.plugins(healthy);
        T.eq("三个 dsh 插件", 3, plugins.size());
        T.eq("显示中文名", "出图", plugins.get(0).label);
        T.isTrue("开着的标开", plugins.get(0).enabled);
        T.isFalse("关着的标关", plugins.get(2).enabled);
        // 框架自带插件关掉会出事，服务端也拒绝，界面上不该出现
        Map<String, Object> withBuiltin = T.deepCopy(healthy);
        com.dafeiyu.console.Json.arr(withBuiltin, "plugins").add(
                T.map("name", "astrbot", "label", "框架核心",
                        "enabled", Boolean.TRUE));
        T.eq("框架插件被过滤掉", 3, Ui.plugins(withBuiltin).size());
        T.eq("没有插件数据时是空表", 0, Ui.plugins(T.map()).size());

        T.group("Ui：配置页摘要");
        Map<String, Object> vals = new LinkedHashMap<String, Object>();
        vals.put("provider_ltm_settings.active_reply.enable", Boolean.TRUE);
        vals.put("provider_ltm_settings.active_reply.possibility_reply",
                Double.valueOf(0.25));
        T.contains("匹配到模式时说模式名",
                Ui.configSummary(healthy, modes, vals), "日常");
        Map<String, Object> custom = T.tweak(healthy, "mode", "");
        T.contains("自定义时给概率", Ui.configSummary(custom, modes, vals), "25%");
        Map<String, Object> quiet = new LinkedHashMap<String, Object>();
        quiet.put("provider_ltm_settings.active_reply.enable", Boolean.FALSE);
        T.contains("关掉插话时说清楚",
                Ui.configSummary(custom, modes, quiet), "不主动插话");
        T.contains("认不出的模式 id 也显示",
                Ui.configSummary(T.tweak(healthy, "mode", "weird"), modes, vals),
                "weird");

        T.group("Ui：数据新鲜度");
        T.contains("显示服务器时间", Ui.freshness(healthy), "2026-09-03 02:30:00");
        T.contains("提示下拉刷新", Ui.freshness(healthy), "下拉刷新");
        T.contains("没时间也有话", Ui.freshness(T.map()), "刷新");

        T.group("Ui：stable 不返回 null");
        T.eq("null 变空表", 0, Ui.stable(null).size());
        T.eq("原样拷贝", 2, Ui.stable(T.map("a", 1, "b", 2)).size());
    }
}
