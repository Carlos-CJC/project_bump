param(
    [Parameter(Mandatory=$true)][ValidateSet('Pause','Resume','Inspect')][string]$Action,
    [Parameter(Mandatory=$true)][string]$StateDirectory
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$state = (Resolve-Path -LiteralPath $StateDirectory).Path
$run = Split-Path -Parent $PSScriptRoot
$group = Split-Path -Parent (Split-Path -Parent $state)
$groupName = Split-Path -Leaf $group
if ($groupName -notmatch '^hotspot-[a-f0-9]{32}$' -or
    $state -ne (Join-Path "$run\private\$groupName" 'private\motion_hotspot')) {
    throw 'Expected the exact state directory of a project-owned temporary hotspot.'
}
$sessionPath = Join-Path $state 'session.json'
$marker = Join-Path $state 'manual-outage.json'
$keeper = Join-Path $PSScriptRoot 'hotspot_keeper.py'
$resume = Join-Path $PSScriptRoot 'Resume-TestHotspot.ps1'
function Test-Session {
    if ((Test-Path -LiteralPath (Join-Path $state 'stop.request')) -or
        (Test-Path -LiteralPath (Join-Path $state 'result.json')) -or
        -not (Test-Path -LiteralPath $sessionPath)) { throw 'The owner is ending or has ended this test session.' }
    $s = Get-Content -Raw -LiteralPath $sessionPath | ConvertFrom-Json
    if (-not $s.ready -or $s.pid -ne $session.pid) { throw 'The recorded owner has changed.' }
    $current = $manager.GetCurrentAccessPointConfiguration()
    if ($current.Ssid -cne $s.ssid -or $current.Passphrase -cne $s.password -or $current.Band.ToString() -ne $s.band) {
        throw 'The active access point configuration differs from this session.'
    }
}
function Invoke-Keeper([string]$operation) {
    $info = & python -B $keeper $operation $state
    if ($LASTEXITCODE -ne 0) { throw 'Could not verify the original owner and exact keeper process.' }
    $info | ConvertFrom-Json
}
function Stop-TemporaryRadio {
    $type = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and
        $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
        Select-Object -First 1
    $task = $method.MakeGenericMethod($type).Invoke($null, @($manager.StopTetheringAsync()))
    if (-not $task.Wait(30000) -or $task.Result.Status.ToString() -ne 'Success') { throw 'Could not pause test hotspot.' }
}
$mutex = New-Object Threading.Mutex($false, "Local\BumpOutage-$groupName")
$locked = $false
try {
    try { $locked = $mutex.WaitOne(5000) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Another outage operation is in progress for this session.' }
    $session = Get-Content -Raw -LiteralPath $sessionPath | ConvertFrom-Json
    $owned = Invoke-Keeper 'inspect'
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $profile = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]::GetConnectionProfiles() |
        Where-Object { $_.IsWlanConnectionProfile } | Select-Object -First 1
    $manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]::CreateFromConnectionProfile($profile)
    Test-Session
    if ($Action -eq 'Pause') {
        if (-not (Test-Path -LiteralPath $marker)) {
            @{owner=$owned.owner; restore_keeper=(@($owned.keepers).Count -gt 0); utc=[DateTime]::UtcNow.ToString('o')} |
                ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath $marker
        }
        $pause = Get-Content -Raw -LiteralPath $marker | ConvertFrom-Json
        if ($pause.owner -ne $owned.owner) { throw 'Pause marker belongs to a different owner.' }
        $null = Invoke-Keeper 'stop'
        Test-Session
        if ($manager.TetheringOperationalState.ToString() -ne 'Off') { Stop-TemporaryRadio }
        if ($manager.TetheringOperationalState.ToString() -ne 'Off') { throw 'Hotspot is still transmitting.' }
    } elseif ($Action -eq 'Resume') {
        if (-not (Test-Path -LiteralPath $marker)) { throw 'No pause marker exists for this session.' }
        $pause = Get-Content -Raw -LiteralPath $marker | ConvertFrom-Json
        if ($pause.owner -ne $owned.owner) { throw 'Pause marker belongs to a different owner.' }
        & $resume -StateDirectory $state | Out-Null
        Test-Session
        if ($manager.TetheringOperationalState.ToString() -ne 'On') { throw 'Owned hotspot did not return.' }
        if ($pause.restore_keeper -and @($owned.keepers).Count -eq 0) {
            $argv = '-NoProfile -ExecutionPolicy Bypass -File "' + $resume + '" -StateDirectory "' + $state + '" -KeepOn'
            $guard = Start-Process powershell.exe -WindowStyle Hidden -WorkingDirectory $run -ArgumentList $argv -PassThru
            $end = [DateTime]::UtcNow.AddSeconds(8)
            do {
                $owned = Invoke-Keeper 'inspect'
                if (@($owned.keepers) -contains $guard.Id) { break }
                Start-Sleep -Milliseconds 250
            } while ([DateTime]::UtcNow -lt $end -and -not $guard.HasExited)
            if (@($owned.keepers) -notcontains $guard.Id) { throw 'Could not confirm the restarted keeper.' }
        }
        Remove-Item -LiteralPath $marker
    }
    Test-Session
    $owned = Invoke-Keeper 'inspect'
    [pscustomobject]@{action=$Action;state=$manager.TetheringOperationalState.ToString();owner=$owned.owner;
        keepers=@($owned.keepers);paused=(Test-Path -LiteralPath $marker);utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
