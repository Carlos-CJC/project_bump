param([Parameter(Mandatory=$true)][string]$StateDirectory)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$profile = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]::GetConnectionProfiles() |
    Where-Object { $_.IsWlanConnectionProfile } | Select-Object -First 1
$manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]::CreateFromConnectionProfile($profile)
$resultType = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
function Await-WinRt($Op, $Type) {
    $methods = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 }
    if ($Type) {
        $method = $methods | Where-Object { $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
        $task = $method.MakeGenericMethod($Type).Invoke($null, @($Op))
    } else {
        $method = $methods | Where-Object { -not $_.IsGenericMethod -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' } | Select-Object -First 1
        $task = $method.Invoke($null, @($Op))
    }
    if (-not $task.Wait(30000)) { throw 'Hotspot configuration timed out' }
    if ($Type) { return $task.Result }
}
$backup = Import-Clixml -LiteralPath (Join-Path $StateDirectory 'original_configuration.dpapi.xml')
$original = $manager.GetCurrentAccessPointConfiguration()
$savedCurrent = $original.Ssid
$original.Ssid = $backup.ssid
$handle = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($backup.passphrase)
try { $original.Passphrase = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($handle) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($handle) }
$original.Band = [Enum]::Parse([Windows.Networking.NetworkOperators.TetheringWiFiBand], $backup.band)
$readyFile = Join-Path $StateDirectory 'session.json'
if ($savedCurrent -ne $backup.ssid) {
    $active = Get-Content -LiteralPath $readyFile -Raw | ConvertFrom-Json
    if ($active.ssid -ne $savedCurrent) { throw 'Current hotspot differs from both the saved and temporary settings' }
}
if ($manager.TetheringOperationalState.ToString() -ne 'Off') {
    if ($savedCurrent -eq $backup.ssid) { throw 'Original hotspot is currently in use' }
    $status = Await-WinRt ($manager.StopTetheringAsync()) $resultType
    if ($manager.TetheringOperationalState.ToString() -ne 'Off') { throw ('Test hotspot still active: ' + $status.Status) }
}
Await-WinRt ($manager.ConfigureAccessPointAsync($original)) $null
$actual = $manager.GetCurrentAccessPointConfiguration()
$ok = $actual.Ssid -eq $backup.ssid -and $actual.Passphrase -eq $original.Passphrase -and $actual.Band -eq $original.Band
$record = @{ original_configuration_restored=$ok; final_state=$manager.TetheringOperationalState.ToString(); verified_utc=[DateTime]::UtcNow.ToString('o') }
$record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $StateDirectory 'recovery-result.json') -Encoding UTF8
if (-not $ok) { throw 'Could not verify restoration of saved Wi-Fi settings' }
if (Test-Path -LiteralPath $readyFile) { Remove-Item -LiteralPath $readyFile }
$record | ConvertTo-Json -Compress
