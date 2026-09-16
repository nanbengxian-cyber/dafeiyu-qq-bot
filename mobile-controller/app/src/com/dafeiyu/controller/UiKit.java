package com.dafeiyu.controller;

import android.content.Context;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * 手搓控件工厂 —— 这个项目的资源是 aapt2 手编的，没有布局 XML；
 * 所有界面都用这里的助手在代码里搭。一处定义卡片/输入框/按钮的样式，
 * 避免每个页面各画各的。
 */
public final class UiKit {

    private UiKit() {
    }

    /** 竖向滚动容器（页面根）。 */
    public static ScrollView scroll(Context ctx) {
        ScrollView sv = new ScrollView(ctx);
        sv.setBackgroundColor(Theme.BG);
        sv.setFillViewport(true);
        return sv;
    }

    public static LinearLayout column(Context ctx) {
        LinearLayout col = new LinearLayout(ctx);
        col.setOrientation(LinearLayout.VERTICAL);
        return col;
    }

    public static LinearLayout pageColumn(Context ctx) {
        LinearLayout col = column(ctx);
        int pad = Theme.dp(ctx, 12);
        col.setPadding(pad, pad, pad, Theme.dp(ctx, 32));
        return col;
    }

    /** 圆角卡片，返回外层；内容列永远是 child(0)（不走 setTag —— 非 resource key 会崩）。 */
    public static LinearLayout card(Context ctx, String title) {
        LinearLayout card = new LinearLayout(ctx);
        card.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Theme.CARD);
        bg.setCornerRadius(Theme.dp(ctx, 10));
        bg.setStroke(1, Theme.CARD_LINE);
        card.setBackground(bg);
        int pad = Theme.dp(ctx, 12);
        card.setPadding(pad, pad, pad, pad);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = Theme.dp(ctx, 10);
        card.setLayoutParams(lp);
        LinearLayout inner = column(ctx);
        card.addView(inner);
        if (title != null && !title.isEmpty()) {
            TextView t = caption(ctx, title);
            inner.addView(t);
        }
        return card;
    }

    /** 取卡片的内容列（card() 的第一个孩子）。 */
    public static LinearLayout inner(LinearLayout card) {
        return (LinearLayout) card.getChildAt(0);
    }

    public static TextView caption(Context ctx, String text) {
        TextView t = new TextView(ctx);
        t.setText(text);
        t.setTextColor(Theme.DIM);
        t.setTextSize(12);
        t.setLetterSpacing(-0.02f);
        int pad = Theme.dp(ctx, 2);
        t.setPadding(pad, 0, pad, Theme.dp(ctx, 4));
        return t;
    }

    public static TextView text(Context ctx, String text, float sizeSp, int color) {
        TextView t = new TextView(ctx);
        t.setText(text);
        t.setTextColor(color);
        t.setTextSize(sizeSp);
        t.setLineSpacing(Theme.dp(ctx, 1), 1f);
        return t;
    }

    public static EditText input(Context ctx, String hint, boolean password) {
        EditText e = new EditText(ctx);
        e.setHint(hint);
        e.setHintTextColor(Theme.DIM);
        e.setTextColor(Theme.TEXT);
        e.setTextSize(14);
        e.setBackground(box(ctx));
        int pad = Theme.dp(ctx, 9);
        e.setPadding(pad, pad, pad, pad);
        if (password) {
            e.setInputType(android.text.InputType.TYPE_CLASS_TEXT
                    | android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD);
        }
        e.setSingleLine(true);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = Theme.dp(ctx, 6);
        e.setLayoutParams(lp);
        return e;
    }

    private static GradientDrawable box(Context ctx) {
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Theme.INPUT);
        bg.setCornerRadius(Theme.dp(ctx, 7));
        bg.setStroke(1, Theme.CARD_LINE);
        return bg;
    }

    public static Button button(Context ctx, String text, boolean accent) {
        Button b = new Button(ctx);
        b.setText(text);
        b.setTextSize(14);
        b.setAllCaps(false);
        b.setTextColor(accent ? 0xFFFFFFFF : Theme.TEXT);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(accent ? Theme.ACCENT : Theme.CARD_LINE);
        bg.setCornerRadius(Theme.dp(ctx, 8));
        b.setBackground(bg);
        int padV = Theme.dp(ctx, 10);
        b.setPadding(0, padV, 0, padV);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = Theme.dp(ctx, 8);
        b.setLayoutParams(lp);
        return b;
    }

    /** 横排容器。 */
    public static LinearLayout row(Context ctx) {
        LinearLayout r = new LinearLayout(ctx);
        r.setOrientation(LinearLayout.HORIZONTAL);
        r.setGravity(Gravity.CENTER_VERTICAL);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = Theme.dp(ctx, 6);
        r.setLayoutParams(lp);
        return r;
    }

    /** 等宽字体的日志/状态区。 */
    public static TextView mono(Context ctx, String text) {
        TextView t = text(ctx, text, 12, Theme.TEXT);
        t.setTypeface(Typeface.MONOSPACE);
        return t;
    }

    public static void setEnabledDeep(View v, boolean enabled) {
        v.setEnabled(enabled);
        if (v instanceof ViewGroup) {
            ViewGroup g = (ViewGroup) v;
            for (int i = 0; i < g.getChildCount(); i++) {
                setEnabledDeep(g.getChildAt(i), enabled);
            }
        }
    }

    public static int withAlpha(int color, float alpha) {
        return Color.argb((int) (alpha * 255), Color.red(color), Color.green(color),
                Color.blue(color));
    }
}
