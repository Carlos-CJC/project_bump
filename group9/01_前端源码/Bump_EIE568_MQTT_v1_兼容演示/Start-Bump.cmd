@echo off
setlocal
set "PSModulePath="
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\..\run.ps1" demo %*
set "BUMP_EXIT=%ERRORLEVEL%"
if not "%BUMP_EXIT%"=="0" (
  echo.
  echo Bump could not start. See the message above and LOCAL_WEB_GUIDE.md.
  pause
)
exit /b %BUMP_EXIT%
