package com.dafeiyu.controller;

import java.util.ArrayList;
import java.util.List;

/**
 * 主聊天 API 的「去哪儿申请」引导数据。
 *
 * 为什么要有这个类：用户反馈「有些人他不知道 API 怎么获取」。这是整条流程里
 * 唯一一个**必须在别的网站完成**的步骤 —— 其他步骤（填群号、写人格、点启动）
 * 都在 App 里，只有 API Key 要跑去服务商官网注册、充值、复制。
 * 不给出入口，新手就卡死在这一步。
 *
 * ── 关于 base_url：实测核实过 ──────────────────────────────────────────
 * 下面每个 base_url 都用**无效 Key 实测过 /models**，返回 401/403
 * 才算数（证明「地址真实存在」且「是 OpenAI 兼容鉴权端点」）。
 * 这是防止「AI 编造申请地址、用户白跑一趟」最硬的证伪手段。
 *
 * ── 关于模型名：**故意只当提示，不当答案** ─────────────────────────────
 * 模型名是这套东西里烂得最快的部分。实测教训：
 *   * deepseek-chat / deepseek-reasoner 已于 2026-07-24 官方停用
 *     （旧名直接报错），现在是 deepseek-flash / deepseek-v4-pro；
 *   * 通义千问、豆包、混元的模型名几个月就换一茬。
 * 所以：模型名只作为「大概是长这样」的示例，**真正的答案是让用户点
 * 「获取可用模型」**——服务器会拿他的地址和 Key 去问服务商要**当前**
 * 的列表，从里面选。这样这个 App 放半年也不会因为模型改名而失效。
 *
 * 刻意不碰 android.*：这样它能进单测（见 test/run-tests.sh）。
 */
public final class ApiGuide {

    private ApiGuide() {
    }

    /** 一个服务商的引导信息。 */
    public static final class Provider {
        /** 显示名（如「DeepSeek 深度求索」）。 */
        public final String name;
        /** 一句话说明（是否好上手、要不要实名）。 */
        public final String note;
        /** 申请 API Key 的页面。 */
        public final String keyUrl;
        /** OpenAI 兼容的接口地址，实测核实过，可直接填。 */
        public final String baseUrl;
        /** 模型名**示例**（会过时；真正该做的是点「获取可用模型」）。 */
        public final List<String> models;
        /** 额外提醒（可为空串）；有坑的地方必须写清楚。 */
        public final String warn;

        Provider(String name, String note, String keyUrl, String baseUrl,
                 String[] models, String warn) {
            this.name = name;
            this.note = note;
            this.keyUrl = keyUrl;
            this.baseUrl = baseUrl;
            this.models = new ArrayList<String>();
            for (String m : models) {
                this.models.add(m);
            }
            this.warn = warn == null ? "" : warn;
        }

        /** 示例模型（列表第一个）。 */
        public String firstModel() {
            return models.isEmpty() ? "" : models.get(0);
        }
    }

    /**
     * 收录的服务商。**国内优先**（用户主要在国内；境外 API 在国内服务器上
     * 往往直接超时，所以境外那几家必须带警告）。
     *
     * 顺序 = 推荐顺序：越靠前越省事。
     */
    public static List<Provider> providers() {
        List<Provider> out = new ArrayList<Provider>();

        out.add(new Provider(
                "DeepSeek 深度求索",
                "最推荐：便宜、注册简单、中文好",
                "https://platform.deepseek.com/api_keys",
                "https://api.deepseek.com/v1",
                new String[]{"deepseek-flash", "deepseek-v4-pro"},
                "模型名以官网为准（旧名 deepseek-chat 已于 2026-07-24 停用）。"
                        + "拿不准就点「获取可用模型」自动列出来。"));

        out.add(new Provider(
                "阿里云百炼（通义千问）",
                "阿里云账号登录，新用户有免费额度",
                "https://bailian.console.aliyun.com/",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
                new String[]{"qwen-plus", "qwen-turbo", "qwen-max"},
                "地址结尾的 compatible-mode 不能少，否则不是 OpenAI 兼容接口。"));

        out.add(new Provider(
                "智谱 AI（GLM）",
                "注册送额度，有便宜的 flash 模型",
                "https://open.bigmodel.cn/usercenter/apikeys",
                "https://open.bigmodel.cn/api/paas/v4",
                new String[]{"glm-4-flash", "glm-4-plus"},
                "地址结尾是 /v4 不是 /v1，照抄即可。"));

        out.add(new Provider(
                "月之暗面 Kimi",
                "长文本强，网页版和 API 同一账号",
                "https://platform.moonshot.cn/console/api-keys",
                "https://api.moonshot.cn/v1",
                new String[]{"moonshot-v1-8k", "moonshot-v1-32k"},
                ""));

        out.add(new Provider(
                "硅基流动 SiliconFlow",
                "聚合平台：一个 Key 用很多家的开源模型",
                "https://cloud.siliconflow.cn/account/ak",
                "https://api.siliconflow.cn/v1",
                new String[]{"deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-7B-Instruct"},
                "模型名带斜杠（如 deepseek-ai/DeepSeek-V3），"
                        + "要从它给的模型列表里复制。"));

        out.add(new Provider(
                "火山方舟（豆包）",
                "字节跳动，豆包模型",
                "https://console.volcengine.com/ark",
                "https://ark.cn-beijing.volces.com/api/v3",
                new String[]{"doubao-pro-32k"},
                "★ 两个坑：① 要先在控制台「开通模型服务」，只申请 Key 还不够；"
                        + "② 通常要「创建接入点」，把接入点 ID（形如 ep-2024xxxx-xxxxx）"
                        + "填到模型名那栏。嫌麻烦就换 DeepSeek。"));

        out.add(new Provider(
                "百度千帆（文心一言）",
                "百度账号登录",
                "https://console.bce.baidu.com/qianfan/overview",
                "https://qianfan.baidubce.com/v2",
                new String[]{"ernie-4.0-turbo-8k", "ernie-3.5-8k"},
                "要用「应用」里的 API Key（不是百度智能云的 Access Key）。"));

        out.add(new Provider(
                "OpenRouter（聚合，境外）",
                "一个 Key 用 GPT/Claude/Gemini 等",
                "https://openrouter.ai/settings/keys",
                "https://openrouter.ai/api/v1",
                new String[]{"openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"},
                "★ 境外服务：服务器在国内时可能连不上（超时）。"
                        + "填完先点「测试连接」确认服务器能通。"));

        return out;
    }

    /**
     * 某个服务商的**手把手**教程。
     *
     * 用户原话：「还没有各大官网的API获取地址，那新手不知道在哪里获取怎么办？
     * 加，并且加上对应的教程」。
     *
     * 关键在「教程」两个字：光给一堆网址，新手到了官网还是不知道点哪。
     * 所以这里逐步写清楚「在哪个页面、点哪个按钮、看到什么、复制什么」，
     * 并把他会踩的坑（实名、充值、只显示一次）提前说出来。
     *
     * 拿不准的地方**不编**：比如各家界面改版频繁，具体按钮文案可能变，
     * 所以描述的是「找什么样的东西」而不是「点第 3 个按钮」。
     * 宁可说得稳一点，也不要让他照着一份过期的截图找不到北。
     */
    public static String tutorial(String providerName) {
        Provider p = byName(providerName);
        if (p == null) {
            return genericTutorial();
        }
        return "【第 1 步】点下面的按钮打开官网，用手机号注册/登录。\n\n"
                + "【第 2 步】实名认证。国内这几家基本都要，一般要身份证。\n"
                + "　　（没实名的话建 Key 那一步会被拦住。）\n\n"
                + "【第 3 步】充值。多数 ¥10 起，够聊很久了 —— "
                + "先充最少的一档试，觉得好用再加。\n\n"
                + "【第 4 步】进「API Keys」/「密钥管理」页面，"
                + "点「创建新的 API Key」。\n"
                + "　　申请地址直接给你：\n　　" + p.keyUrl + "\n\n"
                + "【第 5 步】★ 那串 Key **只显示一次**，"
                + "立刻复制下来（长按选中→复制），关掉页面就再也看不到了。\n"
                + "　　它长这样：sk- 开头的一长串。\n\n"
                + "【第 6 步】回到这个 App，在机器人的配置里粘到「API Key」那栏。\n"
                + "　　接口地址填这个（已经帮你填好了）：\n　　" + p.baseUrl + "\n\n"
                + "【第 7 步】点「测试连接」。显示通了就成功了；"
                + "不通它会告诉你卡在哪一步（地址错、Key 错、还是服务器连不上）。\n\n"
                + "【第 8 步】点「获取可用模型」，从列表里挑一个 —— "
                + "不用自己记模型名，那个最容易写错。";
    }

    /** 没有指定服务商时的通用教程。 */
    public static String genericTutorial() {
        return "【第 1 步】从下面挑一家（推荐 DeepSeek），点「去申请」打开官网。\n\n"
                + "【第 2 步】注册 → 实名认证 → 充值（多数 ¥10 起）。\n\n"
                + "【第 3 步】在「API Keys」页面创建一个 Key。\n\n"
                + "【第 4 步】★ 那串 Key **只显示一次**，立刻复制下来。\n\n"
                + "【第 5 步】回到这里，填「接口地址」和 Key，点「测试连接」。\n\n"
                + "【第 6 步】点「获取可用模型」，从列表里挑一个。";
    }

    /** 引导页开头的一段话（告诉用户这一步要干什么、大概花多少钱）。 */
    public static String intro() {
        return "「API」是让机器人会说话的大脑。它不在本机，要去服务商官网申请一串"
                + "「API Key」（类似密码），填进来机器人才能回话。\n\n"
                + "四步：\n"
                + "① 从下面挑一家（推荐 DeepSeek，最便宜也最简单），点「去申请」；\n"
                + "② 在官网注册 → 实名 → 充值（多数 ¥10 起）→ 创建 API Key；\n"
                + "③ 复制那串 sk- 开头的字符（只显示一次，先复制好再关页面）；\n"
                + "④ 回到这里填「接口地址」和 Key，点「测试连接」——通不通当场就知道。\n\n"
                + "模型名不用记：填完地址和 Key 后点「获取可用模型」，"
                + "它会从服务商那儿把**当前**能用的名字列出来给你选。";
    }

    /**
     * 关于「Key 放在手机上安不安全」的如实说明。
     *
     * 阿里云官方文档明确警告「请勿在客户端代码（如移动应用）或不可信环境中
     * 配置或使用长期有效的 API Key」—— 我们确实是在手机 App 里让用户填 Key，
     * 所以必须如实告诉他这件事，并说清我们做了什么来降低风险。
     * 隐瞒这个风险比风险本身更糟：他有权知道，然后自己决定用哪个 Key。
     */
    public static String securityNote() {
        return "关于安全（如实说）：\n"
                + "• 你填的 Key 不会存在手机上，只在点「保存」时经加密隧道传到"
                + "你自己的服务器，写进机器人配置（文件权限 600，仅 root 可读）。\n"
                + "• 但云服务商官方提醒过：不要在手机 App 里用「长期有效」的 Key。"
                + "建议你专门为这个机器人建一个 Key，万一泄露，"
                + "去官网把它删掉就行，不影响你别的服务。\n"
                + "• 也建议给这个 Key 设个消费上限（各家控制台都有），防止被刷。";
    }

    /** 按名字找服务商；找不到返回 null。 */
    public static Provider byName(String name) {
        if (name == null) {
            return null;
        }
        for (Provider p : providers()) {
            if (p.name.equals(name)) {
                return p;
            }
        }
        return null;
    }
}
