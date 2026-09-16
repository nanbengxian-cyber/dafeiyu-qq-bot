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
        System.exit(T.report());
    }
}
