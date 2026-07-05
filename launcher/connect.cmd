@echo off
rem Double-click wrapper for connect.ps1 (Windows). Drop hires.conf next to
rem this file and double-click to connect.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0connect.ps1" %*
set RC=%ERRORLEVEL%
pause
exit /b %RC%
