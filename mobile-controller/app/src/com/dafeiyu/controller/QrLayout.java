package com.dafeiyu.controller;

/**
 * 二维码成图的几何计算 —— 从 QrPainter 里拆出来，专门为了能单测。
 *
 * 为什么值得拆：QrPainter 依赖 android.graphics.Bitmap，进不了纯 JVM 测试面；
 * 但「静区留几个模块、一个模块放大几倍、最终成图多大」这段算术是纯函数，
 * 而它恰恰是最容易悄悄错的地方：
 *   · scale 算成 0 → 画出来是空白图（扫不出，但界面「看起来有图」）
 *   · 静区给少了 → 扫码器找不到定位角，同样扫不出
 *   · 成图过小 → 屏幕上糊成一团
 * 这些错法都不会抛异常，只会表现为「二维码不显示 / 扫不出来」。
 */
public final class QrLayout {

    /** 规范要求的静区宽度（模块数）。给 4 是 QR 标准，少了扫码器会挑。 */
    public static final int QUIET_MODULES = 4;

    private QrLayout() {
    }

    /**
     * 一个模块放大几倍。
     *
     * 至少要 1：scale=0 会让下面所有 drawRect 退化成零面积，结果是
     * 一张全白的图 —— 界面有图、就是扫不出，最难查的一种坏法。
     */
    public static int scaleFor(int qrSize, int targetPx) {
        int modules = qrSize + QUIET_MODULES * 2;
        if (modules <= 0) {
            return 1;
        }
        return Math.max(1, targetPx / modules);
    }

    /** 成图边长（含静区）。 */
    public static int pixelSize(int qrSize, int targetPx) {
        return (qrSize + QUIET_MODULES * 2) * scaleFor(qrSize, targetPx);
    }
}
