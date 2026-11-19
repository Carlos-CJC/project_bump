param([ValidateSet("normal", "offline", "retry")][string]$Scenario = "normal")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Run Setup.ps1 first." }
& $python -m bump simulate --scenario $Scenario
exit $LASTEXITCODE
