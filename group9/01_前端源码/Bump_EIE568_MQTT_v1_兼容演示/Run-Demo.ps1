param([ValidateSet("normal", "offline", "retry")][string]$Scenario = "normal")
$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot '..\..\run.ps1'
& $runner simulate --scenario $Scenario
exit $LASTEXITCODE
