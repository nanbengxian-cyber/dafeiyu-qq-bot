package com.dafeiyu.controller;

import android.content.Context;
import android.graphics.Typeface;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 「机器人」页 —— 这是 App 的主界面。
 *
 * 设计目标：**只填三个配置就能把机器人跑起来**，而且能多开。
 *   ① 主聊天 API（接口地址 / Key / 模型名）
 *   ② 要聊天的群号和私聊 QQ 号
 *   ③ 人格提示词
 *
 * 其余一切（服务器地址、端口、容器、目录、Token）都由服务器侧自动分配，
 * 用户永远不用碰 —— 这是「降低门槛」的核心。
 *
 * 多开 = 一个「机器人」= 一个 QQ 号。列表里每行一个，各自独立配置，
 * 互不影响。点进去就能改那三个配置。
 *
 * 线程模型：所有网络/SSH 操作都在 pool 里跑，结果用 ui.post 回主线程 ——
 * 安卓上碰 UI 必须在主线程，这是硬规则。
 */
public final class RobotsView {

    public interface Host {
        void toast(String msg);

        /** 是否已经连上服务器（隧道通了 + 口令对）。 */
        boolean connected();

        /** 请宿主把界面切到「服务器」页，引导用户先连服务器。 */
        void gotoServerTab();

        /** 请宿主切到「登录 QQ」页 —— 用户点了某个机器人的「登录这个 QQ」。 */
        void gotoLoginTab();
    }

    private final Context ctx;
    private final Host host;
    private final ExecutorService pool = Executors.newSingleThreadExecutor();
    private final android.os.Handler ui = new android.os.Handler(
            android.os.Looper.getMainLooper());

    private ScrollView root;
    private LinearLayout listBox;
    private TextView statusLine;
    private EditText nameInput;
    private Button createBtn;
    private Button refreshBtn;

    /** 当前展开编辑的实例名（null = 都在折叠态）。 */
    private String expanded;

    public RobotsView(Context ctx, Host host) {
        this.ctx = ctx;
        this.host = host;
    }

    // ------------------------------------------------------------ 界面

    public View view() {
        root = UiKit.scroll(ctx);
        LinearLayout page = UiKit.pageColumn(ctx);
        root.addView(page);

        statusLine = UiKit.text(ctx, "还没连服务器", 12, Theme.DIM);
        page.addView(statusLine);

        // ---- 新建机器人 ----
        LinearLayout c1 = UiKit.card(ctx, "新建机器人");
        LinearLayout in1 = UiKit.inner(c1);
        in1.addView(UiKit.caption(ctx,
                "一个机器人 = 一个 QQ 号。想同时挂几个号，就建几个。"));
        nameInput = UiKit.input(ctx, "给它起个名字，如 qq1、test（字母数字）", false);
        in1.addView(nameInput);
        createBtn = UiKit.button(ctx, "新建", true);
        in1.addView(createBtn);
        page.addView(c1);

        // ---- 列表 ----
        LinearLayout c2 = UiKit.card(ctx, "我的机器人");
        LinearLayout in2 = UiKit.inner(c2);
        refreshBtn = UiKit.button(ctx, "刷新列表", false);
        in2.addView(refreshBtn);
        listBox = UiKit.column(ctx);
        in2.addView(listBox);
        page.addView(c2);

        createBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                doCreate();
            }
        });
        refreshBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                reload();
            }
        });

        setBusy(false);
        return root;
    }

    /** 页面被切到时调用：如果已连服务器就自动拉列表。 */
    public void onShow() {
        if (host.connected()) {
            reload();
        } else {
            statusLine.setText("还没连服务器 —— 先去「服务器」页连上。");
        }
    }

    private void setBusy(boolean busy) {
        UiKit.setEnabledDeep(createBtn, !busy);
        UiKit.setEnabledDeep(refreshBtn, !busy);
    }

    private ManagerClient client() throws Deployer.DeployException {
        ManagerClient c = Session.client();
        if (c == null) {
            throw new Deployer.DeployException("还没连服务器，先去「服务器」页连上。");
        }
        return c;
    }

    // ------------------------------------------------------------ 列表

    private void reload() {
        if (!host.connected()) {
            statusLine.setText("还没连服务器 —— 先去「服务器」页连上。");
            return;
        }
        setBusy(true);
        statusLine.setText("正在读取…");
        pool.execute(new Runnable() {
            public void run() {
                try {
                    final List<ManagerClient.Instance> items = client().list();
                    ui.post(new Runnable() {
                        public void run() {
                            renderList(items);
                            setBusy(false);
                        }
                    });
                } catch (final Deployer.DeployException e) {
                    ui.post(new Runnable() {
                        public void run() {
                            statusLine.setText("读取失败：" + e.getMessage());
                            setBusy(false);
                        }
                    });
                }
            }
        });
    }

    private void renderList(List<ManagerClient.Instance> items) {
        listBox.removeAllViews();
        if (items.isEmpty()) {
            statusLine.setText("还没有机器人 —— 在上面起个名字，点「新建」。");
            return;
        }
        // 选中的实例被删掉后，必须把选中状态清掉 ——
        // 否则登录页会一直往一个不存在的实例发请求，报「实例不存在」，
        // 而用户看不出是「选中的机器人已被删除」。
        if (!RoutingTransport.activeInstance().isEmpty()) {
            boolean still = false;
            for (ManagerClient.Instance x : items) {
                if (x.name.equals(RoutingTransport.activeInstance())) {
                    still = true;
                    break;
                }
            }
            if (!still) {
                RoutingTransport.setActiveInstance("");
            }
        }
        statusLine.setText("共 " + items.size() + " 个机器人。点「登录这个 QQ」去扫码。");
        for (ManagerClient.Instance it : items) {
            listBox.addView(rowFor(it));
        }
    }

    private View rowFor(final ManagerClient.Instance it) {
        LinearLayout box = UiKit.column(ctx);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = Theme.dp(ctx, 8);
        box.setLayoutParams(lp);

        // 标题行：名字 + 状态
        LinearLayout head = UiKit.row(ctx);
        TextView name = UiKit.text(ctx, it.name, 15, Theme.TEXT);
        name.setTypeface(name.getTypeface(), Typeface.BOLD);
        head.addView(name);
        TextView st = UiKit.text(ctx, "　" + it.stateText(), 12,
                it.running() ? 0xFF3FB950 : Theme.DIM);
        head.addView(st);
        box.addView(head);

        // 操作按钮
        LinearLayout btns = UiKit.row(ctx);
        btns.addView(smallBtn("展开配置", new View.OnClickListener() {
            public void onClick(View v) {
                expanded = expanded != null && expanded.equals(it.name) ? null : it.name;
                reload();
            }
        }));
        // 「登录这个 QQ」：把这个实例设成当前操作的实例，然后跳到登录页。
        // 登录页的请求会经管理服务代理到这个实例的 NapCat 上 ——
        // 用户不需要知道端口，也不需要填 WebUI 地址。
        btns.addView(smallBtn("登录这个 QQ", new View.OnClickListener() {
            public void onClick(View v) {
                RoutingTransport.setActiveInstance(it.name);
                host.toast("已选中「" + it.name + "」，去「登录 QQ」页扫码");
                host.gotoLoginTab();
            }
        }));
        if (!it.running()) {
            btns.addView(smallBtn("启动", new View.OnClickListener() {
                public void onClick(View v) {
                    act("start", it.name);
                }
            }));
        } else {
            btns.addView(smallBtn("停止", new View.OnClickListener() {
                public void onClick(View v) {
                    act("stop", it.name);
                }
            }));
        }
        box.addView(btns);

        // 展开：三配置
        if (expanded != null && expanded.equals(it.name)) {
            box.addView(configPanel(it));
        }
        return box;
    }

    private Button smallBtn(String text, View.OnClickListener l) {
        Button b = UiKit.button(ctx, text, false);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        lp.setMargins(Theme.dp(ctx, 2), Theme.dp(ctx, 4), Theme.dp(ctx, 2), 0);
        b.setLayoutParams(lp);
        b.setOnClickListener(l);
        return b;
    }

    // ------------------------------------------------------------ 三配置面板

    private final List<EditText> cfgFields = new ArrayList<EditText>();

    private View configPanel(final ManagerClient.Instance it) {
        cfgFields.clear();
        LinearLayout panel = UiKit.column(ctx);

        final EditText groups = UiKit.input(ctx, "群号，多个用逗号隔开", false);
        final EditText friends = UiKit.input(ctx, "私聊 QQ 号，多个用逗号隔开", false);
        final EditText apiBase = UiKit.input(ctx, "主聊天 API 接口地址，如 https://…/v1", false);
        final EditText apiKey = UiKit.input(ctx, "API Key（不回显，留空=不改）", true);
        final EditText apiModel = UiKit.input(ctx, "模型名，如 deepseek-chat", false);
        final EditText persona = UiKit.input(ctx, "人格提示词（它是谁、怎么说话）", false);

        panel.addView(UiKit.caption(ctx, "① 聊天范围"));
        panel.addView(groups);
        panel.addView(friends);
        panel.addView(UiKit.caption(ctx, "② 主聊天 API"));
        panel.addView(apiBase);
        panel.addView(apiKey);
        panel.addView(apiModel);
        panel.addView(UiKit.caption(ctx, "③ 人格提示词"));
        panel.addView(persona);

        final TextView note = UiKit.text(ctx, "正在读取当前配置…", 11, Theme.DIM);
        panel.addView(note);

        Button save = UiKit.button(ctx, "保存到服务器", true);
        panel.addView(save);

        // 先把现有配置读回来填进框里（Key 除外，服务器不回显）
        pool.execute(new Runnable() {
            public void run() {
                try {
                    final Map<String, Object> d = client().detail(it.name);
                    final Map<String, Object> cfg = Json.obj(d, "config");
                    ui.post(new Runnable() {
                        public void run() {
                            if (cfg == null || !Json.bool(cfg, "ready", false)) {
                                // 配置还没生成时有两种人，说的话正好相反：
                                //   started=false → 真没点过「启动」
                                //   started=true  → 刚点过，容器在拉镜像/初始化
                                // 不区分就会把刚点过启动的人打发回去反复点启动，
                                // 而他要做的其实只是「等一会儿」。
                                boolean started = cfg != null
                                        && Json.bool(cfg, "started", true);
                                note.setText(started
                                        ? "这个机器人正在初始化（第一次启动要拉镜像、建目录）。"
                                          + "配置生成后就能填了，稍等一两分钟再回来。"
                                        : "这个机器人还没启动过，配置要等它先跑起来一次。"
                                          + "点上面的「启动」，等十几秒再回来看。");
                                // 不禁用「保存」：服务器那边会先等 AstrBot 就绪再写，
                                // 刚点完启动就来填配置是完全正常的操作顺序。
                                save.setEnabled(true);
                                return;
                            }
                            groups.setText(join(Json.arr(cfg, "groups")));
                            friends.setText(join(Json.arr(cfg, "friends")));
                            apiBase.setText(Json.str(cfg, "api_base", ""));
                            apiModel.setText(Json.str(cfg, "api_model", ""));
                            persona.setText(Json.str(cfg, "persona", ""));
                            boolean keySet = Json.bool(cfg, "api_key_set", false);
                            apiKey.setHint(keySet
                                    ? "API Key 已设置（要换就填新的，留空=不改）"
                                    : "API Key（必填）");
                            note.setText(keySet
                                    ? "已读取当前配置。API Key 已在服务器上，这里不回显。"
                                    : "已读取当前配置。还没设 API Key。");
                        }
                    });
                } catch (final Deployer.DeployException e) {
                    ui.post(new Runnable() {
                        public void run() {
                            note.setText("读配置失败：" + e.getMessage());
                        }
                    });
                }
            }
        });

        save.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                final String g = groups.getText().toString().trim();
                final String f = friends.getText().toString().trim();
                final String ab = apiBase.getText().toString().trim();
                final String ak = apiKey.getText().toString().trim();
                final String am = apiModel.getText().toString().trim();
                final String pe = persona.getText().toString().trim();
                if (g.isEmpty() && f.isEmpty() && ab.isEmpty() && ak.isEmpty()
                        && am.isEmpty() && pe.isEmpty()) {
                    host.toast("什么都没填。");
                    return;
                }
                note.setText("正在写入…");
                UiKit.setEnabledDeep(save, false);
                pool.execute(new Runnable() {
                    public void run() {
                        try {
                            final List<String> changed = client().applyConfig(
                                    it.name, g, f, ab, ak, am, pe);
                            ui.post(new Runnable() {
                                public void run() {
                                    apiKey.setText("");
                                    note.setText("已写入并生效："
                                            + android.text.TextUtils.join("、", changed));
                                    host.toast("配置已生效（约 10 秒后机器人按新配置说话）");
                                    UiKit.setEnabledDeep(save, true);
                                }
                            });
                        } catch (final Deployer.DeployException e) {
                            ui.post(new Runnable() {
                                public void run() {
                                    note.setText("写入失败：" + e.getMessage());
                                    UiKit.setEnabledDeep(save, true);
                                }
                            });
                        }
                    }
                });
            }
        });
        return panel;
    }

    private static String join(List<Object> arr) {
        StringBuilder sb = new StringBuilder();
        for (Object o : arr) {
            if (sb.length() > 0) {
                sb.append(",");
            }
            sb.append(String.valueOf(o));
        }
        return sb.toString();
    }

    // ------------------------------------------------------------ 动作

    private void doCreate() {
        final String name = nameInput.getText().toString().trim();
        if (name.isEmpty()) {
            host.toast("先给它起个名字。");
            return;
        }
        if (!name.matches("[a-z0-9][a-z0-9-]{0,30}")) {
            host.toast("名字只能用小写字母、数字和短横线。");
            return;
        }
        if (!host.connected()) {
            host.toast("先连服务器。");
            host.gotoServerTab();
            return;
        }
        setBusy(true);
        statusLine.setText("正在创建 " + name + "…");
        pool.execute(new Runnable() {
            public void run() {
                try {
                    client().create(name);
                    ui.post(new Runnable() {
                        public void run() {
                            nameInput.setText("");
                            host.toast("已创建。点它下面的「启动」把机器人跑起来。");
                            reload();
                        }
                    });
                } catch (final Deployer.DeployException e) {
                    ui.post(new Runnable() {
                        public void run() {
                            statusLine.setText("创建失败：" + e.getMessage());
                            setBusy(false);
                        }
                    });
                }
            }
        });
    }

    private void act(final String action, final String name) {
        setBusy(true);
        statusLine.setText("正在" + ("start".equals(action) ? "启动 " : "停止 ") + name + "…");
        pool.execute(new Runnable() {
            public void run() {
                try {
                    ManagerClient c = client();
                    if ("start".equals(action)) {
                        c.start(name);
                    } else {
                        c.stop(name);
                    }
                    ui.post(new Runnable() {
                        public void run() {
                            host.toast("start".equals(action)
                                    ? "已启动，容器要十几秒才起完。"
                                    : "已停止。");
                            reload();
                        }
                    });
                } catch (final Deployer.DeployException e) {
                    ui.post(new Runnable() {
                        public void run() {
                            statusLine.setText("操作失败：" + e.getMessage());
                            setBusy(false);
                        }
                    });
                }
            }
        });
    }
}
