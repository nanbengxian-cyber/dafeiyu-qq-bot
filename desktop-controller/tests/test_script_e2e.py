# -*- coding: utf-8 -*-
"""端到端验证生成的远程脚本。

不连接任何真实服务器：把 git / docker / uname 换成临时目录里的假命令，
在临时 HOME 里真跑一遍脚本，检查它确实完成了
「下载源码 → 生成 compose → 拉镜像 → 启动 → 写状态」这条链路。

这样能验证 shell 语法、目录保护、幂等更新，而不会碰到本机的 Docker 或任何生产服务。
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import deployer
from deployer import DeployConfig, build_remote_script

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
    "deploy_dir": "",
    "install_dependencies": True,
    "napcat_port": "3001",
    "astrbot_port": "6185",
    "astrbot_api_port": "6186",
    "bind_address": "0.0.0.0",
    "napcat_image": "mlikiowa/napcat-docker:latest",
    "astrbot_image": "soulter/astrbot:latest",
}

FAKE_UNAME = """#!/bin/sh
echo Linux
"""

FAKE_GIT = """#!/bin/sh
case "$1" in
  clone)
    for last in "$@"; do :; done
    mkdir -p "$last/.git" "$last/deploy"
    printf '# 模板\\nDSH_DECIDE=1\\n' > "$last/deploy/robot.env.example"
    ;;
esac
exit 0
"""

FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *" ps"*) printf '{"Service":"astrbot","State":"running"}\\n' ;;
esac
exit 0
"""


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    os.chmod(str(path), 0o755)


class ScriptSandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deploy-e2e-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "fakebin"
        self.bin.mkdir()
        _write(self.bin / "uname", FAKE_UNAME)
        _write(self.bin / "git", FAKE_GIT)
        _write(self.bin / "docker", FAKE_DOCKER)
        self.docker_log = self.tmp / "docker.log"
        self.target = self.home / "dafeiyu-bot"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _config(self, **overrides):
        values = dict(BASE)
        values["deploy_dir"] = str(self.target)
        values.update(overrides)
        return DeployConfig.from_values(values)

    def _run(self, cfg):
        script = build_remote_script(cfg)
        env = dict(os.environ)
        env.update({
            "HOME": str(self.home),
            "PATH": "%s:%s" % (self.bin, env.get("PATH", "/usr/bin:/bin")),
            "FAKE_DOCKER_LOG": str(self.docker_log),
        })
        return subprocess.run(
            ["sh", "-s"], input=script, text=True, env=env,
            capture_output=True, timeout=120, cwd=str(self.tmp),
        )

    def test_script_is_valid_posix_shell(self):
        script = build_remote_script(self._config())
        result = subprocess.run(["sh", "-n"], input=script, text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_first_run_deploys(self):
        result = self._run(self._config())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DSH_PROGRESS:从仓库下载源码", result.stdout)
        self.assertIn("DSH_PROGRESS:部署完成", result.stdout)

        # 源码目录与标记
        self.assertTrue((self.target / ".git").is_dir())
        self.assertTrue((self.target / deployer.APP_MARKER).is_file())
        # compose 已生成且端口正确
        compose = (self.target / deployer.COMPOSE_NAME).read_text(encoding="utf-8")
        self.assertIn('"0.0.0.0:3001:3001"', compose)
        self.assertIn('"0.0.0.0:6185:6185"', compose)
        self.assertIn("./deploy/robot.env", compose)
        # 配置模板被复制成真实配置，且权限收紧
        env_file = self.target / "deploy" / "robot.env"
        self.assertTrue(env_file.is_file())
        self.assertIn("DSH_DECIDE=1", env_file.read_text(encoding="utf-8"))
        self.assertEqual(oct(env_file.stat().st_mode & 0o777), "0o600")
        # docker 真的被按顺序调用过
        calls = self.docker_log.read_text(encoding="utf-8")
        self.assertIn("compose -f docker-compose.generated.yml pull", calls)
        self.assertIn("up -d --remove-orphans", calls)
        self.assertIn(" ps", calls)

    def test_second_run_updates_instead_of_recloning(self):
        self.assertEqual(self._run(self._config()).returncode, 0)
        result = self._run(self._config())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DSH_PROGRESS:更新仓库源码", result.stdout)
        self.assertNotIn("从仓库下载源码", result.stdout)

    def test_existing_foreign_directory_is_refused(self):
        self.target.mkdir(parents=True)
        (self.target / "important.txt").write_text("mine", encoding="utf-8")
        result = self._run(self._config())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("已拒绝覆盖", result.stderr)
        # 用户的东西没被删
        self.assertTrue((self.target / "important.txt").is_file())

    def test_script_contains_no_plaintext_password(self):
        script = build_remote_script(self._config())
        self.assertNotIn("super-secret-password", script)
        # 日志里也不该出现
        self._run(self._config())
        for log in (self.docker_log.read_text(encoding="utf-8"),):
            self.assertNotIn("super-secret-password", log)

    def test_tilde_deploy_dir_expands_to_home(self):
        """默认部署目录是 ~/dafeiyu-bot，脚本必须真的展开到 $HOME 下。"""
        result = self._run(self._config(deploy_dir="~/dafeiyu-bot"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / "dafeiyu-bot" / ".git").is_dir())
        self.assertTrue((self.home / "dafeiyu-bot" / deployer.APP_MARKER).is_file())
        self.assertFalse((self.home / "~").exists(), "不应产生字面量 ~ 目录")

    def test_preflight_script_runs_and_parses(self):
        env = dict(os.environ)
        env.update({
            "HOME": str(self.home),
            "PATH": "%s:%s" % (self.bin, env.get("PATH", "/usr/bin:/bin")),
            "FAKE_DOCKER_LOG": str(self.docker_log),
        })
        result = subprocess.run(["sh", "-s"], input=deployer.PREFLIGHT_SCRIPT,
                                text=True, env=env, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = deployer.parse_key_values(result.stdout)
        self.assertEqual(info["OS"], "Linux")
        self.assertEqual(info["GIT"], "yes")
        self.assertEqual(info["DOCKER"], "yes")
        self.assertEqual(info["COMPOSE"], "yes")
        self.assertIn(info["PRIVILEGE"], ("root", "passwordless-sudo", "limited"))


if __name__ == "__main__":
    unittest.main()
