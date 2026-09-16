package com.dafeiyu.controller;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * RFC 6238 TOTP（30 秒步长、SHA-1、6 位数字）—— NapCat WebUI 开启两步验证时
 * 登录接口要带 totpCode。纯 JVM 实现，不碰 android.*，可以直接单测。
 *
 * 只在用户填了动态码字段时才会用到；没开启 2FA 的服务器完全用不到它。
 */
public final class Totp {

    private Totp() {
    }

    /** @param unixSeconds 当前 Unix 秒；@param secretKey 用户密钥（明文字符串）。 */
    public static String code6(String secretKey, long unixSeconds) {
        return code(secretKey.getBytes(), unixSeconds / 30L, 6);
    }

    public static String code(byte[] secret, long counter, int digits) {
        byte[] msg = new byte[8];
        long c = counter;
        for (int i = 7; i >= 0; i--) {
            msg[i] = (byte) (c & 0xFFL);
            c >>>= 8;
        }
        try {
            Mac mac = Mac.getInstance("HmacSHA1");
            mac.init(new SecretKeySpec(secret, "HmacSHA1"));
            byte[] hash = mac.doFinal(msg);
            int offset = hash[hash.length - 1] & 0x0F;
            int binary = ((hash[offset] & 0x7F) << 24)
                    | ((hash[offset + 1] & 0xFF) << 16)
                    | ((hash[offset + 2] & 0xFF) << 8)
                    | (hash[offset + 3] & 0xFF);
            int mod = 1;
            for (int i = 0; i < digits; i++) {
                mod *= 10;
            }
            int value = binary % mod;
            StringBuilder sb = new StringBuilder(Integer.toString(value));
            while (sb.length() < digits) {
                sb.insert(0, '0');
            }
            return sb.toString();
        } catch (java.security.GeneralSecurityException e) {
            throw new IllegalStateException("TOTP 计算失败", e);
        }
    }
}
