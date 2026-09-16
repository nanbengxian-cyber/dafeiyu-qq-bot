package com.dafeiyu.controller;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * 「聊天范围 + 主聊天 API」写进服务器上的 AstrBot 配置。
 *
 * 依据（都是从 AstrBot 源码与生产配置里核出来的，不是猜的）：
 *
 * 1) 聊天范围 = `platform_settings.enable_id_white_list` + `platform_settings.id_whitelist`。
 *    `astrbot/core/pipeline/whitelist_check/stage.py` 的判据是
 *    `event.unified_msg_origin not in whitelist and group_id not in whitelist` → 拦掉事件。
 *    所以白名单里群聊写**裸群号**，私聊写 **`<平台id>:FriendMessage:<QQ号>`**
 *    （平台 id 取自 cmd_config.json 的 platform[0].id，通常是 default）。
 *    打开白名单后，服务器就「只在填进去的群和私聊里说话」，别处一律不理。
 *
 * 2) 主聊天 API = `provider_sources[]`（api_base + key 列表）+ `provider[]`（model）
 *    + `provider_settings.default_provider_id` 指向它。
 *    键名与结构照生产配置与官方 provider 结构写（provider=openai、
 *    type=openai_chat_completion、provider_type=chat_completion）。
 *
 * 为什么用「脚本 + 载荷」两个文件：
 * - 密钥**不能进命令行**（会留在服务器 ps/历史里），所以密钥只出现在 SFTP 传上去的
 *   600 权限载荷文件里，脚本读完即删；
 * - 脚本本身是静态的，方便单测逐条断言它没有硬编码任何值。
 */
public final class ChatSetup {

    /** AstrBot 配置在部署目录下的相对路径（与 compose 的 ./data/astrbot:/AstrBot/data 对应）。 */
    public static final String CFG_REL = "data/astrbot/cmd_config.json";
    /** 远程脚本相对路径。 */
    public static final String SCRIPT_REL = "deploy/chat-setup.py";
    /** 一次性载荷（含密钥），写完即删。 */
    public static final String PAYLOAD_REL = "deploy/.chat-setup.payload.json";

    /** 写进服务器的主聊天 provider / source 的固定 id（幂等：重复写入只更新这一条）。 */
    public static final String SOURCE_ID = "dafeiyu-main_source";
    public static final String PROVIDER_ID = "dafeiyu-main";

    public static class SetupException extends Exception {
        public SetupException(String message) {
            super(message);
        }
    }

    /** 一次要写的内容；两块可以只写一块。 */
    public static final class Request {
        public List<String> groups = new ArrayList<String>();
        public List<String> friends = new ArrayList<String>();
        public boolean scopeApply;

        public String apiBase = "";
        public String apiKey = "";
        public String apiModel = "";
        public boolean apiApply;

        /** 用户在界面上填的东西 → 校验后的请求；没填的那块不会写。 */
        public static Request parse(String groupsText, String friendsText,
                                    String base, String key, String model)
                throws SetupException {
            Request r = new Request();
            r.groups = splitIds(groupsText, "群号");
            r.friends = splitIds(friendsText, "私聊 QQ 号");
            r.scopeApply = !r.groups.isEmpty() || !r.friends.isEmpty();

            String b = trim(base);
            String k = trim(key);
            String m = trim(model);
            boolean anyApi = !b.isEmpty() || !k.isEmpty() || !m.isEmpty();
            if (anyApi) {
                if (b.isEmpty() || k.isEmpty() || m.isEmpty()) {
                    throw new SetupException("主聊天 API 要填全：接口地址、API Key、模型名缺一不可。");
                }
                if (!(b.startsWith("http://") || b.startsWith("https://"))) {
                    throw new SetupException("接口地址要以 http:// 或 https:// 开头（照服务商给的填）。");
                }
                if (hasSpace(b)) {
                    throw new SetupException("接口地址里不能有空格。");
                }
                if (k.indexOf('\n') >= 0 || k.indexOf('\r') >= 0) {
                    throw new SetupException("API Key 里不能有换行。");
                }
                if (hasSpace(m) || m.indexOf('\n') >= 0 || m.indexOf('\r') >= 0) {
                    throw new SetupException("模型名里不能有空格或换行。");
                }
                r.apiBase = b;
                r.apiKey = k;
                r.apiModel = m;
                r.apiApply = true;
            }
            if (!r.scopeApply && !r.apiApply) {
                throw new SetupException("还没填内容：至少填「群号 / 私聊 QQ 号」或「主聊天 API」其中之一。");
            }
            return r;
        }
    }

    /** 逗号/顿号/空格/换行分隔的 QQ 号或群号 → 去重列表。 */
    public static List<String> splitIds(String raw, String label) throws SetupException {
        List<String> out = new ArrayList<String>();
        if (raw == null) {
            return out;
        }
        String normalized = raw.replace('，', ',').replace('、', ',')
                .replace('\n', ',').replace('\r', ',').replace(' ', ',').replace('\t', ',');
        for (String part : normalized.split(",")) {
            String s = part.trim();
            if (s.isEmpty()) {
                continue;
            }
            if (!Pattern.matches("[0-9]{5,12}", s)) {
                throw new SetupException(label + "「" + s + "」不是纯数字的 QQ 号/群号。");
            }
            if (!out.contains(s)) {
                out.add(s);
            }
        }
        return out;
    }

    /** 交给 Deployer 通过 SFTP 写上去的载荷（含密钥 → 服务器端 600 权限 + 用完即删）。 */
    public static String payload(Request r) {
        StringBuilder sb = new StringBuilder();
        sb.append("{\n");
        sb.append("  \"scope\": {\n");
        sb.append("    \"apply\": ").append(r.scopeApply ? "true" : "false").append(",\n");
        sb.append("    \"groups\": [");
        sb.append(joinQuoted(r.groups)).append("],\n");
        sb.append("    \"friends\": [");
        sb.append(joinQuoted(r.friends)).append("]\n");
        sb.append("  },\n");
        sb.append("  \"api\": {\n");
        sb.append("    \"apply\": ").append(r.apiApply ? "true" : "false").append(",\n");
        sb.append("    \"base\": ").append(jsonStr(r.apiBase)).append(",\n");
        sb.append("    \"key\": ").append(jsonStr(r.apiKey)).append(",\n");
        sb.append("    \"model\": ").append(jsonStr(r.apiModel)).append(",\n");
        sb.append("    \"source_id\": ").append(jsonStr(SOURCE_ID)).append(",\n");
        sb.append("    \"provider_id\": ").append(jsonStr(PROVIDER_ID)).append("\n");
        sb.append("  }\n");
        sb.append("}\n");
        return sb.toString();
    }

    private static String joinQuoted(List<String> items) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < items.size(); i++) {
            if (i > 0) {
                sb.append(", ");
            }
            sb.append(jsonStr(items.get(i)));
        }
        return sb.toString();
    }

    private static String jsonStr(String s) {
        StringBuilder sb = new StringBuilder("\"");
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c == '\n') {
                sb.append("\\n");
            } else if (c == '\r') {
                sb.append("\\r");
            } else if (c == '\t') {
                sb.append("\\t");
            } else if (c < 0x20) {
                sb.append(String.format("\\u%04x", (int) c));
            } else {
                sb.append(c);
            }
        }
        return sb.append('"').toString();
    }

    /**
     * 服务器端脚本（静态、不含任何用户值）。
     * 它按「键名 + 结构」改 cmd_config.json 的对应字段，改前备份、保留 BOM、
     * 原子替换，最后打印 DSH_RESULT 一行给 App 解析。
     */
    public static String script() {
        return SCRIPT;
    }

    /** 解析脚本最后那行 DSH_RESULT。 */
    public static Map<String, Object> parseResult(String stdout) throws SetupException {
        for (String line : (stdout == null ? "" : stdout).split("\n")) {
            String s = line.trim();
            if (s.startsWith("DSH_RESULT:")) {
                try {
                    Object o = Json.parse(s.substring("DSH_RESULT:".length()));
                    if (o instanceof Map) {
                        @SuppressWarnings("unchecked")
                        Map<String, Object> m = (Map<String, Object>) o;
                        return m;
                    }
                } catch (Json.JsonError e) {
                    throw new SetupException("服务器返回的结果看不懂：" + e.getMessage());
                }
            }
        }
        throw new SetupException("服务器没有返回结果，请重试。");
    }

    /** 把结果里的改动项拼成给用户看的一段话。 */
    public static String describe(Map<String, Object> result) {
        StringBuilder sb = new StringBuilder();
        List<Object> changed = Json.arr(result, "changed");
        for (int i = 0; i < changed.size(); i++) {
            if (i > 0) {
                sb.append('\n');
            }
            sb.append("· ").append(String.valueOf(changed.get(i)));
        }
        if (Json.bool(result, "verified", false)) {
            sb.append("\n· 已回读校验：服务器上落盘的配置与本次填写一致");
        }
        String backup = Json.str(result, "backup", "");
        if (!backup.isEmpty()) {
            sb.append("\n（原配置已备份为 ").append(backup).append("）");
        }
        return sb.toString();
    }

    private static String trim(String s) {
        return s == null ? "" : s.trim();
    }

    private static boolean hasSpace(String s) {
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == ' ' || c == '\t') {
                return true;
            }
        }
        return false;
    }

    private static final String SCRIPT = ""
            + "#!/usr/bin/env python3\n"
            + "# 手机控制台写入：聊天范围（白名单）+ 主聊天 API。只动对应字段，改前备份。\n"
            + "# 用法：chat-setup.py <cmd_config.json> <payload.json>（密钥只在载荷文件里，不进命令行）\n"
            + "import json\n"
            + "import os\n"
            + "import shutil\n"
            + "import sys\n"
            + "import time\n"
            + "\n"
            + "\n"
            + "def log(msg):\n"
            + "    sys.stdout.write('DSH_PROGRESS:' + msg + '\\n')\n"
            + "    sys.stdout.flush()\n"
            + "\n"
            + "\n"
            + "def fail(msg):\n"
            + "    sys.stdout.write('DSH_ERROR:' + msg + '\\n')\n"
            + "    sys.stdout.flush()\n"
            + "    sys.exit(1)\n"
            + "\n"
            + "\n"
            + "if len(sys.argv) < 3:\n"
            + "    fail('脚本参数不对')\n"
            + "cfg_path = sys.argv[1]\n"
            + "payload_path = sys.argv[2]\n"
            + "\n"
            + "if not os.path.isfile(cfg_path):\n"
            + "    fail('服务器上还没有 AstrBot 配置（cmd_config.json）：先在控制台点「开始部署」，'\n"
            + "         '等容器起来一次再来写。')\n"
            + "\n"
            + "try:\n"
            + "    with open(payload_path, encoding='utf-8') as fh:\n"
            + "        payload = json.load(fh)\n"
            + "except (OSError, ValueError) as exc:\n"
            + "    fail('读不到这次要写的内容：' + str(exc))\n"
            + "\n"
            + "raw = open(cfg_path, 'rb').read()\n"
            + "has_bom = raw.startswith(b'\\xef\\xbb\\xbf')\n"
            + "try:\n"
            + "    cfg = json.loads(raw.decode('utf-8-sig'))\n"
            + "except ValueError as exc:\n"
            + "    fail('AstrBot 配置不是合法 JSON，先别动它：' + str(exc))\n"
            + "\n"
            + "changed = []\n"
            + "\n"
            + "# ---------------- 聊天范围 ----------------\n"
            + "scope = payload.get('scope') or {}\n"
            + "if scope.get('apply'):\n"
            + "    groups = [str(g).strip() for g in (scope.get('groups') or []) if str(g).strip()]\n"
            + "    friends = [str(q).strip() for q in (scope.get('friends') or []) if str(q).strip()]\n"
            + "    if not groups and not friends:\n"
            + "        fail('聊天范围至少要有一个群号或私聊 QQ 号')\n"
            + "    plat = 'default'\n"
            + "    plats = cfg.get('platform') or []\n"
            + "    if plats and isinstance(plats[0], dict) and plats[0].get('id'):\n"
            + "        plat = str(plats[0]['id'])\n"
            + "    entries = list(groups) + [plat + ':FriendMessage:' + q for q in friends]\n"
            + "    settings = cfg.setdefault('platform_settings', {})\n"
            + "    settings['enable_id_white_list'] = True\n"
            + "    settings['id_whitelist'] = entries\n"
            + "    changed.append('聊天范围：' + '、'.join(entries))\n"
            + "\n"
            + "# ---------------- 主聊天 API ----------------\n"
            + "api = payload.get('api') or {}\n"
            + "if api.get('apply'):\n"
            + "    base = str(api.get('base') or '').strip()\n"
            + "    key = str(api.get('key') or '')\n"
            + "    model = str(api.get('model') or '').strip()\n"
            + "    sid = str(api.get('source_id') or 'dafeiyu-main_source')\n"
            + "    pid = str(api.get('provider_id') or 'dafeiyu-main')\n"
            + "    if not base.startswith(('http://', 'https://')):\n"
            + "        fail('接口地址要以 http:// 或 https:// 开头')\n"
            + "    if not key:\n"
            + "        fail('API Key 不能为空')\n"
            + "    if not model:\n"
            + "        fail('模型名不能为空')\n"
            + "    sources = cfg.setdefault('provider_sources', [])\n"
            + "    src = None\n"
            + "    for item in sources:\n"
            + "        if isinstance(item, dict) and item.get('id') == sid:\n"
            + "            src = item\n"
            + "            break\n"
            + "    if src is None:\n"
            + "        src = {'id': sid, 'provider': 'openai', 'type': 'openai_chat_completion',\n"
            + "               'provider_type': 'chat_completion', 'enable': True, 'timeout': 120,\n"
            + "               'proxy': '', 'custom_headers': {}}\n"
            + "        sources.append(src)\n"
            + "    src['provider'] = src.get('provider') or 'openai'\n"
            + "    src['type'] = src.get('type') or 'openai_chat_completion'\n"
            + "    src['provider_type'] = src.get('provider_type') or 'chat_completion'\n"
            + "    src['key'] = [key]\n"
            + "    src['api_base'] = base\n"
            + "    src['enable'] = True\n"
            + "    providers = cfg.setdefault('provider', [])\n"
            + "    prov = None\n"
            + "    for item in providers:\n"
            + "        if isinstance(item, dict) and item.get('id') == pid:\n"
            + "            prov = item\n"
            + "            break\n"
            + "    if prov is None:\n"
            + "        prov = {'id': pid, 'enable': True, 'modalities': ['text', 'tool_use']}\n"
            + "        providers.append(prov)\n"
            + "    prov['provider_source_id'] = sid\n"
            + "    prov['model'] = model\n"
            + "    prov['enable'] = True\n"
            + "    cfg.setdefault('provider_settings', {})['default_provider_id'] = pid\n"
            + "    changed.append('主聊天 API：' + base + ' → ' + model + '（' + pid + '）')\n"
            + "\n"
            + "if not changed:\n"
            + "    fail('没有要改的内容')\n"
            + "\n"
            + "backup = cfg_path + '.bak.controller.' + time.strftime('%Y%m%d-%H%M%S')\n"
            + "shutil.copy2(cfg_path, backup)\n"
            + "tmp = cfg_path + '.tmp.controller'\n"
            + "with open(tmp, 'w', encoding='utf-8-sig' if has_bom else 'utf-8') as fh:\n"
            + "    fh.write(json.dumps(cfg, ensure_ascii=False, indent=2))\n"
            + "os.replace(tmp, cfg_path)\n"
            + "log('原配置已备份：' + os.path.basename(backup))\n"
            + "\n"
            + "# ---------------- 回读校验 ----------------\n"
            + "# 写完不算完：重新把文件读一遍，确认落盘的内容真的是我们要的。\n"
            + "# 只信「自己刚写进去的变量」等于没验证 —— 磁盘可能没落、格式可能被改坏。\n"
            + "try:\n"
            + "    with open(cfg_path, encoding='utf-8-sig') as fh:\n"
            + "        check = json.load(fh)\n"
            + "except (OSError, ValueError) as exc:\n"
            + "    fail('写入后配置读不回来，已中止：' + str(exc))\n"
            + "\n"
            + "if scope.get('apply'):\n"
            + "    cs = check.get('platform_settings') or {}\n"
            + "    if cs.get('enable_id_white_list') is not True:\n"
            + "        fail('回读校验失败：白名单开关没打开')\n"
            + "    if [str(x) for x in (cs.get('id_whitelist') or [])] != entries:\n"
            + "        fail('回读校验失败：白名单内容和写进去的不一致')\n"
            + "\n"
            + "if api.get('apply'):\n"
            + "    csrc = None\n"
            + "    for item in (check.get('provider_sources') or []):\n"
            + "        if isinstance(item, dict) and item.get('id') == sid:\n"
            + "            csrc = item\n"
            + "    cprov = None\n"
            + "    for item in (check.get('provider') or []):\n"
            + "        if isinstance(item, dict) and item.get('id') == pid:\n"
            + "            cprov = item\n"
            + "    if csrc is None or cprov is None:\n"
            + "        fail('回读校验失败：新 provider 没落盘')\n"
            + "    if csrc.get('api_base') != base or list(csrc.get('key') or []) != [key]:\n"
            + "        fail('回读校验失败：接口地址或密钥没写对')\n"
            + "    if cprov.get('model') != model or cprov.get('provider_source_id') != sid:\n"
            + "        fail('回读校验失败：模型或来源没写对')\n"
            + "    if (check.get('provider_settings') or {}).get('default_provider_id') != pid:\n"
            + "        fail('回读校验失败：默认聊天模型没指过去')\n"
            + "\n"
            + "log('回读校验通过')\n"
            + "for line in changed:\n"
            + "    log(line)\n"
            + "sys.stdout.write('DSH_RESULT:' + json.dumps(\n"
            + "    {'changed': changed, 'backup': os.path.basename(backup), 'verified': True},\n"
            + "    ensure_ascii=False) + '\\n')\n";

    /** 结果里「聊天范围」那行原文（测试与界面提示共用）。 */
    public static Map<String, String> emptyValues() {
        return new LinkedHashMap<String, String>();
    }

    /**
     * 可以落盘的**非密钥**部分：群号、私聊 QQ、接口地址、模型名。
     * API Key 刻意不在其中 —— 和 SSH 密码同一待遇，只进内存。
     * 返回的键必须在 DeployConfig.PERSIST_KEYS 里，否则 Store 不会存。
     */
    public static Map<String, String> persistable(String groupsText, String friendsText,
                                                  String apiBase, String apiModel) {
        Map<String, String> out = new LinkedHashMap<String, String>();
        out.put("chat_groups", trim(groupsText));
        out.put("chat_friends", trim(friendsText));
        out.put("api_base", trim(apiBase));
        out.put("api_model", trim(apiModel));
        return out;
    }

    /** 界面输入里真正需要保密的那一个（供日志脱敏用；没有则空串）。 */
    public static String secretOf(String apiKey) {
        return apiKey == null ? "" : apiKey.trim();
    }
}
