import com.dafeiyu.controller.WebProxyPath;

/** 把注入脚本打到 stdout，供 node 用真 JS 引擎验证（见 run-shim-test.sh）。 */
public final class ShimGen {
    public static void main(String[] args) {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 41000;
        String inst = args.length > 1 ? args[1] : "qq1";
        System.out.print(WebProxyPath.shimJs(port, inst));
    }
}
