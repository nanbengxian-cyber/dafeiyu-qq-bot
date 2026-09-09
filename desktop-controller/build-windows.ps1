# Windows-only build script for the desktop controller.
# Builds in an isolated venv and produces a single EXE.
# The EXE contains no server address, password, key, Token, or bot configuration.
#
# 用法：
#   .\build-windows.ps1                                    用 Windows 的 py -3
#   .\build-windows.ps1 -PythonExe C:\Python312\python.exe 指定解释器
# 不传 -PythonExe 时需要 py 启动器；CI 里由 setup-python 提供 python，直接传路径。

param(
    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw '未找到 Windows Python 启动器 py。请先安装 Python 3.10+ 并勾选 Add Python to PATH，或用 -PythonExe 指定 python.exe。'
    }
    $basePython = @('py', '-3')
} else {
    if (-not (Test-Path $PythonExe)) {
        throw ('指定的 Python 不存在：' + $PythonExe)
    }
    $basePython = @($PythonExe)
}

# 独立虚拟环境打包，避免污染系统 Python，也保证可重复构建。
if (-not (Test-Path '.\.venv-build')) {
    $venvArgs = @()
    if ($basePython.Count -gt 1) { $venvArgs = $basePython[1..($basePython.Count - 1)] }
    & $basePython[0] @venvArgs -m venv .venv-build
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

$exe = Get-ChildItem -Path '.\dist\*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $exe) {
    throw '未找到预期 EXE，打包未完成。'
}

Write-Host ('完成：' + $exe.FullName)
