import com.dafeiyu.controller.WebProxyPath;

/**
 * 把 WebUI 自动登录地址打到 stdout，供 node 用真 JS 引擎验证
 * （见 run-weblogin-test.sh）。
 *
 * 参数：&lt;隧道端口&gt; &lt;实例名&gt; &lt;token&gt;
 * 输出：App 实际会加载的那条地址。
 */
public final class TokenGen {
    public static void main(String[] args) {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 41000;
        String inst = args.length > 1 ? args[1] : "qq1";
        String tok = args.length > 2 ? args[2] : "";
        System.out.print(WebProxyPath.withToken(
                WebProxyPath.proxyUrlFor(port, inst, "/webui/"), tok));
    }
}
