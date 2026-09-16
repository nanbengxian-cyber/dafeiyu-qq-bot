package com.dafeiyu.controller;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Paint;
import io.nayuki.qrcodegen.QrCode;

/**
 * 把 NapCat 下发的「二维码内容 URL」画成能扫的图。
 *
 * 关键事实：WebUI 的 CheckLoginStatus / RefreshQRcode 返回的 qrcodeurl
 * **不是图片**，是二维码里编码的那个跳转 URL —— 官方网页前端也是拿到它之后
 * 自己用前端库现画二维码。所以手机端同样要本地生成，不能指望服务器给图。
 *
 * 静区（四周留白）按规范给 4 个模块，底色纯白 —— 扫码器对反色和贴边都很挑。
 */
public final class QrPainter {

    private QrPainter() {
    }

    /** @param targetPx 期望的成图边长（含静区）；返回 null 表示内容编不进二维码。 */
    public static Bitmap paint(String content, int targetPx) {
        if (content == null || content.isEmpty()) {
            return null;
        }
        QrCode qr;
        try {
            qr = QrCode.encodeText(content, QrCode.Ecc.MEDIUM);
        } catch (IllegalArgumentException | NullPointerException e) {
            return null;
        }
        int quiet = 4;
        int modules = qr.size + quiet * 2;
        int scale = Math.max(1, targetPx / modules);
        int size = modules * scale;
        Bitmap bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bmp);
        canvas.drawColor(0xFFFFFFFF);
        Paint black = new Paint();
        black.setColor(0xFF000000);
        for (int y = 0; y < qr.size; y++) {
            for (int x = 0; x < qr.size; x++) {
                if (qr.getModule(x, y)) {
                    canvas.drawRect((x + quiet) * scale, (y + quiet) * scale,
                            (x + quiet + 1) * scale, (y + quiet + 1) * scale, black);
                }
            }
        }
        return bmp;
    }
}
