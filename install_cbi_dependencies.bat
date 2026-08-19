@echo off
title CBI Python Dependency Setup
echo ============================================================
echo  CBI PYTHON DEPENDENCY SETUP
echo ============================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_cbi_dependencies.ps1"
set EXITCODE=%ERRORLEVEL%

echo.
echo ============================================================
echo Installer exited with code %EXITCODE%
echo ============================================================
echo.
pause
exit /b %EXITCODE%
