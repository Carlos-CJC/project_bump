param([int]$WaitSeconds = 50, [ValidateRange(5,120)][int]$MaxMinutes = 75)
$ErrorActionPreference = 'Stop'
$project = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$existing = Join-Path $project '2026-11-15_单板调试与进度报告\tools\temporary_board_hotspot.ps1'
$private = Join-Path (Split-Path $PSScriptRoot -Parent) 'private'
$session = Join-Path $private ('hotspot-' + [Guid]::NewGuid().ToString('N'))
$toolsDir = New-Item -ItemType Directory -Path (Join-Path $session 'tools') -Force
$helper = Get-Content -LiteralPath $existing -Raw -Encoding UTF8
# Twenty physical discovery windows plus firmware changes can exceed the single-board test's 30 minutes.
$deadlineExpression = '[DateTime]::UtcNow.AddMinutes(30)'
if (-not $helper.Contains($deadlineExpression)) { throw 'The baseline hotspot duration has changed; review it before use.' }
$helper.Replace($deadlineExpression, "[DateTime]::UtcNow.AddMinutes($MaxMinutes)") |
    Set-Content -LiteralPath (Join-Path $toolsDir.FullName 'temporary_board_hotspot.ps1') -Encoding UTF8
$state = Join-Path $session 'private\motion_hotspot'
$process = Start-Process powershell.exe -WindowStyle Hidden -PassThru -WorkingDirectory $toolsDir.FullName -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','.\temporary_board_hotspot.ps1' -RedirectStandardOutput (Join-Path $session 'stdout.txt') -RedirectStandardError (Join-Path $session 'stderr.txt')
$deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
while (-not $process.HasExited -and [DateTime]::UtcNow -lt $deadline) {
    if (Test-Path -LiteralPath (Join-Path $state 'session.json')) {
        [pscustomobject]@{StateDirectory=$state; ProcessId=$process.Id; Ready=$true} | ConvertTo-Json -Compress
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
if (Test-Path -LiteralPath $state) { New-Item -ItemType File -Path (Join-Path $state 'stop.request') -Force | Out-Null }
throw ('Hotspot did not start. See the private run logs at ' + $session)
