package com.dafeiyu.controller;

import android.content.Context;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 「服务器」页 —— 建立到服务器的隧道。
 *
 * 定制版（Preset 里有预设）打开就是「一键连接」：地址、密钥、指纹、口令
 * 全在包里，用户点一下就行。这是「降低门槛」的关键一步 ——
 * 目标用户不需要知道什么是 SSH、什么是端口转发。
 *
 * 公开版则要手填这些，因为公开包里不能带任何真实服务器信息。
 *
 * 连上之后，App 与服务器之间就是一条加密隧道，后面所有操作
 * （建机器人、写配置、扫码登录）都走这条隧道。
 */
public final class ServerView {

    public interface Host {
        void toast(String msg);

        /** 连上/断开后通知宿主刷新其它页。 */
        void onConnectionChanged();
    }

    private final Context ctx;
    private final Host host;
    private final ExecutorService pool = Executors.newSingleThreadExecutor();
    private final android.os.Handler ui = new android.os.Handler(
            android.os.Looper.getMainLooper());

    private TextView statusLine;
    private TextView logView;
    private Button connectBtn;
    private Button disconnectBtn;
    private EditText hostInput;
    private EditText portInput;
    private EditText userInput;
    private EditText keyInput;
    private EditText fpInput;
    private EditText tokenInput;
    private LinearLayout manualBox;

    public ServerView(Context ctx, Host host) {
        this.ctx = ctx;
        this.host = host;
    }

    public View view() {
        android.widget.ScrollView scroll = UiKit.scroll(ctx);
        LinearLayout page = UiKit.pageColumn(ctx);
        scroll.addView(page);

        statusLine = UiKit.text(ctx, "未连接", 13, Theme.DIM);
        page.addView(statusLine);

        // ---- 预设模式：一键连接 ----
        if (Preset.usable()) {
            LinearLayout c = UiKit.card(ctx, "连接服务器");
            LinearLayout in = UiKit.inner(c);
            in.addView(UiKit.caption(ctx,
                    "这个版本已经内置了服务器信息，点下面的按钮就行。"));
            connectBtn = UiKit.button(ctx, "连接", true);
            in.addView(connectBtn);
            disconnectBtn = UiKit.button(ctx, "断开", false);
            in.addView(disconnectBtn);
            if (!Preset.HINT.isEmpty()) {
                in.addView(UiKit.text(ctx, Preset.HINT, 11, Theme.DIM));
            }
            page.addView(c);
        } else {
            // ---- 公开版：手填 ----
            LinearLayout c = UiKit.card(ctx, "连接服务器");
            LinearLayout in = UiKit.inner(c);
            in.addView(UiKit.caption(ctx,
                    "填你自己服务器上的信息。服务器要先装好管理服务（见教程）。"));
            manualBox = UiKit.column(ctx);
            hostInput = UiKit.input(ctx, "服务器地址", false);
            portInput = UiKit.input(ctx, "SSH 端口，默认 22", false);
            userInput = UiKit.input(ctx, "SSH 账号", false);
            keyInput = UiKit.input(ctx, "RSA 私钥（PEM，整段粘贴）", false);
            fpInput = UiKit.input(ctx, "服务器指纹 SHA256:…（建议填）", false);
            tokenInput = UiKit.input(ctx, "管理口令", true);
            manualBox.addView(hostInput);
            manualBox.addView(portInput);
            manualBox.addView(userInput);
            manualBox.addView(keyInput);
            manualBox.addView(fpInput);
            manualBox.addView(tokenInput);
            in.addView(manualBox);
            connectBtn = UiKit.button(ctx, "连接", true);
            in.addView(connectBtn);
            disconnectBtn = UiKit.button(ctx, "断开", false);
            in.addView(disconnectBtn);
            page.addView(c);
        }

        // ---- 日志 ----
        LinearLayout c2 = UiKit.card(ctx, "连接日志");
        LinearLayout in2 = UiKit.inner(c2);
        logView = UiKit.mono(ctx, "（还没开始）");
        in2.addView(logView);
        page.addView(c2);

        connectBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                connect();
            }
        });
        disconnectBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                Session.close();
                log("已断开。");
                refreshStatus();
                host.onConnectionChanged();
            }
        });

        refreshStatus();
        return scroll;
    }

    /** 页面被切到时调用。 */
    public void onShow() {
        refreshStatus();
        // 定制版打开就自动连一次 —— 用户不用点任何东西
        if (Preset.usable() && !Session.connected()) {
            connect();
        }
    }

    private void refreshStatus() {
        if (Session.connected()) {
            Tunnel t = Session.tunnel();
            statusLine.setText("已连接（隧道端口 " + (t == null ? "?" : t.port()) + "）");
            statusLine.setTextColor(0xFF3FB950);
        } else {
            statusLine.setText("未连接");
            statusLine.setTextColor(Theme.DIM);
        }
    }

    private void log(String msg) {
        final String cur = logView.getText().toString();
        final String next = "（还没开始）".equals(cur) ? msg : cur + "\n" + msg;
        // 只留最后 30 行，免得越滚越长
        String[] lines = next.split("\n");
        if (lines.length > 30) {
            StringBuilder sb = new StringBuilder();
            for (int i = lines.length - 30; i < lines.length; i++) {
                sb.append(lines[i]).append("\n");
            }
            logView.setText(sb.toString().trim());
        } else {
            logView.setText(next);
        }
    }

    private void connect() {
        UiKit.setEnabledDeep(connectBtn, false);
        log("正在连接…");

        final String h, u, key, fp, tok;
        final int p;
        if (Preset.usable()) {
            h = Preset.HOST;
            p = Preset.SSH_PORT;
            u = Preset.SSH_USER;
            key = Preset.SSH_KEY;
            fp = Preset.HOST_FINGERPRINT;
            tok = Preset.MANAGER_TOKEN;
        } else {
            h = hostInput.getText().toString().trim();
            u = userInput.getText().toString().trim();
            key = keyInput.getText().toString().trim();
            fp = fpInput.getText().toString().trim();
            tok = tokenInput.getText().toString().trim();
            int pp;
            try {
                pp = Integer.parseInt(portInput.getText().toString().trim());
            } catch (NumberFormatException e) {
                pp = 22;
            }
            p = pp;
            if (h.isEmpty() || u.isEmpty() || key.isEmpty() || tok.isEmpty()) {
                log("地址、账号、私钥、口令都要填。");
                UiKit.setEnabledDeep(connectBtn, true);
                return;
            }
        }

        pool.execute(new Runnable() {
            public void run() {
                Tunnel t = new Tunnel();
                try {
                    int localPort = t.open(h, p, u, key.getBytes(), ManagerClient.REMOTE_PORT,
                            fp, new Tunnel.Log() {
                                public void line(String msg) {
                                    final String m = msg;
                                    ui.post(new Runnable() {
                                        public void run() {
                                            log(m);
                                        }
                                    });
                                }
                            });
                    ManagerClient c = new ManagerClient(localPort, tok);
                    c.health();     // 确认口令对、服务在
                    Session.set(t, c);
                    ui.post(new Runnable() {
                        public void run() {
                            log("连接成功 ✓");
                            refreshStatus();
                            UiKit.setEnabledDeep(connectBtn, true);
                            host.onConnectionChanged();
                        }
                    });
                } catch (final Deployer.DeployException e) {
                    t.close();
                    ui.post(new Runnable() {
                        public void run() {
                            log("失败：" + e.getMessage());
                            refreshStatus();
                            UiKit.setEnabledDeep(connectBtn, true);
                        }
                    });
                }
            }
        });
    }
}
