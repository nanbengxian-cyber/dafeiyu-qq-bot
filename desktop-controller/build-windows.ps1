# Windows-only build script for the desktop controller.
# Builds in an isolated venv and produces a single EXE.
# The EXE contains no server address, password, key, Token, or bot configuration.

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw '未找到 Windows Python 启动器 py。请先安装 Python 3.10+ 并勾选 Add Python to PATH。'
}

# 独立虚拟环境打包，避免污染系统 Python，也保证可重复构建。
if (-not (Test-Path '.\.venv-build')) {
    py -3 -m venv .venv-build
}
$py = '.\.venv-build\Scripts\python.exe'

# 先装运行时依赖（paramiko），再装打包工具。
& $py -m pip install --disable-pip-version-check --requirement requirements.txt
& $py -m pip install --disable-pip-version-check --requirement requirements-build.txt

& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --hidden-import paramiko `
    --collect-submodules paramiko `
    --name '大肥鱼机器人控制台' `
    app.py

$exe = '.\dist\大肥鱼机器人控制台.exe'
if (-not (Test-Path $exe)) {
    throw '未找到预期 EXE，打包未完成。'
}

Write-Host ('完成：' + $exe)
