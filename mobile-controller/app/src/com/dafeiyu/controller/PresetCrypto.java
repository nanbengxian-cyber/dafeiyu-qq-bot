package com.dafeiyu.controller;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.SecureRandom;
import java.util.Base64;

import javax.crypto.Cipher;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.PBEKeySpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * 定制版预设的加解密 —— 只服务一件事：让「私有定制版 APK」里能带一个
 * 加密过的 WebUI Token，而**公开版什么也不带**。
 *
 * 为什么不是「直接把 Token 写进代码」：
 *   APK 是可以反编译的（dex → smali 是常识工具链）。写死的 Token 等于公开的 Token。
 *   所以这里用真加密：Token 以 AES-256-GCM 密文形式存在 APK 里，
 *   密钥由使用者自己的一次性口令经 PBKDF2-HMAC-SHA256 派生（高迭代次数抗暴力）。
 *   拿不到口令，反编译也只能看到一段解不开的密文。
 *
 * 诚实边界（写在文档里，不假装更强）：
 *   * 口令太弱（如 6 位纯数字）时，离线暴力破解仍可行 —— 迭代次数只是抬高成本，
 *     不是数学上的不可能。口令请用长一点的随机串。
 *   * 解出来的 Token 只在内存里，和手填 Token 走同一条路（不落盘、不进日志）。
 *   * 这个类**不参与**公开版的任何行为：公开版没有预设，压根不会调用它。
 *
 * 算法选型：
 *   * AES/GCM/NoPadding —— 带认证的加密，密文被改动会直接解密失败（不会静默出垃圾）；
 *   * PBKDF2WithHmacSHA256 —— API 26+ 与 JDK 8+ 都支持（本工程 minSdk 26）；
 *   * 每个预设独立随机 salt(16B) + iv(12B)，GCM 的 12 字节 IV 是标准长度。
 */
public final class PresetCrypto {

    private PresetCrypto() {
    }

    /** PBKDF2 迭代次数。20 万次在手机上约几百毫秒，可以接受；暴力破解成本被抬高很多。 */
    public static final int ITERATIONS = 200000;
    private static final int KEY_BITS = 256;
    private static final int TAG_BITS = 128;
    private static final int SALT_BYTES = 16;
    private static final int IV_BYTES = 12;

    /** 解密失败（口令错 / 密文被改）时抛这个，消息可直接给用户看。 */
    public static class PresetException extends Exception {
        public PresetException(String message) {
            super(message);
        }
    }

    /** 从口令派生密钥。 */
    private static SecretKeySpec keyOf(String passphrase, byte[] salt)
            throws GeneralSecurityException {
        PBEKeySpec spec = new PBEKeySpec(passphrase.toCharArray(), salt, ITERATIONS, KEY_BITS);
        try {
            SecretKeyFactory f = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256");
            return new SecretKeySpec(f.generateSecret(spec).getEncoded(), "AES");
        } finally {
            spec.clearPassword();
        }
    }

    /** 解密预设里的 Token。口令错或密文损坏 → PresetException（不是 RuntimeException）。 */
    public static String decrypt(String passphrase, String saltB64, String ivB64, String ctB64)
            throws PresetException {
        if (passphrase == null || passphrase.isEmpty()) {
            throw new PresetException("请填写解锁口令。");
        }
        try {
            byte[] salt = Base64.getDecoder().decode(saltB64);
            byte[] iv = Base64.getDecoder().decode(ivB64);
            byte[] ct = Base64.getDecoder().decode(ctB64);
            Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
            c.init(Cipher.DECRYPT_MODE, keyOf(passphrase, salt), new GCMParameterSpec(TAG_BITS, iv));
            return new String(c.doFinal(ct), StandardCharsets.UTF_8);
        } catch (GeneralSecurityException e) {
            // GCM 校验失败也走这里：口令不对与密文被改在密码学上不可区分，
            // 所以给同一句人话，不泄露「口令对不对」这个信息。
            throw new PresetException("口令不对，或者预设文件被改动过。");
        } catch (IllegalArgumentException e) {
            throw new PresetException("预设文件格式不对（base64 解不开）。");
        }
    }

    /** 加密（只给构建期的本地工具用；App 运行时不需要加密）。 */
    public static String[] encrypt(String passphrase, String plain)
            throws GeneralSecurityException {
        SecureRandom rnd = new SecureRandom();
        byte[] salt = new byte[SALT_BYTES];
        byte[] iv = new byte[IV_BYTES];
        rnd.nextBytes(salt);
        rnd.nextBytes(iv);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.ENCRYPT_MODE, keyOf(passphrase, salt), new GCMParameterSpec(TAG_BITS, iv));
        byte[] ct = c.doFinal(plain.getBytes(StandardCharsets.UTF_8));
        Base64.Encoder b64 = Base64.getEncoder();
        return new String[]{b64.encodeToString(salt), b64.encodeToString(iv), b64.encodeToString(ct)};
    }
}
