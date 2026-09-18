package com.dafeiyu.controller;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;

import java.io.File;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 公告展示 + 检查更新 + 下载安装 —— 一整套流程的 UI 层。
 *
 * 触发时机：MainActivity 在连接成功后调 {@link #maybeCheck(Activity)}。
 * 流程：
 *   连接成功 → 后台 GET /app/update → 解析（AppUpdate）
 *   → 有新版本或公告 → 弹 AlertDialog：
 *       [下载并安装] [稍后]
 *   → 点「下载并安装」：后台 get /app/apk 写到应用私有目录
 *   → sha256 校验（AppUpdate.sha256Matches）
 *   → 校验过 → 走系统安装器（ACTION_VIEW + content:// 由 ApkProvider 提供）
 *   → sha 对不上 / 下载失败 → toast 报错，绝不清零静默吞掉
 *
 * 为什么 APK 走服务器下载而不是私有仓库：
 *   隧道 + Bearer token 双认证已经证明「App 能访问这台服务器」，
 *   服务器把部署时放进去的 APK 原样发回。App 里不需要内置任何
 *   服务器以外的凭据（私有仓库的 token 打死也不能进 App）。
 *
 * 为什么 sha256 必须核对：下载断掉/传坏是弱网下的正常风险，
 * 装一个半截 APK 要么装不上、要么装上是坏的还不自知。
 * 这里的 sha256 是完整性校验（防传坏），不是安全边界
 * （隧道本身可信，见 AppUpdate.sha256Matches 的注释）。
 *
 * 安装走 content://：Android 7+ 禁止 file:// 进 Intent，
 * 又没有 androidx（项目无依赖，见 build.sh），所以手写一个
 * 极小的 ContentProvider（ApkProvider）把私有目录的 APK 以
 * content:// 暴露给系统安装器，并 grantUriPermissions。
 */
public final class UpdateFlow {

    private static final Handler UI = new Handler(Looper.getMainLooper());
    private static final ExecutorService POOL = Executors.newSingleThreadExecutor();

    /** 下载到应用私有目录（install 前由 ApkProvider 以 content:// 暴露）。 */
    public static final String APK_NAME = "update.apk";

    /** 同一次连接只提醒一次（避免连一次弹一次，用户烦）。 */
    private static boolean checkedThisConnection = false;

    private UpdateFlow() {
    }

    /** 连接成功后的入口。内部自行判断有没有更新/公告。 */
    public static void maybeCheck(final Activity act) {
        if (checkedThisConnection) {
            return;                      // 这次连接已经查过
        }
        if (!Session.connected()) {
            return;
        }
        checkedThisConnection = true;
        final ManagerClient client = Session.client();
        POOL.execute(new Runnable() {
            public void run() {
                try {
                    AppUpdate upd = AppUpdate.from(client.appUpdate());
                    final long localCode = localVersionCode(act);
                    if (upd.hasUpdate(localCode) || upd.hasAnnouncement()) {
                        UI.post(new Runnable() {
                            public void run() {
                                showPrompt(act, upd);
                            }
                        });
                    }
                } catch (Exception e) {
                    // 查更新失败不打扰用户 —— 它只是锦上添花，不是核心功能。
                    // 真把「连不上 /app/update」弹出来，用户会以为服务器坏了。
                }
            }
        });
    }

    private static long localVersionCode(Activity act) {
        try {
            return act.getPackageManager()
                    .getPackageInfo(act.getPackageName(), 0).versionCode;
        } catch (Exception e) {
            return 0;
        }
    }

    /** 弹「公告 + 更新」对话框。 */
    private static void showPrompt(final Activity act, final AppUpdate upd) {
        StringBuilder msg = new StringBuilder();
        if (upd.hasUpdate(localVersionCode(act))) {
            msg.append("发现新版本 ");
            msg.append(upd.latestName == null || upd.latestName.isEmpty()
                    ? "" : upd.latestName + " ");
            msg.append("(v").append(upd.latestCode).append(")\n\n");
        }
        if (upd.hasAnnouncement()) {
            msg.append(upd.announcement.trim()).append("\n\n");
        }
        if (!upd.hasUpdate(localVersionCode(act))) {
            msg.append("——以上是公告，无新版本。");
        }
        new AlertDialog.Builder(act)
                .setTitle(upd.hasUpdate(localVersionCode(act)) ? "版本更新" : "公告")
                .setMessage(msg.toString().trim())
                .setPositiveButton("下载并安装", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int w) {
                        downloadAndInstall(act, upd);
                    }
                })
                .setNegativeButton("稍后", null)
                .show();
    }

    /** 后台下载 → sha256 校验 → 交给系统安装器。 */
    private static void downloadAndInstall(final Activity act, final AppUpdate upd) {
        toast(act, "正在下载更新包…");
        POOL.execute(new Runnable() {
            public void run() {
                // err 用可写中间变量：javac 不允许 final 变量在 try 的多个分支
                // 和 catch 里各自赋值（「might already have been assigned」）。
                String errTmp = null;
                final File apk = new File(act.getFilesDir(), APK_NAME);
                boolean ok = false;
                try {
                    if (Session.client() == null) {
                        errTmp = "连接已断开，请重新连接后再更新。";
                    } else if (!Session.client().downloadApk(apk)) {
                        errTmp = "服务器上还没有可下载的安装包。";
                    } else if (upd.sha256 != null && !upd.sha256.isEmpty()
                            && !upd.sha256Matches(apk)) {
                        errTmp = "下载的安装包校验失败（可能传坏了），已删除，请重试。";
                    } else {
                        ok = true;
                    }
                } catch (Exception e) {
                    errTmp = "更新失败：" + e.getMessage();
                    ok = false;
                }
                final boolean success = ok;
                final String errMsg = errTmp;
                UI.post(new Runnable() {
                    public void run() {
                        if (success) {
                            launchInstaller(act, apk);
                        } else {
                            toast(act, errMsg);
                            if (apk != null && apk.exists()) {
                                apk.delete();   // 半截/校验不过的 APK 必须删掉
                            }
                        }
                    }
                });
            }
        });
    }

    /**
     * 用系统安装器装 APK。
     *
     * content:// URI + FLAG_GRANT_READ_URI_PERMISSION：
     *   系统安装器有权读我们的 ApkProvider 暴露的文件；没这个 flag
     *   它会因为没有读权限直接 403（声音像「装不了」）。
     */
    private static void launchInstaller(Activity act, File apk) {
        Uri uri = Uri.parse("content://" + ApkProvider.AUTHORITY + "/" + ApkProvider.APK_NAME);
        Intent i = new Intent(Intent.ACTION_VIEW);
        i.setDataAndType(uri, "application/vnd.android.package-archive");
        i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        try {
            act.startActivity(i);
        } catch (Exception e) {
            toast(act, "打不开安装器：" + e.getMessage());
        }
    }

    private static void toast(final Activity act, final String s) {
        UI.post(new Runnable() {
            public void run() {
                Toast.makeText(act, s, Toast.LENGTH_LONG).show();
            }
        });
    }
}