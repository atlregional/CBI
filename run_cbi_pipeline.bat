@echo off
title CBI Multi-Corridor Pipeline
echo ============================================================
echo  CBI MULTI-CORRIDOR PIPELINE
echo ============================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_cbi_pipeline.ps1"
set EXITCODE=%ERRORLEVEL%

echo.
echo ============================================================
echo PowerShell exited with code %EXITCODE%
echo ============================================================
echo.
pause
exit /b %EXITCODE%
