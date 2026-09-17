package tests;

import com.dafeiyu.controller.QrLayout;
import io.nayuki.qrcodegen.QrCode;

/**
 * 二维码几何 + 「画出来真的能扫」测试。
 *
 * 起因（v1.0.4 真机 bug）：APK 死活不出二维码。根因是 NapCatClient 没剥 data 外壳
 * （在 NapCatClientTest 里另有反回归），但排查过程中发现 QrPainter 本身
 * **一个测试都没有** —— 也就是说「就算内容拿到了，图能不能扫」这件事从来没人验证过。
 * 这里把它补上：几何用纯算术验，成图用真解码器验。
 */
public final class QrLayoutTest {

    /** 真实服务器返回过的二维码内容（NapCat CheckLoginStatus 的 qrcodeurl）。 */
    private static final String REAL_QR =
            "https://txz.qq.com/p?k=b3DEDMx6LrRXo1XfOOJCEcHwXfX4z*Yh&f=1600001615";

    public static void run() {
        T.group("二维码几何：静区与放大倍数");

        QrCode qr = QrCode.encodeText(REAL_QR, QrCode.Ecc.MEDIUM);
        T.eq("真实内容的模块数（版本 5）", 37, qr.size);

        // 静区必须够：规范是 4 个模块，给少了扫码器找不到定位角
        T.eq("静区是规范要求的 4 个模块", 4, QrLayout.QUIET_MODULES);

        // 手机常见的三种密度下算一遍，成图不能太小、也不能退化成 0
        int[] targets = {280, 560, 840};
        for (int t : targets) {
            int scale = QrLayout.scaleFor(qr.size, t);
            int size = QrLayout.pixelSize(qr.size, t);
            T.eq("target=" + t + " 时 scale 至少 1", true, scale >= 1);
            T.eq("target=" + t + " 时成图 = (模块+8)*scale", (qr.size + 8) * scale, size);
            // 成图至少要有 200px，否则手机上糊成一团扫不出
            T.eq("target=" + t + " 时成图不小于 200px", true, size >= 200);
        }

        // 极端入参不许崩、不许算出 0
        T.eq("targetPx=0 时 scale 兜底为 1", 1, QrLayout.scaleFor(qr.size, 0));
        T.eq("targetPx=0 时成图 = 模块数+8", qr.size + 8, QrLayout.pixelSize(qr.size, 0));
        T.eq("qrSize=0 时仍算出合理倍数（8 个静区模块）", 105, QrLayout.scaleFor(0, 840));
        // 负数（理论上不该出现）也要兜住，绝不能返回 0 或负数
        T.eq("qrSize=-8（模块数算成 0）时兜底为 1", 1, QrLayout.scaleFor(-8, 840));
        T.eq("qrSize=-100（模块数算成负）时兜底为 1", 1, QrLayout.scaleFor(-100, 840));
        T.eq("targetPx 为负时也不退化", true, QrLayout.scaleFor(qr.size, -5) >= 1);

        // scale 绝不能是 0：那会让所有 drawRect 变成零面积 → 全白图 → 「有图但扫不出」
        T.eq("小 target 也不会算出 scale=0", 1, QrLayout.scaleFor(qr.size, 10));

        // 最硬的一条：把 App 的绘制逻辑**逐模块重画一遍**，再用第三方解码器
        // （ZXing，跟 App 用的 nayuki 编码器完全无关）真去解，解出来的内容必须
        // 与输入一字不差。只断言「几何算对了」是不够的 —— 画错位一样扫不出。
        T.group("二维码成图能被真正的解码器扫出来（ZXing 独立解码）");
        String decoded = decodePainted(qr, 840);
        if (decoded == null) {
            T.bad("画出来的二维码解不开（扫码器会扫不出）", "ZXing 返回 null");
        } else {
            T.eq("解码内容与输入完全一致", REAL_QR, decoded);
        }

        // 换几种手机密度再验一次：不同 scale 下都得能解
        for (int t : new int[]{280, 560}) {
            String d = decodePainted(qr, t);
            T.eq("target=" + t + " 时也能扫出同样内容", REAL_QR, d);
        }
    }

    /**
     * 按 QrPainter 的算法把二维码画成像素矩阵，交给 ZXing 解码。
     *
     * 这里刻意不调 QrPainter 本身（它依赖 android.graphics），而是照抄它的
     * 绘制规则：静区 4 模块、模块放大 scale 倍、黑模块涂满。抄错就解不开。
     */
    private static String decodePainted(QrCode qr, int targetPx) {
        int quiet = QrLayout.QUIET_MODULES;
        int scale = QrLayout.scaleFor(qr.size, targetPx);
        int size = QrLayout.pixelSize(qr.size, targetPx);
        java.awt.image.BufferedImage img =
                new java.awt.image.BufferedImage(size, size,
                        java.awt.image.BufferedImage.TYPE_INT_RGB);
        for (int y = 0; y < size; y++) {
            for (int x = 0; x < size; x++) {
                img.setRGB(x, y, 0xFFFFFF);          // 底色纯白
            }
        }
        for (int y = 0; y < qr.size; y++) {
            for (int x = 0; x < qr.size; x++) {
                if (!qr.getModule(x, y)) {
                    continue;
                }
                for (int dy = 0; dy < scale; dy++) {
                    for (int dx = 0; dx < scale; dx++) {
                        img.setRGB((x + quiet) * scale + dx,
                                   (y + quiet) * scale + dy, 0x000000);
                    }
                }
            }
        }
        try {
            com.google.zxing.LuminanceSource src =
                    new com.google.zxing.client.j2se.BufferedImageLuminanceSource(img);
            com.google.zxing.BinaryBitmap bmp =
                    new com.google.zxing.BinaryBitmap(
                            new com.google.zxing.common.HybridBinarizer(src));
            java.util.Map<com.google.zxing.DecodeHintType, Object> hints =
                    new java.util.HashMap<com.google.zxing.DecodeHintType, Object>();
            hints.put(com.google.zxing.DecodeHintType.TRY_HARDER, Boolean.TRUE);
            return new com.google.zxing.MultiFormatReader().decode(bmp, hints).getText();
        } catch (Exception e) {
            return null;
        }
    }
}
