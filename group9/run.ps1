# Windows PowerShell 入口：优先项目内 .venv，其次 pyenv-win，再退回 py 启动器。
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$venv = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venv -PathType Leaf) {
    & $venv (Join-Path $PSScriptRoot 'run.py') @args
    exit $LASTEXITCODE
}

if (Get-Command pyenv -ErrorAction SilentlyContinue) {
    & pyenv exec python (Join-Path $PSScriptRoot 'run.py') @args
    exit $LASTEXITCODE
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.12 (Join-Path $PSScriptRoot 'run.py') @args
    exit $LASTEXITCODE
}

throw '未找到 Python 3.12。请先安装 pyenv-win 并执行：pyenv install 3.12.14'
