package com.dafeiyu.controller;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;

import java.io.File;
import java.io.FileNotFoundException;

/**
 * 极简 ContentProvider：把应用私有目录里的更新 APK 以 content:// 暴露给
 * 系统安装器（ACTION_VIEW）。
 *
 * 为什么需要它（而不是直接用 file:// 或 Uri.fromFile）：
 *   Android 7（API 24）起 file:// 进 Intent 会抛 FileUriExposedException，
 *   必须用 content://。项目没有 androidx（纯手写构建链，见 build.sh），
 *   没有 FileProvider 可用，所以手写一个只服务「安装自己下载的 APK」
 *   这一个动作的最小实现。
 *
 * 安全边界：exported=false + grantUriPermissions；只在本进程内由
 * UpdateFlow 显式授权（FLAG_GRANT_READ_URI_PERMISSION）时，
 * 系统安装器才能读。不导出、不开文件目录，别的 App 碰不到。
 */
public final class ApkProvider extends ContentProvider {

    /** content:// authority（manifest 里注册的同一个）。 */
    public static final String AUTHORITY = "com.dafeiyu.controller.apk";

    /** APK 文件名 —— 和 UpdateFlow.APK_NAME 一致（都在私有目录 filesDir）。 */
    public static final String APK_NAME = "update.apk";

    /** 唯一路径：content://com.dafeiyu.controller.apk/update.apk */
    public static final Uri CONTENT_URI =
            Uri.parse("content://" + AUTHORITY + "/" + APK_NAME);

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public String getType(Uri uri) {
        return "application/vnd.android.package-archive";
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode)
            throws FileNotFoundException {
        Context c = getContext();
        if (c == null) {
            throw new FileNotFoundException("context is null");
        }
        File apk = new File(c.getFilesDir(), APK_NAME);
        if (!apk.exists()) {
            throw new FileNotFoundException("update.apk not downloaded yet");
        }
        return ParcelFileDescriptor.open(apk, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    // 其余 ContentProvider 方法本 provider 用不到（它不是数据库/文件列表），
    // 按协议返回空即可 —— 系统不会因为返回 null 就崩，只会报「不支持」。
    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        return null;
    }

    @Override
    public String[] getStreamTypes(Uri uri, String mimeTypeFilter) {
        return new String[]{getType(uri)};
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection,
                      String[] selectionArgs) {
        return 0;
    }
}