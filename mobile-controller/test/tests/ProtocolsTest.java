package tests;

import com.dafeiyu.controller.Protocols;

import java.util.List;

/**
 * 接口协议表的测试。
 *
 * 对应用户反馈：「API 需要更多的详细自定义请求体和协议选择」。
 *
 * 这里钉住的是**下拉框的取值映射**。它看起来很简单，但错一处的后果
 * 都很重，而且都不会有任何报错：
 *
 *   ① keyOfLabel 映射错 → 用户在界面上选的是 Anthropic，实际提交给
 *      服务器的却是 OpenAI 兼容。机器人不回话，用户完全查不出来。
 *   ② indexOfKey 认不出时**静默返回 0** → 用户打开面板看到默认项，
 *      一保存就把本来正确的协议改成了别的。他什么都没动过。
 *      所以这里必须钉住「认不出时 knows() 要为 false」，
 *      让界面有机会先警告他。
 *   ③ 两份协议表（App / 服务器）不一致 → 用户选到服务器不认的协议。
 *      （跨语言的一致性由 test/check-protocol-wiring.py 静态核对。）
 */
public final class ProtocolsTest {

    public static void run() {
        T.group("接口协议：下拉框取值映射");

        // ── 表本身要完整 ──────────────────────────────────────────────
        List<String> labels = Protocols.labels();
        T.isTrue("★ 协议表非空", labels.size() >= 4);
        T.eq("★ 显示名个数 = 选项个数",
                labels.size(), Protocols.options().size());

        // 第一个必须是 OpenAI 兼容 —— 绝大多数用户用的就是它，
        // 默认落在第一个能让他不用滚。
        T.eq("★ 第一项是 OpenAI 兼容（默认项）",
                Protocols.DEFAULT, Protocols.options().get(0).key);
        T.isTrue("★ 第一项的显示名要明确说「选这个就对了」",
                labels.get(0).contains("最常见"));

        // 每个选项的显示名都不能重复 —— 重复的话用户选哪个是随机的，
        // 而选错的后果是配置被写坏。
        // ★ 只在**真的发现重复**时才断言：写成「每一对都断言一次不相等」
        //   的话，14 个协议会打印 91 行一模一样的「不重复」，
        //   真正的失败信息会被淹掉 —— 那样测试报告就没人看了。
        java.util.Set<String> seen = new java.util.HashSet<String>();
        java.util.List<String> dup = new java.util.ArrayList<String>();
        for (String l : labels) {
            if (!seen.add(l)) {
                dup.add(l);
            }
        }
        T.eq("★ 显示名互不重复（重复会让用户选错协议）", "[]", dup.toString());

        // ── 显示名 → key（保存时走这条路）─────────────────────────────
        for (Protocols.Option o : Protocols.options()) {
            T.eq("★ 显示名能映射回自己的 key：" + o.key,
                    o.key, Protocols.keyOfLabel(o.label));
        }
        T.eq("★ 认不出的显示名回落到默认值（不能返回空串）",
                Protocols.DEFAULT, Protocols.keyOfLabel("这个协议不存在"));
        T.eq("★ null 显示名也回落到默认值",
                Protocols.DEFAULT, Protocols.keyOfLabel(null));

        // ── key → 下拉框位置（回填时走这条路）─────────────────────────
        for (int i = 0; i < Protocols.options().size(); i++) {
            Protocols.Option o = Protocols.options().get(i);
            T.eq("★ key 能定位到自己的位置：" + o.key,
                    i, Protocols.indexOfKey(o.key));
        }
        // 老实例没有这个字段 → 服务器给空串 → 必须落在默认项上
        T.eq("★ 空串落在默认项（老实例升级上来无感）",
                0, Protocols.indexOfKey(""));
        T.eq("★ null 落在默认项", 0, Protocols.indexOfKey(null));
        // 服务器比 App 新 → App 不认识 → 也落在默认项，
        // **但 knows() 必须为 false**，这样界面才能先警告用户
        // 「不要直接保存」。没有这个信号的话，用户一保存就把
        // 本来正确的协议改成了 OpenAI 兼容，而他什么都没动过。
        T.eq("认不出的 key 落在默认项",
                0, Protocols.indexOfKey("future_protocol_v2"));
        T.isFalse("★ 认不出的 key 必须 knows()=false（界面靠它给警告）",
                Protocols.knows("future_protocol_v2"));
        T.isTrue("★ 已知的 key knows()=true",
                Protocols.knows(Protocols.DEFAULT));
        T.isTrue("空串当成「认识」（那是「没配过」，不是「不认识」）",
                Protocols.knows(""));
        T.isTrue("null 当成「认识」", Protocols.knows(null));

        // ── 给用户看的名字 ────────────────────────────────────────────
        T.eq("★ 已知 key 返回显示名",
                Protocols.options().get(0).label,
                Protocols.labelOfKey(Protocols.DEFAULT));
        // 认不出时原样返回 key，而不是「未知」——
        // 把 key 显示出来，用户至少能把它抄给开发者；
        // 显示「未知」就什么线索都没有了。
        T.eq("★ 认不出的 key 原样返回（比显示「未知」有用）",
                "future_protocol_v2", Protocols.labelOfKey("future_protocol_v2"));
        T.eq("null 返回空串（不能崩）", "", Protocols.labelOfKey(null));

        T.isTrue("★ 有说明文字（用户不知道该选哪个时能照着读）",
                Protocols.hintOfKey(Protocols.DEFAULT).length() > 5);
        T.eq("认不出的 key 说明为空（不编造）",
                "", Protocols.hintOfKey("future_protocol_v2"));

        // ── 几个关键协议必须在表里 ────────────────────────────────────
        // 缺了它们，用这些服务的用户根本选不到正确的协议，
        // 只能一直用 OpenAI 兼容去撞，而撞不对时的报错会指向「地址写错了」。
        T.isTrue("★ 有 Anthropic 原生协议", Protocols.knows("anthropic_chat_completion"));
        T.isTrue("★ 有 Gemini 原生协议", Protocols.knows("googlegenai_chat_completion"));
        T.isTrue("★ 有 OpenAI Responses", Protocols.knows("openai_responses"));
        T.isTrue("★ 有智谱协议", Protocols.knows("zhipu_chat_completion"));

        // key 必须是不带空格的小写串 —— 它会被写进 AstrBot 配置当 type 用，
        // 里面混进大写或空格就是加载失败（报错只有一行 traceback）。
        for (Protocols.Option o : Protocols.options()) {
            T.isTrue("★ key 只含小写字母/数字/下划线：" + o.key,
                    o.key.matches("[a-z0-9_]+"));
            T.isTrue("★ key 以 _chat_completion 或已知后缀结尾：" + o.key,
                    o.key.endsWith("_chat_completion") || o.key.equals("openai_responses"));
        }

        // ── options() 不能被外部改坏 ──────────────────────────────────
        List<Protocols.Option> os = Protocols.options();
        int before = os.size();
        try {
            os.clear();
        } catch (UnsupportedOperationException e) {
            // 不可变是理想情况
        }
        T.eq("★ options() 返回的列表不能被改坏（否则下拉框会凭空少项）",
                before, Protocols.options().size());
    }
}
