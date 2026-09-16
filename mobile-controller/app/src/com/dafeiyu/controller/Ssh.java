package com.dafeiyu.controller;

import com.jcraft.jsch.ChannelExec;
import com.jcraft.jsch.ChannelSftp;
import com.jcraft.jsch.HostKey;
import com.jcraft.jsch.HostKeyRepository;
import com.jcraft.jsch.JSch;
import com.jcraft.jsch.Session;
import com.jcraft.jsch.SftpException;
import com.jcraft.jsch.UserInfo;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;

/**
 * SSH 传输 —— Deployer.Transport 的 JSch 实现。
 *
 * 主机指纹策略与桌面控制台对齐：
 * - 勾了「首次连接接受指纹」→ 本连不校验，连上后把指纹记进 Store，下次开始校验；
 * - 没勾 → 只接受已记录的指纹：没记录 = 拒绝（提示先勾选），不一致 = 拒绝（防中间人）。
 *
 * 密码只进 JSch 的内存参数，不打日志；所有对外错误都先过 Deployer.redact。
 */
public final class Ssh implements Deployer.Transport {

    /** 指纹存取（安卓上由 Store 实现；解耦成接口便于测试）。 */
    public interface HostKeys {
        String get(String host, int port);

        void put(String host, int port, String type, String base64Key);
    }

    private static final int CONNECT_TIMEOUT_MS = 15000;
    private static final int MAX_OUTPUT = 512 * 1024;

    private final DeployConfig cfg;
    private final HostKeys keys;
    private Session session;

    public Ssh(DeployConfig cfg, HostKeys keys) {
        this.cfg = cfg;
        this.keys = keys;
    }

    // ---------------------------------------------------------------- 连接

    public void connect() throws Deployer.DeployException {
        try {
            JSch jsch = new JSch();
            String stored = keys.get(cfg.host, cfg.port);
            if (cfg.trustNewHost) {
                // 本连不校验；连上后记住指纹。JSch 的 "no" 也会尝试写 known_hosts，
                // 用自定义空仓库接住，别让它在手机上乱写文件。
                jsch.setHostKeyRepository(new BlackholeRepo());
            } else if (stored != null && !stored.isEmpty()) {
                jsch.setHostKeyRepository(new PinnedRepo(stored));
            } else {
                // 没记录又不肯勾选：直接给可行动的提示，而不是甩一句 reject HostKey。
                throw new Deployer.DeployException(
                        "这是首次连接该服务器：请勾选「首次连接接受服务器指纹」，"
                                + "或先用电脑 ssh 确认指纹。");
            }
            Session s = jsch.getSession(cfg.username, cfg.host, cfg.port);
            s.setPassword(cfg.password);
            s.setConfig("PreferredAuthentications", "password,keyboard-interactive");
            s.setConfig("StrictHostKeyChecking", cfg.trustNewHost ? "no" : "yes");
            // 刻意不设 Session.setTimeout：它控制的是会话内所有读的 SO_TIMEOUT，
            // 部署时 docker pull 半分钟没输出很正常，15 秒超时会把长部署误杀。
            // 连接阶段用 connect(timeoutMs)，执行阶段用我们自己的 deadline。
            try {
                s.connect(CONNECT_TIMEOUT_MS);
            } catch (com.jcraft.jsch.JSchException e) {
                s.disconnect();
                throw translate(e, stored);
            }
            if (cfg.trustNewHost) {
                HostKey hk = s.getHostKey();
                if (hk != null) {
                    keys.put(cfg.host, cfg.port, hk.getType(), hk.getKey());
                }
            }
            session = s;
        } catch (com.jcraft.jsch.JSchException e) {
            if (e.getMessage() != null && e.getMessage().contains("Auth fail")) {
                throw new Deployer.DeployException("SSH 登录失败，请检查用户名和密码。");
            }
            throw new Deployer.DeployException("SSH 连接失败："
                    + Deployer.redact(String.valueOf(e.getMessage()), new String[]{cfg.password}));
        }
    }

    private Deployer.DeployException translate(com.jcraft.jsch.JSchException e, String stored) {
        String msg = String.valueOf(e.getMessage());
        if (msg.contains("reject HostKey") || msg.contains("HostKey has been changed")) {
            if (stored == null || stored.isEmpty()) {
                return new Deployer.DeployException(
                        "这是首次连接该服务器：请勾选「首次连接接受服务器指纹」，"
                                + "或先用电脑 ssh 确认指纹。");
            }
            return new Deployer.DeployException("服务器指纹与已记录的不一致，已拒绝连接。"
                    + "如果服务器重装过，清掉手机存储数据后重新勾选接受。");
        }
        if (msg.contains("Auth fail") || msg.contains("AUTH FAIL")) {
            return new Deployer.DeployException("SSH 登录失败，请检查用户名和密码。");
        }
        if (msg.contains("timeout") || msg.contains("Timeout") || msg.contains("socket is not established")) {
            return new Deployer.DeployException("无法连接服务器，请检查地址、端口与安全组。");
        }
        return new Deployer.DeployException("SSH 连接失败："
                + Deployer.redact(msg, new String[]{cfg.password}));
    }

    private static final class BlackholeRepo implements HostKeyRepository {
        public int check(String host, byte[] key) {
            return OK;
        }

        public void add(HostKey hostkey, UserInfo ui) {
        }

        public void remove(String host, String type) {
        }

        public void remove(String host, String type, byte[] key) {
        }

        public String getKnownHostsRepositoryID() {
            return "";
        }

        public HostKey[] getHostKey() {
            return new HostKey[0];
        }

        public HostKey[] getHostKey(String host, String type) {
            return new HostKey[0];
        }
    }

    /** 只认一枚已记录指纹的仓库。pinned 格式：type|base64。 */
    private static final class PinnedRepo implements HostKeyRepository {
        private final String pinned;

        PinnedRepo(String pinned) {
            this.pinned = pinned == null ? "" : pinned;
        }

        public int check(String host, byte[] key) {
            if (key == null) {
                return NOT_INCLUDED;
            }
            int bar = pinned.indexOf('|');
            String pinnedKey = bar >= 0 ? pinned.substring(bar + 1) : pinned;
            String b64 = java.util.Base64.getEncoder().encodeToString(key);
            if (pinnedKey.isEmpty()) {
                return NOT_INCLUDED;
            }
            return pinnedKey.equals(b64) ? OK : CHANGED;
        }

        public void add(HostKey hostkey, UserInfo ui) {
        }

        public void remove(String host, String type) {
        }

        public void remove(String host, String type, byte[] key) {
        }

        public String getKnownHostsRepositoryID() {
            return "";
        }

        public HostKey[] getHostKey() {
            return new HostKey[0];
        }

        public HostKey[] getHostKey(String host, String type) {
            return new HostKey[0];
        }
    }

    // ---------------------------------------------------------------- 执行

    @Override
    public Deployer.RunResult run(String command, int timeoutMs, Deployer.LineSink onLine) {
        if (session == null || !session.isConnected()) {
            return new Deployer.RunResult(-1, "", "尚未连接服务器");
        }
        ChannelExec channel = null;
        try {
            channel = (ChannelExec) session.openChannel("exec");
            channel.setCommand(command);
            channel.setInputStream(null);
            final ByteArrayOutputStream errBuf = new ByteArrayOutputStream();
            channel.setErrStream(errBuf, false);
            InputStream out = channel.getInputStream();
            channel.connect(Math.max(timeoutMs, CONNECT_TIMEOUT_MS));
            StringBuilder stdout = new StringBuilder();
            byte[] buf = new byte[8192];
            long deadline = System.currentTimeMillis() + timeoutMs;
            while (true) {
                while (out.available() > 0) {
                    int n = out.read(buf);
                    if (n < 0) {
                        break;
                    }
                    if (stdout.length() < MAX_OUTPUT) {
                        stdout.append(new String(buf, 0, n, "UTF-8"));
                    }
                    if (onLine != null) {
                        emitLines(stdout, onLine);
                    }
                }
                if (channel.isClosed() || channel.getExitStatus() >= 0) {
                    // 关闭后再把残料读干净
                    while (out.available() > 0) {
                        int n = out.read(buf);
                        if (n < 0) {
                            break;
                        }
                        if (stdout.length() < MAX_OUTPUT) {
                            stdout.append(new String(buf, 0, n, "UTF-8"));
                        }
                    }
                    break;
                }
                if (System.currentTimeMillis() > deadline) {
                    channel.disconnect();
                    return new Deployer.RunResult(-1, stdout.toString(),
                            "命令执行超时（" + (timeoutMs / 1000) + " 秒）");
                }
                Thread.sleep(120);
            }
            while (!channel.isClosed()) {
                Thread.sleep(50);
            }
            String err = new String(errBuf.toByteArray(), "UTF-8");
            if (err.length() > MAX_OUTPUT) {
                err = err.substring(err.length() - MAX_OUTPUT);
            }
            return new Deployer.RunResult(channel.getExitStatus(), stdout.toString(), err);
        } catch (Exception e) {
            return new Deployer.RunResult(-1, "",
                    "SSH 连接中断：" + Deployer.redact(String.valueOf(e), new String[]{cfg.password}));
        } finally {
            if (channel != null) {
                channel.disconnect();
            }
        }
    }

    private void emitLines(StringBuilder sb, Deployer.LineSink sink) {
        // 只把「完整的行」交给回调；最后一行可能还没写完，留在缓冲里。
        while (true) {
            int nl = indexOfNl(sb);
            if (nl < 0) {
                return;
            }
            String line = sb.substring(0, nl);
            sb.delete(0, nl + 1);
            if (line.endsWith("\r")) {
                line = line.substring(0, line.length() - 1);
            }
            sink.line(line);
        }
    }

    private static int indexOfNl(StringBuilder sb) {
        for (int i = 0; i < sb.length(); i++) {
            if (sb.charAt(i) == '\n') {
                return i;
            }
        }
        return -1;
    }

    // ---------------------------------------------------------------- 文件

    @Override
    public void putText(String path, String text) throws Deployer.DeployException {
        String parent = path.contains("/") ? path.substring(0, path.lastIndexOf('/')) : ".";
        Deployer.RunResult r = run("mkdir -p " + Deployer.shq(parent), 60000, null);
        if (r.exit != 0) {
            throw new Deployer.DeployException("无法创建远程目录："
                    + Deployer.redact(r.stderr, new String[]{cfg.password}));
        }
        ChannelSftp sftp = null;
        try {
            sftp = (ChannelSftp) session.openChannel("sftp");
            sftp.connect(CONNECT_TIMEOUT_MS);
            OutputStream os = sftp.put(path, ChannelSftp.OVERWRITE);
            try {
                os.write(text.getBytes("UTF-8"));
                os.flush();
            } finally {
                os.close();
            }
        } catch (com.jcraft.jsch.JSchException | java.io.IOException | SftpException e) {
            throw new Deployer.DeployException("写入远程文件失败。");
        } finally {
            if (sftp != null) {
                sftp.disconnect();
            }
        }
    }

    @Override
    public String getText(String path, int limit) throws Deployer.DeployException {
        ChannelSftp sftp = null;
        try {
            sftp = (ChannelSftp) session.openChannel("sftp");
            sftp.connect(CONNECT_TIMEOUT_MS);
            InputStream in = sftp.get(path);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0 && out.size() < limit) {
                out.write(buf, 0, n);
            }
            in.close();
            return new String(out.toByteArray(), "UTF-8");
        } catch (com.jcraft.jsch.JSchException | java.io.IOException | SftpException e) {
            Throwable cause = e;
            if (e instanceof SftpException && ((SftpException) e).id == 2 /* NO_SUCH_FILE */) {
                throw new Deployer.DeployException("服务器上找不到文件：" + path);
            }
            throw new Deployer.DeployException("读取远程文件失败。");
        } finally {
            if (sftp != null) {
                sftp.disconnect();
            }
        }
    }

    @Override
    public void close() {
        if (session != null) {
            session.disconnect();
            session = null;
        }
    }
}
