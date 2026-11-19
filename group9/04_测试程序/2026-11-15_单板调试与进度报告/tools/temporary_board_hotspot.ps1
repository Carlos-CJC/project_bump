$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$taskRoot = Split-Path $PSScriptRoot -Parent
$privateDir = Join-Path $taskRoot 'private\motion_hotspot'
New-Item -ItemType Directory -Path $privateDir -Force | Out-Null
$stopPath = Join-Path $privateDir 'stop.request'
$readyPath = Join-Path $privateDir 'session.json'
if (Test-Path -LiteralPath $readyPath) { throw 'A hotspot session record already exists' }

function Wait-WinRt($operation, $resultType) {
    if ($resultType) {
        $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
            Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
            Select-Object -First 1
        $task = $method.MakeGenericMethod($resultType).Invoke($null, @($operation))
    } else {
        $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
            Where-Object { $_.Name -eq 'AsTask' -and -not $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' } |
            Select-Object -First 1
        $task = $method.Invoke($null, @($operation))
    }
    if (-not $task.Wait(30000)) { throw 'Hotspot operation timed out' }
    if ($resultType) { return $task.Result }
}

$profile = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]::GetConnectionProfiles() |
    Where-Object { $_.IsWlanConnectionProfile } | Select-Object -First 1
$managerType = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
$manager = $managerType::CreateFromConnectionProfile($profile)
if ($manager.TetheringOperationalState.ToString() -ne 'Off' -or $manager.ClientCount -ne 0) { throw 'Existing hotspot use prevents a temporary test' }
$original = $manager.GetCurrentAccessPointConfiguration()
$backupPath = Join-Path $privateDir 'original_configuration.dpapi.xml'
if (Test-Path -LiteralPath $backupPath) {
    $backup = Import-Clixml -LiteralPath $backupPath
    $original = New-Object Windows.Networking.NetworkOperators.NetworkOperatorTetheringAccessPointConfiguration
    $original.Ssid = $backup.ssid
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($backup.passphrase)
    try { $original.Passphrase = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
    $original.Band = [Enum]::Parse([Windows.Networking.NetworkOperators.TetheringWiFiBand], $backup.band)
    Wait-WinRt ($manager.ConfigureAccessPointAsync($original)) $null
} else {
    $originalSecret = New-Object System.Security.SecureString
    foreach ($character in $original.Passphrase.ToCharArray()) { $originalSecret.AppendChar($character) }
    $originalSecret.MakeReadOnly()
    [pscustomobject]@{
        ssid = $original.Ssid
        passphrase = $originalSecret
        band = $original.Band.ToString()
    } | Export-Clixml -LiteralPath $backupPath
}
$resultType = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
$configured = $false
$started = $false
$record = @{pid=$PID; started_utc=[DateTime]::UtcNow.ToString('o'); ready=$false; original_state='Off'}
try {
    $config = New-Object Windows.Networking.NetworkOperators.NetworkOperatorTetheringAccessPointConfiguration
    $config.Ssid = 'BumpBoardTest-' + [Guid]::NewGuid().ToString('N').Substring(0,8)
    $config.Passphrase = [Guid]::NewGuid().ToString('N')
    $config.Band = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::TwoPointFourGigahertz
    $configured = $true
    Wait-WinRt ($manager.ConfigureAccessPointAsync($config)) $null
    $result = Wait-WinRt ($manager.StartTetheringAsync()) $resultType
    $record.status = $result.Status.ToString()
    if ($record.status -ne 'Success') { throw ('Hotspot start status: ' + $record.status) }
    $started = $true
    $record.ready = $true
    $record.ssid = $config.Ssid
    $record.password = $config.Passphrase
    $record.band = 'TwoPointFourGigahertz'
    $record | ConvertTo-Json | Set-Content -LiteralPath $readyPath -Encoding UTF8
    Write-Output 'Temporary 2.4 GHz board hotspot ready; credentials kept private.'
    $deadline = [DateTime]::UtcNow.AddMinutes(30)
    while ([DateTime]::UtcNow -lt $deadline -and -not (Test-Path -LiteralPath $stopPath)) { Start-Sleep -Seconds 1 }
} catch {
    $record.error = $_.Exception.Message
    Write-Output ('Hotspot test error: ' + $_.Exception.Message)
} finally {
    if ($started) {
        $stopped = Wait-WinRt ($manager.StopTetheringAsync()) $resultType
        $record.stop_status = $stopped.Status.ToString()
    }
    if ($configured) { Wait-WinRt ($manager.ConfigureAccessPointAsync($original)) $null }
    $restored = $manager.GetCurrentAccessPointConfiguration()
    $record.original_configuration_restored = ($restored.Ssid -eq $original.Ssid -and $restored.Passphrase -eq $original.Passphrase -and $restored.Band -eq $original.Band)
    $record.final_state = $manager.TetheringOperationalState.ToString()
    $record.ready = $false
    $record.Remove('ssid')
    $record.Remove('password')
    $record.finished_utc = [DateTime]::UtcNow.ToString('o')
    $record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $privateDir 'result.json') -Encoding UTF8
    if (Test-Path -LiteralPath $readyPath) { Remove-Item -LiteralPath $readyPath }
    Write-Output ('Hotspot finished; original configuration restored: ' + $record.original_configuration_restored)
}
