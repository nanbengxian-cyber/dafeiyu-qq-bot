package com.dafeiyu.controller;

import android.app.Activity;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;
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
 * 「登录 QQ」页 —— 轻量登录中枢。
 *
 * 本页常驻内存（是四个页签之一），所以做得很轻：只有连接卡、一行文本状态、
 * 三个验证入口按钮和快速登录/重启。**不再常驻**任何二维码位图、密码输入框
 * 或 WebView —— 那些重资源都搬进了「虚拟屏」：
 *
 *   ① 二维码登录 → QrScreen（覆盖层虚拟屏，打开才画码、关掉即 recycle）
 *   ② 密码登录   → PwScreen（覆盖层虚拟屏，关掉即清空密码明文）
 *   ③ 短信/网页验证 → WebScreen（覆盖层虚拟屏，关掉即 destroy WebView）
 *
 * 三大验证统一为虚拟屏（2026-09-19 用户要求）：原来第③块是独立
 * Activity，一点「短信 / 网页验证」就跳出一整页网页 —— 用户看到的就是
 * 「之前的网页」，和另两块的悬浮屏体验割裂。现在它也是盖在主界面上的屏。
 *
 * 三块都是「用完即删、需要再建」：点开才构建、关闭立即释放，平时零占用。
 *
 * 状态轮询只在本页前台且没有虚拟屏打开时跑，且只更新一行文字（无位图），
 * 开虚拟屏前会先停掉，避免和屏内轮询并发打同一个连接。
 *
 * 安全：Token/密码/动态码只进内存，绝不进 Store。
 */
public final class LoginView {

    private static final long POLL_MS = 3000L;

    public interface Host {
        void toast(String msg);

        /** 打开一块覆盖层虚拟屏（二维码 / 密码 / 网页验证）。 */
        void openScreen(VirtualScreen screen);

        /** 关闭当前覆盖层虚拟屏。 */
        void closeScreen();
    }

    private final Context ctx;
    private final Host host;
    private final Store store;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService pool = Executors.newSingleThreadExecutor();
    // 共享的、已连接的 NapCat 客户端 —— 连接一次，各虚拟屏复用。
    private final NapCatClient client = new NapCatClient(new RoutingTransport());

    private View root;
    private EditText address;
    private EditText token;
    private EditText totp;
    private Button connectBtn;
    private TextView connState;
    private TextView serverNote;
    private TextView manualNote;
    private TextView statusLine;
    private TextView detailLine;
    private Button qrEntry;
    private Button pwEntry;
    private Button smsEntry;
    private LinearLayout quickBox;

    private boolean polling;
    private int pollErrors;
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

        // ① 连接
        LinearLayout connCard = UiKit.card(ctx, "① 连接机器人的 NapCat 网页");
        LinearLayout c1 = UiKit.inner(connCard);
        serverNote = UiKit.text(ctx, "", 12, Theme.GOOD);
        c1.addView(serverNote);
        manualNote = UiKit.text(ctx, "填你服务器上 NapCat WebUI 的地址和 Token。"
                + "地址形如 1.2.3.4:6099；Token 在服务器 webui.json 里（或启动日志）。",
                12, Theme.DIM);
        c1.addView(manualNote);
        address = UiKit.input(ctx, "WebUI 地址，例如 1.2.3.4:6099", false);
        c1.addView(address);
        token = UiKit.input(ctx, "WebUI Token（不会保存）", true);
        c1.addView(token);
        totp = UiKit.input(ctx, "两步验证动态码（没开 2FA 就留空）", false);
        c1.addView(totp);
        connectBtn = UiKit.button(ctx, "连接", true);
        c1.addView(connectBtn);
        connState = UiKit.text(ctx, "未连接。", 12, Theme.DIM);
        c1.addView(connState);
        page.addView(connCard);

        // ② 状态（纯文本，无二维码位图）
        LinearLayout statusCard = UiKit.card(ctx, "② 机器人状态");
        LinearLayout c2 = UiKit.inner(statusCard);
        statusLine = UiKit.text(ctx, "还没连接。", 17, Theme.TEXT);
        statusLine.setTypeface(statusLine.getTypeface(), android.graphics.Typeface.BOLD);
        c2.addView(statusLine);
        detailLine = UiKit.text(ctx, "", 12, Theme.DIM);
        c2.addView(detailLine);
        page.addView(statusCard);

        // ③ 验证入口（点开才构建对应虚拟屏）
        LinearLayout loginCard = UiKit.card(ctx, "③ 登录方式（点开才构建，用完即释放）");
        LinearLayout c3 = UiKit.inner(loginCard);
        c3.addView(UiKit.text(ctx, "下面三种验证各是一块独立「虚拟屏」：点开才占内存，"
                + "关掉立刻释放，平时零占用。", 12, Theme.DIM));
        qrEntry = UiKit.button(ctx, "二维码登录", true);
        qrEntry.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                openQr();
            }
        });
        c3.addView(qrEntry);
        pwEntry = UiKit.button(ctx, "密码登录", false);
        pwEntry.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                openPw();
            }
        });
        c3.addView(pwEntry);
        smsEntry = UiKit.button(ctx, "短信 / 网页验证（验证码 · 两步验证）", false);
        smsEntry.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                openWebVerify();
            }
        });
        c3.addView(smsEntry);
        page.addView(loginCard);

        // ④ 快速登录 + 重启（轻量控件，留在中枢）
        LinearLayout quickCard = UiKit.card(ctx, "④ 快速登录 / 重启");
        LinearLayout c4 = UiKit.inner(quickCard);
        quickBox = UiKit.column(ctx);
        c4.addView(quickBox);
        quickBox.addView(UiKit.text(ctx, "服务器上登录过的 QQ 会列在这里，点一下直接登录。"
                + "「获取列表」在连接后可用。", 12, Theme.DIM));
        Button reloadQuick = UiKit.button(ctx, "获取快速登录列表", false);
        reloadQuick.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                loadQuickList();
            }
        });
        c4.addView(reloadQuick);
        Button restartBtn = UiKit.button(ctx, "重启 NapCat（卡死时用，会出新的二维码）", false);
        restartBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                confirmRestart();
            }
        });
        c4.addView(restartBtn);
        page.addView(quickCard);

        connectBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                connect();
            }
        });

        address.setText(store.webuiBase());
        setEntriesEnabled(false);
        root = scroll;
        return root;
    }

    private void setEntriesEnabled(boolean on) {
        if (qrEntry != null) {
            qrEntry.setEnabled(on);
            pwEntry.setEnabled(on);
            smsEntry.setEnabled(on);
        }
    }

    // ------------------------------------------------------------ 虚拟屏入口

    private void openQr() {
        if (!client.connected()) {
            host.toast("先连接");
            return;
        }
        stopPolling();   // 让二维码屏独占轮询，避免并发打同一连接
        host.openScreen(new QrScreen(client, new Runnable() {
            public void run() {
                // 登录成功：关屏 + 刷新中枢状态/账号/快速登录列表
                host.closeScreen();
                loadAccount();
                loadQuickList();
            }
        }));
    }

    private void openPw() {
        if (!client.connected()) {
            host.toast("先连接");
            return;
        }
        stopPolling();
        host.openScreen(new PwScreen(client, store, new PwScreen.OnNeedWebVerify() {
            public void run(boolean needCaptcha, boolean needNewDevice) {
                // 需要安全验证：关掉密码屏，转到网页验证屏
                host.closeScreen();
                openWebVerify();
            }
        }));
    }

    /** 短信 / 验证码 / 两步验证 → 网页验证虚拟屏（假 WebView 屏，用完即毁）。 */
    private void openWebVerify() {
        if (!client.connected()) {
            host.toast("先连接");
            return;
        }
        if (RoutingTransport.viaServer()) {
            final String inst = RoutingTransport.activeInstance();
            final String pw = RoutingTransport.activeUnlockPassword();
            final String mt = Session.client() != null ? Session.client().token() : "";
            final int port = Session.tunnel() != null ? Session.tunnel().port() : 0;
            smsEntry.setEnabled(false);
            pool.execute(new Runnable() {
                public void run() {
                    String tok = "";
                    try {
                        if (Session.client() != null) {
                            tok = Session.client().webuiToken(inst, pw);
                        }
                    } catch (Exception e) {
                        tok = "";
                    }
                    final String useTok = tok == null ? "" : tok;
                    onUi(new Runnable() {
                        public void run() {
                            if (smsEntry != null) {
                                smsEntry.setEnabled(true);
                            }
                            // 第三块虚拟屏：网页验证不再是独立 Activity，
                            // 和二维码/密码一样盖在主界面上（用户要求三大验证统一）。
                            host.openScreen(new WebScreen(
                                    "http://127.0.0.1", port, inst, mt, useTok));
                        }
                    });
                }
            });
            return;
        }
        String base = client.base().isEmpty() ? store.webuiBase() : client.base();
        if (base.isEmpty()) {
            host.toast("先填上面的 WebUI 地址");
            return;
        }
        host.openScreen(new WebScreen(base, 0, "", "", token.getText().toString().trim()));
    }

    private void confirmRestart() {
        if (!(ctx instanceof Activity)) {
            return;
        }
        if (!client.connected()) {
            host.toast("先连接");
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
                                    setStatus("已发送重启指令，等它起来后重新连接。", Theme.WARN);
                                } catch (NapCatClient.ApiError e) {
                                    setStatus("重启失败：" + e.getMessage(), Theme.BAD);
                                }
                            }
                        });
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    // ------------------------------------------------------------ 连接与轮询

    private void connect() {
        if (busy) {
            return;
        }
        final boolean via = RoutingTransport.viaServer();
        final String base = via ? "http://127.0.0.1"
                : NapCatClient.normalize(address.getText().toString());
        final String tok = via ? "" : token.getText().toString();
        final String code = via ? "" : totp.getText().toString();
        if (base.isEmpty()) {
            host.toast("请填 WebUI 地址");
            return;
        }
        if (!via && tok.isEmpty()) {
            host.toast("请填 WebUI Token");
            return;
        }
        if (via && RoutingTransport.activeInstance().isEmpty()) {
            host.toast("先回「机器人」页点「登录这个 QQ」");
            return;
        }
        busy = true;
        connectBtn.setEnabled(false);
        connState.setText("连接中…");
        connState.setTextColor(Theme.DIM);
        bg(new Runnable() {
            public void run() {
                String useTok = tok;
                if (via) {
                    try {
                        useTok = Session.client().webuiToken(
                                RoutingTransport.activeInstance(),
                                RoutingTransport.activeUnlockPassword());
                    } catch (Exception e) {
                        fail("拿不到这个机器人的 WebUI 凭据：" + e.getMessage());
                        return;
                    }
                    if (useTok == null || useTok.isEmpty()) {
                        fail("这个机器人还没跑起来，先回「机器人」页点「启动」。");
                        return;
                    }
                }
                client.configure(base, useTok, code);
                try {
                    client.login();
                } catch (NapCatClient.ApiError e) {
                    fail(e.getMessage());
                    return;
                }
                if (!via) {
                    store.setWebuiBase(base);
                }
                onUi(new Runnable() {
                    public void run() {
                        busy = false;
                        connectBtn.setEnabled(true);
                        connState.setText(via
                                ? "已连接：" + RoutingTransport.activeInstance()
                                : "已连接：" + base);
                        connState.setTextColor(Theme.GOOD);
                        token.setText("");
                        totp.setText("");
                        setEntriesEnabled(true);
                        host.toast("已连接。选一种登录方式打开虚拟屏。");
                    }
                });
                startPolling();
                loadQuickList();
            }
        });
    }

    /** 页面显示时调用（也用作虚拟屏关闭后的恢复入口）。 */
    public void onShow() {
        boolean via = RoutingTransport.viaServer();
        serverNote.setVisibility(via ? View.VISIBLE : View.GONE);
        manualNote.setVisibility(via ? View.GONE : View.VISIBLE);
        address.setVisibility(via ? View.GONE : View.VISIBLE);
        token.setVisibility(via ? View.GONE : View.VISIBLE);
        totp.setVisibility(via ? View.GONE : View.VISIBLE);
        connectBtn.setText(via ? "登录选中的机器人" : "连接");

        if (via) {
            String inst = RoutingTransport.activeInstance();
            serverNote.setText("正在操作「" + inst + "」（经服务器，无需填地址）。"
                    + "在「机器人」页点别的机器人可以切换。");
        } else if (Session.connected()) {
            serverNote.setVisibility(View.VISIBLE);
            serverNote.setText("已连服务器，但还没选机器人。"
                    + "回「机器人」页点「登录这个 QQ」。");
        }
        setEntriesEnabled(client.connected());
        if (client.connected()) {
            startPolling();
        }
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
                        polling = false;
                        onUi(new Runnable() {
                            public void run() {
                                connState.setText("连接断了（连续多次失败）：" + e.getMessage()
                                        + "。重新点「连接」即可。");
                                connState.setTextColor(Theme.BAD);
                                statusLine.setText("状态不明");
                                statusLine.setTextColor(Theme.WARN);
                                setEntriesEnabled(false);
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

    /** 只更新一行文本状态，不画二维码（二维码归 QrScreen）。 */
    private void renderStatus(final Map<String, Object> st) {
        final boolean isLogin = Json.bool(st, "isLogin", false);
        final boolean isOffline = Json.bool(st, "isOffline", false);
        final String phase = Json.str(st, "loginPhase", isLogin ? "ready" : "waiting_qrcode");
        final boolean wasLogin = this.lastOnline;
        this.lastOnline = isLogin;
        onUi(new Runnable() {
            public void run() {
                if (statusLine == null) {
                    return;
                }
                if (isLogin) {
                    statusLine.setText("在线");
                    statusLine.setTextColor(Theme.GOOD);
                    if (!wasLogin) {
                        loadAccount();
                    }
                    return;
                }
                if (isOffline) {
                    statusLine.setText("已掉线");
                    statusLine.setTextColor(Theme.BAD);
                    detailLine.setText("点「二维码登录」重新扫码，或用「快速登录」。");
                } else {
                    statusLine.setText(phaseText(phase));
                    statusLine.setTextColor(Theme.WARN);
                    detailLine.setText("");
                }
            }
        });
    }

    private boolean lastOnline;

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
        return "等待登录";
    }

    private void loadAccount() {
        bg(new Runnable() {
            public void run() {
                try {
                    final Map<String, Object> info = client.loginInfo();
                    onUi(new Runnable() {
                        public void run() {
                            if (detailLine == null) {
                                return;
                            }
                            String u = Json.str(info, "uin", Json.str(info, "uid", ""));
                            String nick = Json.str(info, "nick", Json.str(info, "nickname", ""));
                            if (!u.isEmpty() || !nick.isEmpty()) {
                                detailLine.setText("账号：" + nick + "（" + u + "）");
                                detailLine.setTextColor(Theme.GOOD);
                            }
                        }
                    });
                } catch (NapCatClient.ApiError ignored) {
                }
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
                        if (quickBox == null) {
                            return;
                        }
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
                    setStatus("已发起快速登录 " + account + "，等几秒看状态。", Theme.GOOD);
                } catch (NapCatClient.ApiError e) {
                    setStatus("快速登录失败：" + e.getMessage(), Theme.BAD);
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

    private void setStatus(final String msg, final int color) {
        onUi(new Runnable() {
            public void run() {
                if (statusLine != null) {
                    statusLine.setText(msg);
                    statusLine.setTextColor(color);
                }
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
}
