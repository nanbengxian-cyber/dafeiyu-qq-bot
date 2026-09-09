# -*- coding: utf-8 -*-
"""桌面控制台的动态配置字段。

界面按这份 schema 生成，不把控件散落写死在 UI 代码中。以后增减部署参数只需改表，
EXE 会自动显示；visible_if 用来实现认证方式切换时的配置浮动。
"""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    kind: str = "text"  # text/password/int/bool/choice/file
    default: object = ""
    hint: str = ""
    required: bool = True
    choices: Tuple[str, ...] = ()
    visible_if: Optional[Tuple[str, object]] = None
    secret: bool = False
    advanced: bool = False


DEPLOY_FIELDS = (
    Field("host", "服务器地址", "服务器连接", hint="公网 IP 或域名，不要带 http://"),
    Field("port", "SSH 端口", "服务器连接", "int", 22, hint="通常是 22"),
    Field("username", "SSH 用户名", "服务器连接", default="root"),
    Field("auth_type", "登录方式", "服务器连接", "choice", "密码",
          choices=("密码", "SSH 私钥")),
    Field("password", "SSH 密码", "服务器连接", "password", "",
          hint="仅保留在内存，不写入配置或日志", visible_if=("auth_type", "密码"),
          secret=True),
    Field("key_file", "私钥文件", "服务器连接", "file", "",
          hint="私钥只在本机使用，不上传服务器", visible_if=("auth_type", "SSH 私钥"),
          secret=True),
    Field("key_passphrase", "私钥口令", "服务器连接", "password", "",
          hint="没有口令可留空", required=False, visible_if=("auth_type", "SSH 私钥"),
          secret=True),
    Field("trust_new_host", "首次连接接受服务器指纹", "服务器连接", "bool", False,
          hint="仅在确认服务器地址可信时勾选；不勾选则要求本机已记录该指纹",
          required=False),

    Field("repo_url", "源码仓库地址", "源码与部署",
          hint="只支持不含账号/Token 的 HTTPS 或 SSH Git 地址"),
    Field("repo_ref", "分支或标签", "源码与部署", default="main",
          hint="例如 main 或 v1.0.0"),
    Field("deploy_dir", "服务器部署目录", "源码与部署", default="~/dafeiyu-bot",
          hint="控制台只管理带专用标记的目录"),
    Field("install_dependencies", "自动安装 Git/Docker", "源码与部署", "bool", True,
          hint="需要 root 或免密 sudo；只支持 Debian/Ubuntu"),

    Field("napcat_port", "NapCat 管理端口", "服务端口", "int", 3001),
    Field("astrbot_port", "AstrBot 管理端口", "服务端口", "int", 6185),
    Field("astrbot_api_port", "AstrBot API 端口", "服务端口", "int", 6186),
    Field("bind_address", "监听地址", "服务端口", default="0.0.0.0",
          hint="公网使用时请在云防火墙仅放行自己的 IP", advanced=True),

    Field("napcat_image", "NapCat 镜像", "高级选项",
          default="mlikiowa/napcat-docker:latest", advanced=True),
    Field("astrbot_image", "AstrBot 镜像", "高级选项",
          default="soulter/astrbot:latest", advanced=True),
)

GROUP_ORDER = ("服务器连接", "源码与部署", "服务端口", "高级选项")


def public_defaults():
    """返回可持久化的非敏感默认值；密码、私钥路径和口令绝不进入文件。"""
    return {field.key: field.default for field in DEPLOY_FIELDS if not field.secret}
