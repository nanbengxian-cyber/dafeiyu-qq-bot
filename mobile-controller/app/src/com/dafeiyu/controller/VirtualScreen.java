package com.dafeiyu.controller;

import android.content.Context;
import android.view.View;

/**
 * 「虚拟屏」—— 一个用完即弃的登录/验证界面。
 *
 * 设计目标（用户要求）：短信、二维码、密码认证这些界面不常驻内存，
 * 点开的时候才 {@link #build} 出来，关掉时 {@link #dispose} 立刻把
 * 位图、WebView、轮询、线程池等一切资源放掉，平时零占用；下次要用
 * 再重新构建一块全新的。
 *
 * 生命周期（由 {@link ScreenHost} 驱动，一次只活一块）：
 *   build(ctx)  → 现造视图树，返回根 View；只在这一刻分配内存。
 *   onEnter()   → 视图已挂到覆盖层、对用户可见后调用，启动轮询/发首个请求。
 *   onBack()    → 系统返回键；返回 true 表示自己消化了（不关屏）。
 *   dispose()   → 关屏时调用，释放全部资源。dispose 后本对象即被丢弃。
 *
 * 约定：实现类不得缓存 build 出来的 View（不能像旧 LoginView 那样把 root
 * 存成字段常驻）；ScreenHost 关屏后会丢弃整个 VirtualScreen 实例。
 */
public interface VirtualScreen {

    /** 顶部标题栏文字。 */
    String title();

    /** 现场构建视图树并返回根 View。只在打开这一刻调用一次。 */
    View build(Context ctx);

    /** 视图已可见后调用：可在此启动轮询、发起首个网络请求。 */
    void onEnter();

    /** 系统返回键。返回 true = 已自行处理，不要关屏；false = 照常关屏。 */
    boolean onBack();

    /** 关屏：释放位图 / WebView / 轮询 / 线程池等一切资源。之后实例被丢弃。 */
    void dispose();
}
