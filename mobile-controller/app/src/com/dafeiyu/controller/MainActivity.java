package com.dafeiyu.controller;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * 壳 Activity：四个页签（机器人 / 登录 QQ / 服务器 / 教程）。
 *
 * 页签切换只是改可见性 —— 各页面各自持有状态（连接、已渲染的列表），
 * 切走再切回来不丢。竖屏锁定：列表和二维码的排版按竖屏设计。
 *
 * 「机器人」页是主界面：只填三个配置就能把机器人跑起来，而且能多开。
 */
public final class MainActivity extends Activity {

    private Store store;
    private LoginView loginView;
    private RobotsView robotsView;
    private ServerView serverView;
    private Button[] tabs;
    private View[] pages;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        store = new Store(this);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Theme.BG);

        // 头部
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.VERTICAL);
        header.setPadding(Theme.dp(this, 16), Theme.dp(this, 14), Theme.dp(this, 16),
                Theme.dp(this, 6));
        TextView title = UiKit.text(this, "大肥鱼", 19, Theme.TEXT);
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        header.addView(title);
        header.addView(UiKit.text(this, "填三个配置就能跑 · 支持多个 QQ 号同时在线",
                11, Theme.DIM));
        root.addView(header);

        // 页签
        LinearLayout tabRow = new LinearLayout(this);
        tabRow.setOrientation(LinearLayout.HORIZONTAL);
        int pad = Theme.dp(this, 8);
        tabRow.setPadding(pad, Theme.dp(this, 4), pad, 0);
        String[] names = {"机器人", "登录 QQ", "服务器", "教程"};
        tabs = new Button[names.length];
        for (int i = 0; i < names.length; i++) {
            final int index = i;
            Button b = new Button(this);
            b.setText(names[i]);
            b.setAllCaps(false);
            b.setTextSize(12);
            b.setTextColor(Theme.TEXT);
            b.setBackground(tabStyle(false));
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
            lp.setMargins(Theme.dp(this, 2), 0, Theme.dp(this, 2), 0);
            b.setLayoutParams(lp);
            b.setOnClickListener(new View.OnClickListener() {
                public void onClick(View v) {
                    selectTab(index);
                }
            });
            tabRow.addView(b);
            tabs[i] = b;
        }
        root.addView(tabRow);

        // 页面
        loginView = new LoginView(this, new LoginView.Host() {
            public void toast(String msg) {
                android.widget.Toast.makeText(MainActivity.this, msg,
                        android.widget.Toast.LENGTH_SHORT).show();
            }

            public void openWebLogin(String base) {
                Intent it = new Intent(MainActivity.this, WebLoginActivity.class);
                it.putExtra("base", base);
                startActivity(it);
            }
        }, store);

        robotsView = new RobotsView(this, new RobotsView.Host() {
            public void toast(String msg) {
                android.widget.Toast.makeText(MainActivity.this, msg,
                        android.widget.Toast.LENGTH_SHORT).show();
            }

            public boolean connected() {
                return Session.connected();
            }

            public void gotoServerTab() {
                selectTab(2);
            }

            public void gotoLoginTab() {
                selectTab(1);
            }
        });

        serverView = new ServerView(this, new ServerView.Host() {
            public void toast(String msg) {
                android.widget.Toast.makeText(MainActivity.this, msg,
                        android.widget.Toast.LENGTH_SHORT).show();
            }

            public void onConnectionChanged() {
                robotsView.onShow();
            }
        });

        pages = new View[]{robotsView.view(), loginView.view(),
                serverView.view(), tutorialView()};
        for (View p : pages) {
            // 高度 0 + weight 1：让 ScrollView 吃掉标题和页签之外的剩余空间
            root.addView(p, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        }
        setContentView(root);
        selectTab(Math.max(0, Math.min(3, store.tab())));
    }

    private android.graphics.drawable.GradientDrawable tabStyle(boolean active) {
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable();
        bg.setColor(active ? Theme.ACCENT : UiKit.withAlpha(Theme.CARD_LINE, 0.6f));
        bg.setCornerRadius(Theme.dp(this, 8));
        return bg;
    }

    private void selectTab(int index) {
        for (int i = 0; i < tabs.length; i++) {
            tabs[i].setBackground(tabStyle(i == index));
            tabs[i].setTextColor(i == index ? 0xFFFFFFFF : Theme.TEXT);
            pages[i].setVisibility(i == index ? View.VISIBLE : View.GONE);
        }
        store.setTab(index);
        if (index == 0) {
            robotsView.onShow();
        }
        if (index == 1) {
            loginView.onShow();
        }
        if (index == 2) {
            serverView.onShow();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        loginView.onResume();
    }

    @Override
    protected void onPause() {
        super.onPause();
        loginView.onPause();
    }

    // ------------------------------------------------------------ 教程

    private View tutorialView() {
        ScrollView scroll = UiKit.scroll(this);
        LinearLayout page = UiKit.pageColumn(this);
        scroll.addView(page);
        TextView t = UiKit.text(this, TUTORIAL, 14, Theme.TEXT);
        t.setLineSpacing(Theme.dp(this, 2), 1f);
        page.addView(t);
        return scroll;
    }

    private static final String TUTORIAL = ""
            + "这个 App 让你在手机上把机器人跑起来，并且能同时挂好几个 QQ 号。\n"
            + "\n"
            + "【怎么用：填三个配置就行】\n"
            + " 1. 打开 App，「服务器」页会自动连上（如果没连上，点一下「连接」）。\n"
            + " 2. 到「机器人」页，起个名字点「新建」—— 一个机器人 = 一个 QQ 号。\n"
            + " 3. 点这个机器人的「启动」，等十几秒。\n"
            + " 4. 到「登录 QQ」页扫码，把这个号登上去。\n"
            + " 5. 回到「机器人」页，点「展开配置」，填三样东西：\n"
            + "    ① 主聊天 API：接口地址、API Key、模型名（用你自己的）。\n"
            + "    ② 聊天范围：要它说话的群号，和允许私聊的 QQ 号，逗号隔开。\n"
            + "    ③ 人格提示词：它是谁、该怎么说话。\n"
            + "    点「保存到服务器」，约 10 秒后生效。\n"
            + "\n"
            + "【想同时挂好几个 QQ 号？】\n"
            + " · 在「机器人」页再点一次「新建」，起另一个名字（比如 qq2）。\n"
            + " · 各自「启动」，各自到「登录 QQ」页扫码登录不同的号。\n"
            + " · 每个号的配置互相独立，互不影响。\n"
            + " · 服务器会自动给每个机器人分配独立的端口和数据目录，\n"
            + "   你不用管这些，也不会串号。\n"
            + "\n"
            + "【API Key 从哪来？】\n"
            + " · 去你想用的模型服务商那里申请（DeepSeek、通义、智谱、月之暗面等）。\n"
            + " · 接口地址一般形如 https://api.xxx.com/v1，模型名问服务商要。\n"
            + " · App 不保存 Key，界面上也不回显；Key 只写进服务器上该机器人的配置里。\n"
            + "\n"
            + "【机器人不回话怎么办？】\n"
            + " · 先看「机器人」页那行状态是不是「运行中」。\n"
            + " · 再看「登录 QQ」页顶部状态是不是在线（离线就重新扫码）。\n"
            + " · 再看群号/私聊号填对没有 —— 机器人只在你填的会话里说话。\n"
            + " · 还不行就检查 API Key 和模型名，填错会导致它收到消息但答不出来。\n"
            + "\n"
            + "【安全说明】\n"
            + " · App 和服务器之间是一条加密隧道，服务器上的管理端口不对公网开放。\n"
            + " · App 里内置的是一把「只能连管理端口」的专用钥匙，\n"
            + "   它登不了服务器的命令行，也连不了别的端口。\n"
            + " · 手机上不保存 QQ 密码、API Key 这类东西，退出 App 就没了。\n"
            + " · 这个 App 只管理它自己创建的那些机器人，不碰服务器上别的东西。\n";
}
