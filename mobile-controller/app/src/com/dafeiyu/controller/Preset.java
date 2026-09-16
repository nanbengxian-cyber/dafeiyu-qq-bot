package com.dafeiyu.controller;

/**
 * 预设服务器 —— **公开版这里是空的**，定制版由构建脚本覆盖本文件生成。
 *
 * 为什么要做成「一个可被覆盖的文件」而不是配置文件：
 *   公开仓库 / 公开 Release 必须零真实信息（这是本项目的硬规矩）。
 *   定制版要「打开就能用」，就得把地址和加密 Token 编进包里。
 *   两者共用一个仓库，最干净的做法是：仓库里永远只有这份空模板，
 *   定制版在**本地构建时**被临时覆盖，构建完还原 —— 公开历史里从头到尾没有真实值。
 *
 * 注意：即使被覆盖，本文件里也**只有密文**（Token 用 PresetCrypto 加密），
 * 没有明文 Token。
 */
public final class Preset {

    private Preset() {
    }

    /** 是否带预设。空模板为 false —— 公开版的代码路径完全不碰预设。 */
    public static final boolean HAS_PRESET = false;

    /** 预设的 WebUI 地址（host:port）。空模板为空串。 */
    public static final String WEBUI_BASE = "";

    /** 给使用者看的提示（例如「解锁口令是构建时给你的那串」）。 */
    public static final String HINT = "";

    /** 加密后的 WebUI Token：salt / iv / 密文，都是 base64。空模板全为空串。 */
    public static final String TOKEN_SALT = "";
    public static final String TOKEN_IV = "";
    public static final String TOKEN_CT = "";
}
