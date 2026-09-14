# -*- coding: utf-8 -*-
import json
import unittest

import deployer
from deployer import (
    DeployConfig,
    DeployError,
    SSHDeployer,
    build_compose_override,
    build_remote_script,
    format_containers,
    format_environment,
    parse_compose_ps,
    parse_key_values,
    redact,
    validate_repo_url,
)

BASE = {
    "host": "bot.example.invalid",
    "port": "22",
    "username": "root",
    "auth_type": "密码",
    "password": "super-secret-password",
    "key_file": "",
    "key_passphrase": "",
    "trust_new_host": True,
    "repo_url": "https://git.example.invalid/owner/repo.git",
    "repo_ref": "main",
    "deploy_dir": "~/dafeiyu-bot",
    "install_dependencies": True,
    "napcat_port": "3001",
    "astrbot_port": "6185",
    "astrbot_api_port": "6186",
    "bind_address": "0.0.0.0",
    "napcat_image": "mlikiowa/napcat-docker:latest",
    "astrbot_image": "soulter/astrbot:latest",
}


def cfg(**overrides):
    values = dict(BASE)
    values.update(overrides)
    return DeployConfig.from_values(values)


class ValidationTests(unittest.TestCase):
    def test_valid_config(self):
        config = cfg()
        self.assertEqual(config.host, "bot.example.invalid")
        self.assertEqual(config.port, 22)
        self.assertEqual(config.napcat_port, 3001)

    def test_rejects_bad_inputs(self):
        cases = [
            ({"host": ""}, "空地址"),
            ({"host": "bad host"}, "地址带空格"),
            ({"port": "70000"}, "端口越界"),
            ({"port": "abc"}, "端口非数字"),
            ({"username": "bad user"}, "用户名带空格"),
            ({"password": ""}, "密码为空"),
            ({"repo_url": ""}, "仓库为空"),
            ({"repo_url": "http://git.example.invalid/o/r.git"}, "HTTP 仓库"),
            ({"repo_url": "https://user:pw@git.example.invalid/o/r.git"}, "仓库内嵌凭据"),
            ({"repo_ref": "bad ref"}, "分支带空格"),
            ({"deploy_dir": "/"}, "部署到根目录"),
            ({"deploy_dir": "~"}, "部署到主目录"),
            ({"napcat_port": "6185"}, "端口重复"),
            ({"bind_address": "not-an-ip"}, "监听地址非法"),
            ({"astrbot_image": "bad image!"}, "镜像名非法"),
        ]
        for overrides, why in cases:
            with self.subTest(why=why):
                with self.assertRaises(DeployError):
                    cfg(**overrides)

    def test_key_auth_requires_existing_file(self):
        with self.assertRaises(DeployError):
            cfg(auth_type="SSH 私钥", key_file="/nonexistent/id_ed25519", password="")

    def test_unknown_auth_type_rejected(self):
        with self.assertRaises(DeployError):
            cfg(auth_type="指纹", password="x")

    def test_safe_profile_excludes_secrets(self):
        profile = cfg().safe_profile()
        for secret in ("password", "key_file", "key_passphrase"):
            self.assertNotIn(secret, profile)
        self.assertEqual(profile["host"], "bot.example.invalid")
        self.assertNotIn("super-secret-password", json.dumps(profile))

    def test_repo_url_forms(self):
        validate_repo_url("https://git.example.invalid/o/r.git")
        validate_repo_url("git@git.example.invalid:o/r.git")
        for bad in ("ftp://x/y", "https://", "git@host", "https://x/y?token=1"):
            with self.subTest(bad=bad):
                with self.assertRaises(DeployError):
                    validate_repo_url(bad)


class ScriptTests(unittest.TestCase):
    def test_compose_contains_ports_and_env_file(self):
        text = build_compose_override(cfg())
        self.assertIn('"0.0.0.0:3001:3001"', text)
        self.assertIn('"0.0.0.0:6185:6185"', text)
        self.assertIn('"0.0.0.0:6186:6186"', text)
        self.assertIn("./deploy/robot.env", text)
        self.assertIn("soulter/astrbot:latest", text)

    def test_script_quotes_inputs_and_has_no_password(self):
        script = build_remote_script(cfg())
        self.assertIn("git clone", script)
        self.assertIn(deployer.APP_MARKER, script)
        self.assertIn("$DOCKER compose", script)
        self.assertNotIn("super-secret-password", script)
        # 目录必须带标记保护
        self.assertIn("已拒绝覆盖", script)

    def test_script_shell_quotes_injection_attempt(self):
        script = build_remote_script(cfg(deploy_dir="~/x; rm -rf /tmp/evil"))
        self.assertIn("'~/x; rm -rf /tmp/evil'", script)
        self.assertNotIn("; rm -rf /tmp/evil\n", script)

    def test_install_flag_off(self):
        script = build_remote_script(cfg(install_dependencies=False))
        self.assertIn("INSTALL=0", script)

    def test_compose_marker_collision_is_rejected(self):
        with self.assertRaises(DeployError):
            build_remote_script(cfg(), compose_text="DSH_COMPOSE_EOF\n")

    def test_compose_shell_uses_sudo_fallback(self):
        command = deployer.compose_shell(cfg(), "ps")
        self.assertIn("sudo docker", command)
        self.assertIn("docker-compose.generated.yml", command)


class RedactTests(unittest.TestCase):
    def test_redacts_url_credentials_and_keywords(self):
        text = "clone https://user:token@git.example.invalid/o/r.git"
        self.assertNotIn("user:token", redact(text))
        self.assertIn("<已隐藏>", redact("password=abc123"))
        self.assertIn("<已隐藏>", redact("Authorization: Bearer abc"))
        self.assertIn("<已隐藏>", redact("API_KEY=xyz"))

    def test_redacts_explicit_secret_but_keeps_short_values(self):
        self.assertNotIn("super-secret-password",
                         redact("pw=super-secret-password", ("super-secret-password",)))
        # 太短的值不替换，避免把普通数字打成乱码
        self.assertEqual(redact("port 22", ("22",)), "port 22")


class FakeTransport:
    """假传输层：记录命令、模拟文件系统，不碰网络。"""

    def __init__(self, responses=None):
        self.commands = []
        self.puts = []
        self.files = {}
        self.closed = False
        self.responses = responses or {}

    def run(self, command, timeout=900, on_line=None):
        self.commands.append(command)
        for needle, (rc, out, err) in self.responses.items():
            if needle in command:
                if on_line:
                    for line in out.splitlines():
                        on_line(line)
                return rc, out, err
        return 0, "", ""

    def put_text(self, path, text):
        self.puts.append((path, text))
        self.files[path] = text

    def get_text(self, path, limit=262144):
        return self.files.get(path, "")

    def close(self):
        self.closed = True


class DeployerFlowTests(unittest.TestCase):
    def make(self, responses=None):
        transport = FakeTransport(responses)
        deployer_obj = SSHDeployer(cfg(), transport_factory=lambda _c: transport)
        return deployer_obj, transport

    def test_deploy_forwards_progress_and_does_not_close(self):
        responses = {"git clone": (0, "DSH_PROGRESS:从仓库下载源码\nDSH_PROGRESS:部署完成", "")}
        session, transport = self.make(responses)
        seen = []
        session.progress = seen.append
        session.open()
        session.deploy()
        self.assertEqual(seen[-1], "部署完成")
        self.assertFalse(transport.closed, "部署后连接应保持，供读取配置使用")
        session.close()
        self.assertTrue(transport.closed)

    def test_deploy_error_is_mapped_and_redacted(self):
        responses = {"git clone": (1, "", "DSH_ERROR:源码下载失败")}
        session, _transport = self.make(responses)
        session.open()
        with self.assertRaisesRegex(DeployError, "源码下载失败"):
            session.deploy()

    def test_deploy_error_falls_back_to_tail(self):
        responses = {"git clone": (2, "some line\nlast meaningful line", "")}
        session, _transport = self.make(responses)
        session.open()
        with self.assertRaisesRegex(DeployError, "last meaningful line"):
            session.deploy()

    def test_check_environment(self):
        out = "OS=Linux\nGIT=yes\nDOCKER=no\nPRIVILEGE=root\nDISK_FREE_MB=2048"
        session, _transport = self.make({"uname -s": (0, out, "")})
        session.open()
        info = session.check_environment()
        self.assertEqual(info["OS"], "Linux")
        self.assertEqual(info["DOCKER"], "no")
        self.assertIn("未安装", format_environment(info))

    def test_status_parses_json_lines(self):
        out = '{"Service":"astrbot","State":"running","Publishers":[{"PublishedPort":6185,"TargetPort":6185}]}'
        session, _transport = self.make({"compose -f": (0, out, "")})
        session.open()
        containers = session.status()
        self.assertEqual(containers[0]["Service"], "astrbot")
        self.assertIn("astrbot：running", format_containers(containers))

    def test_read_schema_and_env(self):
        schema = json.dumps({"knobs": [{"key": "DSH_X", "type": "bool"}]})
        session, _transport = self.make({
            "console-config.json": (0, schema, ""),
            "robot.env": (0, "DSH_X=1\n", ""),
        })
        session.open()
        self.assertEqual(session.read_knob_schema()["knobs"][0]["key"], "DSH_X")
        self.assertEqual(session.read_env(), "DSH_X=1\n")

    def test_read_schema_empty_returns_list(self):
        session, _transport = self.make({"console-config.json": (0, "", "")})
        session.open()
        self.assertEqual(session.read_knob_schema(), [])

    def test_read_schema_bad_json_raises(self):
        session, _transport = self.make({"console-config.json": (0, "not json", "")})
        session.open()
        with self.assertRaises(DeployError):
            session.read_knob_schema()

    def test_write_env_uses_tmp_then_move(self):
        session, transport = self.make({
            "printf": (0, "/home/tester/dafeiyu-bot", ""),
            "mv ": (0, "", ""),
        })
        session.open()
        session.write_env("DSH_X=2\n")
        # SFTP 不展开 ~，必须用解析后的绝对路径
        self.assertEqual(transport.puts[0][0],
                         "/home/tester/dafeiyu-bot/deploy/robot.env.tmp")
        self.assertIn("mv ", transport.commands[-1])
        self.assertIn("chmod 600", transport.commands[-1])

    def test_remote_dir_rejects_unresolved_path(self):
        session, _transport = self.make({"printf": (0, "~/dafeiyu-bot", "")})
        session.open()
        with self.assertRaises(DeployError):
            session.remote_dir()

    def test_recreate_uses_force_recreate(self):
        session, transport = self.make({"up -d": (0, "", "")})
        session.open()
        session.recreate(("astrbot",))
        self.assertIn("--force-recreate", transport.commands[-1])
        self.assertIn("astrbot", transport.commands[-1])

    def test_recreate_no_services_is_noop(self):
        session, transport = self.make()
        session.open()
        session.recreate(())
        self.assertEqual(transport.commands, [])

    def test_write_env_failure_raises(self):
        session, _transport = self.make({"mv ": (1, "", "permission denied")})
        session.open()
        with self.assertRaises(DeployError):
            session.write_env("DSH_X=2\n")

    def test_operations_require_open(self):
        session = SSHDeployer(cfg(), transport_factory=lambda _c: FakeTransport())
        with self.assertRaises(DeployError):
            session.status()


class ParseTests(unittest.TestCase):
    def test_parse_compose_ps_variants(self):
        self.assertEqual(parse_compose_ps(""), [])
        self.assertEqual(parse_compose_ps('[{"Service":"a"}]'), [{"Service": "a"}])
        self.assertEqual(parse_compose_ps('{"Service":"a"}'), [{"Service": "a"}])
        self.assertEqual(parse_compose_ps('{"Service":"a"}\n{"Service":"b"}'),
                         [{"Service": "a"}, {"Service": "b"}])
        self.assertEqual(parse_compose_ps("garbage"), [])

    def test_format_containers_empty(self):
        self.assertIn("刷新状态", format_containers([]))

    def test_parse_key_values(self):
        self.assertEqual(parse_key_values("A=1\nB=two=2\n\n"),
                         {"A": "1", "B": "two=2"})

    def test_format_environment_limited_privilege(self):
        text = format_environment({"OS": "Linux", "PRIVILEGE": "limited"})
        self.assertIn("无法自动安装依赖", text)


if __name__ == "__main__":
    unittest.main()
