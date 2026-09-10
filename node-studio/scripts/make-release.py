from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "release"
ARCHIVE_NAME = "dafeiyu-node-studio-source-v0.1.0.zip"
ALLOWLIST = [
    ".gitignore", "README.md", "CHANGELOG.md",
    "dafeiyu_flow/__init__.py", "dafeiyu_flow/cli.py",
    "dafeiyu_flow/desktop.py", "dafeiyu_flow/engine.py", "dafeiyu_flow/model.py",
    "dafeiyu_flow/registry.py", "dafeiyu_flow/server.py", "dafeiyu_flow/types.py",
    "dafeiyu_flow/static/index.html", "dafeiyu_flow/static/styles.css", "dafeiyu_flow/static/app.js",
    "fixtures/messages.json", "graphs/01-basic-chat.json", "graphs/02-proactive-chat.json",
    "graphs/03-image-context.json", "packaging/build-windows.ps1",
    "packaging/dafeiyu-node-studio.spec", "packaging/requirements-build.txt",
    "packaging/windows_entry.py", "scripts/make-release.py", "scripts/start-studio.sh",
    "docs/01-Windows新手安装与启动.md", "docs/02-五分钟节点编辑入门.md",
    "docs/03-源码构建与故障排查.md", "tests/test_engine.py", "tests/test_server.py",
]
SENSITIVE_PATTERNS = {
    "private_key": re.compile(br"BEGIN (?:RSA |OPENSSH )?PRIVATE KEY"),
    "github_token": re.compile(br"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{16,}"),
    "production_identifier": re.compile(br"(?:/opt/|/AstrBot/data|\b(?:QQ|group)[_-]?\d{6,}\b)", re.IGNORECASE),
    "private_ipv4": re.compile(br"(?<![0-9])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![0-9])"),
}


def selected_files() -> List[Path]:
    files = [ROOT / name for name in ALLOWLIST]
    missing = [path.relative_to(ROOT).as_posix() for path in files if not path.is_file()]
    if missing:
        raise RuntimeError("归档白名单文件缺失:\n" + "\n".join(missing))
    return files


def audit(files: Iterable[Path]) -> None:
    findings = []
    for path in files:
        data = path.read_bytes()
        if path == ROOT / "scripts" / "make-release.py":
            continue
        for name, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(data): findings.append("%s: %s" % (path.relative_to(ROOT), name))
    if findings: raise RuntimeError("源码归档敏感信息扫描失败:\n" + "\n".join(findings))


def main() -> int:
    files = selected_files(); audit(files); RELEASE.mkdir(exist_ok=True)
    archive = RELEASE / ARCHIVE_NAME
    epoch = (2026, 1, 1, 0, 0, 0)
    manifest = []  # type: List[Dict[str, object]]
    with zipfile.ZipFile(str(archive), "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for path in files:
            relative = path.relative_to(ROOT).as_posix(); data = path.read_bytes()
            info = zipfile.ZipInfo("dafeiyu-node-studio/" + relative, epoch); info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if relative.startswith("scripts/") else 0o644) << 16
            output.writestr(info, data)
            manifest.append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        info = zipfile.ZipInfo("dafeiyu-node-studio/SOURCE-MANIFEST.json", epoch); info.compress_type = zipfile.ZIP_DEFLATED; info.external_attr = 0o644 << 16
        output.writestr(info, (json.dumps({"version": "0.1.0", "files": manifest}, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    checksum.write_text("%s  %s\n" % (digest, archive.name), encoding="ascii")
    print(str(archive)); print(str(checksum)); print("files=%d sha256=%s" % (len(manifest), digest))
    return 0


if __name__ == "__main__": raise SystemExit(main())
