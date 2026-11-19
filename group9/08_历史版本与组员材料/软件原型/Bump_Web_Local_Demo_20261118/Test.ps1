$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Run Setup.ps1 first." }
& $python -m pytest -q --junitxml=evidence/latest-tests.xml
exit $LASTEXITCODE
