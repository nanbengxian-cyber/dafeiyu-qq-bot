package tests;

import com.dafeiyu.console.KnobModel;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * KnobModel 的测试。
 *
 * 主线是**算术**：滑块只认整数格，实值和格数要来回换。这里错了的表现是
 * 「概率 0.25 拖出来变成 0.24」「滑块拖不到最大值」「提交的值带一串浮点噪声」——
 * 都是肉眼极难发现、但会真写进服务器配置文件的错。
 */
public final class KnobModelTest {

    public static void run() {
        KnobModel.Schema s = T.schema();

        T.group("KnobModel：解析 schema");
        T.eq("认得的旋钮全在", 5, s.knobs.size());
        T.eq("不认识的类型被跳过", null, s.knob("future.knob"));
        T.eq("模式数", 3, s.modes.size());
        T.eq("动作数", 2, s.actions.size());
        T.eq("聊天模型数", 3, s.chatModels.size());
        T.eq("识图渠道数", 2, s.visionProviders.size());
        T.eq("识图渠道名", "glm-4.6v", s.visionProviders.get("zhipu-vision"));
        T.eq("插件中文名", "出图", s.pluginLabels.get("dsh-imagegen"));

        T.group("KnobModel：分组");
        List<String> groups = s.groups();
        T.eq("组数", 3, groups.size());
        T.eq("首组按服务端顺序", "说话风格", groups.get(0));
        T.eq("说话风格里 2 项", 2, s.inGroup("说话风格").size());
        T.eq("记忆里 2 项", 2, s.inGroup("记忆").size());

        T.group("KnobModel：类型判定");
        KnobModel.Knob enable = s.knob("provider_ltm_settings.active_reply.enable");
        KnobModel.Knob prob = s.knob(
                "provider_ltm_settings.active_reply.possibility_reply");
        KnobModel.Knob turns = s.knob("provider_settings.max_context_length");
        KnobModel.Knob persona = s.knob("provider_settings.default_personality");
        KnobModel.Knob rate = s.knob("platform_settings.rate_limit.count");
        T.isTrue("bool", enable.isBool());
        T.isTrue("float 算数值", prob.isNumber());
        T.isTrue("int 算数值", turns.isNumber());
        T.isTrue("enum", persona.isEnum());
        T.isFalse("enum 不算数值", persona.isNumber());
        T.isTrue("hot 默认为真", enable.hot);
        T.isFalse("显式 hot=false 要保留", rate.hot);

        T.group("KnobModel：显示");
        T.eq("bool 开", "开", KnobModel.show(enable, Boolean.TRUE));
        T.eq("bool 关", "关", KnobModel.show(enable, Boolean.FALSE));
        T.eq("float 两位", "0.25", KnobModel.show(prob, Double.valueOf(0.25)));
        T.eq("float 去尾零", "0.3", KnobModel.show(prob, Double.valueOf(0.3)));
        T.eq("float 整数不带小数", "1", KnobModel.show(prob, Double.valueOf(1.0)));
        T.eq("int 带单位", "40 轮", KnobModel.show(turns, Long.valueOf(40)));
        T.eq("int 收到 double 也当整数", "40 轮",
                KnobModel.show(turns, Double.valueOf(40.0)));
        T.eq("enum 原样", "大肥鱼", KnobModel.show(persona, "大肥鱼"));
        T.eq("null 显示破折号", "—", KnobModel.show(prob, null));

        T.group("KnobModel：滑块格数");
        T.eq("float 0~1 步 0.05 共 21 格", 21, KnobModel.ticks(prob));
        T.eq("int 10~100 步 5 共 19 格", 19, KnobModel.ticks(turns));
        T.eq("没给步长的 int 按 1", 60, KnobModel.ticks(rate));

        T.group("KnobModel：实值 ↔ 格数");
        T.eq("0.25 在第 5 格", 5, KnobModel.toTick(prob, Double.valueOf(0.25)));
        T.eq("第 5 格回 0.25", Double.valueOf(0.25),
                KnobModel.fromTick(prob, 5));
        T.eq("0 在第 0 格", 0, KnobModel.toTick(prob, Double.valueOf(0)));
        T.eq("1.0 在末格", 20, KnobModel.toTick(prob, Double.valueOf(1.0)));
        T.eq("末格能拖到 1.0", Double.valueOf(1.0), KnobModel.fromTick(prob, 20));
        T.eq("40 轮在第 6 格", 6, KnobModel.toTick(turns, Long.valueOf(40)));
        T.eq("第 6 格回 40（整数型）", Long.valueOf(40),
                KnobModel.fromTick(turns, 6));

        T.group("KnobModel：浮点噪声必须被压掉");
        // 0.05 累加 5 次原生会得到 0.25000000000000006，
        // 那串东西会被写进服务器的 cmd_config.json 里。
        for (int i = 0; i <= 20; i++) {
            Object v = KnobModel.fromTick(prob, i);
            String text = String.valueOf(v);
            if (text.length() > 5) {
                T.bad("第 " + i + " 格出现浮点噪声", text);
                break;
            }
            if (i == 20) {
                T.ok("21 格全部无浮点噪声");
            }
        }
        T.eq("往返稳定（0.35）", Double.valueOf(0.35),
                KnobModel.fromTick(prob, KnobModel.toTick(prob,
                        Double.valueOf(0.35))));
        T.eq("往返稳定（0.85）", Double.valueOf(0.85),
                KnobModel.fromTick(prob, KnobModel.toTick(prob,
                        Double.valueOf(0.85))));

        T.group("KnobModel：越界值钳到两端");
        // 服务端配置可能被手改成范围外的值，界面不该崩也不该拖不动
        T.eq("超上限钳到末格", 20, KnobModel.toTick(prob, Double.valueOf(5.0)));
        T.eq("低于下限钳到 0", 0, KnobModel.toTick(prob, Double.valueOf(-3.0)));
        T.eq("负值格钳到下限", Double.valueOf(0.0), KnobModel.fromTick(prob, -5));
        T.eq("超大格钳到上限", Double.valueOf(1.0), KnobModel.fromTick(prob, 999));
        T.eq("int 越界也钳", Long.valueOf(100), KnobModel.fromTick(turns, 999));

        T.group("KnobModel：小数位数推断");
        T.eq("0.05 → 2 位", 2, KnobModel.decimals(0.05));
        T.eq("0.1 → 1 位", 1, KnobModel.decimals(0.1));
        T.eq("1 → 0 位", 0, KnobModel.decimals(1.0));
        T.eq("0.001 → 3 位", 3, KnobModel.decimals(0.001));

        T.group("KnobModel：校验");
        T.eq("合法 bool", null, KnobModel.validate(enable, Boolean.TRUE));
        T.eq("合法 float", null, KnobModel.validate(prob, Double.valueOf(0.5)));
        T.eq("合法 int", null, KnobModel.validate(turns, Long.valueOf(40)));
        T.eq("合法 enum", null, KnobModel.validate(persona, "大肥鱼"));
        T.eq("边界下限合法", null, KnobModel.validate(prob, Double.valueOf(0)));
        T.eq("边界上限合法", null, KnobModel.validate(prob, Double.valueOf(1)));
        T.contains("超上限被拒", KnobModel.validate(prob, Double.valueOf(1.5)),
                "不能大于");
        T.contains("低于下限被拒", KnobModel.validate(turns, Long.valueOf(3)),
                "不能小于");
        T.contains("int 收到小数被拒", KnobModel.validate(turns, Double.valueOf(40.5)),
                "整数");
        T.contains("bool 收到数字被拒", KnobModel.validate(enable, Long.valueOf(1)),
                "开或关");
        T.contains("数值收到文字被拒", KnobModel.validate(prob, "很多"), "数字");
        T.contains("enum 越界被拒", KnobModel.validate(persona, "不存在的人格"),
                "只能是");
        T.contains("认不出的旋钮被拒", KnobModel.validate(null, "x"), "不认");

        T.group("KnobModel：规范化");
        T.eq("bool 归一", Boolean.TRUE, KnobModel.normalize(enable, "true"));
        T.eq("int 归一为 Long", Long.valueOf(40),
                KnobModel.normalize(turns, Double.valueOf(40.0)));
        T.eq("int 四舍五入", Long.valueOf(41),
                KnobModel.normalize(turns, Double.valueOf(40.7)));
        T.eq("float 按步长精度", Double.valueOf(0.35),
                KnobModel.normalize(prob, Double.valueOf(0.35000000000000003)));
        T.eq("enum 转字符串", "默认", KnobModel.normalize(persona, "默认"));

        T.group("KnobModel：相等判断");
        T.isTrue("1 和 1.0 相等", KnobModel.same(turns, Long.valueOf(1),
                Double.valueOf(1.0)));
        T.isTrue("0.25 和 0.25 相等", KnobModel.same(prob, Double.valueOf(0.25),
                Double.valueOf(0.25)));
        T.isFalse("0.25 和 0.3 不等", KnobModel.same(prob, Double.valueOf(0.25),
                Double.valueOf(0.3)));
        T.isTrue("true 和 true 相等", KnobModel.same(enable, Boolean.TRUE,
                Boolean.TRUE));
        T.isFalse("null 和值不等", KnobModel.same(prob, null, Double.valueOf(0.1)));
        T.isTrue("两个 null 相等", KnobModel.same(prob, null, null));

        T.group("KnobModel：只提交真改动的项");
        Map<String, Object> current = new LinkedHashMap<String, Object>();
        current.put("provider_ltm_settings.active_reply.enable", Boolean.TRUE);
        current.put("provider_ltm_settings.active_reply.possibility_reply",
                Double.valueOf(0.25));
        current.put("provider_settings.max_context_length", Long.valueOf(40));
        current.put("provider_settings.default_personality", "大肥鱼");

        Map<String, Object> edited = new LinkedHashMap<String, Object>();
        // 拖了滑块又拖回原位 —— 不该提交
        edited.put("provider_ltm_settings.active_reply.possibility_reply",
                Double.valueOf(0.25));
        // 真改了
        edited.put("provider_settings.max_context_length", Long.valueOf(60));
        Map<String, Object> diff = KnobModel.diff(s, current, edited);
        T.eq("只有一项", 1, diff.size());
        T.eq("就是真改的那项", Long.valueOf(60),
                diff.get("provider_settings.max_context_length"));
        T.isFalse("拖回原位的不提交",
                diff.containsKey(
                        "provider_ltm_settings.active_reply.possibility_reply"));

        // 类型不同但值相同（Long 40 vs Double 40.0）不该算改动
        Map<String, Object> sameVal = new LinkedHashMap<String, Object>();
        sameVal.put("provider_settings.max_context_length", Double.valueOf(40.0));
        T.eq("Long/Double 同值不算改动", 0,
                KnobModel.diff(s, current, sameVal).size());

        // 认不出的路径要丢掉，别往服务端塞它不认的键
        Map<String, Object> bogus = new LinkedHashMap<String, Object>();
        bogus.put("dashboard.password", "偷改密码");
        T.eq("白名单外的路径被丢弃", 0, KnobModel.diff(s, current, bogus).size());

        T.group("KnobModel：需要重启的改动要能识别");
        Map<String, Object> hotOnly = new LinkedHashMap<String, Object>();
        hotOnly.put("provider_settings.max_context_length", Long.valueOf(60));
        T.isFalse("只改热更新项不用重启", KnobModel.needsRestart(s, hotOnly));
        Map<String, Object> coldOne = new LinkedHashMap<String, Object>();
        coldOne.put("platform_settings.rate_limit.count", Long.valueOf(20));
        T.isTrue("改了 hot=false 的项要重启", KnobModel.needsRestart(s, coldOne));

        T.group("KnobModel：真值判断");
        T.isTrue("Boolean true", KnobModel.truthy(Boolean.TRUE));
        T.isTrue("字符串 true", KnobModel.truthy("true"));
        T.isTrue("字符串 1", KnobModel.truthy("1"));
        T.isTrue("中文开", KnobModel.truthy("开"));
        T.isTrue("非零数字", KnobModel.truthy(Long.valueOf(3)));
        T.isFalse("零", KnobModel.truthy(Long.valueOf(0)));
        T.isFalse("Boolean false", KnobModel.truthy(Boolean.FALSE));
        T.isFalse("空串", KnobModel.truthy(""));
        T.isFalse("null", KnobModel.truthy(null));

        T.group("KnobModel：容错解析");
        Map<String, Object> broken = T.map(
                "knobs", T.list(
                        T.map("name", "没有 path"),
                        T.map("path", "a.b"),
                        T.map("path", "c.d", "type", "bool")),
                "modes", T.list(T.map("name", "没有 id")),
                "actions", T.list(T.map("id", "ok", "name", "行")),
                "chat_models", "不是数组");
        KnobModel.Schema bs = KnobModel.parse(broken);
        T.eq("缺 path/type 的被跳过", 1, bs.knobs.size());
        T.eq("留下的是完整那个", "c.d", bs.knobs.get(0).path);
        T.eq("缺 id 的模式被跳过", 0, bs.modes.size());
        T.eq("完整的动作留下", 1, bs.actions.size());
        T.eq("模型列表类型不符时是空表", 0, bs.chatModels.size());
        T.eq("完全空的 schema 不崩", 0, KnobModel.parse(T.map()).knobs.size());

        T.group("KnobModel：没给 min/max 时的兜底");
        KnobModel.Schema noRange = KnobModel.parse(T.map("knobs", T.list(
                T.map("path", "x.f", "name", "浮点", "type", "float"),
                T.map("path", "x.i", "name", "整数", "type", "int"))));
        KnobModel.Knob f = noRange.knob("x.f");
        KnobModel.Knob i = noRange.knob("x.i");
        T.near("float 默认下限 0", 0, KnobModel.minOf(f));
        T.near("float 默认上限 1", 1, KnobModel.maxOf(f));
        T.near("float 默认步长 0.05", 0.05, KnobModel.stepOf(f));
        T.near("int 默认上限 100", 100, KnobModel.maxOf(i));
        T.near("int 默认步长 1", 1, KnobModel.stepOf(i));
        T.eq("没范围也能算格数", 21, KnobModel.ticks(f));
    }
}
