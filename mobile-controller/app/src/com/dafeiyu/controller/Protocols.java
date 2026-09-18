package com.dafeiyu.controller;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 接口协议表 —— 这是**服务端 PROTOCOLS 的镜像**。
 *
 * 为什么 App 也要有一份、而不是全靠服务器下发：
 *   ① 下拉框要在**打开配置面板的瞬间**就画出来，不能等一次网络往返
 *      （面板卡住不动、用户以为 App 死了）；
 *   ② 用户可能还没连上服务器就要先看有哪些选项。
 *
 * ★ 两份必须保持一致。不一致的后果很隐蔽：
 *   App 下拉里有、服务器不认 → 用户选了却被服务器报「不认识的协议」；
 *   服务器有、App 下拉里没有 → 用户根本选不到那个协议。
 *   所以这里每一项都对应 AstrBot 里真实存在的适配器
 *   （由 register_provider_adapter 注册，查不到就加载失败）。
 *
 * 表里的 key 就是写进 AstrBot 配置 provider_sources[].type 的值 ——
 * 那个字段是 AstrBot 查适配器的键（provider/manager.py:700），
 * 写错就是加载失败，而报错只有一行 traceback，界面上完全看不出来。
 * 所以用户永远不该手打它，只能从这里选。
 */
public final class Protocols {

    /** 一个协议选项：机器用的 key + 给人看的名字 + 一句说明。 */
    public static final class Option {
        public final String key;
        public final String label;
        public final String hint;

        Option(String key, String label, String hint) {
            this.key = key;
            this.label = label;
            this.hint = hint;
        }
    }

    /** 默认协议：老实例没有这个字段时就是它。 */
    public static final String DEFAULT = "openai_chat_completion";

    private static final Map<String, Option> TABLE = new LinkedHashMap<String, Option>();

    private static void add(String key, String label, String hint) {
        TABLE.put(key, new Option(key, label, hint));
    }

    static {
        // 顺序 = 下拉框里的顺序。把最常用的放最前面 ——
        // 绝大多数用户（和中转站）用的都是第一个，让他不用滚。
        add("openai_chat_completion",
                "OpenAI 兼容（最常见，选这个就对了）",
                "绝大多数中转站、DeepSeek、Kimi、智谱、通义都用这个。");
        add("anthropic_chat_completion",
                "Anthropic 原生（Claude 官方 / Claude 中转）",
                "接口地址填到域名即可，程序会自己补 /v1/messages。");
        add("googlegenai_chat_completion",
                "Google Gemini 原生",
                "Gemini 官方接口。国内直连不通，需要能出国的服务器。");
        add("openai_responses",
                "OpenAI Responses（官方新接口）",
                "只有官方 /v1/responses 才用这个。填成聊天接口会 400。");
        add("zhipu_chat_completion", "智谱 GLM", "智谱官方接口，走 OpenAI 兼容格式。");
        add("groq_chat_completion", "Groq", "Groq 官方接口，速度很快。");
        add("openrouter_chat_completion", "OpenRouter",
                "OpenRouter 官方接口，一个 Key 用很多家模型。");
        add("xai_chat_completion", "xAI Grok", "xAI 官方接口。");
        add("xiaomi_chat_completion", "小米 MiMo", "小米官方接口。");
        add("kimi_code_chat_completion", "Kimi Code（Anthropic 格式）",
                "Kimi 的 Anthropic 兼容端点。");
        add("longcat_chat_completion", "美团 LongCat", "LongCat 官方接口。");
        add("aihubmix_chat_completion", "AiHubMix", "AiHubMix 中转站。");
        // ★ 这里原来有一项 MiraRouter，**已删除**（2026-09-18）：
        //   生产 AstrBot 里根本没有 mirarouter 这个适配器（全库 grep 零命中），
        //   它是从一份过时的清单里抄来的。留着它 = 给用户一个「选了就坏」的
        //   选项：AstrBot 加载时找不到适配器会失败，而 App 上显示的是
        //   「保存成功」，用户完全查不出机器人为什么不回话。
        //   这条现在由 test/check-protocol-adapters.py 对着真实源码钉住。
        add("ssycloud_chat_completion", "胜算云", "胜算云接口。");
    }

    private Protocols() {
    }

    /** 下拉框用的显示名（按固定顺序）。 */
    public static List<String> labels() {
        List<String> out = new ArrayList<String>();
        for (Option o : TABLE.values()) {
            out.add(o.label);
        }
        return out;
    }

    public static List<Option> options() {
        return Collections.unmodifiableList(new ArrayList<Option>(TABLE.values()));
    }

    /** 显示名 → 机器用的 key。找不到就返回默认值。 */
    public static String keyOfLabel(String label) {
        for (Option o : TABLE.values()) {
            if (o.label.equals(label)) {
                return o.key;
            }
        }
        return DEFAULT;
    }

    /** 机器用的 key → 下拉框里的位置。找不到返回 0（默认项）。 */
    public static int indexOfKey(String key) {
        if (key == null || key.isEmpty()) {
            return 0;
        }
        int i = 0;
        for (Option o : TABLE.values()) {
            if (o.key.equals(key)) {
                return i;
            }
            i++;
        }
        // ★ 认不出来时回落到默认项，而不是抛异常或留空。
        //   认不出来的真实原因通常是：服务器比 App 新，加了新协议。
        //   这时让用户停在默认项上、并且下面会显示一行提示，
        //   比直接崩掉或静默改成别的协议都安全 ——
        //   静默改成别的协议会把一个本来好的配置改坏。
        return 0;
    }

    /** 这个 key 认不认识。用于「服务器给的协议 App 不认识」时给用户提示。 */
    public static boolean knows(String key) {
        return key == null || key.isEmpty() || TABLE.containsKey(key);
    }

    /** 给用户看的名字。认不出来就原样返回 key（比显示「未知」有用）。 */
    public static String labelOfKey(String key) {
        Option o = TABLE.get(key);
        return o != null ? o.label : (key == null ? "" : key);
    }

    public static String hintOfKey(String key) {
        Option o = TABLE.get(key);
        return o != null ? o.hint : "";
    }
}
