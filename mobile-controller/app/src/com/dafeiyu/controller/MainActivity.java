package com.dafeiyu.controller;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * 壳 Activity：三个页签（登录 QQ / 控制台 / 教程）+ 各页面的宿主。
 *
 * 页签切换只是改可见性 —— 两个页面各自持有状态（连接、已渲染的旋钮），
 * 切走再切回来不丢。竖屏锁定：旋钮和二维码的排版按竖屏设计，旋转重建
 * 会把这些状态冲掉，不值得为它写恢复逻辑。
 */
public final class MainActivity extends Activity {

    private Store store;
    private LoginView loginView;
    private ConsoleView consoleView;
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
        TextView title = UiKit.text(this, "大肥鱼控制台", 19, Theme.TEXT);
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        header.addView(title);
        header.addView(UiKit.text(this, "开放式登录 + 部署控制 · 第 1 版", 11, Theme.DIM));
        root.addView(header);

        // 页签
        LinearLayout tabRow = new LinearLayout(this);
        tabRow.setOrientation(LinearLayout.HORIZONTAL);
        int pad = Theme.dp(this, 12);
        tabRow.setPadding(pad, Theme.dp(this, 4), pad, 0);
        String[] names = {"登录 QQ", "控制台", "教程"};
        tabs = new Button[names.length];
        for (int i = 0; i < names.length; i++) {
            final int index = i;
            Button b = new Button(this);
            b.setText(names[i]);
            b.setAllCaps(false);
            b.setTextSize(13);
            b.setTextColor(Theme.TEXT);
            b.setBackground(tabStyle(false));
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
            lp.setMargins(Theme.dp(this, 3), 0, Theme.dp(this, 3), 0);
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
        consoleView = new ConsoleView(this, new ConsoleView.Host() {
            public void toast(String msg) {
                android.widget.Toast.makeText(MainActivity.this, msg,
                        android.widget.Toast.LENGTH_LONG).show();
            }
        }, store);
        pages = new View[]{loginView.view(), consoleView.view(), tutorialView()};
        for (View p : pages) {
            // 高度 0 + weight 1：让 ScrollView 吃掉标题和页签之外的剩余空间，
            // 写 WRAP_CONTENT 会让滚动区缩成内容高度，页面短时底部留白、长时滚不动。
            root.addView(p, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        }
        setContentView(root);
        selectTab(Math.max(0, Math.min(2, store.tab())));
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
            + "这个 App 做两件事：把机器人 QQ 登录到你自己的服务器上，"
            + "以及像桌面控制台一样一键部署。\n"
            + "\n"
            + "【先准备一台服务器】\n"
            + " · 一台 Linux 服务器（推荐 Debian 12 或 Ubuntu 22.04），内存 2 GB 以上。\n"
            + " · 你能用 SSH 登录它（有地址、端口、用户名和密码）。\n"
            + " · 安全组放行：SSH 端口、NapCat 端口、AstrBot 端口（默认 6185/6186）。\n"
            + "\n"
            + "【部署机器人】（「控制台」页）\n"
            + " 1. 填服务器地址、SSH 端口、用户名、密码。\n"
            + " 2. 源码仓库地址保持默认（官方镜像仓库），或换成你自己的 fork。\n"
            + " 3. 点「测试连接」—— 只读检查系统、Git、Docker 和磁盘，不改服务器。\n"
            + " 4. 点「开始部署」—— 自动完成：下载源码 → 准备目录与配置 → 拉镜像 → 启动。\n"
            + "    部署目录默认 ~/dafeiyu-bot，只管理带专用标记的目录，不会动你已有的项目。\n"
            + " 5. 部署完成后在「配置」里按需调参数；没把握就先不动。\n"
            + "\n"
            + "【告诉机器人该在哪儿说话、用哪个模型】（「控制台」页 ⑤）\n"
            + " · 群号：填允许机器人说话的群，多个用逗号隔开（如 100000001,100000002）。\n"
            + " · 私聊 QQ 号：填允许机器人回私聊的人，多个用逗号隔开。\n"
            + " · 两个都填就群聊私聊都管；填完点「写入服务器」——\n"
            + "   服务器只会在这几个会话里说话，别处一律不理（这是机器人的白名单）。\n"
            + " · 主聊天 API：接口地址、API Key、模型名，用**你自己的**。\n"
            + "   这个 App 不带任何 API Key，你不填机器人就没法回答。\n"
            + "   API Key 只上传到你的服务器，App 不保存、界面不回显。\n"
            + " · 写完 App 会自动重启 AstrBot 让配置生效（约 10 秒）。\n"
            + "\n"
            + "【登录 QQ】（「登录」页）\n"
            + " 1. 填 NapCat WebUI 的地址（服务器 IP:WebUI 端口）和 Token。\n"
            + "    Token 在服务器的 webui.json 里，或容器启动日志里找。\n"
            + " 2. 点「连接」。之后三个登录方式任选：\n"
            + "    · 扫码 —— 页面直接出二维码，用手机 QQ 扫；\n"
            + "    · 密码 —— 填 QQ 号和密码点登录；要求安全验证时按提示切网页；\n"
            + "    · 快速登录 —— 服务器上登录过的号一键再登。\n"
            + " 3. 密码框上面那个 QQ 号还是**防呆校验**：登录后 App 会拿服务器上\n"
            + "   真正登录的号和它比，对不上会红字提醒你（登录错号从二维码上看不出来）。\n"
            + " 4. 顶部「机器人状态」每 2.5 秒自动刷新，在线变绿就成功了。\n"
            + "\n"
            + "【安全边界】\n"
            + " · App 里不保存任何密码/Token/密钥：SSH 密码、WebUI Token、QQ 密码\n"
            + "   都只在内存里，退出即没。保存的只有地址类的非敏感配置。\n"
            + " · App 与服务器之间是明文 HTTP/SSH 2，别在不可信的公共 Wi-Fi 下操作。\n"
            + " · 部署只动带 .dafeiyu-managed 标记的目录，绝不会覆盖你已有的部署。\n"
            + " · 部署日志全部脱敏后上屏，不会把密码打进日志。\n"
            + "\n"
            + "【常见问题】\n"
            + " · 「首次连接该服务器」：勾选「首次连接接受服务器指纹」再连一次。\n"
            + " · 「当前账号无权访问 Docker」：用 root，或把用户加入 docker 组。\n"
            + " · 「部署目录已存在且不是本控制台创建的」：换一个部署目录。\n"
            + " · 二维码扫不动：点「刷新二维码」；还不行就「重启 NapCat」出新码。\n"
            + " · 密码登录要求验证码/新设备验证：点「打开内置网页登录页」完成那一步。\n"
            + " · 「服务器上还没有 AstrBot 配置」：先点「开始部署」把容器跑起来一次，\n"
            + "   再回来写聊天范围和主聊天 API。\n"
            + " · 机器人不回复：先看「运行状态」两个容器是不是 running；\n"
            + "   再看群号/私聊号有没有写对（机器人只在白名单里的会话说话）。\n";
}
