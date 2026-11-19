param(
    [switch]$SelfCheck,
    [switch]$NoBrowser,
    [string]$RuntimeDir = "",
    [int]$WebPort = 8765,
    [int]$MqttPort = 18883
)
$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot '..\..\run.ps1'
$forward = @('demo', '--web-port', "$WebPort", '--mqtt-port', "$MqttPort")
if ($RuntimeDir) { $forward += @('--runtime', $RuntimeDir) }
if ($SelfCheck) { $forward += '--self-check' }
if ($NoBrowser) { $forward += '--no-browser' }
& $runner @forward
exit $LASTEXITCODE
