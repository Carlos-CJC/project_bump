$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot '..\..\run.ps1'
& $runner demo
exit $LASTEXITCODE
