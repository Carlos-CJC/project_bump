$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot '..\..\run.ps1'
& $runner test --suite compat
exit $LASTEXITCODE
