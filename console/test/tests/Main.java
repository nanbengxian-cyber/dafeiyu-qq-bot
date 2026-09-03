package tests;

/**
 * 测试入口。退出码 0 = 全过，1 = 有失败 —— auto/verify.sh 靠这个判。
 */
public final class Main {

    public static void main(String[] args) {
        JsonTest.run();
        StatusFmtTest.run();
        KnobModelTest.run();
        ApiTest.run();
        UiTest.run();
        System.exit(T.report());
    }
}
