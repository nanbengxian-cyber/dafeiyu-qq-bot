package tests;

public final class Main {

    public static void main(String[] args) {
        JsonTest.run();
        ChatSetupTest.run();
        TotpTest.run();
        KnobsTest.run();
        DeployScriptTest.run();
        NapCatClientTest.run();
        System.exit(T.report());
    }
}
