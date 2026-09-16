package tests;

import com.dafeiyu.controller.Totp;

/**
 * RFC 6238 附录 B 的测试向量（SHA-1，取 6 位截断）：
 *   T=59         → 94287082（8位）→ 287082
 *   T=1111111109 → 07081804（8位）→ 081804
 * 密钥 "12345678901234567890"（ASCII）。
 */
public final class TotpTest {

    public static void run() {
        T.group("TOTP / RFC 6238 向量");
        byte[] key = "12345678901234567890".getBytes();
        T.eq("T=59", "287082", Totp.code(key, 59L / 30L, 6));
        T.eq("T=1111111109", "081804", Totp.code(key, 1111111109L / 30L, 6));
        T.eq("T=1111111111", "050471", Totp.code(key, 1111111111L / 30L, 6));
        T.eq("补零到 6 位", "005924", Totp.code(key, 1234567890L / 30L, 6));
        T.eq("code6 走同一实现", "287082", Totp.code6("12345678901234567890", 59L));
    }
}
