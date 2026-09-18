package tests;

import com.dafeiyu.controller.AppUpdate;

import java.io.File;
import java.io.FileOutputStream;
import java.util.HashMap;
import java.util.Map;

/**
 * 「公告 + 检查更新」纯逻辑测试。
 *
 * 为什么必须单独钉住：这块的逻辑错了，症状离原因很远 ——
 *   ① 解析错字段（拿 announcement 当 latest_code）：用户手机永远看不到更新提示，
 *      但他不知道这功能存在，也没有任何报错 —— 和没做一模一样。
 *   ② sha256 校验错：下载的 APK 传坏了也会被放行，装一个坏的包。
 *   ③ 大小比较方向反了：每次都弹「有更新」或永远不弹。
 *
 * 服务器端（app-update.json 解析、/app/apk 字节流）由 test/test-app-update.py
 * 覆盖；App 端的接线（连接成功 → 弹窗 → 下载 → 安装）由
 * test/check-app-update-wiring.py 静态扫。
 */
public final class AppUpdateTest {

    public static void run() {
        T.group("公告 + 检查更新（1.2.0 新增）");

        // ── 解析 ──────────────────────────────────────────────────────
        Map<String, Object> full = new HashMap<String, Object>();
        full.put(AppUpdate.K_CODE, 2L);
        full.put(AppUpdate.K_NAME, "1.2.0");
        full.put(AppUpdate.K_ANNOUNCEMENT, "新增公告与检查更新");
        full.put(AppUpdate.K_SHA256, "  ABC123ABC123  ");
        AppUpdate u = AppUpdate.from(full);
        T.eq("★ 解析出 latest_code", 2L, u.latestCode);
        T.eq("解析出 latest_name", "1.2.0", u.latestName);
        T.eq("解析出公告", "新增公告与检查更新", u.announcement);
        T.eq("★ sha256 去空格转小写（服务器可能发大写/带空格）",
                "abc123abc123", u.sha256);

        // 空 Map → 空壳，绝不能因为少字段崩掉 App
        AppUpdate empty = AppUpdate.from(new HashMap<String, Object>());
        T.eq("★ 空响应 → latestCode=0（不弹更新）", 0L, empty.latestCode);
        T.eq("空响应 → 无公告", "", empty.announcement);
        T.eq("★ null 响应 → 空壳", 0L, AppUpdate.from(null).latestCode);

        // 字段类型不对（服务器哪天把 code 发成字符串 / JSON 数字解析成 Double）
        Map<String, Object> weird = new HashMap<String, Object>();
        weird.put(AppUpdate.K_CODE, "abc");
        T.eq("★ 类型不对 → 当没有更新（不弹、不崩）",
                0L, AppUpdate.from(weird).latestCode);
        Map<String, Object> asDouble = new HashMap<String, Object>();
        asDouble.put(AppUpdate.K_CODE, 2.0);
        T.eq("★ Double 型 code 也认（JSON 数字可能解析成 Double）",
                2L, AppUpdate.from(asDouble).latestCode);

        // ── 判定 ──────────────────────────────────────────────────────
        T.isTrue("★ 服务器 2 > 本地 1 → 有新版本", AppUpdate.from(full).hasUpdate(1L));
        T.isFalse("服务器 2 = 本地 2 → 无新版本", AppUpdate.from(full).hasUpdate(2L));
        T.isFalse("服务器 0（没配）→ 无新版本",
                AppUpdate.from(new HashMap<String, Object>()).hasUpdate(1L));
        T.isTrue("★ 只有公告没有新版：有公告（两个字段独立）",
                AppUpdate.from(full).hasAnnouncement());
        T.isFalse("空壳无公告", AppUpdate.from(null).hasAnnouncement());

        // ── sha256 ────────────────────────────────────────────────────
        T.isFalse("★ 服务器没给 sha256 → 校验不通过（保守，调用方删半截包）",
                empty.sha256Matches(tmpFile("x")));
        T.isFalse("★ 文件不存在 → 校验不通过",
                AppUpdate.from(full).sha256Matches(new File("/no/such/file.apk")));
        T.isFalse("★ 传 null → 校验不通过", AppUpdate.from(full).sha256Matches(null));

        // 真正的文件往返：先算出真实 sha256，再要它放行
        byte[] bytes = "hello-update-apk".getBytes();
        File f = tmpFile("real");
        try {
            String real = AppUpdate.sha256(bytes);
            Map<String, Object> withReal = new HashMap<String, Object>();
            withReal.put(AppUpdate.K_CODE, 2L);
            withReal.put(AppUpdate.K_SHA256, real);
            AppUpdate u2 = AppUpdate.from(withReal);

            FileOutputStream out = new FileOutputStream(f);
            out.write(bytes);
            out.close();
            T.isTrue("★ sha256 一致 → 放行", u2.sha256Matches(f));

            // 故意在尾部多写一个字节 → 必须拦下（弱网下最常见的坏法）
            FileOutputStream out2 = new FileOutputStream(f);
            out2.write(bytes);
            out2.write(0x42);
            out2.close();
            T.isFalse("★ 内容多一个字节 → 拦下（传坏也要删）", u2.sha256Matches(f));

            // 同长度但内容不同 → 也必须拦下（截断/坏块）
            FileOutputStream out3 = new FileOutputStream(f);
            byte[] other = bytes.clone();
            other[0] = (byte) (other[0] ^ 0xff);
            out3.write(other);
            out3.close();
            T.isFalse("★ 同长度内容不同 → 拦下", u2.sha256Matches(f));
        } catch (Exception e) {
            T.bad("sha256 文件往返测试", e.toString());
        } finally {
            if (f.exists()) {
                f.delete();
            }
        }
    }

    private static File tmpFile(String name) {
        return new File(System.getProperty("java.io.tmpdir") + File.separator
                + "dafeiyu-AppUpdateTest-" + name);
    }
}