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
    private EditText searchInput;
    private Button createBtn;
    private Button refreshBtn;

    /** 最近一次从服务器拉到的完整列表（搜索过滤在它上面做，不再发请求）。 */
    private List<ManagerClient.Instance> lastItems = new ArrayList<ManagerClient.Instance>();

    /** 当前展开编辑的实例名（null = 都在折叠态）。 */
    private String expanded;

    /** 已经解锁的私密实例（本次会话内有效，退出 App 就忘）。
     *
     *  只放密码在内存里、不落盘 —— 落盘就等于把锁的钥匙放在锁旁边。
     *  换页/刷新都还在，杀掉 App 就没了，这是刻意的。 */
    private final Map<String, String> unlockedPasswords = new java.util.HashMap<String, String>();

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

        // ---- 新手第一步：API 去哪申请 ----------------------------------------
        //
        // ★ 为什么要把这个提到最上面（用户反馈「还没有各大官网的API获取地址，
        //   那新手不知道在哪里获取怎么办？」）：
        //
        // 原来引导只藏在「某个机器人的展开配置」里。问题是 ——
        // **新手还没有机器人**，他根本没机会展开任何配置，也就永远看不到
        // 这份引导。功能「存在」但「不可达」，等于不存在。
        //
        // 而且「API 从哪来」是整条流程里唯一必须在**别的网站**完成的步骤，
        // 恰恰是最该放在最显眼位置的一步。所以提到首屏、独立成卡、
        // 默认就能看见（不用点开）。
        page.addView(buildApiCard());

        // ---- 列表 ----
        LinearLayout c2 = UiKit.card(ctx, "我的机器人");
        LinearLayout in2 = UiKit.inner(c2);
        refreshBtn = UiKit.button(ctx, "刷新列表", false);
        in2.addView(refreshBtn);

        // ── 按名字搜索 ──────────────────────────────────────────────────
        //
        // 需求原话：「机器人列表那里，是公开的，不是说不好，是不能定向搜索
        // 机器人名字」。机器人一多，列表就是一大坨，找一个得从头看到尾。
        //
        // 做法是**本地过滤**，不是发请求去搜：
        //   * 列表本来就已经全在手里了（一次 /instances 全拿到），
        //     再往服务器跑一趟纯属浪费 —— 而且要等网络，打字时卡顿明显；
        //   * 本地过滤是即时的，边打边筛。
        // 只有机器人数量大到一次拉不完时才需要服务端搜索，那个量级还很远。
        searchInput = UiKit.input(ctx, "搜索机器人名字（输几个字就筛出来）", false);
        LinearLayout.LayoutParams slp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        slp.topMargin = Theme.dp(ctx, 6);
        searchInput.setLayoutParams(slp);
        in2.addView(searchInput);
        searchInput.addTextChangedListener(new android.text.TextWatcher() {
            public void beforeTextChanged(CharSequence s, int a, int b, int c) {
            }

            public void onTextChanged(CharSequence s, int a, int b, int c) {
            }

            public void afterTextChanged(android.text.Editable e) {
                // 只重画列表，不再请求服务器 —— 打字不该触发网络。
                renderFiltered();
            }
        });

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
        lastItems = items;
        renderFiltered();
    }

    /**
     * 按搜索框内容过滤后重画列表。
     *
     * 抽出来是因为它有两个调用点：拉到新数据时、搜索框打字时。
     * 两处必须是同一套逻辑 —— 否则「刷新之后搜索结果变了样」这种
     * 不一致会很难查。
     */
    private void renderFiltered() {
        List<ManagerClient.Instance> items = lastItems;
        // 搜索框的监听器是在 listBox 之前挂上的，理论上首次构造时不会触发，
        // 但一旦哪天 UiKit.input 改成预置文本就会立刻 NPE。
        // 这里挡一下：宁可少画一次，也不要在用户面前崩。
        if (listBox == null || statusLine == null) {
            return;
        }
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

        String q = searchInput == null ? ""
                : searchInput.getText().toString().trim();
        // 过滤逻辑在 RobotFilter 里（那样才能进单测）。
        // 这里只是把结果套回界面。
        List<String> names = new ArrayList<String>();
        for (ManagerClient.Instance it : items) {
            names.add(it.name);
        }
        List<ManagerClient.Instance> shown = new ArrayList<ManagerClient.Instance>();
        for (Integer idx : RobotFilter.match(names, q)) {
            shown.add(items.get(idx));
        }

        if (shown.isEmpty()) {
            // ★ 空结果必须说清楚「是搜不到，不是没有机器人」——
            // 否则用户会以为机器人被删了，转头去重新建一个。
            statusLine.setText(RobotFilter.statusText(q, 0, items.size()));
            return;
        }
        statusLine.setText(RobotFilter.statusText(q, shown.size(), items.size()));
        for (ManagerClient.Instance it : shown) {
            listBox.addView(rowFor(it));
        }
    }

    private View rowFor(final ManagerClient.Instance it) {
        LinearLayout box = UiKit.column(ctx);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = Theme.dp(ctx, 8);
        box.setLayoutParams(lp);

        // 标题行：名字 + 状态 + 锁
        LinearLayout head = UiKit.row(ctx);
        TextView name = UiKit.text(ctx, it.name, 15, Theme.TEXT);
        name.setTypeface(name.getTypeface(), Typeface.BOLD);
        head.addView(name);
        if (it.locked) {
            // 锁图标：一眼看出哪个是私密的。
            // 用文字「🔒 私密」而不是只画个锁，是因为用户不一定认识图标含义，
            // 而「私密」两个字直接说明了它的性质。
            TextView lk = UiKit.text(ctx, "　🔒 私密", 12, Theme.WARN);
            head.addView(lk);
        }
        TextView st = UiKit.text(ctx, "　" + it.stateText(), 12,
                it.running() ? 0xFF3FB950 : Theme.DIM);
        head.addView(st);
        box.addView(head);

        // 私密机器人还没解锁时，**配置面板直接不给展开** ——
        // 光靠「展开后不显示内容」不够：用户会以为面板坏了。
        // 这里换成一句明确的「锁着，点解锁」，并给解锁按钮。
        if (it.locked && !unlockedPasswords.containsKey(it.name)) {
            LinearLayout lockedBox = UiKit.column(ctx);
            lockedBox.addView(UiKit.text(ctx,
                    "这个机器人设了私密，配置要输密码才能看和改。",
                    12, Theme.DIM));
            Button un = UiKit.button(ctx, "输入密码解锁", true);
            un.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) {
                    askPassword(it, null);
                }
            });
            lockedBox.addView(un);
            box.addView(lockedBox);
        } else {
            // 操作按钮
            LinearLayout btns = UiKit.row(ctx);
            btns.addView(smallBtn("展开配置", new View.OnClickListener() {
                public void onClick(View v) {
                    expanded = expanded != null && expanded.equals(it.name)
                            ? null : it.name;
                    reload();
                }
            }));
            // 「登录这个 QQ」：把这个实例设成当前操作的实例，然后跳到登录页。
            // 登录页的请求会经管理服务代理到这个实例的 NapCat 上 ——
            // 用户不需要知道端口，也不需要填 WebUI 地址。
            btns.addView(smallBtn("登录这个 QQ", new View.OnClickListener() {
                public void onClick(View v) {
                    // 私密机器人：把刚才解锁用的口令一并交给登录页，
                    // 否则它拿不到 WebUI token，会误报「还没跑起来」。
                    String pw = unlockedPasswords.containsKey(it.name)
                            ? unlockedPasswords.get(it.name) : "";
                    RoutingTransport.setActiveInstance(it.name, pw);
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
        }
        return box;
    }

    /**
     * 弹密码框解锁。
     *
     * 解锁成功后把密码**只存在内存里**（unlockedPasswords），
     * 这样用户点「保存」时能带上它，不用反复输。
     * 不落盘：落盘就等于把钥匙挂在锁上。
     *
     * onDone 不为 null 时，解锁成功后回调它（比如「保存配置」前先解锁）。
     */
    private void askPassword(final ManagerClient.Instance it, final Runnable onDone) {
        final EditText pw = UiKit.input(ctx, "密码", true);
        new android.app.AlertDialog.Builder(ctx)
                .setTitle("解锁「" + it.name + "」")
                .setView(pw)
                .setPositiveButton("解锁", new android.content.DialogInterface.OnClickListener() {
                    public void onClick(android.content.DialogInterface d, int w) {
                        final String p = pw.getText().toString();
                        if (p.isEmpty()) {
                            host.toast("密码不能为空");
                            return;
                        }
                        host.toast("正在验证…");
                        pool.execute(new Runnable() {
                            public void run() {
                                try {
                                    client().unlock(it.name, p);
                                    ui.post(new Runnable() {
                                        public void run() {
                                            unlockedPasswords.put(it.name, p);
                                            host.toast("解锁成功");
                                            if (onDone != null) {
                                                onDone.run();
                                            }
                                            reload();
                                        }
                                    });
                                } catch (final Deployer.DeployException e) {
                                    ui.post(new Runnable() {
                                        public void run() {
                                            // 密码错就明确说密码错 —— 不要笼统报「失败」，
                                            // 否则用户会怀疑是网络或 App 坏了。
                                            host.toast(e.getMessage());
                                        }
                                    });
                                }
                            }
                        });
                    }
                })
                .setNegativeButton("取消", null)
                .show();
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

    // ------------------------------------------------------------ 新手第一步：API

    /**
     * 首屏的「API 去哪申请」卡。**默认展开**，不用点开就能看到。
     *
     * 用户原话：「还没有各大官网的API获取地址，那新手不知道在哪里获取怎么办？
     * 加，并且加上对应的教程」。
     *
     * 之前的问题不是「没有内容」，而是「内容不可达」—— 藏在某个机器人的
     * 展开面板里，而新手连机器人都还没有。所以这里：
     *   ① 提到首屏第一张卡，独立于任何机器人；
     *   ② 默认就展开（不折叠），第一眼就能看见；
     *   ③ 带上「怎么注册、去哪复制 Key、粘到哪」的分步教程，
     *      不只是丢一堆网址让人自己猜。
     */
    private View buildApiCard() {
        LinearLayout card = UiKit.card(ctx, "第一步：搞一个 API（新手必看）");
        LinearLayout in = UiKit.inner(card);

        // 用大白话讲清楚「API 是什么、为什么非得有它」——
        // 不说清楚，新手会以为这是可选项而跳过，然后卡在机器人不说话上。
        in.addView(UiKit.text(ctx, ApiGuide.intro(), 12, Theme.DIM));

        // 分步教程：每一步都写清楚「在哪个页面、点哪个按钮、看到什么」。
        // 泛泛说「去官网申请」对新手等于没说。
        in.addView(UiKit.caption(ctx, "手把手（以最推荐的 DeepSeek 为例）"));
        in.addView(UiKit.text(ctx, ApiGuide.tutorial("DeepSeek 深度求索"),
                12, Theme.DIM));

        // 直达申请页的按钮 —— 单独给一个最推荐的，免得新手在 8 家里挑花眼
        ApiGuide.Provider first = ApiGuide.providers().isEmpty()
                ? null : ApiGuide.providers().get(0);
        if (first != null) {
            Button go = UiKit.button(ctx,
                    "① 去 DeepSeek 官网申请 Key（点这里打开）", true);
            final String url = first.keyUrl;
            go.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) {
                    openUrl(url);
                }
            });
            in.addView(go);
        }

        // 全部服务商：折叠起来，需要的人自己展开。
        // 默认只展示「怎么申请」的通用教程 + 最推荐那家，
        // 避免一屏塞 8 家的信息把新手淹掉。
        final LinearLayout allBox = UiKit.column(ctx);
        allBox.setVisibility(View.GONE);
        final Button allBtn = UiKit.button(ctx,
                "② 看全部 " + ApiGuide.providers().size() + " 家服务商（含官网地址）", false);
        allBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                boolean show = allBox.getVisibility() != View.VISIBLE;
                allBox.setVisibility(show ? View.VISIBLE : View.GONE);
                allBtn.setText(show
                        ? "收起服务商列表"
                        : "② 看全部 " + ApiGuide.providers().size()
                          + " 家服务商（含官网地址）");
                if (show && allBox.getChildCount() == 0) {
                    buildProviderList(allBox, null, null, null);
                }
            }
        });
        in.addView(allBtn);
        in.addView(allBox);

        // 安全提醒必须跟着教程一起给 —— 别让人糊里糊涂就把 Key 交出去
        in.addView(UiKit.text(ctx, ApiGuide.securityNote(), 11, Theme.DIM));

        in.addView(UiKit.text(ctx,
                "拿到 Key 之后：回到「我的机器人」→ 展开配置 → 把 Key 粘进"
                + "「API Key」那栏 → 点「保存到服务器」。"
                + "填完记得点「测试连接」，通不通当场就知道。", 12, Theme.GOOD));
        return card;
    }

    /**
     * 列出所有服务商，每家带「用这家」（填地址+模型）和「去申请」（开官网）。
     *
     * apiBase/apiKey/apiModel 为 null 时只显示信息、不放「用这家」按钮 ——
     * 首屏那张卡不属于任何机器人，没地方填。
     */
    private void buildProviderList(final LinearLayout box, final EditText apiBase,
                                   final EditText apiKey, final EditText apiModel) {
        for (final ApiGuide.Provider p : ApiGuide.providers()) {
            LinearLayout card = UiKit.column(ctx);
            android.graphics.drawable.GradientDrawable bg =
                    new android.graphics.drawable.GradientDrawable();
            bg.setColor(Theme.INPUT);
            bg.setCornerRadius(Theme.dp(ctx, 8));
            card.setBackground(bg);
            int pad = Theme.dp(ctx, 8);
            card.setPadding(pad, pad, pad, pad);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            lp.topMargin = Theme.dp(ctx, 8);
            card.setLayoutParams(lp);

            TextView title = UiKit.text(ctx, p.name, 14, Theme.TEXT);
            title.setTypeface(title.getTypeface(), Typeface.BOLD);
            card.addView(title);
            card.addView(UiKit.text(ctx, p.note, 12, Theme.DIM));
            // 申请地址用可长按复制的方式给全 —— 用户要的就是这个网址
            card.addView(UiKit.text(ctx, "申请地址：" + p.keyUrl, 11, Theme.GOOD));
            card.addView(UiKit.text(ctx, "接口地址：" + p.baseUrl, 11, Theme.DIM));
            card.addView(UiKit.text(ctx, "模型名示例：" + joinList(p.models), 11, Theme.DIM));
            if (!p.warn.isEmpty()) {
                card.addView(UiKit.text(ctx, p.warn, 11, Theme.WARN));
            }

            LinearLayout btns = UiKit.row(ctx);
            if (apiBase != null) {
                Button use = UiKit.button(ctx, "用这家", true);
                LinearLayout.LayoutParams w1 = new LinearLayout.LayoutParams(
                        0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
                w1.rightMargin = Theme.dp(ctx, 4);
                use.setLayoutParams(w1);
                use.setOnClickListener(new View.OnClickListener() {
                    public void onClick(View v) {
                        apiBase.setText(p.baseUrl);
                        if (apiModel != null
                                && apiModel.getText().toString().trim().isEmpty()) {
                            apiModel.setText(p.firstModel());
                        }
                        host.toast("已填好接口地址。去官网申请 Key，"
                                + "复制回来粘到 API Key 那栏。");
                    }
                });
                btns.addView(use);
            }

            Button apply = UiKit.button(ctx, "去申请", apiBase == null);
            LinearLayout.LayoutParams w2 = new LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
            w2.leftMargin = Theme.dp(ctx, 4);
            apply.setLayoutParams(w2);
            apply.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) {
                    openUrl(p.keyUrl);
                }
            });
            btns.addView(apply);
            card.addView(btns);
            box.addView(card);
        }
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
        final EditText apiModel = UiKit.input(ctx, "模型名，如 deepseek-flash", false);
        final EditText persona = UiKit.input(ctx, "人格提示词（它是谁、怎么说话）", false);

        panel.addView(UiKit.caption(ctx, "① 聊天范围"));
        panel.addView(groups);
        panel.addView(friends);
        panel.addView(UiKit.caption(ctx, "② 主聊天 API"));
        panel.addView(apiBase);
        panel.addView(apiKey);
        panel.addView(apiModel);

        // ── 不知道去哪申请 API？展开引导 ────────────────────────────────
        //
        // 这是整条流程里唯一一个必须在**别的网站**完成的步骤，新手最容易卡死。
        // 默认折叠，不打扰已经会的人；点开才有内容，也不会把面板撑得太长。
        final LinearLayout guideBox = UiKit.column(ctx);
        guideBox.setVisibility(View.GONE);
        final Button guideBtn = UiKit.button(ctx, "不知道 API 去哪申请？点这里", false);
        guideBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                boolean show = guideBox.getVisibility() != View.VISIBLE;
                guideBox.setVisibility(show ? View.VISIBLE : View.GONE);
                guideBtn.setText(show ? "收起申请说明" : "不知道 API 去哪申请？点这里");
                if (show && guideBox.getChildCount() == 0) {
                    buildGuide(guideBox, apiBase, apiKey, apiModel);
                }
            }
        });
        panel.addView(guideBtn);
        panel.addView(guideBox);

        // ── 测连通 + 拉模型列表 ────────────────────────────────────────
        final TextView apiResult = UiKit.text(ctx, "", 12, Theme.DIM);
        Button testBtn = UiKit.button(ctx, "测试连接（在服务器上测）", false);
        testBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                String b = apiBase.getText().toString().trim();
                String k = apiKey.getText().toString().trim();
                if (b.isEmpty()) {
                    host.toast("先填接口地址");
                    return;
                }
                apiResult.setTextColor(Theme.DIM);
                apiResult.setText("正在从服务器上测试…");
                UiKit.setEnabledDeep(testBtn, false);
                pool.execute(new Runnable() {
                    public void run() {
                        try {
                            final Map<String, Object> r = client().testApi(it.name, b, k,
                                    apiModel.getText().toString().trim());
                            ui.post(new Runnable() {
                                public void run() {
                                    UiKit.setEnabledDeep(testBtn, true);
                                    boolean okAll = Json.bool(r, "reachable", false)
                                            && Json.bool(r, "auth_ok", false);
                                    apiResult.setTextColor(okAll ? Theme.GOOD : Theme.WARN);
                                    apiResult.setText(Json.str(r, "message", "测完了。"));
                                    // 顺手把拉到的模型列表塞进模型框的选择器里
                                    List<String> models = new ArrayList<String>();
                                    for (Object o : Json.arr(r, "models")) {
                                        models.add(String.valueOf(o));
                                    }
                                    if (!models.isEmpty()) {
                                        showModelPicker(apiModel, models);
                                    }
                                }
                            });
                        } catch (final Deployer.DeployException e) {
                            ui.post(new Runnable() {
                                public void run() {
                                    UiKit.setEnabledDeep(testBtn, true);
                                    apiResult.setTextColor(Theme.WARN);
                                    apiResult.setText("测试失败：" + e.getMessage());
                                }
                            });
                        }
                    }
                });
            }
        });
        panel.addView(testBtn);

        Button modelsBtn = UiKit.button(ctx, "获取可用模型（从服务商拉当前列表）", false);
        modelsBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                String b = apiBase.getText().toString().trim();
                String k = apiKey.getText().toString().trim();
                apiResult.setTextColor(Theme.DIM);
                apiResult.setText("正在获取模型列表…");
                UiKit.setEnabledDeep(modelsBtn, false);
                pool.execute(new Runnable() {
                    public void run() {
                        try {
                            final List<String> models = client().listModels(it.name, b, k);
                            ui.post(new Runnable() {
                                public void run() {
                                    UiKit.setEnabledDeep(modelsBtn, true);
                                    if (models.isEmpty()) {
                                        apiResult.setTextColor(Theme.WARN);
                                        apiResult.setText("没取到模型列表。"
                                                + "这家可能没提供该接口 —— 请照它官网文档手填模型名。");
                                        return;
                                    }
                                    apiResult.setTextColor(Theme.GOOD);
                                    apiResult.setText("取到 " + models.size()
                                            + " 个模型，点下面选一个。");
                                    showModelPicker(apiModel, models);
                                }
                            });
                        } catch (final Deployer.DeployException e) {
                            ui.post(new Runnable() {
                                public void run() {
                                    UiKit.setEnabledDeep(modelsBtn, true);
                                    apiResult.setTextColor(Theme.WARN);
                                    apiResult.setText("获取失败：" + e.getMessage());
                                }
                            });
                        }
                    }
                });
            }
        });
        panel.addView(modelsBtn);
        panel.addView(apiResult);

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
                    // 私密实例要带上解锁密码，否则服务器返回 config=null
                    String lockPw = unlockedPasswords.containsKey(it.name)
                            ? unlockedPasswords.get(it.name) : "";
                    final Map<String, Object> d = client().detail(it.name, lockPw);
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
                            // 私密实例要带上解锁密码，否则服务器会拒绝写入
                            // （锁只挡看不挡改 = 没锁）。
                            String lockPw = unlockedPasswords.containsKey(it.name)
                                    ? unlockedPasswords.get(it.name) : "";
                            final List<String> changed = client().applyConfig(
                                    it.name, g, f, ab, ak, am, pe, lockPw);
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

        // ── 私密设置 ────────────────────────────────────────────────────
        //
        // 需求原话：「还有没有可以设为私密的机器人配置，可以用密码来解锁」。
        // 放在面板最底部：这是「配置好之后」才考虑的选项，不该挡在前面。
        panel.addView(UiKit.caption(ctx, "④ 私密（可选）"));
        final Button lockBtn = UiKit.button(ctx,
                it.locked ? "🔒 已设为私密（点这里改密码/取消）" : "设为私密（用密码锁起来）",
                false);
        lockBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                showLockDialog(it);
            }
        });
        panel.addView(lockBtn);
        panel.addView(UiKit.text(ctx,
                "锁上之后，看配置和改配置都要先输密码。\n"
                + "说明：这个锁是防「别人拿你手机顺手点开看到」，"
                + "不是防拿到服务器 root 的人 —— 有 root 就能直接读配置文件。"
                + "所以别把它当保险箱用。", 11, Theme.DIM));
        return panel;
    }

    /**
     * 设私密 / 改密码 / 取消私密。
     *
     * 三种情况合成一个对话框，是因为用户的心智模型就是「管理这个锁」，
     * 分成三个入口反而要他自己判断该点哪个。
     */
    private void showLockDialog(final ManagerClient.Instance it) {
        LinearLayout box = UiKit.column(ctx);
        final EditText oldPw = UiKit.input(ctx, "当前密码（还没设过就留空）", true);
        final EditText newPw = UiKit.input(ctx,
                it.locked ? "新密码（留空=只取消私密）" : "设一个密码（至少 4 位）", true);
        if (it.locked) {
            box.addView(UiKit.text(ctx, "已经锁着了。改密码要输当前密码。", 12, Theme.DIM));
            box.addView(oldPw);
        }
        box.addView(newPw);
        box.addView(UiKit.text(ctx,
                "· 点「保存」= 设成私密 / 改密码\n"
                + "· 想取消私密：新密码留空，只填当前密码，点「取消私密」", 11, Theme.DIM));

        new android.app.AlertDialog.Builder(ctx)
                .setTitle(it.locked ? "管理「" + it.name + "」的锁" : "把「" + it.name + "」设为私密")
                .setView(box)
                .setPositiveButton("保存", new android.content.DialogInterface.OnClickListener() {
                    public void onClick(android.content.DialogInterface d, int w) {
                        doSetLock(it, true, newPw.getText().toString(),
                                oldPw.getText().toString());
                    }
                })
                .setNeutralButton("取消私密", new android.content.DialogInterface.OnClickListener() {
                    public void onClick(android.content.DialogInterface d, int w) {
                        doSetLock(it, false, "", oldPw.getText().toString());
                    }
                })
                .setNegativeButton("返回", null)
                .show();
    }

    private void doSetLock(final ManagerClient.Instance it, final boolean enabled,
                           final String password, final String oldPassword) {
        host.toast("正在处理…");
        pool.execute(new Runnable() {
            public void run() {
                try {
                    client().setLock(it.name, enabled, password, oldPassword);
                    ui.post(new Runnable() {
                        public void run() {
                            // 锁状态变了，内存里那个解锁密码也要跟着更新/清掉 ——
                            // 否则会出现「已经取消了私密，本地还记着旧密码」，
                            // 或者「改了密码，本地还是旧的」导致保存失败。
                            if (enabled && !password.isEmpty()) {
                                unlockedPasswords.put(it.name, password);
                            } else if (!enabled) {
                                unlockedPasswords.remove(it.name);
                            }
                            host.toast(enabled ? "已设为私密" : "已取消私密");
                            reload();
                        }
                    });
                } catch (final Deployer.DeployException e) {
                    ui.post(new Runnable() {
                        public void run() {
                            host.toast(e.getMessage());
                        }
                    });
                }
            }
        });
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

    // ------------------------------------------------------------ API 申请引导

    /**
     * 搭「去哪申请 API」的引导内容。
     *
     * 点「用这家」= 直接把它那套（接口地址 + 模型名示例）填好；
     * 点「去申请」= 打开它的官网页面。用户只需要复制一个 Key 回来粘上，
     * 「不知道填什么」这件事就没了。
     */
    private void buildGuide(final LinearLayout box, final EditText apiBase,
                            final EditText apiKey, final EditText apiModel) {
        box.addView(UiKit.text(ctx, ApiGuide.intro(), 12, Theme.DIM));
        box.addView(UiKit.caption(ctx, "手把手（以最推荐的 DeepSeek 为例）"));
        box.addView(UiKit.text(ctx, ApiGuide.tutorial("DeepSeek 深度求索"),
                12, Theme.DIM));
        buildProviderList(box, apiBase, apiKey, apiModel);
        box.addView(UiKit.text(ctx, ApiGuide.securityNote(), 11, Theme.DIM));
    }

    /** 把模型列表做成可点的选择器（用户不用手打模型名）。 */
    private void showModelPicker(final EditText apiModel, final List<String> models) {
        final String[] arr = models.toArray(new String[0]);
        new android.app.AlertDialog.Builder(ctx)
                .setTitle("选一个模型（共 " + arr.length + " 个）")
                .setItems(arr, new android.content.DialogInterface.OnClickListener() {
                    public void onClick(android.content.DialogInterface d, int which) {
                        apiModel.setText(arr[which]);
                        host.toast("已选：" + arr[which] + "。记得点「保存到服务器」。");
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void openUrl(String url) {
        try {
            android.content.Intent it = new android.content.Intent(
                    android.content.Intent.ACTION_VIEW, android.net.Uri.parse(url));
            it.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
            ctx.startActivity(it);
        } catch (Exception e) {
            // 没有浏览器等情况：把地址说出来让他自己复制，别静默失败
            host.toast("打不开浏览器，请手动访问：" + url);
        }
    }

    private static String joinList(List<String> xs) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < xs.size(); i++) {
            if (i > 0) {
                sb.append("、");
            }
            sb.append(xs.get(i));
        }
        return sb.toString();
    }
}
