param(
    [string]$ArduinoCli = "arduino-cli",
    [string]$ArduinoConfig = "",
    [switch]$RealImu
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$modeName = if ($RealImu) { "real-imu-api" } else { "mock" }
$buildPath = Join-Path $projectRoot "evidence\firmware\build\$modeName"
$outputPath = Join-Path $projectRoot "evidence\firmware\binaries\$modeName"
$logPath = Join-Path $projectRoot "evidence\firmware\compile-$modeName.log"
New-Item -ItemType Directory -Path $buildPath, $outputPath -Force | Out-Null
$argsList = @(
    "compile", "--fqbn", "arduino:mbed_nano:nanorp2040connect",
    "--build-path", $buildPath, "--output-dir", $outputPath,
    "--warnings", "all", "--no-color"
)
if ($ArduinoConfig) { $argsList += @("--config-file", $ArduinoConfig) }
if ($RealImu) {
    $argsList += @("--build-property", "compiler.cpp.extra_flags=-DBUMP_MOCK_IO=0")
}
$argsList += (Join-Path $PSScriptRoot "BumpBadge")
# Redirect core cache writes to this project; never change the reused Lab config.
$env:ARDUINO_BUILD_CACHE_PATH = Join-Path $projectRoot "evidence\firmware\build\core-cache"
& $ArduinoCli @argsList 2>&1 | Tee-Object -FilePath $logPath
if ($LASTEXITCODE -ne 0) { throw "Arduino compilation failed: $logPath" }
Write-Host "Compilation succeeded ($modeName). No upload was performed."
