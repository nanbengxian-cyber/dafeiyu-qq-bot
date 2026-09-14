package com.dafeiyu.console;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.CompoundButton;
import android.widget.EditText;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 主界面：状态 / 配置 / 模式 三个页签。
 *
 * 结构上只有一条原则值得说：**这个类只管画和点，一切判断都在
 * {@link StatusFmt}、{@link KnobModel}、{@link Ui} 里**。所以它长，但没有逻辑；
 * 那三个类短，但装着全部会出错的东西 —— 而它们能在普通 JVM 上被测穿。
 *
 * 界面全部代码搭建，不用 XML 布局：这个项目没有 Gradle，资源是手写 aapt2 编的，
 * 每多一个布局文件就多一处「R.id 对不上就闪退」的风险，而这种崩溃在没有 adb
 * 的情况下几乎无法定位。
 *
 * 线程模型：所有网络请求都丢到临时线程，回主线程更新界面。没有引任何异步库，
 * 就是 Thread + Handler —— 这个 App 一共只有七八个请求，引框架不值得。
 */
public class MainActivity extends Activity {

    private Store store;
    private final Handler ui = new Handler(Looper.getMainLooper());

    /** 服务端最近一次给的状态 / schema / 配置值。null 表示还没拿到。 */
    private Map<String, Object> status;
    private KnobModel.Schema schema;
    private Map<String, Object> values;
    /** 用户在配置页改动但还没提交的值。 */
    private final Map<String, Object> edited = new LinkedHashMap<String, Object>();

    /** 观察页数据（v2）：群聊流 / 心智 / 日志。null 表示还没拉到。 */
    private Map<String, Object> liveData;
    private Map<String, Object> mindData;
    private Map<String, Object> logData;
    private String liveGroup = "";        // 当前选中的群
    private long liveOldestTs;            // 已加载的最旧一条（翻页锚点）
    private String logLevel = "all";
    private String logName = "";
    private boolean liveAutoRefresh = true;

    /** 观察页自动刷新的节拍器。15 秒一拍，只在观察页且自动开时真正发请求。 */
    private static final long FEED_POLL_MS = 15000L;
    private final Runnable feedTicker = new Runnable() {
        @Override
        public void run() {
            if (isFinishing() || isDestroyed()) {
                return;
            }
            // 节拍器不停；只有处在观察页、自动开、且没有别的请求在跑时才拉
            if (tab == TAB_LIVE && liveAutoRefresh && !busy) {
                loadFeed(false);
            }
            ui.postDelayed(this, FEED_POLL_MS);
        }
    };

    private int tab;
    private boolean busy;

    private LinearLayout tabBar;
    private LinearLayout content;
    private TextView banner;
    private Button refreshBtn;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        store = new Store(this);
        if (!store.loggedIn()) {
            kickToLogin();
            return;
        }
        tab = store.tab();
        // 旧 APK 存的 tab 只有 0..2，v2 有 6 个页签 —— 越界的值回状态页，
        // 比渲染出一个空白页好。
        if (tab < 0 || tab >= TABS.length) {
            tab = 0;
        }
        setContentView(buildShell());
        loadAll(false);
        checkServerVersion();
        ui.postDelayed(feedTicker, FEED_POLL_MS);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        ui.removeCallbacks(feedTicker);
    }

    // ---------------------------------------------------------------- 外壳

    private View buildShell() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Theme.BG);

        // 顶栏：标题 + 刷新 + 退出
        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.setGravity(Gravity.CENTER_VERTICAL);
        int pad = Theme.dp(this, 14);
        top.setPadding(pad, Theme.dp(this, 12), pad, Theme.dp(this, 8));

        TextView title = new TextView(this);
        title.setText("大肥鱼控制台");
        title.setTextColor(Theme.TEXT);
        title.setTextSize(19);
        top.addView(title, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        refreshBtn = flatButton("刷新", new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                loadAll(true);
            }
        });
        top.addView(refreshBtn);

        top.addView(flatButton("退出", new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                confirm("退出登录", "会清掉本机保存的登录凭据，下次要重新输密码。",
                        new Runnable() {
                            @Override
                            public void run() {
                                store.clearCookie();
                                kickToLogin();
                            }
                        });
            }
        }));
        root.addView(top);

        // 顶部横幅：一句话总结当前最坏的情况
        banner = new TextView(this);
        banner.setTextSize(15);
        banner.setPadding(pad, Theme.dp(this, 10), pad, Theme.dp(this, 10));
        banner.setText("正在读取…");
        banner.setTextColor(Theme.DIM);
        banner.setBackgroundColor(Theme.CARD);
        root.addView(banner);

        // 页签
        tabBar = new LinearLayout(this);
        tabBar.setOrientation(LinearLayout.HORIZONTAL);
        root.addView(tabBar);
        buildTabs();

        content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(content, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(scroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        return root;
    }

    private static final String[] TABS = {"状态", "实时", "心智", "日志", "配置", "模式"};
    private static final int TAB_LIVE = 1;
    private static final int TAB_MIND = 2;
    private static final int TAB_LOG = 3;

    private void buildTabs() {
        tabBar.removeAllViews();
        for (int i = 0; i < TABS.length; i++) {
            final int idx = i;
            TextView t = new TextView(this);
            t.setText(TABS[i]);
            t.setTextSize(15);
            t.setGravity(Gravity.CENTER);
            t.setPadding(0, Theme.dp(this, 12), 0, Theme.dp(this, 12));
            boolean on = i == tab;
            t.setTextColor(on ? Theme.TEXT : Theme.DIM);
            t.setBackgroundColor(on ? Theme.ACCENT : Theme.BG);
            t.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    if (tab == idx) {
                        return;
                    }
                    tab = idx;
                    store.setTab(idx);
                    buildTabs();
                    render();
                }
            });
            tabBar.addView(t, new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        }
    }

    private Button flatButton(String text, View.OnClickListener click) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(14);
        b.setTextColor(Theme.TEXT);
        b.setBackgroundColor(Theme.CARD);
        b.setPadding(Theme.dp(this, 10), 0, Theme.dp(this, 10), 0);
        b.setOnClickListener(click);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.leftMargin = Theme.dp(this, 6);
        b.setLayoutParams(lp);
        return b;
    }

    // ---------------------------------------------------------------- 取数

    /**
     * 拉状态 + schema + 配置值。
     *
     * schema 只在第一次拉（或强制刷新时）—— 它要问渠道方要模型列表，最慢的一环，
     * 而它几乎不变。状态每次都拉，那才是人打开 App 想看的东西。
     */
    private void loadAll(final boolean force) {
        if (busy) {
            return;
        }
        setBusy(true);
        final boolean needSchema = force || schema == null;
        new Thread(new Runnable() {
            @Override
            public void run() {
                final Api api = store.api();
                Map<String, Object> st = null;
                KnobModel.Schema sc = null;
                Map<String, Object> vals = null;
                Map<String, Object> feed = null;
                Map<String, Object> md = null;
                Map<String, Object> lg = null;
                String err = null;
                boolean expired = false;
                try {
                    st = api.status();
                    if (needSchema) {
                        sc = KnobModel.parse(api.schema());
                    }
                    vals = Ui.stable(mapOf(api.config(), "values"));
                    // 观察页的数据独立拉：切页时才拉，但首次打开时一起拿到，
                    // 用户打开 App 第一眼看到的就是「机器人在干啥」。
                    feed = api.live(liveGroup, 60, 0);
                    md = api.mind();
                    lg = api.log(logLevel, logName, 180, 200);
                } catch (Api.ApiException exc) {
                    err = Ui.explain(exc);
                    expired = exc.code == 401;
                } catch (Exception exc) {  // noqa
                    err = Ui.explain(exc);
                }
                final Map<String, Object> fSt = st;
                final KnobModel.Schema fSc = sc;
                final Map<String, Object> fVals = vals;
                final Map<String, Object> fFeed = feed;
                final Map<String, Object> fMd = md;
                final Map<String, Object> fLg = lg;
                final String fErr = err;
                final boolean fExpired = expired;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        setBusy(false);
                        if (fExpired) {
                            store.clearCookie();
                            toast("登录过期了，重新登录一次");
                            kickToLogin();
                            return;
                        }
                        if (fErr != null && fSt == null) {
                            banner.setTextColor(Theme.BAD);
                            banner.setText(fErr);
                            return;
                        }
                        status = fSt;
                        if (fSc != null) {
                            schema = fSc;
                        }
                        values = fVals;
                        edited.clear();
                        if (fFeed != null) {
                            liveData = fFeed;
                            liveGroup = Feed.currentGroup(fFeed, liveGroup);
                            List<Feed.Msg> msgs = Feed.messages(fFeed);
                            if (!msgs.isEmpty()) {
                                liveOldestTs = msgs.get(msgs.size() - 1).ts;
                            }
                        }
                        if (fMd != null) {
                            mindData = fMd;
                        }
                        if (fLg != null) {
                            logData = fLg;
                        }
                        render();
                    }
                });
            }
        }, "load").start();
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> mapOf(Map<String, Object> root, String key) {
        Object v = root == null ? null : root.get(key);
        return v instanceof Map ? (Map<String, Object>) v : new LinkedHashMap<String, Object>();
    }

    private void setBusy(boolean b) {
        busy = b;
        if (refreshBtn != null) {
            refreshBtn.setEnabled(!b);
            refreshBtn.setText(b ? "…" : "刷新");
        }
    }

    /** 版本端点只负责提示，不自动安装：明文 HTTP 下自动下载并静默安装不可控。 */
    private void checkServerVersion() {
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    Map<String, Object> info = store.api().version();
                    final String url = store.api().base() + "/console.apk";
                    Object apk = info.get("apk");
                    Long serverCode = apk instanceof Map ? Json.lng(apk, "version_code") : null;
                    if (apk instanceof Map && Json.bool(apk, "available", false)
                            && serverCode != null && serverCode.longValue() > 1L) {
                        final long bytes = Json.lng(apk, "size") == null ? 0
                                : Json.lng(apk, "size").longValue();
                        ui.post(new Runnable() {
                            @Override
                            public void run() {
                                new AlertDialog.Builder(MainActivity.this)
                                        .setTitle("服务器有新的控制台 APK")
                                        .setMessage("新版大小 " + (bytes / 1024) + " KB，打开下载页更新。")
                                        .setNegativeButton("稍后", null)
                                        .setPositiveButton("打开下载", new DialogInterface.OnClickListener() {
                                            @Override
                                            public void onClick(DialogInterface d, int which) {
                                                startActivity(new Intent(Intent.ACTION_VIEW,
                                                        android.net.Uri.parse(url)));
                                            }
                                        }).show();
                            }
                        });
                    }
                } catch (Exception ignored) {
                    // 版本提示失败不影响状态和配置页。
                }
            }
        }, "version").start();
    }

    // ---------------------------------------------------------------- 渲染

    private void render() {
        content.removeAllViews();
        if (status == null) {
            return;
        }
        StatusFmt.Row head = Ui.headline(status);
        banner.setText(head.value);
        banner.setTextColor(Theme.tone(head.tone));

        switch (tab) {
            case TAB_LIVE:
                renderLive();
                break;
            case TAB_MIND:
                renderMind();
                break;
            case TAB_LOG:
                renderLog();
                break;
            case 4:
                renderConfig();
                break;
            case 5:
                renderModes();
                break;
            default:
                renderStatus();
        }
    }

    // ---- 状态页 ----

    private void renderStatus() {
        List<Object> modes = new ArrayList<Object>();
        if (schema != null) {
            // modeRow 需要原始 modes 列表来查中文名，这里现造一份最小结构
            for (KnobModel.Mode m : schema.modes) {
                Map<String, Object> one = new LinkedHashMap<String, Object>();
                one.put("id", m.id);
                one.put("name", m.name);
                modes.add(one);
            }
        }
        for (StatusFmt.Row r : Ui.statusRows(status, modes)) {
            content.addView(rowView(r));
        }

        Object qq = Json.raw(status, "qq");
        String reason = Json.str(qq, "reason", "");
        if (!reason.isEmpty()) {
            content.addView(note("判定依据：" + reason));
        }
        content.addView(note(Ui.freshness(status)));

        if (schema != null && !schema.actions.isEmpty()) {
            content.addView(sectionTitle("操作"));
            for (final KnobModel.Action a : schema.actions) {
                content.addView(bigButton(a.name, a.danger, new Runnable() {
                    @Override
                    public void run() {
                        confirm(a.name, Ui.confirmAction(a), new Runnable() {
                            @Override
                            public void run() {
                                runAction(a);
                            }
                        });
                    }
                }));
            }
        }
    }

    private View rowView(StatusFmt.Row r) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = Theme.dp(this, 14);
        box.setPadding(pad, Theme.dp(this, 10), pad, Theme.dp(this, 10));

        TextView label = new TextView(this);
        label.setText(r.label);
        label.setTextColor(Theme.DIM);
        label.setTextSize(12);
        box.addView(label);

        TextView value = new TextView(this);
        value.setText(r.value);
        value.setTextColor(Theme.tone(r.tone));
        value.setTextSize(16);
        box.addView(value);
        return box;
    }

    // ---- 实时页（群聊动向）----

    /**
     * 拉 /live。force=true 是下拉/切页时：重置翻页锚点；
     * 轮询（force=false）保留 liveOldestTs，拿到的新消息插在数组前面。
     */
    private void loadFeed(final boolean force) {
        if (busy && !force) {
            return;
        }
        setBusy(true);
        final String group = liveGroup;
        final long before = force ? 0 : liveOldestTs;
        new Thread(new Runnable() {
            @Override
            public void run() {
                final Api api = store.api();
                Map<String, Object> st = null;
                Map<String, Object> md = null;
                Map<String, Object> lg = null;
                String err = null;
                boolean expired = false;
                try {
                    // 切页/下拉时顺带把心智页和日志页也刷了 —— 三页共享一套
                    // 网络开销，一次轮询把该刷的都刷到，省得每页单独等。
                    st = api.live(group, 60, before);
                    if (force && group != null && !group.isEmpty()) {
                        md = api.mind();
                        lg = api.log(logLevel, logName, 180, 200);
                    }
                } catch (Api.ApiException exc) {
                    err = Ui.explain(exc);
                    expired = exc.code == 401;
                } catch (Exception exc) {  // noqa
                    err = Ui.explain(exc);
                }
                final Map<String, Object> fSt = st;
                final Map<String, Object> fMd = md;
                final Map<String, Object> fLg = lg;
                final String fErr = err;
                final boolean fExpired = expired;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        setBusy(false);
                        if (fExpired) {
                            store.clearCookie();
                            toast("登录过期了，重新登录一次");
                            kickToLogin();
                            return;
                        }
                        if (fErr != null && fSt == null) {
                            banner.setTextColor(Theme.BAD);
                            banner.setText(fErr);
                            // 轮询失败不该清掉已有内容 —— 旧数据仍比空页有用
                            return;
                        }
                        if (fSt != null) {
                            liveData = fSt;
                            liveGroup = Feed.currentGroup(fSt, liveGroup);
                            List<Feed.Msg> msgs = Feed.messages(fSt);
                            if (!msgs.isEmpty()) {
                                liveOldestTs = msgs.get(msgs.size() - 1).ts;
                            } else if (force) {
                                liveOldestTs = 0;
                            }
                        }
                        if (fMd != null) {
                            mindData = fMd;
                        }
                        if (fLg != null) {
                            logData = fLg;
                        }
                        if (tab == TAB_LIVE) {
                            render();   // 实时页在轮询时保持新鲜
                        }
                    }
                });
            }
        }, "live").start();
    }

    private void renderLive() {
        if (liveData == null) {
            content.addView(note("还没拿到群聊数据，下拉刷新"));
            content.addView(bigButton("立即刷新", false, new Runnable() {
                @Override
                public void run() {
                    loadFeed(true);
                }
            }));
            return;
        }
        // 群选择器
        List<Feed.Group> groups = Feed.groups(liveData);
        content.addView(sectionTitle("群"));
        if (groups.isEmpty()) {
            content.addView(note("还没有任何群的消息记录"));
        } else {
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setPadding(Theme.dp(this, 6), Theme.dp(this, 4),
                    Theme.dp(this, 6), Theme.dp(this, 4));
            for (final Feed.Group g : groups) {
                boolean cur = g.id.equals(liveGroup) || g.id.equals(Json.str(liveData, "group", ""));
                Button b = new Button(this);
                b.setText(g.label.replace("群 ", "") + (g.botMsgs > 0 ? "·机" : ""));
                b.setAllCaps(false);
                b.setTextSize(13);
                b.setTextColor(cur ? Theme.BG : Theme.TEXT);
                b.setBackgroundColor(cur ? Theme.ACCENT : Theme.CARD);
                b.setPadding(Theme.dp(this, 8), 0, Theme.dp(this, 8), 0);
                b.setOnClickListener(new View.OnClickListener() {
                    @Override
                    public void onClick(View v) {
                        liveGroup = g.id;
                        loadFeed(true);
                    }
                });
                row.addView(b);
            }
            content.addView(row);
        }

        content.addView(sectionTitle("对话"));
        List<Feed.Msg> msgs = Feed.messages(liveData);
        if (msgs.isEmpty()) {
            content.addView(note("该群还没抓到消息"));
        } else {
            for (int i = msgs.size() - 1; i >= 0; i--) {   // 新的在底部
                content.addView(msgView(msgs.get(i)));
            }
        }
        content.addView(note("自动刷新已" + (liveAutoRefresh ? "开"
                : "关") + " · 只显示缓冲，不翻库"));
        content.addView(bigButton(liveAutoRefresh ? "暂停自动刷新" : "开启自动刷新",
                false, new Runnable() {
                    @Override
                    public void run() {
                        liveAutoRefresh = !liveAutoRefresh;
                        render();
                    }
                }));
    }

    private View msgView(Feed.Msg m) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.HORIZONTAL);
        box.setGravity(Gravity.TOP);
        int pad = Theme.dp(this, 12);
        box.setPadding(pad, Theme.dp(this, 6), pad, Theme.dp(this, 6));

        TextView who = new TextView(this);
        who.setText(m.bot ? "🐟" : "👤");
        who.setTextSize(15);
        box.addView(who);

        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        body.setPadding(Theme.dp(this, 8), 0, 0, 0);

        TextView meta = new TextView(this);
        String tag = m.bot ? "机器人" : (m.who == null || m.who.isEmpty() ? "群友" : m.who);
        meta.setText(tag + " · " + StatusFmt.dur(m.ts <= 0 ? -1
                : Math.max(0, (System.currentTimeMillis() / 1000) - m.ts)) + "前");
        meta.setTextColor(m.bot ? Theme.GOOD : Theme.DIM);
        meta.setTextSize(12);
        body.addView(meta);

        TextView txt = new TextView(this);
        txt.setText(m.bot ? ("⤷ " + Ui.stripMd(m.text)) : m.text);
        txt.setTextColor(m.bot ? Theme.TEXT : Theme.DIM);
        txt.setTextSize(15);
        body.addView(txt);

        if (m.reactions != null) {
            TextView r = new TextView(this);
            r.setText("└ 群友反应 " + m.reactions + " 条");
            r.setTextColor(Theme.DIM);
            r.setTextSize(12);
            body.addView(r);
        }
        box.addView(body, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        return box;
    }

    // ---- 心智页（内在状态 + 行为判词 + 能力变化）----

    private void renderMind() {
        if (mindData == null) {
            content.addView(note("还没拿到心智数据，切到实时页下拉一次"));
            return;
        }
        Feed.MindPage page = Feed.mindPage(mindData);
        content.addView(note(page.pluginSummary));
        content.addView(note(page.dynamics));
        if (!page.honesty.isEmpty()) {
            content.addView(note(page.honesty));
        }
        if (page.revisions != null && !page.revisions.isEmpty()) {
            content.addView(sectionTitle("能力变化"));
            for (Feed.MindRow r : page.revisions) {
                content.addView(note(r.text));
            }
        }
        if (page.rows != null && !page.rows.isEmpty()) {
            content.addView(sectionTitle("内在状态"));
            for (Feed.MindRow r : page.rows) {
                content.addView(note(r.text));
            }
        }
        if (page.effects != null && !page.effects.isEmpty()) {
            content.addView(sectionTitle("发出的话 & 反应"));
            for (Feed.MindRow r : page.effects) {
                content.addView(note(r.text));
            }
        }
        if (!page.hasData) {
            content.addView(note("还没有观测数据（dsh-mind / dsh-effect 可能刚部署）"));
        }
    }

    // ---- 日志页 ----

    private void renderLog() {
        if (logData == null) {
            content.addView(note("还没拿到日志，切到实时页下拉一次"));
            return;
        }
        content.addView(sectionTitle("近期运行日志"));
        List<Feed.LogLine> lines = Feed.logLines(logData);
        if (lines.isEmpty()) {
            content.addView(note("这个时间段没有日志"));
        } else {
            for (Feed.LogLine l : lines) {
                LinearLayout box = new LinearLayout(this);
                box.setOrientation(LinearLayout.HORIZONTAL);
                box.setPadding(Theme.dp(this, 12), Theme.dp(this, 5),
                        Theme.dp(this, 12), Theme.dp(this, 5));
                TextView ts = new TextView(this);
                ts.setText(l.ts);
                ts.setTextColor(Theme.DIM);
                ts.setTextSize(11);
                box.addView(ts);
                LinearLayout body = new LinearLayout(this);
                body.setOrientation(LinearLayout.VERTICAL);
                body.setPadding(Theme.dp(this, 8), 0, 0, 0);
                TextView tag = new TextView(this);
                tag.setText(l.tag == null || l.tag.isEmpty() ? "core" : l.tag);
                tag.setTextColor(Theme.tone(l.tone));
                tag.setTextSize(12);
                body.addView(tag);
                TextView text = new TextView(this);
                text.setText(l.text);
                text.setTextColor(Theme.tone(l.tone));
                text.setTextSize(13);
                body.addView(text);
                box.addView(body, new LinearLayout.LayoutParams(0,
                        ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
                content.addView(box);
            }
        }
        content.addView(note("显示最近 3 小时 · " + Json.str(logData, "name", "全部插件")
                + " · " + logLevel));
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        String[] picks = {"all", "info", "warn", "err"};
        for (final String p : picks) {
            boolean cur = p.equals(logLevel);
            Button b = new Button(this);
            b.setText(p);
            b.setAllCaps(false);
            b.setTextSize(13);
            b.setTextColor(cur ? Theme.BG : Theme.TEXT);
            b.setBackgroundColor(cur ? Theme.ACCENT : Theme.CARD);
            b.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    logLevel = p;
                    loadFeed(true);
                }
            });
            row.addView(b);
        }
        content.addView(row);
    }

    // ---- 配置页 ----

    private void renderConfig() {
        if (schema == null || values == null) {
            content.addView(note("还没拿到配置，下拉刷新"));
            return;
        }
        content.addView(note(Ui.configSummary(status, rawModes(), values)));

        content.addView(sectionTitle("聊天模型"));
        final String curModel = Json.str(Json.raw(status, "models"), "chat_model", "");
        content.addView(pickerRow(curModel.isEmpty() ? "—" : curModel, new Runnable() {
            @Override
            public void run() {
                pickChatModel(curModel);
            }
        }));

        content.addView(sectionTitle("识图渠道"));
        final String curVision = Json.str(Json.raw(status, "models"), "vision_provider", "");
        String visionLabel = Json.str(Json.raw(status, "models"), "vision_model", curVision);
        content.addView(pickerRow(visionLabel.isEmpty() ? "—" : visionLabel, new Runnable() {
            @Override
            public void run() {
                pickVision(curVision);
            }
        }));

        for (String group : schema.groups()) {
            content.addView(sectionTitle(group));
            for (KnobModel.Knob k : schema.inGroup(group)) {
                content.addView(knobView(k));
            }
        }

        content.addView(sectionTitle("插件"));
        for (final Ui.PluginItem p : Ui.plugins(status)) {
            content.addView(pluginView(p));
        }

        content.addView(bigButton("保存改动", false, new Runnable() {
            @Override
            public void run() {
                submitChanges();
            }
        }));
    }

    private List<Object> rawModes() {
        List<Object> out = new ArrayList<Object>();
        if (schema != null) {
            for (KnobModel.Mode m : schema.modes) {
                Map<String, Object> one = new LinkedHashMap<String, Object>();
                one.put("id", m.id);
                one.put("name", m.name);
                out.add(one);
            }
        }
        return out;
    }

    /** 一个旋钮的行。bool 画勾选框，数值画滑块，enum 画点击选择。 */
    private View knobView(final KnobModel.Knob k) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = Theme.dp(this, 14);
        box.setPadding(pad, Theme.dp(this, 10), pad, Theme.dp(this, 10));

        final Object cur = edited.containsKey(k.path) ? edited.get(k.path) : values.get(k.path);

        if (k.isBool()) {
            CheckBox cb = new CheckBox(this);
            cb.setText(k.name);
            cb.setTextColor(Theme.TEXT);
            cb.setTextSize(16);
            cb.setChecked(KnobModel.truthy(cur));
            cb.setOnCheckedChangeListener(new CompoundButton.OnCheckedChangeListener() {
                @Override
                public void onCheckedChanged(CompoundButton v, boolean checked) {
                    edited.put(k.path, Boolean.valueOf(checked));
                }
            });
            box.addView(cb);
        } else if (k.isNumber()) {
            final TextView label = new TextView(this);
            label.setText(k.name + "：" + KnobModel.show(k, cur));
            label.setTextColor(Theme.TEXT);
            label.setTextSize(16);
            box.addView(label);

            SeekBar bar = new SeekBar(this);
            bar.setMax(KnobModel.ticks(k) - 1);
            bar.setProgress(KnobModel.toTick(k, cur));
            bar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
                @Override
                public void onProgressChanged(SeekBar sb, int p, boolean fromUser) {
                    Object v = KnobModel.fromTick(k, p);
                    label.setText(k.name + "：" + KnobModel.show(k, v));
                    if (fromUser) {
                        edited.put(k.path, v);
                    }
                }

                @Override
                public void onStartTrackingTouch(SeekBar sb) {
                }

                @Override
                public void onStopTrackingTouch(SeekBar sb) {
                }
            });
            box.addView(bar);
        } else if (k.isText()) {
            final EditText input = new EditText(this);
            input.setText(KnobModel.csvText(cur));
            input.setTextColor(Theme.TEXT);
            input.setTextSize(16);
            input.setSingleLine(false);
            input.setHint(k.name + (k.isCsv() ? "（逗号分隔）" : ""));
            input.setEnabled(!k.readOnly());
            input.setOnFocusChangeListener(new View.OnFocusChangeListener() {
                @Override
                public void onFocusChange(View v, boolean hasFocus) {
                    if (!hasFocus && !k.readOnly()) {
                        edited.put(k.path, k.isCsv()
                                ? KnobModel.csvParse(input.getText().toString())
                                : input.getText().toString());
                    }
                }
            });
            box.addView(input, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        } else {
            TextView t = new TextView(this);
            t.setText(k.name + "：" + KnobModel.show(k, cur));
            t.setTextColor(Theme.TEXT);
            t.setTextSize(16);
            t.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    pickEnum(k);
                }
            });
            box.addView(t);
        }

        if (k.hint != null && !k.hint.isEmpty()) {
            TextView hint = new TextView(this);
            hint.setText(Ui.stripMd(k.hint) + (k.hot ? "" : "（改完要重启才生效）"));
            hint.setTextColor(Theme.DIM);
            hint.setTextSize(12);
            box.addView(hint);
        }
        return box;
    }

    private View pluginView(final Ui.PluginItem p) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.HORIZONTAL);
        box.setGravity(Gravity.CENTER_VERTICAL);
        int pad = Theme.dp(this, 14);
        box.setPadding(pad, Theme.dp(this, 6), pad, Theme.dp(this, 6));

        TextView name = new TextView(this);
        name.setText(p.label);
        name.setTextColor(Theme.TEXT);
        name.setTextSize(16);
        box.addView(name, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        final CheckBox cb = new CheckBox(this);
        cb.setChecked(p.enabled);
        cb.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                final boolean want = cb.isChecked();
                // 插件开关是**立即生效**的单独接口，不进「保存改动」那一批 ——
                // 混在一起会让人以为没保存就没生效。
                cb.setEnabled(false);
                call(new Job() {
                    @Override
                    public String run(Api api) throws Exception {
                        api.setPlugin(p.name, want);
                        return (want ? "已开启「" : "已关闭「") + p.label + "」";
                    }
                }, true);
            }
        });
        box.addView(cb);
        return box;
    }

    // ---- 模式页 ----

    private void renderModes() {
        if (schema == null) {
            content.addView(note("还没拿到模式列表，下拉刷新"));
            return;
        }
        String cur = Json.str(status, "mode", "");
        for (final KnobModel.Mode m : schema.modes) {
            LinearLayout box = new LinearLayout(this);
            box.setOrientation(LinearLayout.VERTICAL);
            int pad = Theme.dp(this, 14);
            box.setPadding(pad, Theme.dp(this, 12), pad, Theme.dp(this, 12));
            box.setBackgroundColor(m.id.equals(cur) ? Theme.CARD : Theme.BG);

            TextView name = new TextView(this);
            name.setText(m.id.equals(cur) ? (m.name + "  ← 当前") : m.name);
            name.setTextColor(m.id.equals(cur) ? Theme.GOOD : Theme.TEXT);
            name.setTextSize(17);
            box.addView(name);

            if (m.desc != null && !m.desc.isEmpty()) {
                TextView d = new TextView(this);
                d.setText(Ui.stripMd(m.desc));
                d.setTextColor(Theme.DIM);
                d.setTextSize(13);
                box.addView(d);
            }
            box.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    confirm("切换模式", Ui.confirmMode(m), new Runnable() {
                        @Override
                        public void run() {
                            applyMode(m);
                        }
                    });
                }
            });
            content.addView(box);
        }
    }

    // ---------------------------------------------------------------- 动作

    private void submitChanges() {
        if (schema == null || values == null) {
            return;
        }
        final Map<String, Object> changed = KnobModel.diff(schema, values, edited);
        if (changed.isEmpty()) {
            toast("没有改动");
            return;
        }
        // 先本地校验，别让人白等一个来回
        for (Map.Entry<String, Object> e : changed.entrySet()) {
            String err = KnobModel.validate(schema.knob(e.getKey()), e.getValue());
            if (err != null) {
                toast(err);
                return;
            }
        }
        confirm("保存改动", Ui.confirmChanges(schema, values, changed), new Runnable() {
            @Override
            public void run() {
                call(new Job() {
                    @Override
                    public String run(Api api) throws Exception {
                        Map<String, Object> out = api.setKnobs(changed);
                        int n = Json.arr(out, "changed").size();
                        return "已保存 " + n + " 项";
                    }
                }, true);
            }
        });
    }

    private void applyMode(final KnobModel.Mode m) {
        call(new Job() {
            @Override
            public String run(Api api) throws Exception {
                Map<String, Object> out = api.applyMode(m.id);
                store.setLastMode(m.id);
                return Api.describeMode(out);
            }
        }, true);
    }

    private void runAction(final KnobModel.Action a) {
        call(new Job() {
            @Override
            public String run(Api api) throws Exception {
                Map<String, Object> out = api.action(a.id);
                String msg = Json.str(out, "message", "");
                return msg.isEmpty() ? (a.name + " 已执行") : msg;
            }
        }, true);
    }

    private void pickChatModel(final String current) {
        if (schema == null) {
            return;
        }
        final List<String> choices = Ui.modelChoices(schema.chatModels, current);
        pick("选聊天模型", choices, new Pick() {
            @Override
            public void picked(int idx) {
                final String model = Ui.modelOf(choices.get(idx));
                if (model.isEmpty() || model.equals(current) || model.startsWith("（")) {
                    return;
                }
                confirm("换模型", "把聊天模型换成 " + model + "。\n\n"
                        + "换完立即生效，正在进行的对话会用新模型继续。", new Runnable() {
                    @Override
                    public void run() {
                        call(new Job() {
                            @Override
                            public String run(Api api) throws Exception {
                                api.setChatModel(model);
                                return "已换成 " + model;
                            }
                        }, true);
                    }
                });
            }
        });
    }

    private void pickVision(final String current) {
        if (schema == null) {
            return;
        }
        final List<String> choices = Ui.visionChoices(schema, current);
        pick("选识图渠道", choices, new Pick() {
            @Override
            public void picked(int idx) {
                final String id = Ui.visionIdOf(schema, choices.get(idx));
                if (id.isEmpty() || id.equals(current)) {
                    return;
                }
                call(new Job() {
                    @Override
                    public String run(Api api) throws Exception {
                        api.setVision(id);
                        return "识图已切到 " + id;
                    }
                }, true);
            }
        });
    }

    private void pickEnum(final KnobModel.Knob k) {
        if (k.options.isEmpty()) {
            return;
        }
        final List<String> choices = new ArrayList<String>(k.options);
        pick(k.name, choices, new Pick() {
            @Override
            public void picked(int idx) {
                edited.put(k.path, choices.get(idx));
                render();
            }
        });
    }

    // ---------------------------------------------------------------- 通用

    /** 一件要联网的事。返回给用户看的成功消息。 */
    private interface Job {
        String run(Api api) throws Exception;
    }

    private interface Pick {
        void picked(int idx);
    }

    /**
     * 跑一件联网的事：禁按钮 → 后台执行 → 回主线程报结果 →（可选）重新拉数据。
     *
     * 每个写操作后都重新拉一次状态，是因为服务端的改动会连带影响别的字段
     * （比如切模式同时改了模型和插件）。不重拉的话界面会显示旧值，
     * 用户以为没生效又点一次。
     */
    private void call(final Job job, final boolean reloadAfter) {
        if (busy) {
            toast("上一个操作还没完成");
            return;
        }
        setBusy(true);
        banner.setTextColor(Theme.DIM);
        banner.setText("正在执行…");
        new Thread(new Runnable() {
            @Override
            public void run() {
                final Api api = store.api();
                String okMsg = null;
                String err = null;
                boolean expired = false;
                try {
                    okMsg = job.run(api);
                } catch (Api.ApiException exc) {
                    err = Ui.explain(exc);
                    expired = exc.code == 401;
                } catch (Exception exc) {  // noqa
                    err = Ui.explain(exc);
                }
                final String fOk = okMsg;
                final String fErr = err;
                final boolean fExpired = expired;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        setBusy(false);
                        if (fExpired) {
                            store.clearCookie();
                            kickToLogin();
                            return;
                        }
                        if (fErr != null) {
                            banner.setTextColor(Theme.BAD);
                            banner.setText(fErr);
                            alert("没成功", fErr);
                            return;
                        }
                        toast(fOk == null ? "完成" : fOk);
                        if (reloadAfter) {
                            loadAll(false);
                        }
                    }
                });
            }
        }, "job").start();
    }

    private void confirm(String title, String body, final Runnable yes) {
        new AlertDialog.Builder(this)
                .setTitle(title)
                .setMessage(body)
                .setNegativeButton("取消", null)
                .setPositiveButton("确定", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        yes.run();
                    }
                })
                .show();
    }

    private void alert(String title, String body) {
        new AlertDialog.Builder(this).setTitle(title).setMessage(body)
                .setPositiveButton("知道了", null).show();
    }

    private void pick(String title, List<String> choices, final Pick cb) {
        final String[] arr = choices.toArray(new String[0]);
        new AlertDialog.Builder(this)
                .setTitle(title)
                .setItems(arr, new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        cb.picked(which);
                    }
                })
                .show();
    }

    private void toast(String text) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
    }

    private View sectionTitle(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextColor(Theme.ACCENT);
        t.setTextSize(13);
        int pad = Theme.dp(this, 14);
        t.setPadding(pad, Theme.dp(this, 18), pad, Theme.dp(this, 4));
        return t;
    }

    private View note(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextColor(Theme.DIM);
        t.setTextSize(12);
        int pad = Theme.dp(this, 14);
        t.setPadding(pad, Theme.dp(this, 8), pad, Theme.dp(this, 8));
        return t;
    }

    private View pickerRow(String value, final Runnable click) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.HORIZONTAL);
        box.setGravity(Gravity.CENTER_VERTICAL);
        int pad = Theme.dp(this, 14);
        box.setPadding(pad, Theme.dp(this, 12), pad, Theme.dp(this, 12));
        box.setBackgroundColor(Theme.CARD);

        TextView t = new TextView(this);
        t.setText(value);
        t.setTextColor(Theme.TEXT);
        t.setTextSize(16);
        box.addView(t, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        TextView arrow = new TextView(this);
        arrow.setText("更改 ›");
        arrow.setTextColor(Theme.ACCENT);
        arrow.setTextSize(14);
        box.addView(arrow);

        box.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                click.run();
            }
        });
        return box;
    }

    private View bigButton(String text, boolean danger, final Runnable click) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(16);
        b.setTextColor(Color.WHITE);
        b.setBackgroundColor(danger ? Theme.BAD : Theme.ACCENT);
        b.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                click.run();
            }
        });
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        int m = Theme.dp(this, 14);
        lp.setMargins(m, Theme.dp(this, 8), m, Theme.dp(this, 4));
        b.setLayoutParams(lp);
        return b;
    }

    private void kickToLogin() {
        Intent i = new Intent(this, LoginActivity.class);
        i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        startActivity(i);
        finish();
    }
}
