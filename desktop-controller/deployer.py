# -*- coding: utf-8 -*-
"""安全的 SSH 预检、仓库拉取与自动部署。

设计要点：
- 传输层抽象成 Transport 协议，测试用假传输即可完整验证流程，不需要真服务器。
- 远程脚本由本地生成，用户输入一律 shell quote；敏感值不进命令行、不进日志。
- 部署目录必须带专用标记文件，避免误删或覆盖用户自己的目录。
- 只改目标服务器上由本控制台创建的目录，不触碰其他部署。
"""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import socket
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

Progress = Callable[[str], None]
APP_MARKER = ".dafeiyu-managed"
ENV_REL = "deploy/robot.env"
ENV_EXAMPLE_REL = "deploy/robot.env.example"
SCHEMA_REL = "deploy/console-config.json"
COMPOSE_NAME = "docker-compose.generated.yml"

_SECRET_PATTERNS = (
    (re.compile(r"(https?://)[^/@\s]+@", re.I), r"\1<已隐藏>@"),
    (re.compile(
        r"(?i)\b(token|password|passphrase|secret|authorization|api[_-]?key)\b\s*[=:]\s*\S+"
    ), r"\1=<已隐藏>"),
)


class DeployError(RuntimeError):
    """经过脱敏、可直接展示给用户的部署错误。"""


def redact(text: str, secrets: Tuple[str, ...] = ()) -> str:
    """日志兜底脱敏。只替换足够长的敏感值，避免把普通数字打成乱码。"""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "<已隐藏>")
    return text


@dataclass
class DeployConfig:
    host: str
    port: int
    username: str
    auth_type: str
    password: str
    key_file: str
    key_passphrase: str
    trust_new_host: bool
    repo_url: str
    repo_ref: str
    deploy_dir: str
    install_dependencies: bool
    napcat_port: int
    astrbot_port: int
    astrbot_api_port: int
    bind_address: str
    napcat_image: str
    astrbot_image: str

    @classmethod
    def from_values(cls, values: Dict[str, Any]) -> "DeployConfig":
        def integer(name: str, label: str, lo: int, hi: int) -> int:
            try:
                value = int(str(values.get(name, "")).strip())
            except (TypeError, ValueError):
                raise DeployError("%s必须是数字。" % label) from None
            if not lo <= value <= hi:
                raise DeployError("%s必须在 %d 到 %d 之间。" % (label, lo, hi))
            return value

        cfg = cls(
            host=str(values.get("host", "")).strip(),
            port=integer("port", "SSH 端口", 1, 65535),
            username=str(values.get("username", "")).strip(),
            auth_type=str(values.get("auth_type", "密码")),
            password=str(values.get("password", "")),
            key_file=str(values.get("key_file", "")).strip(),
            key_passphrase=str(values.get("key_passphrase", "")),
            trust_new_host=bool(values.get("trust_new_host", False)),
            repo_url=str(values.get("repo_url", "")).strip(),
            repo_ref=str(values.get("repo_ref", "main")).strip(),
            deploy_dir=str(values.get("deploy_dir", "~/dafeiyu-bot")).strip(),
            install_dependencies=bool(values.get("install_dependencies", True)),
            napcat_port=integer("napcat_port", "NapCat 端口", 1, 65535),
            astrbot_port=integer("astrbot_port", "AstrBot 端口", 1, 65535),
            astrbot_api_port=integer("astrbot_api_port", "AstrBot API 端口", 1, 65535),
            bind_address=str(values.get("bind_address", "0.0.0.0")).strip(),
            napcat_image=str(values.get("napcat_image", "")).strip(),
            astrbot_image=str(values.get("astrbot_image", "")).strip(),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.host or any(ch.isspace() for ch in self.host):
            raise DeployError("请填写正确的服务器地址。")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.username):
            raise DeployError("SSH 用户名格式不正确。")
        if self.auth_type == "密码":
            if not self.password:
                raise DeployError("请输入 SSH 密码。")
        elif self.auth_type == "SSH 私钥":
            if not self.key_file or not Path(self.key_file).is_file():
                raise DeployError("请选择存在的 SSH 私钥文件。")
        else:
            raise DeployError("未知的 SSH 登录方式。")
        validate_repo_url(self.repo_url)
        if not re.fullmatch(r"[A-Za-z0-9._/+-]+", self.repo_ref):
            raise DeployError("分支或标签格式不正确。")
        if not self.deploy_dir or any(ch in self.deploy_dir for ch in "\r\n\x00"):
            raise DeployError("部署目录格式不正确。")
        if self.deploy_dir.rstrip("/") in ("", "/", "~", "$HOME", ".", ".."):
            raise DeployError("部署目录不能是根目录或主目录本身。")
        if len({self.napcat_port, self.astrbot_port, self.astrbot_api_port}) != 3:
            raise DeployError("三个服务端口不能重复。")
        try:
            ipaddress.ip_address(self.bind_address)
        except ValueError:
            raise DeployError("监听地址必须是合法 IP。") from None
        for label, image in (("NapCat", self.napcat_image), ("AstrBot", self.astrbot_image)):
            if not re.fullmatch(r"[A-Za-z0-9._/:@-]+", image):
                raise DeployError("%s 镜像名称格式不正确。" % label)

    def safe_profile(self) -> Dict[str, Any]:
        """只返回可保存字段；密码、私钥路径和口令绝不持久化。"""
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "auth_type": self.auth_type,
            "trust_new_host": self.trust_new_host,
            "repo_url": self.repo_url,
            "repo_ref": self.repo_ref,
            "deploy_dir": self.deploy_dir,
            "install_dependencies": self.install_dependencies,
            "napcat_port": self.napcat_port,
            "astrbot_port": self.astrbot_port,
            "astrbot_api_port": self.astrbot_api_port,
            "bind_address": self.bind_address,
            "napcat_image": self.napcat_image,
            "astrbot_image": self.astrbot_image,
        }


def validate_repo_url(url: str) -> None:
    if not url:
        raise DeployError("请填写源码仓库地址。")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("https", "http"):
        if not parsed.netloc or parsed.username or parsed.password:
            raise DeployError("仓库地址不能内嵌账号、密码或 Token。")
        if parsed.query or parsed.fragment:
            raise DeployError("仓库地址不能带参数或片段。")
        if parsed.scheme != "https":
            raise DeployError("请使用 HTTPS 仓库地址；HTTP 会泄露源码。")
        return
    if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+(?:\.git)?", url):
        return
    raise DeployError("仓库地址只支持 HTTPS 或 git@主机:owner/repo.git。")


def _q(value: Any) -> str:
    return shlex.quote(str(value))


def build_compose_override(cfg: DeployConfig) -> str:
    """生成 compose 覆盖文件：不依赖仓库里是否已有 compose，端口与镜像可配。"""
    return """services:
  napcat:
    image: {napcat_image}
    container_name: dafeiyu-napcat
    restart: unless-stopped
    ports:
      - "{bind}:{napcat_port}:3001"
    volumes:
      - ./data/napcat:/app/config
    networks: [dafeiyu-net]

  astrbot:
    image: {astrbot_image}
    container_name: dafeiyu-astrbot
    restart: unless-stopped
    env_file:
      - ./deploy/robot.env
    ports:
      - "{bind}:{astrbot_port}:6185"
      - "{bind}:{api_port}:6186"
    volumes:
      - ./data/astrbot:/AstrBot/data
      - ./plugins:/AstrBot/data/plugins
    depends_on: [napcat]
    networks: [dafeiyu-net]

networks:
  dafeiyu-net:
    driver: bridge
""".format(
        napcat_image=cfg.napcat_image,
        astrbot_image=cfg.astrbot_image,
        bind=cfg.bind_address,
        napcat_port=cfg.napcat_port,
        astrbot_port=cfg.astrbot_port,
        api_port=cfg.astrbot_api_port,
    )


_SCRIPT_TEMPLATE = """#!/bin/sh
set -eu
REPO=__REPO__
REF=__REF__
TARGET=__TARGET__
INSTALL=__INSTALL__
MARKER=__MARKER__

log() { printf 'DSH_PROGRESS:%s\\n' "$1"; }
fail() { printf 'DSH_ERROR:%s\\n' "$1" >&2; exit 1; }

case "$TARGET" in
  '~/'*) TARGET="$HOME/${TARGET#'~/'}" ;;
  '~') TARGET="$HOME" ;;
esac

[ "$(uname -s)" = "Linux" ] || fail "只支持 Linux 服务器"
[ -n "$TARGET" ] || fail "部署目录为空"
case "$TARGET" in /|"$HOME"|.) fail "不安全的部署目录" ;; esac

if [ "$INSTALL" = "1" ]; then
  if ! command -v git >/dev/null 2>&1 || ! command -v docker >/dev/null 2>&1; then
    log "安装系统依赖"
    if command -v apt-get >/dev/null 2>&1; then
      SUDO=""; [ "$(id -u)" = "0" ] || SUDO="sudo"
      $SUDO apt-get update -y >/dev/null
      $SUDO apt-get install -y git docker.io docker-compose-plugin >/dev/null
      $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
    else
      fail "缺少 Git/Docker，且系统不是 Debian/Ubuntu"
    fi
  fi
fi

command -v git >/dev/null 2>&1 || fail "服务器未安装 Git"
command -v docker >/dev/null 2>&1 || fail "服务器未安装 Docker"
DOCKER=docker
docker info >/dev/null 2>&1 || DOCKER="sudo docker"
$DOCKER info >/dev/null 2>&1 || fail "当前账号无权访问 Docker，请用 root 或加入 docker 组"
$DOCKER compose version >/dev/null 2>&1 || fail "缺少 Docker Compose 插件"

if [ -e "$TARGET" ] && [ ! -f "$TARGET/$MARKER" ]; then
  fail "部署目录已存在且不是本控制台创建的，已拒绝覆盖"
fi
if [ -f "$TARGET/$MARKER" ] && [ ! -d "$TARGET/.git" ]; then
  log "重新下载源码"
  rm -rf "$TARGET"
fi

if [ ! -d "$TARGET/.git" ]; then
  log "从仓库下载源码"
  mkdir -p "$(dirname "$TARGET")"
  if ! git clone --branch "$REF" --single-branch "$REPO" "$TARGET"; then
    rm -rf "$TARGET"
    fail "源码下载失败，请检查仓库地址、分支和服务器网络"
  fi
else
  log "更新仓库源码"
  git -C "$TARGET" remote set-url origin "$REPO"
  git -C "$TARGET" fetch --depth=1 origin "$REF" || fail "获取指定分支或标签失败"
  git -C "$TARGET" checkout --detach FETCH_HEAD >/dev/null 2>&1 || fail "切换代码版本失败"
fi
: > "$TARGET/$MARKER"

log "准备目录与配置文件"
mkdir -p "$TARGET/data/napcat" "$TARGET/data/astrbot" "$TARGET/plugins" "$TARGET/deploy"
if [ ! -f "$TARGET/deploy/robot.env" ]; then
  if [ -f "$TARGET/deploy/robot.env.example" ]; then
    cp "$TARGET/deploy/robot.env.example" "$TARGET/deploy/robot.env"
  else
    : > "$TARGET/deploy/robot.env"
  fi
fi
chmod 600 "$TARGET/deploy/robot.env" 2>/dev/null || true

cat > "$TARGET/__COMPOSE_NAME__" <<'DSH_COMPOSE_EOF'
__COMPOSE__
DSH_COMPOSE_EOF

cd "$TARGET"
log "拉取容器镜像"
$DOCKER compose -f __COMPOSE_NAME__ pull || fail "镜像拉取失败，请检查服务器网络"
log "启动机器人服务"
$DOCKER compose -f __COMPOSE_NAME__ up -d --remove-orphans || fail "服务启动失败"
log "读取运行状态"
$DOCKER compose -f __COMPOSE_NAME__ ps || true
log "部署完成"
"""


def build_remote_script(cfg: DeployConfig, compose_text: Optional[str] = None) -> str:
    """生成幂等远程脚本。用户输入全部 shell quote，compose 走 heredoc。"""
    compose = (compose_text if compose_text is not None else build_compose_override(cfg)).rstrip()
    if "DSH_COMPOSE_EOF" in compose:
        raise DeployError("生成的 compose 内容包含保留标记，已中止。")
    script = _SCRIPT_TEMPLATE
    for token, value in (
        ("__REPO__", _q(cfg.repo_url)),
        ("__REF__", _q(cfg.repo_ref)),
        ("__TARGET__", _q(cfg.deploy_dir)),
        ("__INSTALL__", "1" if cfg.install_dependencies else "0"),
        ("__MARKER__", _q(APP_MARKER)),
        ("__COMPOSE_NAME__", _q(COMPOSE_NAME)),
        ("__COMPOSE__", compose),
    ):
        script = script.replace(token, value)
    return script


def _target_prefix(cfg: DeployConfig) -> str:
    """在远程 shell 里解析部署目录（含 ~ 展开）。"""
    return (
        "TARGET=%s; case \"$TARGET\" in '~/'*) TARGET=\"$HOME/${TARGET#'~/'}\" ;; "
        "'~') TARGET=\"$HOME\" ;; esac; " % _q(cfg.deploy_dir)
    )


def compose_shell(cfg: DeployConfig, args: str) -> str:
    """构造一条在部署目录里执行的 docker compose 命令（自动处理 sudo）。"""
    return (
        _target_prefix(cfg)
        + "cd \"$TARGET\" && { DOCKER=docker; docker info >/dev/null 2>&1 || DOCKER=\"sudo docker\"; "
        "$DOCKER compose -f %s %s; }" % (_q(COMPOSE_NAME), args)
    )


class Transport(Protocol):
    """远程执行与文件读写的最小接口，便于测试替身。"""

    def run(self, command: str, timeout: int = 900,
            on_line: Optional[Callable[[str], None]] = None) -> Tuple[int, str, str]:
        ...

    def put_text(self, path: str, text: str) -> None:
        ...

    def get_text(self, path: str, limit: int = 262144) -> str:
        ...

    def close(self) -> None:
        ...


class ParamikoTransport:
    """paramiko 实现。paramiko 只在真正连接时导入，便于离线测试与打包裁剪。"""

    def __init__(self, cfg: DeployConfig, connect_timeout: int = 15):
        self.cfg = cfg
        self.connect_timeout = connect_timeout
        self.client: Any = None

    def open(self) -> "ParamikoTransport":
        try:
            import paramiko  # 延迟导入：无 paramiko 时也能跑纯逻辑测试
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise DeployError("缺少 paramiko 依赖，请重新安装或使用完整版 EXE。") from exc

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.cfg.trust_new_host:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            # 默认拒绝未知主机密钥，防止中间人。
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        kwargs: Dict[str, Any] = {
            "hostname": self.cfg.host,
            "port": self.cfg.port,
            "username": self.cfg.username,
            "timeout": self.connect_timeout,
            "banner_timeout": self.connect_timeout,
            "auth_timeout": self.connect_timeout,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if self.cfg.auth_type == "密码":
            kwargs["password"] = self.cfg.password
        else:
            kwargs["key_filename"] = self.cfg.key_file
            if self.cfg.key_passphrase:
                kwargs["passphrase"] = self.cfg.key_passphrase
        try:
            client.connect(**kwargs)
        except paramiko.BadHostKeyException as exc:
            raise DeployError("服务器指纹与已记录的不一致，已拒绝连接。") from exc
        except paramiko.AuthenticationException as exc:
            raise DeployError("SSH 登录失败，请检查用户名和密码或私钥。") from exc
        except paramiko.SSHException as exc:
            if "not found in known_hosts" in str(exc).lower():
                raise DeployError(
                    "这是首次连接该服务器：请勾选“首次连接接受服务器指纹”，"
                    "或先用系统 ssh 确认指纹。"
                ) from exc
            raise DeployError("SSH 连接失败：%s" % redact(str(exc))) from exc
        except (OSError, socket.error) as exc:
            raise DeployError("无法连接服务器，请检查地址、端口与安全组。") from exc
        self.client = client
        return self

    def run(self, command: str, timeout: int = 900,
            on_line: Optional[Callable[[str], None]] = None) -> Tuple[int, str, str]:
        if self.client is None:
            raise DeployError("尚未连接服务器。")
        try:
            _stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
            out_lines: List[str] = []
            for raw in iter(stdout.readline, ""):
                line = raw.rstrip("\r\n")
                out_lines.append(line)
                if on_line is not None:
                    on_line(line)
            err = stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
        except (OSError, socket.error) as exc:
            raise DeployError("SSH 连接中断。") from exc
        return rc, "\n".join(out_lines), err

    def put_text(self, path: str, text: str) -> None:
        if self.client is None:
            raise DeployError("尚未连接服务器。")
        parent = path.rsplit("/", 1)[0] if "/" in path else "."
        rc, _out, err = self.run("mkdir -p %s" % _q(parent), timeout=60)
        if rc != 0:
            raise DeployError("无法创建远程目录：%s" % redact(err))
        try:
            with self.client.open_sftp() as sftp:
                with sftp.open(path, "w") as handle:
                    handle.write(text)
        except (OSError, socket.error) as exc:
            raise DeployError("写入远程文件失败。") from exc

    def get_text(self, path: str, limit: int = 262144) -> str:
        if self.client is None:
            raise DeployError("尚未连接服务器。")
        try:
            with self.client.open_sftp() as sftp:
                with sftp.open(path, "r") as handle:
                    return handle.read(limit).decode("utf-8", "replace")
        except FileNotFoundError:
            raise DeployError("服务器上找不到文件：%s" % path) from None
        except (OSError, socket.error) as exc:
            raise DeployError("读取远程文件失败。") from exc

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


class SSHDeployer:
    """把配置变成一次完整部署，并读回状态与配置项。"""

    def __init__(self, cfg: DeployConfig,
                 transport_factory: Optional[Callable[[DeployConfig], Transport]] = None,
                 progress: Optional[Progress] = None):
        self.cfg = cfg
        self.progress = progress or (lambda _text: None)
        self.transport_factory = transport_factory or (lambda c: ParamikoTransport(c).open())
        self.transport: Optional[Transport] = None
        self._remote_dir: Optional[str] = None

    # ------------------------------------------------------------ 连接

    def open(self) -> None:
        if self.transport is None:
            self.progress("正在连接服务器…")
            self.transport = self.transport_factory(self.cfg)
            self.progress("SSH 连接成功")

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    def _require(self) -> Transport:
        if self.transport is None:
            raise DeployError("尚未连接服务器。")
        return self.transport

    def _secrets(self) -> Tuple[str, ...]:
        return (self.cfg.password, self.cfg.key_passphrase)

    def remote_dir(self) -> str:
        """部署目录的**绝对路径**。

        SFTP 不展开 `~`（OpenSSH 的 sftp-server 会把 `~` 当普通目录名），
        所以凡是要用 SFTP 写文件的场景都必须先在这里解析成绝对路径，
        否则默认目录 `~/dafeiyu-bot` 会被写成 `/home/用户/~/dafeiyu-bot`。
        """
        if self._remote_dir is None:
            transport = self._require()
            rc, out, _err = transport.run(
                _target_prefix(self.cfg) + 'printf %s "$TARGET"', timeout=60)
            value = (out or "").strip()
            if rc != 0 or not value.startswith("/"):
                raise DeployError("无法确定服务器上的部署目录。")
            self._remote_dir = value
        return self._remote_dir

    # ------------------------------------------------------------ 部署

    def deploy(self) -> None:
        """执行部署。**不关闭连接** —— 调用方通常在部署后还要读配置与状态。"""
        self.open()
        transport = self._require()
        script = build_remote_script(self.cfg)
        rc, out, err = transport.run(script, timeout=1800,
                                     on_line=self._on_remote_line)
        if rc != 0:
            raise DeployError(self._extract_error(out, err))

    def _on_remote_line(self, line: str) -> None:
        if line.startswith("DSH_PROGRESS:"):
            self.progress(line.split(":", 1)[1])

    def _extract_error(self, out: str, err: str) -> str:
        combined = "%s\n%s" % (out or "", err or "")
        for line in combined.splitlines():
            if line.startswith("DSH_ERROR:"):
                return redact(line.split(":", 1)[1], self._secrets())[:300]
        tail = [ln for ln in combined.splitlines()
                if ln.strip() and not ln.startswith("DSH_PROGRESS:")]
        if tail:
            return redact(tail[-1], self._secrets())[:300]
        return "自动部署失败，请检查服务器网络与权限。"

    # ------------------------------------------------------------ 状态

    def check_environment(self) -> Dict[str, str]:
        """只读预检：确认能登录、系统可用、Git/Docker 是否就绪。不改服务器任何东西。"""
        transport = self._require()
        rc, out, err = transport.run(PREFLIGHT_SCRIPT, timeout=120)
        if rc != 0:
            raise DeployError("环境检查失败：%s" % redact(err, self._secrets()))
        return parse_key_values(out)

    def status(self) -> List[Dict[str, Any]]:
        transport = self._require()
        command = compose_shell(self.cfg, "ps --format json")
        rc, out, err = transport.run(command, timeout=120)
        if rc != 0:
            raise DeployError("读取服务状态失败：%s" % redact(err, self._secrets()))
        return parse_compose_ps(out)

    # ------------------------------------------------------------ 配置

    def read_knob_schema(self) -> Any:
        transport = self._require()
        path = "%s/%s" % (self.cfg.deploy_dir.rstrip("/"), SCHEMA_REL)
        rc, out, _err = transport.run(
            _target_prefix(self.cfg) + "cat \"$TARGET/%s\" 2>/dev/null || true" % SCHEMA_REL,
            timeout=60,
        )
        text = out.strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DeployError("仓库里的配置清单不是合法 JSON：%s" % path) from exc

    def read_env(self) -> str:
        transport = self._require()
        rc, out, _err = transport.run(
            _target_prefix(self.cfg)
            + "cat \"$TARGET/%s\" 2>/dev/null || true" % ENV_REL,
            timeout=60,
        )
        return out

    def write_env(self, text: str) -> None:
        transport = self._require()
        # 先写临时文件再 mv，避免写一半把配置写坏。
        # 路径必须是解析过的绝对路径：SFTP 不认 ~。
        tmp = "%s.tmp" % ENV_REL
        transport.put_text("%s/%s" % (self.remote_dir(), tmp), text)
        rc, _out, err = transport.run(
            _target_prefix(self.cfg)
            + "chmod 600 \"$TARGET/%s\" && mv \"$TARGET/%s\" \"$TARGET/%s\""
            % (tmp, tmp, ENV_REL),
            timeout=60,
        )
        if rc != 0:
            raise DeployError("保存远程配置失败：%s" % redact(err, self._secrets()))

    def recreate(self, services: Tuple[str, ...] = ("astrbot",)) -> None:
        if not services:
            return
        names = " ".join(services)
        command = compose_shell(
            self.cfg, "up -d --force-recreate --no-deps %s" % names
        )
        rc, _out, err = self._require().run(command, timeout=600)
        if rc != 0:
            raise DeployError("重启服务失败：%s" % redact(err, self._secrets()))


PREFLIGHT_SCRIPT = r"""
echo "OS=$(uname -s 2>/dev/null || echo unknown)"
echo "ARCH=$(uname -m 2>/dev/null || echo unknown)"
echo "GIT=$(command -v git >/dev/null 2>&1 && echo yes || echo no)"
echo "DOCKER=$(command -v docker >/dev/null 2>&1 && echo yes || echo no)"
echo "COMPOSE=$(docker compose version >/dev/null 2>&1 && echo yes || echo no)"
if [ "$(id -u)" = "0" ]; then
  echo "PRIVILEGE=root"
elif sudo -n true 2>/dev/null; then
  echo "PRIVILEGE=passwordless-sudo"
else
  echo "PRIVILEGE=limited"
fi
echo "HOME=$HOME"
echo "DISK_FREE_MB=$(df -Pm "$HOME" 2>/dev/null | awk 'NR==2{print $4}')"
"""


def parse_key_values(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                out[key] = value.strip()
    return out


def format_environment(info: Dict[str, str]) -> str:
    """把预检结果整理成新手能看懂的一段话。"""
    if not info:
        return "没有读取到服务器信息。"
    lines = [
        "系统：%s（%s）" % (info.get("OS", "未知"), info.get("ARCH", "未知")),
        "Git：%s" % ("已安装" if info.get("GIT") == "yes" else "未安装"),
        "Docker：%s" % ("已安装" if info.get("DOCKER") == "yes" else "未安装"),
        "Compose：%s" % ("已安装" if info.get("COMPOSE") == "yes" else "未安装"),
    ]
    privilege = info.get("PRIVILEGE", "")
    lines.append({
        "root": "权限：root（可自动安装依赖）",
        "passwordless-sudo": "权限：普通用户 + 免密 sudo（可自动安装依赖）",
    }.get(privilege, "权限：普通用户（无法自动安装依赖，请先装好 Git/Docker）"))
    if info.get("DISK_FREE_MB"):
        lines.append("可用磁盘：约 %s MB" % info["DISK_FREE_MB"])
    return "\n".join(lines)


def parse_compose_ps(text: str) -> List[Dict[str, Any]]:
    """解析 docker compose ps --format json，兼容逐行 JSON 与 JSON 数组两种输出。"""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        items: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                items.append(parsed)
        return items
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def format_containers(containers: List[Dict[str, Any]]) -> str:
    """把容器状态整理成给人看的中文行；只保留必要字段。"""
    if not containers:
        return "还没有读取到运行状态，请点“刷新状态”。"
    lines = []
    for item in containers:
        name = item.get("Service") or item.get("Name") or "服务"
        state = item.get("State") or item.get("Status") or "未知"
        health = item.get("Health") or ""
        ports = item.get("Publishers") or []
        port_text = ""
        if isinstance(ports, list):
            pairs = [
                "%s→%s" % (p.get("PublishedPort"), p.get("TargetPort"))
                for p in ports
                if isinstance(p, dict) and p.get("PublishedPort")
            ]
            port_text = ("  端口：" + "，".join(pairs)) if pairs else ""
        suffix = ("（%s）" % health) if health else ""
        lines.append("%s：%s%s%s" % (name, state, suffix, port_text))
    return "\n".join(lines)
