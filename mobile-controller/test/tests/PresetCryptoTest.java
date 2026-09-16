package tests;

import com.dafeiyu.controller.Preset;
import com.dafeiyu.controller.PresetCrypto;

import java.util.Base64;

/**
 * 预设加密测试。
 *
 * 两条主线：
 *   1. 加解密本身对（含「口令错必须失败」「密文被改必须失败」这类反向验证）；
 *   2. **仓库里的 Preset 必须是空模板** —— 这是防止有人把真实地址/密文提交上去的
 *      最后一道闸。公开仓库零真实信息是这个项目的硬规矩，用测试钉死它。
 */
final class PresetCryptoTest {

    private PresetCryptoTest() {
    }

    static void run() {
        try {
            roundTrip();
            wrongPassphraseFails();
            tamperedCiphertextFails();
            uniqueSaltAndIv();
            emptyPassphraseRejected();
            badBase64Rejected();
            templateIsEmpty();
            noPlaintextInTemplate();
        } catch (Exception e) {
            T.bad("预设加密测试抛异常", String.valueOf(e));
        }
    }

    /** 加密 → 解密要还原出原文（含中文与特殊字符）。 */
    private static void roundTrip() throws Exception {
        String secret = "napcat-token-测试_!@#$%^&*()_+ 空格";
        String[] enc = PresetCrypto.encrypt("correct horse battery staple", secret);
        T.eq("密文是三段（salt/iv/ct）", 3, enc.length);
        String back = PresetCrypto.decrypt("correct horse battery staple", enc[0], enc[1], enc[2]);
        T.eq("解密还原原文", secret, back);
    }

    /** 口令错必须失败，且失败信息不能泄露「口令对不对」这种可爆破的信息。 */
    private static void wrongPassphraseFails() throws Exception {
        String[] enc = PresetCrypto.encrypt("right-passphrase", "secret-token");
        try {
            PresetCrypto.decrypt("wrong-passphrase", enc[0], enc[1], enc[2]);
            T.bad("口令错时必须抛 PresetException", "却成功解密了");
        } catch (PresetCrypto.PresetException e) {
            T.contains("口令错给人话提示", e.getMessage(), "口令不对");
        }
    }

    /** 密文被改一个字节就必须解密失败（GCM 认证标签的作用），不能静默出垃圾。 */
    private static void tamperedCiphertextFails() throws Exception {
        String[] enc = PresetCrypto.encrypt("pass", "another-secret-value");
        byte[] raw = Base64.getDecoder().decode(enc[2]);
        raw[0] ^= 0x01;                       // 翻转一位
        String bad = Base64.getEncoder().encodeToString(raw);
        try {
            PresetCrypto.decrypt("pass", enc[0], enc[1], bad);
            T.bad("密文被改必须解密失败", "却解密成功了");
        } catch (PresetCrypto.PresetException e) {
            T.ok("密文被改后解密失败（GCM 认证生效）");
        }
    }

    /** 每次加密的 salt/iv 必须不同（否则相同 Token 会产生相同密文，泄露「两个包用了同一个 Token」）。 */
    private static void uniqueSaltAndIv() throws Exception {
        String[] a = PresetCrypto.encrypt("p", "same-plaintext");
        String[] b = PresetCrypto.encrypt("p", "same-plaintext");
        T.isTrue("两次加密 salt 不同", !a[0].equals(b[0]));
        T.isTrue("两次加密 iv 不同", !a[1].equals(b[1]));
        T.isTrue("两次加密密文不同（同一明文）", !a[2].equals(b[2]));
        T.eq("但都能解回同一明文", "same-plaintext",
                PresetCrypto.decrypt("p", b[0], b[1], b[2]));
    }

    /** 空口令直接拒绝，不进入 PBKDF2（省得白算 20 万轮）。 */
    private static void emptyPassphraseRejected() throws Exception {
        String[] enc = PresetCrypto.encrypt("p", "x");
        try {
            PresetCrypto.decrypt("", enc[0], enc[1], enc[2]);
            T.bad("空口令必须拒绝", "却接受了");
        } catch (PresetCrypto.PresetException e) {
            T.contains("空口令提示填写", e.getMessage(), "请填写解锁口令");
        }
    }

    /** base64 坏掉给「格式不对」，不是崩栈。 */
    private static void badBase64Rejected() {
        try {
            PresetCrypto.decrypt("p", "!!!not-base64!!!", "also-bad", "bad");
            T.bad("坏 base64 必须拒绝", "却接受了");
        } catch (PresetCrypto.PresetException e) {
            T.contains("坏 base64 给人话", e.getMessage(), "格式不对");
        }
    }

    /**
     * 仓库里的 Preset 必须是空模板。
     * 这条测试的价值：万一有人（或某次构建脚本忘了还原）把真实地址/密文提交上来，
     * 测试立刻红 —— 而不是等推上公开仓库后才发现。
     */
    private static void templateIsEmpty() {
        T.isFalse("Preset.HAS_PRESET 为 false（仓库里是空模板）", Preset.HAS_PRESET);
        // 新版字段：服务器地址、账号、密钥、指纹、口令都必须为空。
        // 任何一项非空都说明有人（或忘了还原的构建）把真实值提交上来了。
        T.eq("Preset.HOST 为空", "", Preset.HOST);
        T.eq("Preset.SSH_PORT 为 0", 0, Preset.SSH_PORT);
        T.eq("Preset.SSH_USER 为空", "", Preset.SSH_USER);
        T.eq("Preset.SSH_KEY 为空", "", Preset.SSH_KEY);
        T.eq("Preset.HOST_FINGERPRINT 为空", "", Preset.HOST_FINGERPRINT);
        T.eq("Preset.MANAGER_TOKEN 为空", "", Preset.MANAGER_TOKEN);
        T.isFalse("空模板 usable() 为 false（没配好就不该声称可用）", Preset.usable());
    }

    /**
     * 空模板里不许出现像 IP / 域名 / 密钥的东西。
     *
     * 注意不能拿 HINT 一起检查 —— 提示文案里有中文句号「。」，
     * 会被「不含点号」那条误判。上一版就是因为把 HINT 拼进来才必须
     * 让 HINT 也保持为空；现在 HINT 是有意义的文案，所以只查真值字段。
     */
    private static void noPlaintextInTemplate() {
        String all = Preset.HOST + Preset.SSH_USER + Preset.SSH_KEY
                + Preset.HOST_FINGERPRINT + Preset.MANAGER_TOKEN;
        T.notContains("空模板不含点号（IP/域名特征）", all, ".");
        T.notContains("空模板不含 http", all, "http");
        T.notContains("空模板不含私钥标记", all, "PRIVATE KEY");
        T.isTrue("空模板的真值字段整体为空", all.isEmpty());
    }
}
