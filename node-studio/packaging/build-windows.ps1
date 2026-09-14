param(
    [string]$PythonExecutable = "python",
    [string[]]$PythonPrefixArgs = @(),
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Invoke-CheckedPython([string[]]$CommandArgs) {
    & $PythonExecutable @PythonPrefixArgs @CommandArgs
    if ($LASTEXITCODE -ne 0) { throw "Python command failed: $($CommandArgs -join ' ')" }
}

$Venv = Join-Path $Root ".build-venv"
if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
Invoke-CheckedPython @("-m", "venv", $Venv)
$Py = Join-Path $Venv "Scripts\python.exe"
& $Py -m pip install --disable-pip-version-check --requirement (Join-Path $Root "packaging\requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Build dependencies failed" }
if (-not $SkipTests) {
    & $Py -m compileall -q dafeiyu_flow tests packaging scripts
    if ($LASTEXITCODE -ne 0) { throw "compileall failed" }
    & $Py -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "tests failed" }
}
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
& $Py -m PyInstaller --clean --noconfirm (Join-Path $Root "packaging\dafeiyu-node-studio.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$Exe = Join-Path $Root "dist\dafeiyu-node-studio.exe"
if (-not (Test-Path $Exe)) { throw "EXE not found: $Exe" }
$Hash = (Get-FileHash -Algorithm SHA256 $Exe).Hash.ToLowerInvariant()
"$Hash  dafeiyu-node-studio.exe" | Set-Content -Encoding ascii "$Exe.sha256"
Write-Host "Built: $Exe"
Write-Host "SHA256: $Hash"
