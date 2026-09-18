package com.dafeiyu.controller;

import android.content.Context;
import android.text.InputType;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 密码登录虚拟屏。
 *
 * QQ 号 + 密码 → PasswordLogin（密码在 NapCatClient 里 MD5）。触发腾讯的
 * 安全验证 / 新设备验证时，App 原生页做不了那套 JS 验证组件，回调中枢
 * 打开短信/网页验证屏走完。
 *
 * 关屏 dispose 立刻清空密码输入框内容并断开引用；密码全程只在内存、不落盘。
 */
public final class PwScreen implements VirtualScreen {

    /** 需要安全验证时通知中枢：转到短信/网页验证屏。 */
    public interface OnNeedWebVerify {
        void run(boolean needCaptcha, boolean needNewDevice);
    }

    private final NapCatClient client;
    private final Store store;
    private final OnNeedWebVerify onNeedWeb;

    private final android.os.Handler ui = new android.os.Handler(android.os.Looper.getMainLooper());
    private ExecutorService pool;

    private EditText uin;
    private EditText qqPassword;
    private TextView result;

    public PwScreen(NapCatClient client, Store store, OnNeedWebVerify onNeedWeb) {
        this.client = client;
        this.store = store;
        this.onNeedWeb = onNeedWeb;
    }

    public String title() {
        return "密码登录";
    }

    public View build(Context ctx) {
        pool = Executors.newSingleThreadExecutor();
        ScrollView scroll = UiKit.scroll(ctx);
        LinearLayout page = UiKit.pageColumn(ctx);
        scroll.addView(page);

        LinearLayout card = UiKit.card(ctx, "密码登录（QQ 号 + QQ 密码）");
        LinearLayout inner = UiKit.inner(card);
        inner.addView(UiKit.text(ctx, "密码只用来在服务器上登录这个 QQ 号，App 不保存它。"
                + "触发安全验证时会自动切到网页完成。", 12, Theme.DIM));

        uin = UiKit.input(ctx, "QQ 号", false);
        uin.setInputType(InputType.TYPE_CLASS_NUMBER);
        uin.setText(store.lastUin());
        inner.addView(uin);

        qqPassword = UiKit.input(ctx, "QQ 密码（不会保存）", true);
        inner.addView(qqPassword);

        Button pwBtn = UiKit.button(ctx, "密码登录", true);
        pwBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                submit();
            }
        });
        inner.addView(pwBtn);

        result = UiKit.text(ctx, "", 13, Theme.WARN);
        inner.addView(result);

        page.addView(card);
        return scroll;
    }

    public void onEnter() {
    }

    public boolean onBack() {
        return false;
    }

    public void dispose() {
        if (pool != null) {
            pool.shutdownNow();
            pool = null;
        }
        // 密码只进内存：关屏立刻抹掉输入框里的明文
        if (qqPassword != null) {
            qqPassword.setText("");
        }
        uin = null;
        qqPassword = null;
        result = null;
    }

    // ---------------------------------------------------------------- 提交

    private void submit() {
        final String account = uin.getText().toString().trim();
        final String password = qqPassword.getText().toString();
        if (account.isEmpty()) {
            setResult("请填 QQ 号", Theme.BAD);
            return;
        }
        if (password.isEmpty()) {
            setResult("请填密码", Theme.BAD);
            return;
        }
        store.setLastUin(account);
        setResult("正在提交…", Theme.DIM);
        bg(new Runnable() {
            public void run() {
                Map<String, Object> data;
                try {
                    data = client.passwordLogin(account, password);
                } catch (NapCatClient.ApiError e) {
                    setResult("密码登录失败：" + e.getMessage(), Theme.BAD);
                    return;
                }
                final boolean needCaptcha = Json.bool(data, "needCaptcha", false);
                final boolean needNewDevice = Json.bool(data, "needNewDevice", false);
                onUi(new Runnable() {
                    public void run() {
                        if (qqPassword != null) {
                            qqPassword.setText("");   // 提交后立即清明文
                        }
                        if (needCaptcha || needNewDevice) {
                            setResult(needCaptcha
                                    ? "QQ 要求安全验证（验证码），正在切到网页完成…"
                                    : "QQ 要求新设备验证（扫码确认），正在切到网页完成…",
                                    Theme.WARN);
                            if (onNeedWeb != null) {
                                onNeedWeb.run(needCaptcha, needNewDevice);
                            }
                        } else {
                            setResult("登录请求已发送。返回上一页看状态；"
                                    + "如果要求安全验证会再提示。", Theme.GOOD);
                        }
                    }
                });
            }
        });
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

    private void setResult(final String msg, final int color) {
        onUi(new Runnable() {
            public void run() {
                if (result != null) {
                    result.setText(msg);
                    result.setTextColor(color);
                }
            }
        });
    }
}
