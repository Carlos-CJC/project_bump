@echo off
setlocal
set "PSModulePath="
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Hardware.ps1" %*
set "BUMP_EXIT=%ERRORLEVEL%"
if not "%BUMP_EXIT%"=="0" pause
exit /b %BUMP_EXIT%
