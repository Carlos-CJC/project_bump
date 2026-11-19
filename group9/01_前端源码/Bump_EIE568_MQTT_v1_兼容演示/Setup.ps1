param([string]$PythonPath = "")
$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot '..\..\run.ps1'
$forward = @('setup')
if ($PythonPath) { $forward += @('--python', $PythonPath) }
& $runner @forward
exit $LASTEXITCODE
