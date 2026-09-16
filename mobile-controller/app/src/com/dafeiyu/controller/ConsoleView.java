package com.dafeiyu.controller;

import android.app.Activity;
import android.content.Context;
import android.graphics.Typeface;
import android.text.InputType;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 「控制台」页 —— 桌面控制台三步流程的手机版：
 *   ① 填服务器与仓库配置 → ② 测试连接 / 开始部署（SSH 自动下载源码并启动）
 *   → ③ 配置与状态（容器状态 + 仓库下发的旋钮清单，行级写回 robot.env）。
 *
 * 与桌面版同一条安全边界：
 * - 密码只在内存，保存配置时走 safeProfile 白名单；
 * - 只管理带 .dafeiyu-managed 标记的目录；
 * - 部署日志全部脱敏后才上屏。
 */
public final class ConsoleView {

    public interface Host {
        void toast(String msg);
    }

    private final Context ctx;
    private final Host host;
    private final Store store;
    private final ExecutorService pool = Executors.newSingleThreadExecutor();
    private final android.os.Handler ui = new android.os.Handler(
            android.os.Looper.getMainLooper());

    private View root;
    private final Map<String, EditText> inputs = new LinkedHashMap<String, EditText>();
    private final Map<String, CheckBox> checks = new LinkedHashMap<String, CheckBox>();
    private TextView statusLine;
    private TextView containerText;
    private LinearLayout knobsBox;
    private TextView knobsNote;
    private TextView logView;
    private Button testBtn;
    private Button deployBtn;
    private Button statusBtn;
    private Button reloadBtn;
    private Button applyBtn;
    private Button chatBtn;
    private TextView chatResult;
    private boolean busy;

    private final List<KnobRow> knobRows = new ArrayList<KnobRow>();
    private String envText = "";

    private static final class KnobRow {
        final Knobs.Knob knob;
        final EditText edit;
        final CheckBox check;
        final Spinner spinner;

        KnobRow(Knobs.Knob knob, EditText edit, CheckBox check, Spinner spinner) {
            this.knob = knob;
            this.edit = edit;
            this.check = check;
            this.spinner = spinner;
        }
    }

    public ConsoleView(Context ctx, Host host, Store store) {
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

        page.addView(UiKit.text(ctx, "三步：填配置 → 测试连接 → 开始部署。"
                + "部署完成后到「登录」页扫码或用密码登录 QQ。", 12, Theme.DIM));

        // ① 服务器连接
        LinearLayout c1 = UiKit.column(ctx);
        inputs.put("host", UiKit.input(ctx, "服务器地址（IP 或域名，不带 http://）", false));
        inputs.put("port", UiKit.input(ctx, "SSH 端口（通常 22）", false));
        inputs.get("port").setInputType(InputType.TYPE_CLASS_NUMBER);
        inputs.put("username", UiKit.input(ctx, "SSH 用户名（一般是 root）", false));
        inputs.put("password", UiKit.input(ctx, "SSH 密码（只留在内存，不保存）", true));
        c1.addView(inputs.get("host"));
        c1.addView(inputs.get("port"));
        c1.addView(inputs.get("username"));
        c1.addView(inputs.get("password"));
        CheckBox trust = new CheckBox(ctx);
        trust.setText("首次连接接受服务器指纹");
        trust.setTextColor(Theme.TEXT);
        trust.setTextSize(13);
        checks.put("trust_new_host", trust);
        c1.addView(trust);
        page.addView(cardWith("① 服务器连接", c1));

        // ② 源码与部署
        LinearLayout c2 = UiKit.column(ctx);
        inputs.put("repo_url", UiKit.input(ctx, "源码仓库地址（HTTPS，不带账号/Token）", false));
        inputs.get("repo_url").setText(DeployConfig.DEFAULT_REPO);
        inputs.put("repo_ref", UiKit.input(ctx, "分支或标签（例如 main）", false));
        inputs.get("repo_ref").setText("main");
        inputs.put("deploy_dir", UiKit.input(ctx, "服务器部署目录（默认 ~/dafeiyu-bot）", false));
        inputs.get("deploy_dir").setText("~/dafeiyu-bot");
        c2.addView(inputs.get("repo_url"));
        c2.addView(inputs.get("repo_ref"));
        c2.addView(inputs.get("deploy_dir"));
        CheckBox deps = new CheckBox(ctx);
        deps.setText("自动安装 Git/Docker（需要 root 或免密 sudo）");
        deps.setTextColor(Theme.TEXT);
        deps.setTextSize(13);
        deps.setChecked(true);
        checks.put("install_dependencies", deps);
        c2.addView(deps);
        page.addView(cardWith("② 源码与部署", c2));

        // ③ 服务端口
        LinearLayout c3 = UiKit.column(ctx);
        String[][] ports = {
                {"napcat_port", "NapCat 管理端口（默认 3001）", "3001"},
                {"astrbot_port", "AstrBot 管理端口（默认 6185）", "6185"},
                {"astrbot_api_port", "AstrBot API 端口（默认 6186）", "6186"},
                {"bind_address", "监听地址（0.0.0.0 或服务器内网 IP）", "0.0.0.0"},
        };
        for (String[] p : ports) {
            inputs.put(p[0], UiKit.input(ctx, p[1], false));
            inputs.get(p[0]).setText(p[2]);
            c3.addView(inputs.get(p[0]));
        }
        page.addView(cardWith("③ 服务端口", c3));

        // ④ 高级
        LinearLayout c4 = UiKit.column(ctx);
        inputs.put("napcat_image", UiKit.input(ctx, "NapCat 镜像", false));
        inputs.get("napcat_image").setText("mlikiowa/napcat-docker:latest");
        inputs.put("astrbot_image", UiKit.input(ctx, "AstrBot 镜像", false));
        inputs.get("astrbot_image").setText("soulter/astrbot:latest");
        c4.addView(inputs.get("napcat_image"));
        c4.addView(inputs.get("astrbot_image"));
        page.addView(cardWith("④ 高级", c4));

        // ⑤ 聊天范围与主聊天 API（都写进服务器上的 AstrBot 配置）
        // 放在部署按钮之后：这两块写的是 AstrBot 的 cmd_config.json，
        // 而那个文件要等容器第一次跑起来才有。
        LinearLayout c5chat = UiKit.column(ctx);
        c5chat.addView(UiKit.text(ctx, "填完点下面的按钮写进服务器。机器人只会在你填的群/私聊里说话，"
                + "主聊天 API 用你自己的（App 不带任何 Key）。", 12, Theme.DIM));
        inputs.put("chat_groups", UiKit.input(ctx, "允许说话的群号（多个用逗号隔开）", false));
        inputs.put("chat_friends", UiKit.input(ctx, "允许说话的私聊 QQ 号（多个用逗号隔开）", false));
        c5chat.addView(inputs.get("chat_groups"));
        c5chat.addView(inputs.get("chat_friends"));
        inputs.put("api_base", UiKit.input(ctx, "主聊天 API 接口地址（如 https://api.xxx.com/v1）", false));
        inputs.put("api_key", UiKit.input(ctx, "主聊天 API Key（只上传到你的服务器，不保存）", true));
        inputs.put("api_model", UiKit.input(ctx, "主聊天模型名（如 deepseek-v4-flash）", false));
        c5chat.addView(inputs.get("api_base"));
        c5chat.addView(inputs.get("api_key"));
        c5chat.addView(inputs.get("api_model"));
        chatBtn = UiKit.button(ctx, "写入服务器", true);
        c5chat.addView(chatBtn);
        chatResult = UiKit.text(ctx, "还没写入。", 12, Theme.DIM);
        c5chat.addView(chatResult);
        page.addView(cardWith("⑤ 聊天范围与主聊天 API", c5chat));

        // 状态
        LinearLayout c5 = UiKit.column(ctx);
        containerText = UiKit.mono(ctx, "还没有读取到运行状态，请点「刷新状态」。");
        c5.addView(containerText);
        statusBtn = UiKit.button(ctx, "刷新状态", false);
        c5.addView(statusBtn);
        page.addView(cardWith("运行状态", c5));

        // 配置
        LinearLayout c6 = UiKit.column(ctx);
        knobsNote = UiKit.text(ctx, "先完成部署，再点「刷新配置」读取配置项。",
                12, Theme.DIM);
        c6.addView(knobsNote);
        knobsBox = UiKit.column(ctx);
        c6.addView(knobsBox);
        reloadBtn = UiKit.button(ctx, "刷新配置", false);
        applyBtn = UiKit.button(ctx, "应用配置", true);
        c6.addView(reloadBtn);
        c6.addView(applyBtn);
        page.addView(cardWith("配置", c6));

        // 日志
        LinearLayout c7 = UiKit.column(ctx);
        ScrollView logScroll = new ScrollView(ctx);
        logScroll.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, Theme.dp(ctx, 200)));
        logView = UiKit.mono(ctx, "");
        logView.setTextSize(11);
        logScroll.addView(logView);
        c7.addView(logScroll);
        page.addView(cardWith("部署日志（已脱敏）", c7));

        testBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                runOp("test");
            }
        });
        deployBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                confirmDeploy();
            }
        });
        statusBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                runOp("status");
            }
        });
        reloadBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                runOp("config");
            }
        });
        applyBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                applyConfig();
            }
        });
        chatBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                confirmChatSetup();
            }
        });

        loadProfile();
        root = scroll;
        return root;
    }

    /** card() 返回外层，这里把已填充的内容列包回卡片。 */
    private LinearLayout cardWith(String title, LinearLayout inner) {
        LinearLayout card = UiKit.card(ctx, null);
        LinearLayout content = UiKit.inner(card);
        content.removeAllViews();
        content.addView(UiKit.caption(ctx, title));
        for (int i = 0; i < inner.getChildCount(); i++) {
            View child = inner.getChildAt(i);
            inner.removeView(child);
            content.addView(child);
            i--;
        }
        return card;
    }

    // ------------------------------------------------------------ 配置读写

    private Map<String, String> values() {
        Map<String, String> v = new LinkedHashMap<String, String>();
        for (Map.Entry<String, EditText> e : inputs.entrySet()) {
            v.put(e.getKey(), e.getValue().getText().toString());
        }
        for (Map.Entry<String, CheckBox> e : checks.entrySet()) {
            v.put(e.getKey(), e.getValue().isChecked() ? "1" : "0");
        }
        return v;
    }

    private void loadProfile() {
        Map<String, String> saved = store.profile();
        for (Map.Entry<String, EditText> e : inputs.entrySet()) {
            String v = saved.get(e.getKey());
            if (v != null && !v.isEmpty() && !e.getKey().equals("password")) {
                e.getValue().setText(v);
            }
        }
        for (Map.Entry<String, CheckBox> e : checks.entrySet()) {
            String v = saved.get(e.getKey());
            if (v != null) {
                e.getValue().setChecked("1".equals(v));
            }
        }
    }

    private void saveProfile() {
        try {
            Map<String, String> v = values();
            DeployConfig cfg = DeployConfig.from(v);   // 校验顺便做掉
            Map<String, String> safe = cfg.safeProfile();
            // 聊天范围与 API 的**非密钥**部分也记住（下次不用重填）；
            // API Key 不在此列 —— 它和 SSH 密码一样只进内存。
            safe.putAll(ChatSetup.persistable(textOf("chat_groups"), textOf("chat_friends"),
                    textOf("api_base"), textOf("api_model")));
            store.saveProfile(safe);
        } catch (Deployer.DeployException e) {
            // 校验失败就不保存，不打断当前操作
        }
    }

    private DeployConfig config() throws Deployer.DeployException {
        return DeployConfig.from(values());
    }

    /** Ssh 的指纹存取接到 Store（首次连接记住，之后逐次校验）。 */
    private Ssh.HostKeys hostKeys() {
        return new Ssh.HostKeys() {
            public String get(String host, int port) {
                return store.hostKey(host, port);
            }

            public void put(String host, int port, String type, String base64Key) {
                store.rememberHostKey(host, port, type, base64Key);
            }
        };
    }

    // ------------------------------------------------------------ 操作

    private void confirmDeploy() {
        Map<String, String> v = values();
        final DeployConfig cfg;
        try {
            cfg = DeployConfig.from(v);
        } catch (Deployer.DeployException e) {
            host.toast(e.getMessage());
            return;
        }
        if (!(ctx instanceof Activity)) {
            return;
        }
        new android.app.AlertDialog.Builder(ctx)
                .setTitle("开始部署？")
                .setMessage("将在服务器 " + cfg.host + " 的 " + cfg.deployDir
                        + " 目录下载源码并启动容器。\n\n继续吗？")
                .setPositiveButton("部署", new android.content.DialogInterface.OnClickListener() {
                    public void onClick(android.content.DialogInterface d, int w) {
                        runOp("deploy");
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    /** 「写入服务器」前的确认：把要写的东西原样念一遍（API Key 只说「已填」）。 */
    private void confirmChatSetup() {
        final ChatSetup.Request req;
        try {
            req = ChatSetup.Request.parse(
                    textOf("chat_groups"), textOf("chat_friends"),
                    textOf("api_base"), textOf("api_key"), textOf("api_model"));
        } catch (ChatSetup.SetupException e) {
            host.toast(e.getMessage());
            return;
        }
        StringBuilder msg = new StringBuilder();
        if (req.scopeApply) {
            msg.append("聊天范围：");
            if (!req.groups.isEmpty()) {
                msg.append("群 ").append(join(req.groups));
            }
            if (!req.friends.isEmpty()) {
                if (!req.groups.isEmpty()) {
                    msg.append("；");
                }
                msg.append("私聊 ").append(join(req.friends));
            }
            msg.append("\n（打开白名单后，机器人只在这些会话里说话）\n\n");
        }
        if (req.apiApply) {
            msg.append("主聊天 API：").append(req.apiBase)
                    .append("\n模型：").append(req.apiModel)
                    .append("\nAPI Key：已填（只上传到你的服务器，不保存、不回显）\n");
        }
        if (!(ctx instanceof Activity)) {
            return;
        }
        new android.app.AlertDialog.Builder(ctx)
                .setTitle("写入服务器？")
                .setMessage(msg.toString())
                .setPositiveButton("写入", new android.content.DialogInterface.OnClickListener() {
                    public void onClick(android.content.DialogInterface d, int w) {
                        runChatSetup(req);
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void runChatSetup(final ChatSetup.Request req) {
        if (busy) {
            host.toast("还有操作在跑，等一下");
            return;
        }
        final DeployConfig cfg;
        try {
            cfg = config();
        } catch (Deployer.DeployException e) {
            host.toast(e.getMessage());
            return;
        }
        busy = true;
        setBusy(true, "正在写入服务器…");
        chatResult.setTextColor(Theme.DIM);
        chatResult.setText("正在写入…");
        pool.execute(new Runnable() {
            public void run() {
                final Deployer d = new Deployer(cfg, new Deployer.LineSink() {
                    public void line(String s) {
                        appendLog(Deployer.redact(s, new String[]{cfg.password, req.apiKey}));
                    }
                }, new Deployer.TransportFactory() {
                    public Deployer.Transport open(DeployConfig c) throws Deployer.DeployException {
                        Ssh ssh = new Ssh(c, hostKeys());
                        ssh.connect();
                        return ssh;
                    }
                });
                try {
                    Map<String, Object> result = d.applyChatSetup(req);
                    final String text = ChatSetup.describe(result);
                    onUi(new Runnable() {
                        public void run() {
                            chatResult.setTextColor(Theme.GOOD);
                            chatResult.setText("已写入服务器：\n" + text
                                    + "\n（AstrBot 要重启一次才读新配置）");
                        }
                    });
                    if (req.apiApply || req.scopeApply) {
                        d.progress("重启 AstrBot 让新配置生效");
                        d.recreate("astrbot");
                        onUi(new Runnable() {
                            public void run() {
                                chatResult.append("\nAstrBot 已重启，新配置已生效。");
                            }
                        });
                    }
                    onUiDone("写入完成。");
                } catch (Deployer.DeployException e) {
                    appendLog("错误：" + e.getMessage());
                    onUi(new Runnable() {
                        public void run() {
                            chatResult.setTextColor(Theme.BAD);
                            chatResult.setText("写入失败：" + e.getMessage());
                        }
                    });
                    onUiFail(e.getMessage());
                } catch (RuntimeException e) {
                    appendLog("错误：发生未预期的错误，请稍后重试。");
                    onUiFail("发生未预期的错误，请稍后重试。");
                } finally {
                    d.close();
                }
            }
        });
    }

    private String textOf(String key) {
        EditText e = inputs.get(key);
        return e == null ? "" : e.getText().toString();
    }

    private static String join(List<String> items) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < items.size(); i++) {
            if (i > 0) {
                sb.append('、');
            }
            sb.append(items.get(i));
        }
        return sb.toString();
    }

    private void runOp(final String op) {        if (busy) {
            host.toast("还有操作在跑，等一下");
            return;
        }
        final DeployConfig cfg;
        try {
            cfg = config();
        } catch (Deployer.DeployException e) {
            host.toast(e.getMessage());
            return;
        }
        busy = true;
        setBusy(true, "deploy".equals(op) ? "正在部署…" : "正在处理…");
        if ("deploy".equals(op)) {
            appendLog("开始部署到 " + cfg.host);
        }
        pool.execute(new Runnable() {
            public void run() {
                final Deployer d = new Deployer(cfg, new Deployer.LineSink() {
                    public void line(String s) {
                        appendLog(Deployer.redact(s, new String[]{cfg.password}));
                    }
                }, new Deployer.TransportFactory() {
                    public Deployer.Transport open(DeployConfig c) throws Deployer.DeployException {
                        Ssh ssh = new Ssh(c, hostKeys());
                        ssh.connect();
                        return ssh;
                    }
                });
                try {
                    d.open();
                    if ("test".equals(op)) {
                        Map<String, String> info = d.checkEnvironment();
                        appendLog(Deployer.formatEnvironment(info));
                        onUiDone("连接成功。");
                        return;
                    }
                    if ("deploy".equals(op)) {
                        d.progress("检查服务器环境");
                        appendLog(Deployer.formatEnvironment(d.checkEnvironment()));
                        d.progress("下载源码并启动服务");
                        d.deploy();
                        d.progress("读取配置与状态");
                        Object schema = d.readKnobSchema();
                        final String env = d.readEnv();
                        final List<Map<String, Object>> containers = d.status();
                        final Object schemaF = schema;
                        onUi(new Runnable() {
                            public void run() {
                                envText = env;
                                renderKnobs(schemaF);
                                containerText.setText(Deployer.formatContainers(containers));
                            }
                        });
                        onUiDone("部署完成。下一步：到「登录」页扫码或用密码登录 QQ。");
                        return;
                    }
                    if ("status".equals(op)) {
                        final List<Map<String, Object>> containers = d.status();
                        onUi(new Runnable() {
                            public void run() {
                                containerText.setText(Deployer.formatContainers(containers));
                            }
                        });
                        onUiDone("状态已刷新。");
                        return;
                    }
                    if ("config".equals(op)) {
                        final Object schema = d.readKnobSchema();
                        final String env = d.readEnv();
                        final List<Map<String, Object>> containers = d.status();
                        onUi(new Runnable() {
                            public void run() {
                                envText = env;
                                renderKnobs(schema);
                                containerText.setText(Deployer.formatContainers(containers));
                            }
                        });
                        onUiDone("已读取配置。");
                        return;
                    }
                } catch (Deployer.DeployException e) {
                    appendLog("错误：" + e.getMessage());
                    onUiFail(e.getMessage());
                    return;
                } catch (RuntimeException e) {
                    appendLog("错误：发生未预期的错误，请稍后重试。");
                    onUiFail("发生未预期的错误，请稍后重试。");
                    return;
                } finally {
                    d.close();
                }
            }
        });
    }

    private void applyConfig() {
        if (knobRows.isEmpty()) {
            host.toast("还没有配置项，请先完成部署或刷新配置。");
            return;
        }
        Map<String, String> updates = new LinkedHashMap<String, String>();
        try {
            for (KnobRow row : knobRows) {
                String value;
                if (row.knob.secret) {
                    String typed = row.edit.getText().toString();
                    if (typed.trim().isEmpty()) {
                        continue;   // 留空 = 不修改
                    }
                    value = Knobs.coerceValue(row.knob, typed);
                } else if (row.check != null) {
                    value = row.check.isChecked() ? "1" : "0";
                } else if (row.spinner != null) {
                    value = (String) row.spinner.getSelectedItem();
                } else {
                    value = row.edit.getText().toString();
                }
                updates.put(row.knob.key, value);
            }
        } catch (Knobs.KnobException e) {
            host.toast(e.getMessage());
            return;
        }
        final Knobs.ApplyResult result;
        try {
            result = Knobs.applyEnvText(envText, updates);
        } catch (Knobs.KnobException e) {
            host.toast(e.getMessage());
            return;
        }
        if (result.changed.isEmpty()) {
            host.toast("配置没有变化。");
            return;
        }
        boolean needsRestart = false;
        for (String key : result.changed.keySet()) {
            for (KnobRow row : knobRows) {
                if (row.knob.key.equals(key) && row.knob.restart) {
                    needsRestart = true;
                }
            }
        }
        final boolean restart = needsRestart;
        final DeployConfig cfg;
        try {
            cfg = config();
        } catch (Deployer.DeployException e) {
            host.toast(e.getMessage());
            return;
        }
        busy = true;
        setBusy(true, "正在保存配置…");
        pool.execute(new Runnable() {
            public void run() {
                Deployer d = new Deployer(cfg, null, new Deployer.TransportFactory() {
                    public Deployer.Transport open(DeployConfig c) throws Deployer.DeployException {
                        Ssh ssh = new Ssh(c, hostKeys());
                        ssh.connect();
                        return ssh;
                    }
                });
                try {
                    d.open();
                    d.writeEnv(result.text);
                    if (restart) {
                        d.recreate("astrbot");
                    }
                    final String env = d.readEnv();
                    final List<Map<String, Object>> containers = d.status();
                    onUi(new Runnable() {
                        public void run() {
                            envText = env;
                            refreshKnobValues();
                            containerText.setText(Deployer.formatContainers(containers));
                        }
                    });
                    appendLog("已保存配置：" + joinKeys(result.changed));
                    onUiDone(restart ? "配置已保存，服务已重建生效。"
                            : "配置已保存并生效。");
                } catch (Deployer.DeployException e) {
                    appendLog("错误：" + e.getMessage());
                    onUiFail(e.getMessage());
                } finally {
                    d.close();
                }
            }
        });
    }

    private static String joinKeys(Map<String, String[]> changed) {
        List<String> keys = new ArrayList<String>(changed.keySet());
        java.util.Collections.sort(keys);
        StringBuilder sb = new StringBuilder();
        for (String k : keys) {
            if (sb.length() > 0) {
                sb.append("、");
            }
            sb.append(k);
        }
        return sb.toString();
    }

    // ------------------------------------------------------------ 旋钮渲染

    private void renderKnobs(Object schema) {
        knobsBox.removeAllViews();
        knobRows.clear();
        List<Knobs.Knob> knobs = Knobs.parse(schema);
        if (knobs.isEmpty()) {
            knobsNote.setText("服务器上没有配置清单（deploy/console-config.json）。"
                    + "请确认仓库里包含该文件，或先完成一次部署。");
            return;
        }
        knobsNote.setText("共 " + knobs.size() + " 个配置项。改完点「应用配置」；"
                + "标了「要重启」的会自动重建容器。");
        Map<String, String> values = Knobs.envValues(envText);
        for (String group : Knobs.groupOrder(knobs)) {
            knobsBox.addView(sectionTitle(group));
            for (Knobs.Knob knob : knobs) {
                if (!knob.group.equals(group)) {
                    continue;
                }
                knobsBox.addView(knobRow(knob, values.get(knob.key)));
            }
        }
    }

    private View sectionTitle(String title) {
        TextView t = UiKit.text(ctx, title, 13, Theme.ACCENT);
        t.setTypeface(t.getTypeface(), Typeface.BOLD);
        int pad = Theme.dp(ctx, 4);
        t.setPadding(pad, Theme.dp(ctx, 8), pad, pad);
        return t;
    }

    private View knobRow(Knobs.Knob knob, String raw) {
        LinearLayout row = new LinearLayout(ctx);
        row.setOrientation(LinearLayout.VERTICAL);
        int pad = Theme.dp(ctx, 4);
        row.setPadding(0, pad, 0, pad);

        TextView label = UiKit.text(ctx, knob.label
                + (knob.restart ? "（要重启）" : "（即时生效）"), 13, Theme.TEXT);
        row.addView(label);

        EditText edit = null;
        CheckBox check = null;
        Spinner spinner = null;
        if (knob.secret) {
            edit = UiKit.input(ctx, Knobs.secretState(knob, raw) + "；留空表示不修改", true);
            row.addView(edit);
        } else if ("bool".equals(knob.kind)) {
            check = new CheckBox(ctx);
            check.setText("开");
            check.setTextColor(Theme.TEXT);
            check.setChecked(Knobs.isEnabled(knob, raw));
            row.addView(check);
        } else if ("enum".equals(knob.kind) && !knob.options.isEmpty()) {
            spinner = new Spinner(ctx);
            List<String> opts = new ArrayList<String>(knob.options);
            ArrayAdapter<String> ad = new ArrayAdapter<String>(ctx,
                    android.R.layout.simple_spinner_item, opts);
            ad.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
            spinner.setAdapter(ad);
            String cur = Knobs.displayValue(knob, raw);
            int pos = opts.indexOf(cur);
            if (pos < 0) {
                opts.add(cur);
                pos = opts.size() - 1;
                ArrayAdapter<String> ad2 = new ArrayAdapter<String>(ctx,
                        android.R.layout.simple_spinner_item, opts);
                ad2.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
                spinner.setAdapter(ad2);
            }
            spinner.setSelection(pos);
            row.addView(spinner);
        } else {
            edit = UiKit.input(ctx, "", false);
            edit.setText(Knobs.displayValue(knob, raw));
            if ("int".equals(knob.kind)) {
                edit.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_SIGNED);
            } else if ("float".equals(knob.kind)) {
                edit.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_DECIMAL);
            }
            row.addView(edit);
        }
        if (knob.hint != null && !knob.hint.isEmpty()) {
            row.addView(UiKit.text(ctx, knob.hint, 11, Theme.DIM));
        }
        knobRows.add(new KnobRow(knob, edit, check, spinner));
        return row;
    }

    private void refreshKnobValues() {
        Map<String, String> values = Knobs.envValues(envText);
        for (KnobRow row : knobRows) {
            String raw = values.get(row.knob.key);
            if (row.check != null) {
                row.check.setChecked(Knobs.isEnabled(row.knob, raw));
            } else if (row.spinner != null && !row.knob.secret) {
                String cur = Knobs.displayValue(row.knob, raw);
                @SuppressWarnings("unchecked")
                ArrayAdapter<String> ad = (ArrayAdapter<String>) row.spinner.getAdapter();
                if (ad != null) {
                    int pos = ad.getPosition(cur);
                    if (pos >= 0) {
                        row.spinner.setSelection(pos);
                    }
                }
            } else if (row.edit != null && !row.knob.secret) {
                row.edit.setText(Knobs.displayValue(row.knob, raw));
            } else if (row.edit != null) {
                row.edit.setHint(Knobs.secretState(row.knob, raw) + "；留空表示不修改");
            }
        }
    }

    // ------------------------------------------------------------ 日志与状态

    private void appendLog(final String line) {
        onUi(new Runnable() {
            public void run() {
                String current = logView.getText().toString();
                String[] lines = current.isEmpty()
                        ? new String[0] : current.split("\n");
                StringBuilder sb = new StringBuilder();
                int start = Math.max(0, lines.length - 299);
                for (int i = start; i < lines.length; i++) {
                    sb.append(lines[i]).append('\n');
                }
                sb.append(line);
                logView.setText(sb.toString());
            }
        });
    }

    private void setBusy(final boolean on, final String text) {
        onUi(new Runnable() {
            public void run() {
                testBtn.setEnabled(!on);
                deployBtn.setEnabled(!on);
                statusBtn.setEnabled(!on);
                reloadBtn.setEnabled(!on);
                applyBtn.setEnabled(!on);
                statusLine.setText(text);
                statusLine.setTextColor(on ? Theme.WARN : Theme.TEXT);
            }
        });
    }

    private void onUiDone(final String msg) {
        onUi(new Runnable() {
            public void run() {
                busy = false;
                setBusy(false, msg);
                saveProfile();
            }
        });
    }

    private void onUiFail(final String msg) {
        onUi(new Runnable() {
            public void run() {
                busy = false;
                setBusy(false, "出错，请看下方日志。");
                host.toast(msg);
            }
        });
    }

    private void onUi(Runnable task) {
        ui.post(task);
    }
}
