package com.dafeiyu.console;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.text.method.LinkMovementMethod;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * 登录页：填服务器地址 + 密码，换一个 7 天有效的 cookie。
 *
 * 界面是**用代码搭的**，不是 XML 布局。原因很实际：这个项目没有 Gradle，
 * 资源全靠手写 aapt2 命令编，多一个布局 XML 就多一处 ID 对不上就崩的风险。
 * 代码搭布局丑一点，但改起来只需 javac 通过，不必猜 R.id 生成对不对。
 *
 * 三个刻意的行为：
 *   · 密码框永远不预填、不记住 —— {@link Store} 根本没有存密码的字段；
 *   · 登录过程中禁用按钮，防连点触发服务端的爆破锁定（连错 5 次锁 30 秒起，翻倍到 1 小时）；
 *   · 明文 HTTP 的风险直接写在页面上。这是真实局限，藏起来不如让人知道。
 */
public class LoginActivity extends Activity {

    private Store store;
    private EditText hostBox;
    private EditText pwBox;
    private Button loginBtn;
    private TextView msg;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        store = new Store(this);

        // 已经有 cookie 就别再问一次密码，直接进去；失效了主页会踢回来。
        if (store.loggedIn()) {
            goMain();
            return;
        }

        setContentView(buildView());
    }

    private View buildView() {
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Theme.BG);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = Theme.dp(this, 24);
        root.setPadding(pad, Theme.dp(this, 48), pad, pad);
        scroll.addView(root, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView title = new TextView(this);
        title.setText("大肥鱼控制台");
        title.setTextColor(Theme.TEXT);
        title.setTextSize(26);
        title.setGravity(Gravity.CENTER_HORIZONTAL);
        root.addView(title);

        TextView sub = new TextView(this);
        sub.setText("QQ 聊天机器人的状态与开关");
        sub.setTextColor(Theme.DIM);
        sub.setTextSize(14);
        sub.setGravity(Gravity.CENTER_HORIZONTAL);
        sub.setPadding(0, Theme.dp(this, 6), 0, Theme.dp(this, 36));
        root.addView(sub);

        root.addView(label("服务器地址"));
        hostBox = new EditText(this);
        hostBox.setHint("your-server.example.com:8088");
        hostBox.setHintTextColor(Theme.DIM);
        hostBox.setTextColor(Theme.TEXT);
        hostBox.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        hostBox.setSingleLine(true);
        String remembered = store.base();
        hostBox.setText(remembered.isEmpty() ? "your-server.example.com:8088" : remembered);
        root.addView(hostBox);

        TextView hostHint = new TextView(this);
        hostHint.setText("不写端口默认 8088（安全组只放行了这个口）");
        hostHint.setTextColor(Theme.DIM);
        hostHint.setTextSize(12);
        hostHint.setPadding(0, Theme.dp(this, 4), 0, Theme.dp(this, 18));
        root.addView(hostHint);

        root.addView(label("密码"));
        pwBox = new EditText(this);
        pwBox.setHint("扫码页那个密码");
        pwBox.setHintTextColor(Theme.DIM);
        pwBox.setTextColor(Theme.TEXT);
        pwBox.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        pwBox.setSingleLine(true);
        root.addView(pwBox);

        TextView pwHint = new TextView(this);
        pwHint.setText("和网页版扫码页同一个密码。本机不保存密码，只保存服务器签的登录凭据（7 天有效）。");
        pwHint.setTextColor(Theme.DIM);
        pwHint.setTextSize(12);
        pwHint.setPadding(0, Theme.dp(this, 4), 0, Theme.dp(this, 24));
        root.addView(pwHint);

        loginBtn = new Button(this);
        loginBtn.setText("登录");
        loginBtn.setAllCaps(false);
        loginBtn.setTextColor(Color.WHITE);
        loginBtn.setBackgroundColor(Theme.ACCENT);
        loginBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                doLogin();
            }
        });
        root.addView(loginBtn);

        msg = new TextView(this);
        msg.setTextColor(Theme.BAD);
        msg.setTextSize(14);
        msg.setPadding(0, Theme.dp(this, 16), 0, 0);
        msg.setMovementMethod(LinkMovementMethod.getInstance());
        root.addView(msg);

        TextView warn = new TextView(this);
        warn.setText("提醒：8088 是明文 HTTP，密码在网络上不加密。"
                + "在可信网络下用它没问题，在公共 Wi-Fi 上要注意。");
        warn.setTextColor(Theme.WARN);
        warn.setTextSize(12);
        warn.setPadding(0, Theme.dp(this, 40), 0, 0);
        root.addView(warn);

        return scroll;
    }

    private TextView label(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextColor(Theme.TEXT);
        t.setTextSize(15);
        t.setPadding(0, 0, 0, Theme.dp(this, 6));
        return t;
    }

    private void doLogin() {
        final String host = hostBox.getText().toString().trim();
        final String pw = pwBox.getText().toString();
        if (host.isEmpty()) {
            fail("先填服务器地址");
            return;
        }
        if (pw.isEmpty()) {
            fail("密码不能填空");
            return;
        }
        setBusy(true);
        msg.setTextColor(Theme.DIM);
        msg.setText("正在登录…");

        // 网络必须离开主线程，否则 Android 直接抛 NetworkOnMainThreadException。
        new Thread(new Runnable() {
            @Override
            public void run() {
                final Api api = new Api(host, "");
                String err = null;
                try {
                    api.login(pw);
                } catch (Exception exc) {  // noqa
                    err = Ui.explain(exc);
                }
                final String finalErr = err;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        setBusy(false);
                        if (finalErr != null) {
                            fail(finalErr);
                            return;
                        }
                        store.setBase(api.base());
                        store.setCookie(api.cookie());
                        goMain();
                    }
                });
            }
        }, "login").start();
    }

    private void setBusy(boolean busy) {
        loginBtn.setEnabled(!busy);
        loginBtn.setText(busy ? "登录中…" : "登录");
        hostBox.setEnabled(!busy);
        pwBox.setEnabled(!busy);
    }

    private void fail(String text) {
        msg.setTextColor(Theme.BAD);
        msg.setText(text);
    }

    private void goMain() {
        startActivity(new Intent(this, MainActivity.class));
        finish();
    }
}
