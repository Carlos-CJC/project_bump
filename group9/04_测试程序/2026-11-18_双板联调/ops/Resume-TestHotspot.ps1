param([Parameter(Mandatory=$true)][string]$StateDirectory, [switch]$KeepOn)
$ErrorActionPreference = 'Stop'
$session = Get-Content -LiteralPath (Join-Path $StateDirectory 'session.json') -Raw | ConvertFrom-Json
if (-not $session.ready -or (Test-Path -LiteralPath (Join-Path $StateDirectory 'stop.request'))) {
    throw 'No active test session owns this configuration.'
}
if (-not (Get-Process -Id $session.pid -ErrorAction SilentlyContinue)) { throw 'The hotspot owner has exited.' }
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$profile = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]::GetConnectionProfiles() |
    Where-Object { $_.IsWlanConnectionProfile } | Select-Object -First 1
$manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]::CreateFromConnectionProfile($profile)
do {
    if ($KeepOn -and ((Test-Path -LiteralPath (Join-Path $StateDirectory 'stop.request')) -or
                      (Test-Path -LiteralPath (Join-Path $StateDirectory 'result.json')) -or
                      -not (Get-Process -Id $session.pid -ErrorAction SilentlyContinue))) { break }
    $active = $manager.GetCurrentAccessPointConfiguration()
    if ($active.Ssid -ne $session.ssid -or $active.Passphrase -ne $session.password) {
        throw 'Current hotspot differs from the recorded local test network.'
    }
    if ($manager.TetheringOperationalState.ToString() -eq 'Off') {
        if (Test-Path -LiteralPath (Join-Path $StateDirectory 'stop.request')) { break }
        $type = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
        $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
            Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
            Select-Object -First 1
        $task = $method.MakeGenericMethod($type).Invoke($null, @($manager.StartTetheringAsync()))
        if (-not $task.Wait(30000) -or $task.Result.Status.ToString() -ne 'Success') { throw 'Could not resume temporary hotspot.' }
        'Resumed owned test hotspot at ' + [DateTime]::UtcNow.ToString('o')
    }
    if ($KeepOn) { Start-Sleep -Seconds 4 }
} while ($KeepOn)
if (-not $KeepOn) { 'Test hotspot state: ' + $manager.TetheringOperationalState.ToString() }
