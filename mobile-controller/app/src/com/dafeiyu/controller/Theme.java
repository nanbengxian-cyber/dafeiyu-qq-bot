package com.dafeiyu.controller;

import android.content.Context;
import android.graphics.Color;
import android.util.TypedValue;

/**
 * 配色与尺寸 —— 一处定义，全局引用（沿用安卓控制台的深色主题）。
 *
 * 深色底是刻意的：这个 App 最常在「机器人半夜掉线」时打开。
 * 颜色放代码里而不是 res/values/colors.xml：资源是手写 aapt2 编的，
 * 少一层 R.color 引用就少一处对不上的风险。
 */
public final class Theme {

    private Theme() {
    }

    public static final int BG = Color.parseColor("#12161C");
    public static final int CARD = Color.parseColor("#1B2129");
    public static final int CARD_LINE = Color.parseColor("#2A323E");
    public static final int TEXT = Color.parseColor("#E6EAF0");
    public static final int DIM = Color.parseColor("#8A94A6");
    public static final int ACCENT = Color.parseColor("#2F6FED");
    public static final int INPUT = Color.parseColor("#151B23");

    public static final int GOOD = Color.parseColor("#3FBF6F");
    public static final int WARN = Color.parseColor("#E0A72C");
    public static final int BAD = Color.parseColor("#E5484D");

    /** tone → 颜色。0 普通 / 1 好 / 2 警告 / 3 坏。 */
    public static int tone(int tone) {
        switch (tone) {
            case 1:
                return GOOD;
            case 2:
                return WARN;
            case 3:
                return BAD;
            default:
                return TEXT;
        }
    }

    public static int dp(Context ctx, float value) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value,
                ctx.getResources().getDisplayMetrics());
    }
}
