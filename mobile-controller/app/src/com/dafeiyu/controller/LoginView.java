package com.dafeiyu.controller;

import android.app.Activity;
import android.content.Context;
import android.graphics.Bitmap;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 「登录 QQ」页 —— 开放式登录的主体。
 *
 * 一页四块：
 *   1. 连接：WebUI 地址 + Token（+ 可选动态码）→ 换凭据，之后请求全自动带；
 *   2. 状态：在线/掉线/出码中…，每 2.5 秒轮询一次 CheckLoginStatus；
 *   3. 扫码登录：把 CheckLoginStatus 给的 qrcodeurl **本地**画成二维码，
 *      手机 QQ 扫它即可（和官方网页同一个内容源）；
 *   4. 密码登录：QQ 号 + 密码（MD5 后发 PasswordLogin）；要安全验证时
 *      提示切到内置网页登录页（那里是官方验证流程，能跑腾讯的验证码组件）。
 *   另有快速登录（历史账号一键登录）与「打开网页登录页」。
 *
 * 安全：Token/密码/动态码只进内存，绝不进 Store。
 */
public final class LoginView {

    public interface Host {
        void toast(String msg);

        void openWebLogin(String base);
    }

    private static final long POLL_MS = 2500L;

    private final Context ctx;
    private final Host host;
    private final Store store;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService pool = Executors.newSingleThreadExecutor();
    private final NapCatClient client = new NapCatClient(new NapCatClient.Real());

    private View root;
    private EditText address;
    private EditText token;
    private EditText totp;
    private Button connectBtn;
    private TextView connState;
    private TextView statusLine;
    private TextView detailLine;
    private ImageView qrImage;
    private TextView qrNote;
    private Button refreshQrBtn;
    private EditText uin;
    private TextView uinNote;
    private EditText presetPass;
    private Button presetBtn;
    private EditText qqPassword;
    private TextView pwResult;
    private LinearLayout quickBox;
    private LinearLayout cards;

    private boolean polling;
    private int pollErrors;
    private String lastQrUrl = "";
    private boolean lastOnline;
    private boolean busy;

    public LoginView(Context ctx, Host host, Store store) {
        this.ctx = ctx;
        this.host = host;
        this.store = store;
    }

    // ------------------------------------------------------------ 界面

    public View view() {
        if (root != null) {
            return root;
        }
        ScrollView scroll = UiKit.scroll(ctx);
        LinearLayout page = UiKit.pageColumn(ctx);
        scroll.addView(page);
        cards = page;

        // ① 连接
        LinearLayout connCard = UiKit.card(ctx, "① 连接机器人的 NapCat 网页");
        LinearLayout c1 = UiKit.inner(connCard);
        c1.addView(UiKit.text(ctx, "填你服务器上 NapCat WebUI 的地址和 Token。"
                + "地址形如 1.2.3.4:6099；Token 在服务器 webui.json 里（或启动日志）。",
                12, Theme.DIM));
        address = UiKit.input(ctx, "WebUI 地址，例如 1.2.3.4:6099", false);
        c1.addView(address);
        token = UiKit.input(ctx, "WebUI Token（不会保存）", true);
        c1.addView(token);
        totp = UiKit.input(ctx, "两步验证动态码（没开 2FA 就留空）", false);
        c1.addView(totp);
        // 定制版（Preset.HAS_PRESET）：地址已内置，Token 以密文形式在包里，
        // 用构建时给的一次性口令解锁。公开版这块完全不出现 —— 代码路径都不走。
        if (Preset.HAS_PRESET) {
            address.setText(Preset.WEBUI_BASE);
            c1.addView(UiKit.text(ctx, "这是定制版：服务器地址已内置，"
                    + "填解锁口令就能连（口令不会保存）。", 12, Theme.GOOD));
            presetPass = UiKit.input(ctx, "解锁口令", true);
            c1.addView(presetPass);
            presetBtn = UiKit.button(ctx, "解锁并连接", false);
            c1.addView(presetBtn);
            if (!Preset.HINT.isEmpty()) {
                c1.addView(UiKit.text(ctx, Preset.HINT, 11, Theme.DIM));
            }
        }
        connectBtn = UiKit.button(ctx, "连接", true);
        c1.addView(connectBtn);
        connState = UiKit.text(ctx, "未连接。", 12, Theme.DIM);
        c1.addView(connState);
        cards.addView(connCard);

        // ② 状态
        LinearLayout statusCard = UiKit.card(ctx, "② 机器人状态");
        LinearLayout c2 = UiKit.inner(statusCard);
        statusLine = UiKit.text(ctx, "还没连接。", 17, Theme.TEXT);
        statusLine.setTypeface(statusLine.getTypeface(), android.graphics.Typeface.BOLD);
        c2.addView(statusLine);
        detailLine = UiKit.text(ctx, "", 12, Theme.DIM);
        c2.addView(detailLine);
        refreshQrBtn = UiKit.button(ctx, "刷新二维码", false);
        c2.addView(refreshQrBtn);
        qrImage = new ImageView(ctx);
        qrImage.setAdjustViewBounds(true);
        LinearLayout.LayoutParams qlp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        qlp.topMargin = Theme.dp(ctx, 10);
        qrImage.setLayoutParams(qlp);
        c2.addView(qrImage);
        qrNote = UiKit.text(ctx, "连接后这里会显示登录二维码；用手机 QQ 扫它。", 12, Theme.DIM);
        c2.addView(qrNote);
        cards.addView(statusCard);

        // ③ 密码登录
        LinearLayout pwCard = UiKit.card(ctx, "③ 密码登录（QQ 号 + QQ 密码）");
        LinearLayout c3 = UiKit.inner(pwCard);
        c3.addView(UiKit.text(ctx, "密码只用来在服务器上登录这个 QQ 号，App 不保存它。"
                + "触发安全验证时按提示切到网页完成。", 12, Theme.DIM));
        uin = UiKit.input(ctx, "QQ 号（同时用来核对登录的是不是这个号）", false);
        uin.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);
        c3.addView(uin);
        uinNote = UiKit.text(ctx, "登录成功后会在这里告诉你服务器上真正登录的是哪个号。",
                12, Theme.DIM);
        c3.addView(uinNote);
        qqPassword = UiKit.input(ctx, "QQ 密码（不会保存）", true);
        c3.addView(qqPassword);
        Button pwBtn = UiKit.button(ctx, "密码登录", true);
        c3.addView(pwBtn);
        pwResult = UiKit.text(ctx, "", 13, Theme.WARN);
        c3.addView(pwResult);
        cards.addView(pwCard);

        // ④ 快速登录 + 网页
        LinearLayout quickCard = UiKit.card(ctx, "④ 快速登录 / 网页登录");
        LinearLayout c4 = UiKit.inner(quickCard);
        quickBox = UiKit.column(ctx);
        c4.addView(quickBox);
        quickBox.addView(UiKit.text(ctx, "服务器上登录过的 QQ 会列在这里，点一下直接登录"
                + "（相当于勾了 ACCOUNT 的快速登录）。“获取列表”在连接后可用。",
                12, Theme.DIM));
        Button reloadQuick = UiKit.button(ctx, "获取快速登录列表", false);
        c4.addView(reloadQuick);
        Button webBtn = UiKit.button(ctx, "打开内置网页登录页（验证码 / 两步验证兜底）", false);
        c4.addView(webBtn);
        Button restartBtn = UiKit.button(ctx, "重启 NapCat（卡死时用，会出新的二维码）", false);
        c4.addView(restartBtn);
        cards.addView(quickCard);

        // 事件
        connectBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                connect();
            }
        });
        refreshQrBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                bg(new Runnable() {
                    public void run() {
                        try {
                            client.refreshQrcode();
                            tickNow();
                        } catch (NapCatClient.ApiError e) {
                            fail("刷新二维码失败：" + e.getMessage());
                        }
                    }
                });
            }
        });
        pwBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                passwordLogin();
            }
        });
        reloadQuick.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                loadQuickList();
            }
        });
        webBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                String base = client.base().isEmpty() ? store.webuiBase() : client.base();
                if (base.isEmpty()) {
                    host.toast("先填上面的 WebUI 地址");
                    return;
                }
                host.openWebLogin(base);
            }
        });
        restartBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                if (!(ctx instanceof Activity)) {
                    return;
                }
                new android.app.AlertDialog.Builder(ctx)
                        .setTitle("重启 NapCat？")
                        .setMessage("QQ 会掉线并生成新的登录二维码，大概率要重新扫码或快速登录。")
                        .setPositiveButton("重启", new android.content.DialogInterface.OnClickListener() {
                            public void onClick(android.content.DialogInterface d, int w) {
                                bg(new Runnable() {
                                    public void run() {
                                        try {
                                            client.restartNapCat();
                                            say("已发送重启指令，等它起来后重新连接。");
                                        } catch (NapCatClient.ApiError e) {
                                            fail("重启失败：" + e.getMessage());
                                        }
                                    }
                                });
                            }
                        })
                        .setNegativeButton("取消", null)
                        .show();
            }
        });

        address.setText(store.webuiBase());
        uin.setText(store.lastUin());
        if (Preset.HAS_PRESET) {
            presetBtn.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) {
                    unlockAndConnect();
                }
            });
        }
        root = scroll;
        return root;
    }

    /**
     * 定制版专用：用口令解开内置的加密 Token，填进 Token 框后走正常连接流程。
     * 解出来的 Token 只存在于 EditText 与内存里，和手填完全同一条路（不落盘、不进日志）。
     */
    private void unlockAndConnect() {
        String pass = presetPass.getText().toString();
        if (pass.isEmpty()) {
            host.toast("请填写解锁口令");
            return;
        }
        try {
            token.setText(PresetCrypto.decrypt(pass, Preset.TOKEN_SALT, Preset.TOKEN_IV,
                    Preset.TOKEN_CT));
        } catch (PresetCrypto.PresetException e) {
            host.toast(e.getMessage());
            return;
        }
        presetPass.setText("");   // 口令用完即弃，不留内存里
        connect();
    }

    // ------------------------------------------------------------ 连接与轮询

    private void connect() {
        if (busy) {
            return;
        }
        final String base = NapCatClient.normalize(address.getText().toString());
        final String tok = token.getText().toString();
        final String code = totp.getText().toString();
        if (base.isEmpty()) {
            host.toast("请填 WebUI 地址");
            return;
        }
        if (tok.isEmpty()) {
            host.toast("请填 WebUI Token");
            return;
        }
        busy = true;
        connectBtn.setEnabled(false);
        connState.setText("连接中…");
        connState.setTextColor(Theme.DIM);
        bg(new Runnable() {
            public void run() {
                client.configure(base, tok, code);
                try {
                    client.login();
                } catch (NapCatClient.ApiError e) {
                    fail(e.getMessage());
                    return;
                }
                store.setWebuiBase(base);
                onUi(new Runnable() {
                    public void run() {
                        busy = false;
                        connectBtn.setEnabled(true);
                        connState.setText("已连接：" + base);
                        connState.setTextColor(Theme.GOOD);
                        token.setText("");
                        totp.setText("");
                        host.toast("已连接，开始轮询登录状态");
                    }
                });
                startPolling();
                loadQuickList();
            }
        });
    }

    public void onResume() {
        if (client.connected()) {
            startPolling();
        }
    }

    public void onPause() {
        stopPolling();
    }

    private void startPolling() {
        if (polling) {
            return;
        }
        polling = true;
        tickLoop();
    }

    private void stopPolling() {
        polling = false;
    }

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
                        // 连续四次失败才判定连接断了；偶发超时/抖动不值得打断轮询
                        polling = false;
                        onUi(new Runnable() {
                            public void run() {
                                connState.setText("连接断了（连续多次失败）：" + e.getMessage()
                                        + "。重新点「连接」即可。");
                                connState.setTextColor(Theme.BAD);
                                statusLine.setText("状态不明");
                                statusLine.setTextColor(Theme.WARN);
                            }
                        });
                    }
                    return;
                }
                pollErrors = 0;
                renderStatus(st);
            }
        });
    }

    /** 把 CheckLoginStatus 的结果铺到界面上（在后台线程算好，回 UI 线程改控件）。 */
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
        if (qrChanged && !isLogin) {
            bmp = QrPainter.paint(qrUrl, Theme.dp(ctx, 280));
        }
        final Bitmap qr = bmp;
        onUi(new Runnable() {
            public void run() {
                if (isLogin) {
                    statusLine.setText("在线");
                    statusLine.setTextColor(Theme.GOOD);
                    qrImage.setImageBitmap(null);
                    qrNote.setText("已登录。掉线时这里会重新出码。");
                    if (!lastOnline) {
                        loadAccount();
                        loadQuickList();
                    }
                    lastOnline = true;
                    return;
                }
                lastOnline = false;
                if (isOffline) {
                    statusLine.setText("已掉线");
                    statusLine.setTextColor(Theme.BAD);
                } else {
                    statusLine.setText(phaseText(phase));
                    statusLine.setTextColor(Theme.WARN);
                }
                if (!loginError.isEmpty()) {
                    detailLine.setText("最近错误：" + loginError);
                    detailLine.setTextColor(Theme.BAD);
                } else {
                    detailLine.setText("");
                }
                if (qr != null) {
                    qrImage.setImageBitmap(qr);
                    qrNote.setText("用手机 QQ 扫上面的二维码（QQ → 右上角 + → 扫一扫）。");
                } else if (isOffline && qrUrl.isEmpty()) {
                    qrNote.setText("掉线后还没出新码，点「刷新二维码」试一次。");
                }
            }
        });
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

    private void loadAccount() {
        bg(new Runnable() {
            public void run() {
                try {
                    final Map<String, Object> info = client.loginInfo();
                    onUi(new Runnable() {
                        public void run() {
                            String u = Json.str(info, "uin", Json.str(info, "uid", ""));
                            String nick = Json.str(info, "nick", Json.str(info, "nickname", ""));
                            if (!u.isEmpty() || !nick.isEmpty()) {
                                detailLine.setText("账号：" + nick + "（" + u + "）");
                                detailLine.setTextColor(Theme.GOOD);
                            }
                            checkExpectedUin(u);
                        }
                    });
                } catch (NapCatClient.ApiError ignored) {
                }
            }
        });
    }

    /**
     * QQ 号防呆：填了「期望的 QQ 号」就核对服务器上真正登录的是不是它。
     * 登录错号（比如填错、或服务器上早就登着别的号）是这套流程最容易犯的错，
     * 而且从二维码上看不出来 —— 所以对不上时用醒目的颜色明说。
     */
    private void checkExpectedUin(String actualUin) {
        String want = uin.getText().toString().trim();
        if (want.isEmpty() || actualUin == null || actualUin.isEmpty()) {
            return;
        }
        if (want.equals(actualUin.trim())) {
            uinNote.setTextColor(Theme.GOOD);
            uinNote.setText("✓ 已核对：服务器上登录的正是这个 QQ 号（" + want + "）。");
            return;
        }
        uinNote.setTextColor(Theme.BAD);
        uinNote.setText("⚠ 对不上：服务器上登录的是 " + actualUin.trim()
                + "，你填的是 " + want + "。如果要的是另一个号，先「重启 NapCat」再用那个号登录。");
    }

    // ------------------------------------------------------------ 密码登录

    private void passwordLogin() {
        final String account = uin.getText().toString().trim();
        final String password = qqPassword.getText().toString();
        if (account.isEmpty()) {
            host.toast("请填 QQ 号");
            return;
        }
        if (password.isEmpty()) {
            host.toast("请填密码");
            return;
        }
        store.setLastUin(account);
        pwResult.setText("正在提交…");
        pwResult.setTextColor(Theme.DIM);
        bg(new Runnable() {
            public void run() {
                Map<String, Object> data;
                try {
                    data = client.passwordLogin(account, password);
                } catch (NapCatClient.ApiError e) {
                    pwFail("密码登录失败：" + e.getMessage());
                    return;
                }
                final boolean needCaptcha = Json.bool(data, "needCaptcha", false);
                final boolean needNewDevice = Json.bool(data, "needNewDevice", false);
                onUi(new Runnable() {
                    public void run() {
                        if (needCaptcha || needNewDevice) {
                            pwResult.setTextColor(Theme.WARN);
                            pwResult.setText(needCaptcha
                                    ? "QQ 要求安全验证（验证码）。App 里做不了腾讯的验证组件，"
                                    + "请点「打开内置网页登录页」，用密码登录走完那一步。"
                                    : "QQ 要求新设备验证（扫码确认）。请点「打开内置网页登录页」"
                                    + "按提示扫码验证。");
                        } else {
                            pwResult.setTextColor(Theme.GOOD);
                            pwResult.setText("登录请求已发送，等几秒看上面的状态。"
                                    + "如果它要求安全验证，会在这里提示。");
                            qqPassword.setText("");
                        }
                    }
                });
            }
        });
    }

    // ------------------------------------------------------------ 快速登录

    private void loadQuickList() {
        if (!client.connected()) {
            host.toast("先连接 WebUI");
            return;
        }
        bg(new Runnable() {
            public void run() {
                java.util.List<Object> items;
                try {
                    items = client.quickList();
                } catch (NapCatClient.ApiError e) {
                    return;
                }
                final java.util.List<Object> found = items;
                onUi(new Runnable() {
                    public void run() {
                        quickBox.removeViews(1, quickBox.getChildCount() - 1);
                        if (found.isEmpty()) {
                            quickBox.addView(UiKit.text(ctx, "（服务器上还没有历史登录记录）",
                                    12, Theme.DIM));
                            return;
                        }
                        for (final Object item : found) {
                            final String u;
                            final String nick;
                            if (item instanceof Map) {
                                Map<?, ?> m = (Map<?, ?>) item;
                                u = String.valueOf(m.containsKey("uin") ? m.get("uin")
                                        : m.get("uid"));
                                nick = String.valueOf(m.containsKey("nick") ? m.get("nick")
                                        : m.get("nickname"));
                            } else {
                                u = String.valueOf(item);
                                nick = "";
                            }
                            if (u == null || u.isEmpty() || "null".equals(u)) {
                                continue;
                            }
                            String label = nick == null || nick.isEmpty() || "null".equals(nick)
                                    ? u : nick + "（" + u + "）";
                            Button b = UiKit.button(ctx, "快速登录 " + label, false);
                            b.setOnClickListener(new View.OnClickListener() {
                                public void onClick(View v) {
                                    quickLogin(u);
                                }
                            });
                            quickBox.addView(b);
                        }
                    }
                });
            }
        });
    }

    private void quickLogin(final String account) {
        bg(new Runnable() {
            public void run() {
                try {
                    client.setQuickLogin(account);
                    say("已发起快速登录 " + account + "，等几秒看状态。");
                } catch (NapCatClient.ApiError e) {
                    fail("快速登录失败：" + e.getMessage());
                }
            }
        });
    }

    // ------------------------------------------------------------ 工具

    private void bg(Runnable task) {
        pool.execute(task);
    }

    private void onUi(Runnable task) {
        ui.post(task);
    }

    private void say(final String msg) {
        onUi(new Runnable() {
            public void run() {
                pwResult.setTextColor(Theme.GOOD);
                pwResult.setText(msg);
                host.toast(msg);
            }
        });
    }

    private void fail(final String msg) {
        onUi(new Runnable() {
            public void run() {
                busy = false;
                connectBtn.setEnabled(true);
                connState.setText(msg);
                connState.setTextColor(Theme.BAD);
            }
        });
    }

    private void pwFail(final String msg) {
        onUi(new Runnable() {
            public void run() {
                pwResult.setTextColor(Theme.BAD);
                pwResult.setText(msg);
            }
        });
    }
}
