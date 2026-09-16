package com.dafeiyu.controller;

/**
 * 预设服务器 —— **公开版这里是空的**，定制版由构建脚本覆盖本文件生成。
 *
 * 为什么要做成「一个可被覆盖的文件」而不是配置文件：
 *   公开仓库 / 公开 Release 必须零真实信息（这是本项目的硬规矩）。
 *   定制版要「打开就能用」，就得把地址和密钥编进包里。
 *   两者共用一个仓库，最干净的做法是：仓库里永远只有这份空模板，
 *   定制版在**本地构建时**被临时覆盖，构建完还原 —— 公开历史里从头到尾没有真实值。
 *
 * 定制版里装的是什么（对应「只填三个配置就能用」的目标）：
 *   · 服务器地址、SSH 端口、专用账号名；
 *   · 那个「只能转发到管理端口」的账号的 RSA 私钥；
 *   · 服务器主机指纹（防止中间人假冒服务器）；
 *   · 管理口令（管理服务的 Bearer token）。
 *
 * 安全边界要说清楚：定制版把这些编进包里，意味着**拿到这个 APK 的人
 * 就能管理服务器上的机器人实例**。所以定制版只发给信得过的人（比如内部群），
 * 不要公开分发。公开版不含任何预设，只能手填自己的服务器。
 *
 * 另一个注意点：密钥必须是 **RSA**，不能用 ed25519。
 * JSch 的 ed25519 实现要求 Java 15+，而 Android 上的 Ed25519 要 API 33+，
 * 用 ed25519 会一直报「Auth fail」，看起来像密钥错，其实是算法不可用。
 */
public final class Preset {

    private Preset() {
    }

    /** 是否带预设。空模板为 false —— 公开版的代码路径完全不碰预设。 */
    public static final boolean HAS_PRESET = false;

    /** 预设的服务器地址（定制版填）。空模板为空串。 */
    public static final String HOST = "";

    /** SSH 端口。空模板为 0。 */
    public static final int SSH_PORT = 0;

    /** 专用 SSH 账号（服务器上被限制成只能转发到管理端口）。 */
    public static final String SSH_USER = "";

    /** 专用账号的 RSA 私钥（PEM 文本）。 */
    public static final String SSH_KEY = "";

    /**
     * 服务器主机指纹（SHA256:… 形式）。
     * 一定要填 —— 不校验指纹的话，中间人可以假冒服务器，
     * 把隧道里的管理口令和 API Key 全拿走。
     */
    public static final String HOST_FINGERPRINT = "";

    /** 管理服务的口令（Bearer token）。 */
    public static final String MANAGER_TOKEN = "";

    /** 给使用者看的提示。空模板为空串。 */
    public static final String HINT = "";

    // ---- 以下为旧版「预设 WebUI 地址 + 加密 Token」保留字段 ----
    // 早期版本把生产服务器的 WebUI 地址和加密 Token 编进包里，
    // 让用户输口令解锁。现在改成「连自己的服务器」模式后不再使用，
    // 但保留字段以免破坏已有的构建脚本和测试。

    /** 预设的 WebUI 地址（host:port）。空模板为空串。 */
    public static final String WEBUI_BASE = "";

    /** 加密后的 WebUI Token：salt / iv / 密文，都是 base64。空模板全为空串。 */
    public static final String TOKEN_SALT = "";
    public static final String TOKEN_IV = "";
    public static final String TOKEN_CT = "";

    /** 预设是否完整可用（定制版才为 true）。 */
    public static boolean usable() {
        return HAS_PRESET && HOST.length() > 0 && SSH_PORT > 0
                && SSH_USER.length() > 0 && SSH_KEY.length() > 0;
    }
}
