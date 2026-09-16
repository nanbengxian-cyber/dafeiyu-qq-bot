package com.dafeiyu.controller;

import com.jcraft.jsch.JSch;
import com.jcraft.jsch.Session;

import java.security.MessageDigest;
import java.util.Base64;

/**
 * SSH 本地端口转发 —— 手机连服务器的唯一通道。
 *
 * 为什么要隧道：管理服务和各实例的 WebUI 都只监听服务器的 127.0.0.1，
 * 公网上根本没有这些端口。手机用 SSH 把服务器的 127.0.0.1:6199 映射到
 * 手机本地的某个端口，之后的 HTTP 请求全部走这条加密隧道。
 * 好处是：① 不需要公网暴露管理端口；② 认证用密钥，比密码强；
 * ③ 服务器上那个专用账号被限制成「只能转发到 6199」，拿到它也只能干这一件事。
 *
 * 两个容易踩的坑（都是实测踩过的）：
 *   1. **必须用 RSA 密钥，不能用 ed25519。** JSch 的 ed25519 签名实现
 *      要求 Java 15+（源码里直接抛 "SignatureEd25519 requires Java15+"），
 *      而 Android 上的 Ed25519 要 API 33+。用 ed25519 会一直报 Auth fail，
 *      看起来像密码错，其实是算法不可用。
 *   2. **指纹一定要校验。** 用 StrictHostKeyChecking=no 裸连的话，
 *      中间人可以假冒服务器，把隧道里的管理口令和 API Key 全拿走 ——
 *      SSH 密钥认证只证明「我是我」，不证明「对面是它」。
 *      所以连上后立刻比对指纹，对不上就断开，且此时还没有发过任何数据。
 */
public final class Tunnel {

    /** 隧道日志回调（脱敏后的短句，不吐密钥）。 */
    public interface Log {
        void line(String msg);
    }

    private Session session;
    private int localPort = -1;
    private String fingerprint = "";

    /**
     * 建立隧道，返回手机本地可用于访问管理服务的端口。
     *
     * @param host            服务器地址
     * @param sshPort         SSH 端口
     * @param user            专用账号（服务器上被限制成只能转发到管理端口）
     * @param keyBytes        RSA 私钥内容（PEM 或 OpenSSH 格式）
     * @param remotePort      服务器上的管理服务端口（通常是 6199）
     * @param expectPrint     期望的主机指纹（SHA256:... 形式）；空串表示不校验
     * @param log             日志回调，可为 null
     */
    public int open(String host, int sshPort, String user, byte[] keyBytes,
                    int remotePort, String expectPrint, Log log) throws Deployer.DeployException {
        close();
        try {
            JSch jsch = new JSch();
            // 第三个参数传 null：密钥里没有口令（服务器上生成时 -N ""）。
            // 身份名只是 JSch 内部标签，用中性名字 —— 否则公开版 APK 里会出现
            // 与真实服务器账号同名的字符串，脱敏扫描会（正确地）报出来。
            jsch.addIdentity("key", keyBytes, null, null);

            Session s = jsch.getSession(user, host, sshPort);
            // 先不校验（JSch 的校验报错信息对用户没意义），连上后我们自己比对指纹。
            // 顺序很重要：比对在 setPortForwardingL 之前，所以对不上时一个字节都没转发。
            s.setConfig("StrictHostKeyChecking", "no");
            s.setConfig("PreferredAuthentications", "publickey");

            try {
                s.connect(15000);
            } catch (com.jcraft.jsch.JSchException e) {
                String m = String.valueOf(e.getMessage());
                if (m.contains("Auth fail")) {
                    throw new Deployer.DeployException(
                            "服务器不接受这个 App 的密钥。可能是服务器换过密钥，"
                                    + "或者这个 App 版本太旧了。");
                }
                throw new Deployer.DeployException("连不上服务器：" + shortMsg(m));
            }

            // 指纹校验 —— 对不上就断开，防止中间人
            com.jcraft.jsch.HostKey hk = s.getHostKey();
            fingerprint = hk == null ? "" : sha256Fingerprint(hk.getKey());            if (expectPrint != null && !expectPrint.isEmpty()) {
                if (!fingerprint.equalsIgnoreCase(expectPrint)) {
                    s.disconnect();
                    throw new Deployer.DeployException(
                            "服务器指纹对不上，为安全起见已断开。\n"
                                    + "期望：" + expectPrint + "\n"
                                    + "实际：" + fingerprint + "\n"
                                    + "如果不是服务器换过机器，就是有人在中间截获。");
                }
                if (log != null) {
                    log.line("已校验服务器指纹 ✓");
                }
            }

            // lport 传 0 = 让系统随便挑一个空闲端口（避免和手机别的 App 抢端口）
            localPort = s.setPortForwardingL(0, "127.0.0.1", remotePort);
            session = s;
            if (log != null) {
                log.line("隧道已建立：手机 127.0.0.1:" + localPort + " → 服务器 " + remotePort);
            }
            return localPort;
        } catch (com.jcraft.jsch.JSchException e) {
            throw new Deployer.DeployException("建立隧道失败：" + shortMsg(String.valueOf(e.getMessage())));
        }
    }

    /** 隧道是否还活着。 */
    public boolean alive() {
        return session != null && session.isConnected();
    }

    /** 本地端口；未建立时为 -1。 */
    public int port() {
        return localPort;
    }

    /** 服务器主机指纹（连接后才有值）。 */
    public String fingerprint() {
        return fingerprint;
    }

    public void close() {
        if (session != null) {
            try {
                session.disconnect();
            } catch (Exception ignored) {
                // 断开失败无所谓，对象马上被丢弃
            }
            session = null;
        }
        localPort = -1;
    }

    /**
     * 算 OpenSSH 风格的 SHA256 指纹（base64，去掉尾部 =）。
     *
     * 入参是 JSch 的 HostKey.getKey() —— 注意它返回的是**base64 字符串**
     * （不是原始字节），所以要先把 base64 解开再哈希，否则算出来的指纹
     * 和 `ssh-keyscan | ssh-keygen -lf -` 显示的对不上，校验永远失败。
     */
    public static String sha256Fingerprint(String hostKeyBase64) {
        try {
            // 空输入必须返回空串，**不能**返回 SHA256(空) 那个「合法的哈希」：
            // 调用方看到非空指纹会以为校验有效，实际它谁都匹配不上或都能匹配上，
            // 等于把防中间人的校验变成了摆设。
            if (hostKeyBase64 == null || hostKeyBase64.trim().isEmpty()) {
                return "";
            }
            byte[] blob = Base64.getDecoder().decode(hostKeyBase64.trim());
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            String b64 = Base64.getEncoder().encodeToString(md.digest(blob));
            while (b64.endsWith("=")) {
                b64 = b64.substring(0, b64.length() - 1);
            }
            return "SHA256:" + b64;
        } catch (Exception e) {
            return "";
        }
    }

    /** 把 JSch 的英文异常压成一句人话，别把堆栈甩给用户。 */
    private static String shortMsg(String m) {
        if (m == null || m.isEmpty()) {
            return "未知错误";
        }
        if (m.contains("Connection refused")) {
            return "服务器拒绝连接，检查地址和 SSH 端口。";
        }
        if (m.contains("timeout") || m.contains("timed out")) {
            return "连接超时，检查网络，或确认服务器上的 SSH 端口是否放行。";
        }
        if (m.contains("UnknownHost")) {
            return "找不到这个服务器地址。";
        }
        return m.length() > 120 ? m.substring(0, 120) : m;
    }
}
