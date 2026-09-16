package tests;

import com.dafeiyu.controller.ChatSetup;
import java.util.List;
import java.util.Map;

/**
 * 「聊天范围 + 主聊天 API」的写入逻辑测试。
 * 服务器端脚本是**真的跑**（用 python3 + 临时 cmd_config.json），
 * 不是只断言字符串 —— 否则「脚本生成对了但跑不起来」测不出来。
 */
public final class ChatSetupTest {

    private static ChatSetup.Request req(String g, String f, String b, String k, String m) {
        try {
            return ChatSetup.Request.parse(g, f, b, k, m);
        } catch (ChatSetup.SetupException e) {
            throw new IllegalStateException(e);
        }
    }

    public static void run() {
        T.group("聊天范围：解析与校验");
        ChatSetup.Request r = req("100000001, 100000002", "100000003", "", "", "");
        T.eq("群号解析", 2, r.groups.size());
        T.eq("私聊解析", 1, r.friends.size());
        T.eq("要写范围", true, r.scopeApply);
        T.eq("不写 API", false, r.apiApply);
        T.eq("中文逗号也认", 2, req("100000001，100000002", "", "", "", "").groups.size());
        T.eq("顿号也认", 2, req("100000001、100000002", "", "", "", "").groups.size());
        T.eq("重复自动去重", 1, req("100000001,100000001", "", "", "", "").groups.size());
        T.throwsWith("非数字拦下", "不是纯数字", new Runnable() {
            public void run() {
                req("abc", "", "", "", "");
            }
        });
        T.throwsWith("太短拦下", "不是纯数字", new Runnable() {
            public void run() {
                req("123", "", "", "", "");
            }
        });
        T.throwsWith("什么都没填拦下", "至少填", new Runnable() {
            public void run() {
                req("", "", "", "", "");
            }
        });

        T.group("主聊天 API：校验");
        T.throwsWith("只填一半拦下", "缺一不可", new Runnable() {
            public void run() {
                req("", "", "https://api.example.com/v1", "", "m1");
            }
        });
        T.throwsWith("地址没协议头拦下", "http:// 或 https://", new Runnable() {
            public void run() {
                req("", "", "api.example.com/v1", "sk-x", "m1");
            }
        });
        T.throwsWith("地址带空格拦下", "空格", new Runnable() {
            public void run() {
                req("", "", "https://api.example.com /v1", "sk-x", "m1");
            }
        });
        T.throwsWith("模型名带空格拦下", "空格或换行", new Runnable() {
            public void run() {
                req("", "", "https://api.example.com/v1", "sk-x", "my model");
            }
        });
        ChatSetup.Request api = req("", "", "https://api.example.com/v1", "sk-test-key", "model-1");
        T.eq("要写 API", true, api.apiApply);
        T.eq("不写范围", false, api.scopeApply);

        T.group("载荷：密钥只出现在载荷里");
        String payload = ChatSetup.payload(api);
        T.contains("载荷里有 key", payload, "sk-test-key");
        T.contains("载荷里有模型名", payload, "model-1");
        T.notContains("脚本里没有 key", ChatSetup.script(), "sk-test-key");
        T.notContains("脚本里没有模型名", ChatSetup.script(), "model-1");
        T.notContains("脚本里没有示例群号", ChatSetup.script(), "100000001");
        T.contains("固定 source id", payload, ChatSetup.SOURCE_ID);
        T.contains("固定 provider id", payload, ChatSetup.PROVIDER_ID);
        T.contains("脚本用 DSH_RESULT 回话", ChatSetup.script(), "DSH_RESULT:");
        T.contains("脚本改白名单开关", ChatSetup.script(), "enable_id_white_list");
        T.contains("脚本写私聊会话 id", ChatSetup.script(), "FriendMessage:");
        T.contains("脚本设默认 provider", ChatSetup.script(), "default_provider_id");
        T.contains("脚本先备份", ChatSetup.script(), "shutil.copy2");
        T.contains("脚本保 BOM", ChatSetup.script(), "utf-8-sig");

        T.group("载荷：转义");
        ChatSetup.Request quoted = req("", "", "https://api.example.com/v1", "a\"b\\c", "m1");
        String qp = ChatSetup.payload(quoted);
        T.contains("引号被转义", qp, "a\\\"b\\\\c");

        T.group("结果解析");
        try {
            Map<String, Object> res = ChatSetup.parseResult(
                    "noise\nDSH_RESULT:{\"changed\":[\"聊天范围：100000001\"],\"backup\":\"cmd_config.json.bak.x\"}\n");
            T.eq("改动条数", 1, ((List<?>) res.get("changed")).size());
            T.contains("describe 带备份名", ChatSetup.describe(res), "cmd_config.json.bak.x");
            T.contains("describe 带改动", ChatSetup.describe(res), "聊天范围：100000001");
        } catch (ChatSetup.SetupException e) {
            T.bad("结果应当解析成功", e.getMessage());
        }
        T.throwsWith("没有结果行要报错", "没有返回结果", new Runnable() {
            public void run() {
                try {
                    ChatSetup.parseResult("nothing here");
                } catch (ChatSetup.SetupException e) {
                    throw new RuntimeException(e.getMessage());
                }
            }
        });

        T.group("服务器脚本：真的跑一遍（python3 + 临时配置）");
        runScriptEndToEnd();
    }

    /** 把脚本写到临时目录，用真 python3 改一份假 cmd_config.json，检查结果。 */
    private static void runScriptEndToEnd() {
        try {
            java.io.File dir = java.nio.file.Files.createTempDirectory("chatsetup").toFile();
            java.io.File cfg = new java.io.File(dir, "cmd_config.json");
            String original = "{\n"
                    + "  \"platform\": [{\"id\": \"default\", \"type\": \"aiocqhttp\"}],\n"
                    + "  \"platform_settings\": {\"enable_id_white_list\": false, \"id_whitelist\": []},\n"
                    + "  \"provider_sources\": [{\"id\": \"other_source\", \"key\": [\"keep-me\"],\n"
                    + "      \"api_base\": \"https://other.example.com/v1\"}],\n"
                    + "  \"provider\": [{\"id\": \"other\", \"provider_source_id\": \"other_source\",\n"
                    + "      \"model\": \"keep-model\"}],\n"
                    + "  \"provider_settings\": {\"default_provider_id\": \"other\"}\n"
                    + "}\n";
            write(cfg, original);

            java.io.File script = new java.io.File(dir, "chat-setup.py");
            write(script, ChatSetup.script());
            java.io.File payload = new java.io.File(dir, "payload.json");
            write(payload, ChatSetup.payload(req("100000001,100000002", "100000003",
                    "https://api.example.com/v1", "sk-secret-xyz", "my-model")));

            String out = exec(dir, "python3", script.getPath(), cfg.getPath(), payload.getPath());
            T.contains("脚本报进度", out, "DSH_PROGRESS:");
            T.contains("脚本回结果", out, "DSH_RESULT:");

            String after = read(cfg);
            T.contains("白名单开关被打开", after, "\"enable_id_white_list\": true");
            T.contains("群号写进白名单", after, "\"100000001\"");
            T.contains("第二个群号也在", after, "\"100000002\"");
            T.contains("私聊写成会话 id", after, "\"default:FriendMessage:100000003\"");
            T.contains("新 provider source", after, ChatSetup.SOURCE_ID);
            T.contains("新 provider", after, ChatSetup.PROVIDER_ID);
            T.contains("默认 provider 指过去", after,
                    "\"default_provider_id\": \"" + ChatSetup.PROVIDER_ID + "\"");
            T.contains("模型名写进去", after, "my-model");
            T.contains("接口地址写进去", after, "https://api.example.com/v1");
            T.contains("别人的 source 没被动", after, "keep-me");
            T.contains("别人的 provider 没被动", after, "keep-model");
            T.contains("备份文件已生成", String.valueOf(dir.list().length > 2), "true");
            boolean hasBackup = false;
            for (String name : dir.list()) {
                if (name.contains(".bak.controller.")) {
                    hasBackup = true;
                }
            }
            T.eq("存在带时间戳的备份", true, hasBackup);

            // 幂等：再跑一次不应新增条目，且仍然只指向同一个 id
            String out2 = exec(dir, "python3", script.getPath(), cfg.getPath(), payload.getPath());
            T.contains("第二次也成功", out2, "DSH_RESULT:");
            String after2 = read(cfg);
            T.eq("source 没被重复添加", 1, count(after2, "\"id\": \"" + ChatSetup.SOURCE_ID + "\""));
            T.eq("provider 没被重复添加", 1, count(after2, "\"id\": \"" + ChatSetup.PROVIDER_ID + "\""));

            // 只写 API 时不应碰白名单
            java.io.File cfg2 = new java.io.File(dir, "cmd_config2.json");
            write(cfg2, original);
            java.io.File payload2 = new java.io.File(dir, "payload2.json");
            write(payload2, ChatSetup.payload(req("", "", "https://api.example.com/v1",
                    "sk-2", "m2")));
            exec(dir, "python3", script.getPath(), cfg2.getPath(), payload2.getPath());
            String after3 = read(cfg2);
            T.contains("只写 API 时白名单保持关", after3, "\"enable_id_white_list\": false");
            T.contains("只写 API 时默认 provider 也改了", after3,
                    "\"default_provider_id\": \"" + ChatSetup.PROVIDER_ID + "\"");

            // 配置文件不存在时要给人话错误，不能抛栈
            java.io.File missing = new java.io.File(dir, "nope.json");
            String err = execAllowFail(dir, "python3", script.getPath(), missing.getPath(),
                    payload.getPath());
            T.contains("缺配置时给人话", err, "DSH_ERROR:");
            T.contains("提示先部署", err, "开始部署");
            T.notContains("缺配置时不吐 Python 栈", err, "Traceback");

            // 回读校验真的会拦：把「落盘」改成「落盘后清空白名单」，脚本必须报错。
            // 这是变异验证 —— 如果这里仍然成功，说明那条回读校验是摆设。
            java.io.File cfgMut = new java.io.File(dir, "cmd_config_mut.json");
            write(cfgMut, original);
            java.io.File scriptMut = new java.io.File(dir, "chat-setup-mut.py");
            String mutated = ChatSetup.script().replace(
                    "os.replace(tmp, cfg_path)",
                    "os.replace(tmp, cfg_path)\n"
                    + "_d = json.loads(open(cfg_path, encoding='utf-8-sig').read())\n"
                    + "_d['platform_settings']['id_whitelist'] = []\n"
                    + "open(cfg_path, 'w', encoding='utf-8').write(json.dumps(_d))\n");
            T.contains("变异确实改到了脚本", mutated, "_d['platform_settings']");
            write(scriptMut, mutated);
            String mutOut = execAllowFail(dir, "python3", scriptMut.getPath(),
                    cfgMut.getPath(), payload.getPath());
            T.contains("回读校验拦下被改坏的落盘内容", mutOut, "回读校验失败");
            T.contains("回读失败用 DSH_ERROR 上报", mutOut, "DSH_ERROR:");

            // 正常路径必须明确报「回读校验通过」并带 verified
            T.contains("正常路径会报回读通过", out, "回读校验通过");
            T.contains("结果里带 verified", out, "\"verified\": true");

            // BOM 保留：AstrBot 的配置是 utf-8-sig
            java.io.File cfg3 = new java.io.File(dir, "cmd_config3.json");
            java.io.OutputStream os = new java.io.FileOutputStream(cfg3);
            os.write(new byte[]{(byte) 0xEF, (byte) 0xBB, (byte) 0xBF});
            os.write(original.getBytes("UTF-8"));
            os.close();
            exec(dir, "python3", script.getPath(), cfg3.getPath(), payload.getPath());
            byte[] head = new byte[3];
            java.io.InputStream in = new java.io.FileInputStream(cfg3);
            int n = in.read(head);
            in.close();
            T.eq("BOM 保留（3 字节）", 3, n);
            T.eq("BOM 内容正确", true, (head[0] & 0xFF) == 0xEF
                    && (head[1] & 0xFF) == 0xBB && (head[2] & 0xFF) == 0xBF);

            // 清理
            delete(dir);
        } catch (Exception e) {
            T.bad("端到端脚本测试", e.toString());
        }
    }

    private static void write(java.io.File f, String text) throws Exception {
        java.io.OutputStream os = new java.io.FileOutputStream(f);
        os.write(text.getBytes("UTF-8"));
        os.close();
    }

    private static String read(java.io.File f) throws Exception {
        byte[] buf = new byte[(int) f.length()];
        java.io.InputStream in = new java.io.FileInputStream(f);
        int off = 0;
        while (off < buf.length) {
            int n = in.read(buf, off, buf.length - off);
            if (n < 0) {
                break;
            }
            off += n;
        }
        in.close();
        return new String(buf, 0, off, "UTF-8");
    }

    private static String exec(java.io.File dir, String... cmd) throws Exception {
        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.directory(dir);
        pb.redirectErrorStream(true);
        Process p = pb.start();
        java.io.InputStream in = p.getInputStream();
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = in.read(buf)) > 0) {
            bos.write(buf, 0, n);
        }
        p.waitFor();
        return new String(bos.toByteArray(), "UTF-8");
    }

    private static String execAllowFail(java.io.File dir, String... cmd) {
        try {
            return exec(dir, cmd);
        } catch (Exception e) {
            return "exec failed: " + e;
        }
    }

    private static void delete(java.io.File f) {
        if (f.isDirectory()) {
            java.io.File[] kids = f.listFiles();
            if (kids != null) {
                for (java.io.File k : kids) {
                    delete(k);
                }
            }
        }
        f.delete();
    }

    private static int count(String haystack, String needle) {
        int n = 0;
        int i = 0;
        while ((i = haystack.indexOf(needle, i)) >= 0) {
            n++;
            i += needle.length();
        }
        return n;
    }
}
