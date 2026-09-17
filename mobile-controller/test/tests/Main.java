package tests;

public final class Main {

    public static void main(String[] args) {
        JsonTest.run();
        ChatSetupTest.run();
        TotpTest.run();
        KnobsTest.run();
        DeployScriptTest.run();
        NapCatClientTest.run();
        PresetCryptoTest.run();
        // 隧道/管理服务/代理 —— 新增的「连服务器」这一层
        TunnelManagerTest.run();
        // WebView 请求改写 + API 申请引导
        //（对应「网页老是连不上」和「不知道 API 去哪申请」两条反馈）
        WebProxyPathTest.run();
        // 机器人列表搜索（对应「不能定向搜索机器人名字」这条反馈）
        RobotFilterTest.run();
        System.exit(T.report());
    }
}
