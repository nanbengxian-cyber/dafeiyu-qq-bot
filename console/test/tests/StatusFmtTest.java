package tests;

import com.dafeiyu.console.StatusFmt;

import java.util.List;
import java.util.Map;

/**
 * StatusFmt 的测试。
 *
 * 主线是**异常状态**：看门狗停摆、容器挂了、tool_use 丢了、代理回落。
 * 这些在真机上几乎无法复现（要真把服务弄坏），但恰恰是这个 App 存在的理由 ——
 * 它就是给「半夜机器人出事」用的。
 */
public final class StatusFmtTest {

    public static void run() {
        T.group("StatusFmt：时长");
        T.eq("秒", "45 秒", StatusFmt.dur(Long.valueOf(45)));
        T.eq("刚满一分钟", "1 分钟", StatusFmt.dur(Long.valueOf(60)));
        T.eq("分钟", "59 分钟", StatusFmt.dur(Long.valueOf(3599)));
        T.eq("整小时", "1 小时", StatusFmt.dur(Long.valueOf(3600)));
        T.eq("小时带分", "2 小时 30 分", StatusFmt.dur(Long.valueOf(9000)));
        T.eq("整天", "1 天", StatusFmt.dur(Long.valueOf(86400)));
        T.eq("天带小时", "3 天 4 小时", StatusFmt.dur(Long.valueOf(273600)));
        T.eq("零秒", "0 秒", StatusFmt.dur(Long.valueOf(0)));
        T.eq("null 不崩", "—", StatusFmt.dur(null));
        T.eq("负数不崩", "—", StatusFmt.dur(Long.valueOf(-5)));

        T.group("StatusFmt：字节");
        T.eq("小于 1K", "512 B", StatusFmt.bytes(Long.valueOf(512)));
        T.eq("KB", "1.0 KB", StatusFmt.bytes(Long.valueOf(1024)));
        // 三位数省掉小数位：「320 MB」比「320.0 MB」好读，也和 1010 MB 一致
        T.eq("MB", "320 MB",
                StatusFmt.bytes(Long.valueOf(320L * 1024 * 1024)));
        T.eq("两位数保留一位小数", "99.5 MB",
                StatusFmt.bytes(Long.valueOf((long) (99.5 * 1024 * 1024))));
        T.eq("GB", "2.0 GB",
                StatusFmt.bytes(Long.valueOf(2L * 1024 * 1024 * 1024)));
        T.eq("三位数省小数", "1010 MB",
                StatusFmt.bytes(Long.valueOf(1010L * 1024 * 1024)));
        T.eq("零", "0 B", StatusFmt.bytes(Long.valueOf(0)));
        T.eq("null 不崩", "—", StatusFmt.bytes(null));

        // ---------------------------------------------------------------- QQ
        Map<String, Object> healthy = T.healthyStatus();

        T.group("StatusFmt：QQ 在线");
        StatusFmt.Row qq = StatusFmt.qqRow(healthy);
        T.eq("在线是好色调", 1, qq.tone);
        T.contains("显示消息文案", qq.value, "在线");
        T.contains("带上次收发", qq.value, "12 秒前有消息");

        T.group("StatusFmt：QQ 掉线");
        Map<String, Object> off = T.tweak(healthy, "qq.state", "offline");
        off = T.tweak(off, "qq.message", "掉线了");
        StatusFmt.Row r = StatusFmt.qqRow(off);
        T.eq("掉线是坏色调", 3, r.tone);
        T.contains("显示掉线", r.value, "掉线");

        T.group("StatusFmt：看门狗停摆时不信 state");
        // 这是 watchdog v1 的真实事故：QQ 静默掉线，NapCat 不打任何日志，
        // 页面绿了一小时。规则是「status.json 过期就一律显示不确定」。
        Map<String, Object> stale = T.tweak(healthy, "qq.watchdog_stale", Boolean.TRUE);
        stale = T.tweak(stale, "qq.watchdog_age_seconds", Long.valueOf(3600));
        r = StatusFmt.qqRow(stale);
        T.contains("显示不确定", r.value, "不确定");
        T.contains("说清多久没更新", r.value, "1 小时");
        T.eq("不是好色调", 2, r.tone);
        T.notContains("不能显示在线", r.value, "在线");

        T.group("StatusFmt：状态完全读不到");
        Map<String, Object> noState = T.tweak(healthy, "qq.state", "");
        r = StatusFmt.qqRow(noState);
        T.contains("提示读不到", r.value, "读不到");
        T.eq("警告色调", 2, r.tone);

        T.group("StatusFmt：二维码只在掉线时出现");
        T.eq("在线时不显示二维码行", null, StatusFmt.qrRow(healthy));
        Map<String, Object> needQr = T.tweak(off, "qq.qr_available", Boolean.TRUE);
        needQr = T.tweak(needQr, "qq.qr_age_seconds", Long.valueOf(30));
        r = StatusFmt.qrRow(needQr);
        T.isTrue("掉线时有二维码行", r != null);
        T.contains("说可扫", r.value, "可扫");
        r = StatusFmt.qrRow(T.tweak(off, "qq.qr_available", Boolean.FALSE));
        T.contains("没码时说还没生成", r.value, "还没生成");
        T.eq("没码是警告", 2, r.tone);
        // 看门狗停摆时也该提示二维码 —— 那时状态不明，人多半就是要去扫码
        r = StatusFmt.qrRow(T.tweak(stale, "qq.qr_available", Boolean.TRUE));
        T.isTrue("看门狗停摆时也显示二维码行", r != null);

        T.group("StatusFmt：容器");
        List<Object> cs = com.dafeiyu.console.Json.arr(healthy, "containers");
        @SuppressWarnings("unchecked")
        Map<String, Object> bot = (Map<String, Object>) cs.get(0);
        r = StatusFmt.containerRow(bot);
        T.eq("astrbot 显示成中文", "机器人", r.label);
        T.contains("显示运行时长", r.value, "1 小时");
        T.contains("显示内存", r.value, "320 MB");
        T.eq("零重启是好色调", 1, r.tone);

        @SuppressWarnings("unchecked")
        Map<String, Object> nap = (Map<String, Object>) cs.get(1);
        r = StatusFmt.containerRow(nap);
        T.eq("napcat 显示成中文", "QQ 客户端", r.label);
        T.contains("重启过要说出来", r.value, "重启过 2 次");
        T.eq("重启过是警告", 2, r.tone);

        r = StatusFmt.containerRow(T.map("name", "astrbot", "state", "exited"));
        T.eq("挂了是坏色调", 3, r.tone);
        T.eq("显示状态", "exited", r.value);
        r = StatusFmt.containerRow(T.map("name", "astrbot", "state", "missing"));
        T.contains("容器不存在", r.value, "不存在");

        T.group("StatusFmt：主机资源");
        r = StatusFmt.memRow(healthy);
        T.contains("内存显示已用/总量", r.value, "/");
        T.contains("带百分比", r.value, "%");
        T.eq("55% 是正常", 1, r.tone);
        // 这台机器只有 1.9G，内存是最先出事的资源
        Map<String, Object> tight = T.tweak(healthy, "host.mem_available",
                Long.valueOf(150L * 1024 * 1024));
        T.eq("92% 标红", 3, StatusFmt.memRow(tight).tone);
        Map<String, Object> warn = T.tweak(healthy, "host.mem_available",
                Long.valueOf(450L * 1024 * 1024));
        T.eq("78% 警告", 2, StatusFmt.memRow(warn).tone);
        T.eq("缺字段不崩", "—", StatusFmt.memRow(T.map()).value);

        r = StatusFmt.diskRow(healthy);
        T.contains("磁盘显示剩余", r.value, "剩");
        T.eq("45% 正常", 1, r.tone);
        T.eq("缺字段不崩", "—", StatusFmt.diskRow(T.map()).value);

        r = StatusFmt.loadRow(healthy);
        T.contains("负载带核数", r.value, "2 核");
        T.eq("0.42 正常", 1, r.tone);
        T.eq("load 2.5 警告", 2,
                StatusFmt.loadRow(T.tweak(healthy, "host.load1",
                        Double.valueOf(2.5))).tone);
        T.eq("load 4 标红", 3,
                StatusFmt.loadRow(T.tweak(healthy, "host.load1",
                        Double.valueOf(4.0))).tone);

        T.group("StatusFmt：代理");
        r = StatusFmt.proxyRow(healthy);
        T.contains("显示已启用", r.value, "已启用");
        T.contains("显示出口 IP", r.value, "203.0.113.10");
        T.eq("启用是好色调", 1, r.tone);
        Map<String, Object> fallback = T.tweak(healthy, "proxy.active", Boolean.FALSE);
        fallback = T.tweak(fallback, "proxy.tunnel_healthy", Boolean.FALSE);
        r = StatusFmt.proxyRow(fallback);
        T.contains("回落直连要说清是自动的", r.value, "自动摘规则");
        T.eq("回落是警告不是错误", 2, r.tone);
        T.eq("完全没这块数据时是 —", "—", StatusFmt.proxyRow(T.map()).value);

        T.group("StatusFmt：模型");
        T.eq("聊天模型", "deepseek-v4-flash-0731",
                StatusFmt.chatModelRow(healthy).value);
        T.eq("模型缺失是警告", 2,
                StatusFmt.chatModelRow(T.tweak(healthy, "models.chat_model", "")).tone);
        r = StatusFmt.visionRow(healthy);
        T.contains("识图显示模型名", r.value, "glm-4.6v");
        T.contains("标注直连便宜", r.value, "便宜快");
        r = StatusFmt.visionRow(T.tweak(healthy, "models.vision_provider",
                "vision-opus5"));
        T.contains("标注故障转移", r.value, "故障转移");

        T.group("StatusFmt：工具能力（最关键一行）");
        r = StatusFmt.toolRow(healthy);
        T.eq("正常是好色调", 1, r.tone);
        T.contains("说调了几次", r.value, "调了 7 次");
        // tool_use 丢失是「嘴上答应却不发图」的根因，必须最醒目
        Map<String, Object> noTool = T.tweak(healthy, "runtime.tool_use_ok",
                Boolean.FALSE);
        r = StatusFmt.toolRow(noTool);
        T.eq("缺 tool_use 标红", 3, r.tone);
        T.contains("点名 modalities", r.value, "modalities");
        T.contains("说明后果", r.value, "失效");
        // 配置对但没调用过 —— 可疑，不是正常
        Map<String, Object> idle = T.tweak(healthy, "tools.count", Long.valueOf(0));
        r = StatusFmt.toolRow(idle);
        T.eq("一小时没调用是警告", 2, r.tone);
        T.contains("说明没调用过", r.value, "没调用过");
        // 读不到日志（docker logs 失败）不该当成异常
        Map<String, Object> noLogs = T.deepCopy(healthy);
        noLogs.put("tools", null);
        T.eq("读不到调用记录仍是好色调", 1, StatusFmt.toolRow(noLogs).tone);

        T.group("StatusFmt：说话行为");
        r = StatusFmt.talkRow(healthy);
        T.contains("显示开启", r.value, "开启");
        T.contains("概率转百分比", r.value, "25%");
        r = StatusFmt.talkRow(T.tweak(healthy, "runtime.active_reply", Boolean.FALSE));
        T.contains("关闭时说清行为", r.value, "只在被叫到时回");
        T.eq("缺字段是 —", "—", StatusFmt.talkRow(T.map()).value);

        T.eq("限流原样显示", "30 条 / 60 秒", StatusFmt.limitRow(healthy).value);

        T.group("StatusFmt：上下文轮数");
        T.contains("正常显示轮数", StatusFmt.ctxRow(healthy).value, "40 轮");
        // -1 = 不截断，这是「答非所问」的第一病因
        r = StatusFmt.ctxRow(T.tweak(healthy, "runtime.context_turns",
                Long.valueOf(-1)));
        T.eq("不限标红", 3, r.tone);
        T.contains("点明后果", r.value, "答非所问");

        T.group("StatusFmt：模式");
        List<Object> modes = T.modesOf(T.schemaJson());
        r = StatusFmt.modeRow(healthy, modes);
        T.contains("显示中文名", r.value, "日常");
        T.contains("带 emoji", r.value, "🙂");
        r = StatusFmt.modeRow(T.tweak(healthy, "mode", ""), modes);
        T.eq("推不出模式显示自定义", "自定义", r.value);
        r = StatusFmt.modeRow(T.tweak(healthy, "mode", "unknown-id"), modes);
        T.eq("认不出的 id 原样显示", "unknown-id", r.value);

        T.group("StatusFmt：插件");
        r = StatusFmt.pluginRow(healthy);
        T.contains("列出关掉的", r.value, "入群欢迎");
        T.eq("有关掉的是警告", 2, r.tone);
        Map<String, Object> allOn = T.deepCopy(healthy);
        for (Object o : com.dafeiyu.console.Json.arr(allOn, "plugins")) {
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) o;
            m.put("enabled", Boolean.TRUE);
        }
        r = StatusFmt.pluginRow(allOn);
        T.contains("全开时只报数", r.value, "3 个全部启用");
        T.eq("全开是好色调", 1, r.tone);
        Map<String, Object> noPlugins = T.deepCopy(healthy);
        noPlugins.put("plugins", null);
        T.eq("读不到插件是警告", 2, StatusFmt.pluginRow(noPlugins).tone);
        Map<String, Object> emptyPlugins = T.deepCopy(healthy);
        emptyPlugins.put("plugins", T.list());
        T.eq("一个都没装是错误", 3, StatusFmt.pluginRow(emptyPlugins).tone);

        T.group("StatusFmt：空状态不崩");
        Map<String, Object> nothing = T.map();
        int made = 0;
        StatusFmt.Row[] all = {
                StatusFmt.qqRow(nothing), StatusFmt.memRow(nothing),
                StatusFmt.diskRow(nothing), StatusFmt.loadRow(nothing),
                StatusFmt.proxyRow(nothing), StatusFmt.chatModelRow(nothing),
                StatusFmt.visionRow(nothing), StatusFmt.toolRow(nothing),
                StatusFmt.talkRow(nothing), StatusFmt.limitRow(nothing),
                StatusFmt.ctxRow(nothing), StatusFmt.pluginRow(nothing),
                StatusFmt.modeRow(nothing, T.list()),
        };
        for (StatusFmt.Row one : all) {
            if (one != null && one.value != null && !one.value.isEmpty()) {
                made++;
            }
        }
        T.eq("全空数据下每行都有文字", all.length, made);
    }
}
