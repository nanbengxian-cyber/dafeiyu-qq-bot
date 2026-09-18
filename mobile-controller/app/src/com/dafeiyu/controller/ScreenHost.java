package com.dafeiyu.controller;

import android.content.Context;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * 虚拟屏的覆盖层容器 —— 盖在整个 App 之上的一层全屏 FrameLayout。
 *
 * 平时它是空的、GONE 的，一点内存不吃。要用某种验证时：
 *   open(screen) → screen.build() 现造视图树 → 顶一条标题栏（含关闭键）→
 *                  addView 到覆盖层 → 显示 → screen.onEnter()。
 * 关闭时：
 *   close() → screen.dispose() 放掉资源 → removeAllViews() → GONE → 引用置空。
 *
 * 同一时刻最多一块屏存活；open 新屏会先把旧屏关掉。ScreenHost 自己不持有
 * 任何验证相关的重资源，全交给具体 VirtualScreen 在 build/dispose 里管理。
 */
public final class ScreenHost {

    private final Context ctx;
    private final FrameLayout overlay;
    private final FrameLayout body;      // 装 screen.build() 返回的视图
    private final TextView titleView;

    private VirtualScreen current;
    private Runnable onCloseListener;   // 每次关屏后回调（中枢用它恢复状态轮询）

    public ScreenHost(Context ctx) {
        this.ctx = ctx;
        overlay = new FrameLayout(ctx);
        overlay.setBackgroundColor(Theme.BG);
        overlay.setVisibility(View.GONE);
        overlay.setClickable(true);   // 吃掉触摸，别穿透到底下的页签

        LinearLayout col = new LinearLayout(ctx);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        // 标题栏：← 返回 + 标题
        LinearLayout bar = new LinearLayout(ctx);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setBackgroundColor(Theme.CARD);
        int pad = Theme.dp(ctx, 12);
        bar.setPadding(pad, pad, pad, pad);

        Button back = new Button(ctx);
        back.setText("← 关闭");
        back.setAllCaps(false);
        back.setTextSize(14);
        back.setTextColor(Theme.TEXT);
        back.setBackgroundColor(Theme.CARD_LINE);
        back.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                close();
            }
        });
        bar.addView(back);

        titleView = UiKit.text(ctx, "", 16, Theme.TEXT);
        titleView.setTypeface(titleView.getTypeface(), android.graphics.Typeface.BOLD);
        LinearLayout.LayoutParams tlp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        tlp.leftMargin = Theme.dp(ctx, 12);
        titleView.setLayoutParams(tlp);
        bar.addView(titleView);

        col.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        body = new FrameLayout(ctx);
        col.addView(body, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        overlay.addView(col);
    }

    /** 覆盖层根 View，交给 MainActivity 顶到最外层 FrameLayout 里。 */
    public View view() {
        return overlay;
    }

    public boolean isOpen() {
        return current != null;
    }

    /** 设置关屏回调：每次任意虚拟屏关闭后调用（无论是返回键还是屏内触发）。 */
    public void setOnCloseListener(Runnable r) {
        this.onCloseListener = r;
    }

    /** 打开一块虚拟屏：现造 → 挂载 → 可见 → onEnter。会先关掉正在显示的屏。 */
    public void open(VirtualScreen screen) {
        if (screen == null) {
            return;
        }
        if (current != null) {
            close();
        }
        current = screen;
        titleView.setText(screen.title());
        View content = screen.build(ctx);
        body.removeAllViews();
        body.addView(content, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        overlay.setVisibility(View.VISIBLE);
        overlay.bringToFront();
        screen.onEnter();
    }

    /** 关闭当前屏：dispose 释放资源 → 清空视图 → 隐藏 → 丢弃实例。 */
    public void close() {
        VirtualScreen s = current;
        current = null;
        if (s != null) {
            try {
                s.dispose();
            } catch (Throwable ignored) {
                // dispose 不该抛，但即便抛了也要继续把视图清干净
            }
        }
        body.removeAllViews();   // 释放视图树 → 位图 / EditText 等可被回收
        titleView.setText("");
        overlay.setVisibility(View.GONE);
        if (s != null && onCloseListener != null) {
            onCloseListener.run();
        }
    }

    /**
     * 系统返回键。有屏开着就交给它先处理（比如 WebView goBack）；
     * 它不消化就关屏。返回 true 表示已被虚拟屏消化，Activity 不要再走默认返回。
     */
    public boolean onBack() {
        if (current == null) {
            return false;
        }
        if (current.onBack()) {
            return true;
        }
        close();
        return true;
    }
}
