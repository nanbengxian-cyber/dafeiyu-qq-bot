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
            // 按钮怎么给 —— 这里的判据是**「有没有容器还活着」**，
            // 不是「两个都活着」。
            //
            // 2026-09-18 用户报「机器人运行中停止不了」，根因就在这：
            // 原来只按 it.running()（两个都 running）二选一，
            // 于是「NapCat 挂了、AstrBot 还在跑」这种最常见的半死状态
            // 既不给「停止」也不给「修复」，用户完全没有下手的地方。
            // 线上实测有实例就是 astrbot=running + napcat=exited。
            //
            // 现在：
            //   · 还有容器活着 → 一定给「停止」（否则停不掉）
            //   · 没在完整运行   → 也给「启动」（重启一次通常就好了）
            //   两个按钮同时出现是**故意**的，不是重复。
            if (it.partiallyRunning()) {
                btns.addView(smallBtn("停止", new View.OnClickListener() {
                    public void onClick(View v) {
                        act("stop", it.name);
                    }
                }));
            }
            if (!it.running()) {
                btns.addView(smallBtn("启动", new View.OnClickListener() {
                    public void onClick(View v) {
                        act("start", it.name);
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

        // ── 接口协议 ───────────────────────────────────────────────────
        //
        // ★ 为什么协议必须是用户能选的一项：
        //   同一个模型可能只认某一种协议。中转站给的是 Anthropic 原生接口
        //   （/v1/messages + x-api-key）时，按默认的 OpenAI 兼容去填会 404，
        //   而报错指向「地址写错了」—— 用户于是去反复改一个**正确**的地址，
        //   永远改不好。反过来，选错协议也可能拿到 401，被解释成
        //   「Key 不对」，用户就去重新复制一个完全正确的 Key。
        //   这两种错都会把人带偏到完全无关的方向，所以协议必须显式可配。
        //
        // 用下拉而不是让用户填字符串：那个 type 字符串是给机器看的
        //   （AstrBot 拿它查适配器，查不到就加载失败），手打必然出错，
        //   而错的后果是机器人一个字都不回、报错只有一行 traceback。
        final android.widget.Spinner protoSpin = UiKit.spinner(ctx, Protocols.labels());
        final TextView protoHint = UiKit.text(ctx, "", 11, Theme.DIM);
        panel.addView(UiKit.caption(ctx, "接口协议（选错就通不了，不确定就选第一个）"));
        panel.addView(protoSpin);
        panel.addView(protoHint);
        protoSpin.setOnItemSelectedListener(
                new android.widget.AdapterView.OnItemSelectedListener() {
                    public void onItemSelected(android.widget.AdapterView<?> p,
                                               View v, int pos, long id) {
                        protoHint.setText(Protocols.hintOfKey(
                                Protocols.keyOfLabel(String.valueOf(
                                        protoSpin.getSelectedItem()))));
                    }

                    public void onNothingSelected(android.widget.AdapterView<?> p) {
                    }
                });

        // ── 自定义请求体（折叠）─────────────────────────────────────────
        //
        // 为什么折叠：它不是必填的，而且里面是 JSON —— 摆在明面上会让
        // 新手以为必须填，反而卡住。会调参的人自己会点开。
        //
        // 为什么值得做这个功能：不同服务商对同一个模型的默认参数不一样，
        // 有人要调 temperature 让机器人不那么死板，有人要调 max_tokens
        // 控制回复长度，有人要给某些网关塞一个非标准字段。
        // 没有这个入口时，用户只能去改服务器上的配置文件 —— 那正是
        // 这个 App 要消灭的事情。
        final LinearLayout extraBox = UiKit.column(ctx);
        extraBox.setVisibility(View.GONE);
        final EditText extraBody = UiKit.multiline(ctx,
                "留空 = 不传额外参数。例如：\n{\n  \"temperature\": 0.8,\n"
                + "  \"max_tokens\": 2048\n}", 4);
        extraBox.addView(UiKit.caption(ctx,
                "这里写的每一项都会**原样**发给你填的那家接口。\n"
                + "常用的是 temperature（越大越随机，0~2）和 "
                + "max_tokens（回复最长多少字）。\n"
                + "不知道写什么就别填 —— 不填也能正常用。"));
        extraBox.addView(extraBody);
        final Button extraBtn = UiKit.button(ctx, "高级：自定义请求体（可选）", false);
        extraBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                boolean show = extraBox.getVisibility() != View.VISIBLE;
                extraBox.setVisibility(show ? View.VISIBLE : View.GONE);
                extraBtn.setText(show ? "收起自定义请求体"
                        : "高级：自定义请求体（可选）");
            }
        });
        panel.addView(extraBtn);
        panel.addView(extraBox);

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
                            // 把协议和自定义请求体一起发给服务器 —— 否则会
                            // 出现最难查的一种情况：「测试显示通了 ✓，但机器人
                            // 真的跑起来 400」，因为测试发的请求体里没有用户
                            // 自定义的那些参数。
                            final Map<String, Object> r = client().testApi(it.name, b, k,
                                    apiModel.getText().toString().trim(),
                                    Protocols.keyOfLabel(String.valueOf(
                                            protoSpin.getSelectedItem())),
                                    extraBody.getText().toString());
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

        // ── ③ 识图 API（可选）────────────────────────────────────────────
        //
        // 为什么单独一块、而且默认折叠：
        //   ① 它不是必填的 —— 不填机器人照样聊天，只是看不懂图；
        //   ② 它是「进阶」配置，新手看到会以为必须填，反而卡住。
        //   所以默认收起来，写清楚「不填也能用」。
        final LinearLayout visionBox = UiKit.column(ctx);
        visionBox.setVisibility(View.GONE);
        final EditText visionBase = UiKit.input(ctx,
                "识图 API 接口地址，如 https://…/v1", false);
        final EditText visionKey = UiKit.input(ctx,
                "识图 API Key（不回显，留空=不改）", true);
        final EditText visionModel = UiKit.input(ctx,
                "识图模型名，要带 vision 字样，如 glm-4v", false);
        final TextView visionResult = UiKit.text(ctx, "", 12, Theme.DIM);

        visionBox.addView(UiKit.caption(ctx,
                "识图模型和聊天模型可以不是同一家、同一个。\n"
                + "填了它，机器人就能看懂群友发的图；不填就只看得懂文字。"));
        visionBox.addView(visionBase);
        visionBox.addView(visionKey);
        visionBox.addView(visionModel);

        // 识图也要选协议 —— 而且它和主聊天的协议**互不相干**。
        //
        // ★ 为什么不能跟着主协议走：识图常常是另一家（主聊天用便宜的中转站、
        //   识图用智谱或 Gemini 官方）。如果这里错跟了主协议，用户改主 API
        //   的协议会把识图一起改坏，而现象是「文字能聊、图看不懂」——
        //   极难联想到是主协议改动的副作用。
        final android.widget.Spinner visionProto = UiKit.spinner(ctx, Protocols.labels());
        visionBox.addView(UiKit.caption(ctx, "识图接口协议"));
        visionBox.addView(visionProto);

        // 识图的自定义请求体。有些视觉模型必须显式关掉思考模式，
        // 不关就只回思考过程、不回正文，表现为「发了图它答非所问」。
        final EditText visionExtra = UiKit.multiline(ctx,
                "留空 = 不传额外参数。例如：\n{\n  \"temperature\": 0.2\n}", 3);
        visionBox.addView(UiKit.caption(ctx, "识图自定义请求体（可选，不知道就别填）"));
        visionBox.addView(visionExtra);

        final Button visionTest = UiKit.button(ctx,
                "测试识图（真的发一张图看它认不认得）", false);
        visionTest.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                final String b = visionBase.getText().toString().trim();
                final String k = visionKey.getText().toString().trim();
                final String m = visionModel.getText().toString().trim();
                if (b.isEmpty() || m.isEmpty()) {
                    host.toast("识图 API 的地址和模型名都要填");
                    return;
                }
                visionResult.setTextColor(Theme.DIM);
                visionResult.setText("正在让服务器发一张测试图过去…");
                UiKit.setEnabledDeep(visionTest, false);
                pool.execute(new Runnable() {
                    public void run() {
                        try {
                            final Map<String, Object> r = client().testVision(
                                    it.name, b, k, m,
                                    Protocols.keyOfLabel(String.valueOf(
                                            visionProto.getSelectedItem())),
                                    visionExtra.getText().toString());
                            ui.post(new Runnable() {
                                public void run() {
                                    UiKit.setEnabledDeep(visionTest, true);
                                    String vc = Json.str(r, "vision_capable", "");
                                    boolean canSee = "true".equals(vc);
                                    boolean unknown = vc.isEmpty() || "null".equals(vc);
                                    visionResult.setTextColor(
                                            canSee ? Theme.GOOD
                                                   : (unknown ? Theme.DIM : Theme.WARN));
                                    visionResult.setText(Json.str(r, "message", "测完了。"));
                                }
                            });
                        } catch (final Deployer.DeployException e) {
                            ui.post(new Runnable() {
                                public void run() {
                                    UiKit.setEnabledDeep(visionTest, true);
                                    visionResult.setTextColor(Theme.WARN);
                                    visionResult.setText("测试失败：" + e.getMessage());
                                }
                            });
                        }
                    }
                });
            }
        });
        visionBox.addView(visionTest);
        visionBox.addView(visionResult);

        final Button visionBtn = UiKit.button(ctx,
                "③ 识图 API（可选：让机器人看懂图片）", false);
        visionBtn.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                boolean show = visionBox.getVisibility() != View.VISIBLE;
                visionBox.setVisibility(show ? View.VISIBLE : View.GONE);
                visionBtn.setText(show ? "收起识图设置"
                        : "③ 识图 API（可选：让机器人看懂图片）");
            }
        });
        panel.addView(visionBtn);
        panel.addView(visionBox);

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

        // 「修复消息通道」：默认隐藏，只有自检发现没配对时才显示。
        // 为什么藏起来：正常用户不该看到这个按钮 —— 看到了会以为机器人有问题。
        // 它只在真的坏了的时候出现，那时它是唯一能救命的东西。
        final Button repair = UiKit.button(ctx, "修复消息通道（不回话就点这里）", false);
        repair.setVisibility(View.GONE);
        panel.addView(repair);

        repair.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                note.setText("正在修复消息通道（会让这个机器人重新连一次，约半分钟）…");
                UiKit.setEnabledDeep(repair, false);
                pool.execute(new Runnable() {
                    public void run() {
                        try {
                            final Map<String, Object> r =
                                    client().repairChannel(it.name);
                            final Map<String, Object> after =
                                    Json.obj(r, "after");
                            final boolean ok = after != null
                                    && Json.bool(after, "paired", false);
                            ui.post(new Runnable() {
                                public void run() {
                                    UiKit.setEnabledDeep(repair, true);
                                    if (ok) {
                                        note.setText("✓ 消息通道已接上。"
                                                + "现在去 QQ 里给这个机器人发条消息试试，"
                                                + "它应该会回你了。");
                                        repair.setVisibility(View.GONE);
                                    } else {
                                        note.setText("还是没接上。"
                                                + "请把这个机器人「停止」再「启动」一次，"
                                                + "然后回来再点一次修复。");
                                    }
                                }
                            });
                        } catch (final Deployer.DeployException e) {
                            ui.post(new Runnable() {
                                public void run() {
                                    UiKit.setEnabledDeep(repair, true);
                                    note.setText("修复失败：" + e.getMessage());
                                }
                            });
                        }
                    }
                });
            }
        });

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

                            // 回填协议。★ 一定要判断「App 认不认识」：
                            // 服务器可能比 App 新，加了 App 不知道的协议。
                            // 直接 indexOfKey 会静默落到默认项（第一个），
                            // 用户一保存就把一个**本来正确**的协议改成了
                            // OpenAI 兼容 —— 那是把好配置改坏，而且他什么都
                            // 没动过，根本查不出来。所以这里显式提示。
                            //
                            // 提示文案记在 unknownProtoMsg 里，最后统一显示：
                            // 下面的「消息通道」判断也会写 note，直接在这里
                            // setText 会被它覆盖掉，用户就看不到这个警告了。
                            String protoKey = Json.str(cfg, "protocol", "");
                            final String unknownProtoMsg;
                            if (!Protocols.knows(protoKey)) {
                                unknownProtoMsg = "⚠ 这个机器人用的接口协议（"
                                        + protoKey + "）这个 App 版本不认识。"
                                        + "**不要直接保存**，否则会被改成别的协议。"
                                        + "请先更新 App。";
                                protoSpin.setSelection(0);
                            } else {
                                unknownProtoMsg = "";
                                protoSpin.setSelection(Protocols.indexOfKey(protoKey));
                            }

                            // 回填自定义请求体。服务器给的是格式化好的 JSON 文本。
                            String eb = Json.str(cfg, "extra_body", "");
                            extraBody.setText(eb);
                            if (!eb.isEmpty()) {
                                // 配过就自动展开 —— 配过的人多半是来改它的，
                                // 藏在折叠里会让他以为「我配的东西丢了」。
                                extraBox.setVisibility(View.VISIBLE);
                                extraBtn.setText("收起自定义请求体");
                            }

                            boolean keySet = Json.bool(cfg, "api_key_set", false);
                            apiKey.setHint(keySet
                                    ? "API Key 已设置（要换就填新的，留空=不改）"
                                    : "API Key（必填）");

                            // 识图 API：已配过就回填，并自动展开那块 ——
                            // 配过的人多半是来改它的，藏在折叠里会让他找不到。
                            String vBase = Json.str(cfg, "vision_base", "");
                            String vModel = Json.str(cfg, "vision_model", "");
                            visionBase.setText(vBase);
                            visionModel.setText(vModel);
                            visionProto.setSelection(Protocols.indexOfKey(
                                    Json.str(cfg, "vision_protocol", "")));
                            visionExtra.setText(Json.str(cfg, "vision_extra_body", ""));
                            boolean vKeySet = Json.bool(cfg, "vision_key_set", false);
                            visionKey.setHint(vKeySet
                                    ? "识图 API Key 已设置（要换就填新的，留空=不改）"
                                    : "识图 API Key");
                            if (!vBase.isEmpty() || !vModel.isEmpty() || vKeySet) {
                                visionBox.setVisibility(View.VISIBLE);
                                visionBtn.setText("收起识图设置");
                            }

                            // ★ 消息通道自检：这是「机器人一个字都不回」的判据。
                            //
                            // 没配对时用户看到的现象和「API 填错」一模一样（都是
                            // 不回话），他会反复改 API 却永远改不好 —— 因为病根
                            // 不在那里。所以这里必须主动说出来，并且给一个按钮。
                            //
                            // 优先级：协议不认识的警告最高 —— 它会让用户**一保存
                            // 就把好配置改坏**，比「不回话」更紧急（不回话至少
                            // 配置是好的）。所以它盖过下面两条。
                            Map<String, Object> pr = Json.obj(d, "pairing");
                            if (!unknownProtoMsg.isEmpty()) {
                                note.setText(unknownProtoMsg);
                                note.setTextColor(Theme.WARN);
                            } else if (pr != null && !Json.bool(pr, "paired", false)) {
                                note.setText("⚠ 这个机器人的「消息通道」没接上，"
                                        + "它会收不到消息、一个字都不回。"
                                        + "点下面的「修复消息通道」就能修好。");
                                repair.setVisibility(View.VISIBLE);
                            } else {
                                note.setText(keySet
                                        ? "已读取当前配置。API Key 已在服务器上，这里不回显。"
                                        : "已读取当前配置。还没设 API Key。");
                            }
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
                final String vb = visionBase.getText().toString().trim();
                final String vk = visionKey.getText().toString().trim();
                final String vm = visionModel.getText().toString().trim();
                final String proto = Protocols.keyOfLabel(String.valueOf(
                        protoSpin.getSelectedItem()));
                final String eb = extraBody.getText().toString().trim();
                final String vproto = Protocols.keyOfLabel(String.valueOf(
                        visionProto.getSelectedItem()));
                final String veb = visionExtra.getText().toString().trim();
                if (g.isEmpty() && f.isEmpty() && ab.isEmpty() && ak.isEmpty()
                        && am.isEmpty() && pe.isEmpty()
                        && vb.isEmpty() && vk.isEmpty() && vm.isEmpty()
                        && eb.isEmpty() && veb.isEmpty()) {
                    host.toast("什么都没填。");
                    return;
                }
                // 识图三样要么全空（不动它），要么填全 —— 本地先拦一道，
                // 免得白等一次往返才被告知填了一半。
                // 只填一半的话机器人会「收得到图但识不了」，而且界面上
                // 看不出哪里不对，所以这里必须拦住。
                if (!(vb.isEmpty() && vk.isEmpty() && vm.isEmpty())
                        && (vb.isEmpty() || vm.isEmpty())) {
                    host.toast("识图 API 要填全：接口地址和模型名都要填"
                            + "（Key 留空=沿用已保存的）");
                    return;
                }
                // 自定义请求体本地先做一次「是不是 JSON」的粗查。
                //
                // ★ 为什么值得在本地查这一道（服务器也会查）：
                //   手机上打字容易出错，而 JSON 的错（中文引号、多个逗号）
                //   在视觉上几乎看不出来。让用户**立刻**知道，比等一次
                //   网络往返再被告知要好；而且这里只查格式、不查语义，
                //   语义（哪些字段不能写）留给服务器那份权威实现。
                if (!eb.isEmpty() && !looksLikeJsonObject(eb)) {
                    host.toast("自定义请求体要写成一对大括号包起来的字段，"
                            + "比如 {\"temperature\": 0.7}。"
                            + "注意引号要用英文的。");
                    return;
                }
                if (!veb.isEmpty() && !looksLikeJsonObject(veb)) {
                    host.toast("识图的自定义请求体格式不对，"
                            + "要写成 {\"temperature\": 0.2} 这样。");
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
                                    it.name, g, f, ab, ak, am, pe, lockPw,
                                    vb, vk, vm, proto, eb, vproto, veb);
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

    /**
     * 粗查一段文本「像不像一个 JSON 对象」。
     *
     * ★ 这是**故意做粗**的：只查最外层是不是一对大括号。
     *   真正权威的校验在服务器上（parse_extra_body），那里能给出
     *   「第几行第几个字符错了」这种精确报错。App 这边只做一件服务器
     *   做不到的事：**在还没发网络请求之前**就把明显填错的拦下来，
     *   省掉用户一次「等了好几秒才被告知引号用错了」。
     *
     * 为什么只查大括号、不自己写个 JSON 解析器：
     *   手机上的半吊子解析器会把**合法**的 JSON 判成非法
     *   （比如带注释、带尾随空白的），那样用户就被自己的 App 挡住了，
     *   而他填的东西其实是对的 —— 比不查更糟。
     *   所以这里只拦「一眼就不对」的情况，剩下的交给服务器。
     */
    private static boolean looksLikeJsonObject(String s) {
        String t = s.trim();
        return t.startsWith("{") && t.endsWith("}");
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
