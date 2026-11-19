param(
    [switch]$Independent,
    [switch]$CheckOnly,
    [string]$MqttHost = "127.0.0.1"
)
$ErrorActionPreference = 'Stop'
$runner = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\run.ps1'))
$port = if ($Independent) { 8876 } else { 8875 }
$url = "http://127.0.0.1:$port"

function Test-HardwareReceiver {
    try {
        $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 3 -MaximumRedirection 0
        return ($health.source -eq 'hardware' -and $health.ready -eq $true -and $health.mqtt.listening -eq $true)
    } catch {
        return $false
    }
}

if (Test-HardwareReceiver) {
    Write-Host "Hardware receiver: $url"
    if (-not $CheckOnly) { Start-Process $url }
    exit 0
}
if ($CheckOnly -or -not $Independent) {
    Write-Host "No ready Bump hardware receiver at $url. See README.md beside this launcher."
    exit 1
}

$runtime = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\.run\state\hardware'))
if (-not (Test-Path -LiteralPath (Join-Path $runtime 'live-config.json'))) {
    & $runner server init --runtime $runtime --device-a bump-349454276354 --device-b bump-c8c9a3e65ba4 --web-port 8876 --mqtt-port 18884
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Write-Host "Independent receiver: $url"
Write-Host "Web credentials: $(Join-Path $runtime 'credentials.txt')"
Write-Host "Keep this window open. Ctrl+C stops the receiver."
& $runner server serve --runtime $runtime --mqtt-host $MqttHost
exit $LASTEXITCODE
