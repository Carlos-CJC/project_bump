param()
$ErrorActionPreference = 'Stop'

# 旧版脚本会解压随包附带的 Windows Python。该离线目录未纳入仓库，
# 现在统一由 pyenv 选择的 Python 3.12 在项目内 .venv 中运行。
$runner = Join-Path $PSScriptRoot 'run.ps1'
& $runner prepare
exit $LASTEXITCODE
