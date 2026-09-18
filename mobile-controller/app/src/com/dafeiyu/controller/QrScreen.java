package com.dafeiyu.controller;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Typeface;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 二维码登录虚拟屏。
 *
 * 打开时才启动轮询 + 现画二维码；关屏 dispose 时停轮询、recycle 位图、
 * 关线程池 —— 平时（不在这块屏上）一点二维码相关的内存都不占。
 *
 * 复用中枢传进来的、已连接的 NapCatClient；不自己管连接。
 */
public final class QrScreen implements VirtualScreen {

    private static final long POLL_MS = 2500L;

    private final NapCatClient client;
    private final Runnable onLoggedIn;      // 登录成功回调（让中枢关屏 + 刷新）

    private final Handler ui = new Handler(Looper.getMainLooper());
    private ExecutorService pool;

    private TextView statusLine;
    private TextView detailLine;
    private ImageView qrImage;
    private TextView qrNote;

    private volatile boolean polling;
    private int pollErrors;
    private String lastQrUrl = "";
    private Bitmap currentQr;                // 持有当前位图，dispose 时 recycle

    public QrScreen(NapCatClient client, Runnable onLoggedIn) {
        this.client = client;
        this.onLoggedIn = onLoggedIn;
    }

    public String title() {
        return "二维码登录";
    }

    public View build(Context ctx) {
        pool = Executors.newSingleThreadExecutor();
        ScrollView scroll = UiKit.scroll(ctx);
        LinearLayout page = UiKit.pageColumn(ctx);
        scroll.addView(page);

        LinearLayout card = UiKit.card(ctx, "扫码登录");
        LinearLayout inner = UiKit.inner(card);

        statusLine = UiKit.text(ctx, "正在出码…", 17, Theme.TEXT);
        statusLine.setTypeface(statusLine.getTypeface(), Typeface.BOLD);
        inner.addView(statusLine);

        detailLine = UiKit.text(ctx, "", 12, Theme.DIM);
        inner.addView(detailLine);

        qrImage = new ImageView(ctx);
        qrImage.setAdjustViewBounds(true);
        LinearLayout.LayoutParams qlp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        qlp.topMargin = Theme.dp(ctx, 10);
        qrImage.setLayoutParams(qlp);
        inner.addView(qrImage);

        qrNote = UiKit.text(ctx, "稍等，正在向服务器要登录二维码…", 12, Theme.DIM);
        inner.addView(qrNote);

        Button refresh = UiKit.button(ctx, "刷新二维码", false);
        refresh.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                bg(new Runnable() {
                    public void run() {
                        try {
                            client.refreshQrcode();
                            tickNow();
                        } catch (NapCatClient.ApiError e) {
                            setDetail("刷新二维码失败：" + e.getMessage(), Theme.BAD);
                        }
                    }
                });
            }
        });
        inner.addView(refresh);
        page.addView(card);
        return scroll;
    }

    public void onEnter() {
        polling = true;
        tickNow();
        tickLoop();
    }

    public boolean onBack() {
        return false;   // 交给 ScreenHost 关屏
    }

    public void dispose() {
        polling = false;
        if (pool != null) {
            pool.shutdownNow();
            pool = null;
        }
        if (qrImage != null) {
            qrImage.setImageBitmap(null);
        }
        if (currentQr != null && !currentQr.isRecycled()) {
            currentQr.recycle();
        }
        currentQr = null;
        statusLine = null;
        detailLine = null;
        qrImage = null;
        qrNote = null;
    }

    // ---------------------------------------------------------------- 轮询

    private void tickLoop() {
        if (!polling) {
            return;
        }
        ui.postDelayed(new Runnable() {
            public void run() {
                if (!polling) {
                    return;
                }
                tickNow();
                tickLoop();
            }
        }, POLL_MS);
    }

    private void tickNow() {
        bg(new Runnable() {
            public void run() {
                Map<String, Object> st;
                try {
                    st = client.checkLoginStatus();
                } catch (NapCatClient.ApiError e) {
                    pollErrors++;
                    if (pollErrors >= 4) {
                        polling = false;
                        setStatus("连接断了", Theme.BAD);
                        setDetail("连续多次失败：" + e.getMessage() + "。请返回重连。", Theme.BAD);
                    }
                    return;
                }
                pollErrors = 0;
                renderStatus(st);
            }
        });
    }

    private void renderStatus(final Map<String, Object> st) {
        final boolean isLogin = Json.bool(st, "isLogin", false);
        final boolean isOffline = Json.bool(st, "isOffline", false);
        final String phase = Json.str(st, "loginPhase", isLogin ? "ready" : "waiting_qrcode");
        final String qrUrl = Json.str(st, "qrcodeurl", "");
        final String loginError = Json.str(st, "loginError", "");
        final boolean qrChanged = !qrUrl.isEmpty() && !qrUrl.equals(lastQrUrl);
        if (qrChanged) {
            lastQrUrl = qrUrl;
        }
        Bitmap bmp = null;
        if (qrChanged && !isLogin && qrImage != null) {
            bmp = QrPainter.paint(qrUrl, Theme.dp(qrImage.getContext(), 280));
        }
        final Bitmap qr = bmp;
        onUi(new Runnable() {
            public void run() {
                if (qrImage == null) {
                    if (qr != null && !qr.isRecycled()) {
                        qr.recycle();     // 屏已 dispose，别泄漏这张新画的位图
                    }
                    return;
                }
                if (isLogin) {
                    statusLine.setText("登录成功");
                    statusLine.setTextColor(Theme.GOOD);
                    swapQr(null);
                    qrNote.setText("已登录。");
                    if (onLoggedIn != null) {
                        onLoggedIn.run();
                    }
                    return;
                }
                if (isOffline) {
                    statusLine.setText("已掉线");
                    statusLine.setTextColor(Theme.BAD);
                } else {
                    statusLine.setText(phaseText(phase));
                    statusLine.setTextColor(Theme.WARN);
                }
                if (!loginError.isEmpty() && qr == null) {
                    detailLine.setText("最近错误：" + loginError);
                    detailLine.setTextColor(Theme.BAD);
                } else {
                    detailLine.setText("");
                }
                if (qr != null) {
                    swapQr(qr);
                    qrNote.setText("用手机 QQ 扫上面的二维码（QQ → 右上角 + → 扫一扫）。");
                } else if (isOffline && qrUrl.isEmpty()) {
                    qrNote.setText("掉线后还没出新码，点「刷新二维码」试一次。");
                }
            }
        });
    }

    /** 换二维码位图，把上一张 recycle 掉，避免快速换码时位图堆积。 */
    private void swapQr(Bitmap next) {
        if (qrImage != null) {
            qrImage.setImageBitmap(next);
        }
        if (currentQr != null && currentQr != next && !currentQr.isRecycled()) {
            currentQr.recycle();
        }
        currentQr = next;
    }

    private static String phaseText(String phase) {
        if ("qrcode_scanned".equals(phase)) {
            return "二维码已扫，等手机确认…";
        }
        if ("initializing".equals(phase)) {
            return "登录成功，正在启动…";
        }
        if ("reconnecting".equals(phase)) {
            return "正在重连登录服务…";
        }
        if ("generating_qrcode".equals(phase)) {
            return "正在出码…";
        }
        if ("offline".equals(phase)) {
            return "已掉线";
        }
        if ("ready".equals(phase)) {
            return "在线";
        }
        return "等待扫码";
    }

    // ---------------------------------------------------------------- 工具

    private void bg(Runnable task) {
        ExecutorService p = pool;
        if (p != null && !p.isShutdown()) {
            try {
                p.execute(task);
            } catch (java.util.concurrent.RejectedExecutionException ignored) {
            }
        }
    }

    private void onUi(Runnable task) {
        ui.post(task);
    }

    private void setStatus(final String msg, final int color) {
        onUi(new Runnable() {
            public void run() {
                if (statusLine != null) {
                    statusLine.setText(msg);
                    statusLine.setTextColor(color);
                }
            }
        });
    }

    private void setDetail(final String msg, final int color) {
        onUi(new Runnable() {
            public void run() {
                if (detailLine != null) {
                    detailLine.setText(msg);
                    detailLine.setTextColor(color);
                }
            }
        });
    }
}
