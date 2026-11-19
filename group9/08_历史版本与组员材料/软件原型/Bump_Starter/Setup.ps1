param([string]$PythonPath = "")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    if ($PythonPath) {
        & $PythonPath -c "import sys; assert sys.version_info[:2] == (3,12), 'Python 3.12 is required'"
        if ($LASTEXITCODE -ne 0) { throw "Please choose Python 3.12." }
        & $PythonPath -m venv .venv
    } else {
        & py -3.12 -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Install Python 3.12, or pass -PythonPath with its python.exe path." }
}
& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check your connection and retry Setup.ps1." }
& $python -m bump init
if ($LASTEXITCODE -ne 0) { throw "Runtime initialization failed." }
Write-Host "Ready. Run Start.ps1, then Run-Demo.ps1 in another terminal."
