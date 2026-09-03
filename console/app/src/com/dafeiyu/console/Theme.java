package com.dafeiyu.console;

import android.content.Context;
import android.graphics.Color;
import android.util.TypedValue;

/**
 * 配色与尺寸 —— 一处定义，全局引用。
 *
 * 为什么不用 res/values/colors.xml：颜色在代码里要通过 R.color 再 getColor()
 * 取一遍，而这个项目的资源是手写 aapt2 编的，多一层 ID 就多一处对不上的风险。
 * 更重要的是**状态色和语义必须绑死**：{@link StatusFmt.Row#tone} 是 0/1/2/3，
 * 只有在这里做唯一映射，才不会出现「状态页说坏、颜色画成绿」这种自相矛盾。
 *
 * 深色底是刻意的：这个 App 最常在「机器人半夜掉线」时打开。
 */
public final class Theme {

    private Theme() {
    }

    public static final int BG = Color.parseColor("#12161C");
    public static final int CARD = Color.parseColor("#1B2129");
    public static final int TEXT = Color.parseColor("#E6EAF0");
    public static final int DIM = Color.parseColor("#8A94A6");
    public static final int ACCENT = Color.parseColor("#2F6FED");

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
