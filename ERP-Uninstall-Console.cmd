@echo off
setlocal
title ERP Console Uninstall

set "ROOT=%~dp0"
set "SCRIPT=%ROOT%installer\erp-uninstall-launcher.ps1"
set "LOGDIR=%ROOT%installer\logs"

if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>nul

echo.
echo ERP Console Uninstall
echo.
echo Root: %ROOT%
echo Script: %SCRIPT%
echo Logs: %LOGDIR%
echo.

if not exist "%SCRIPT%" (
    echo [ERROR] Uninstall script not found.
    echo Keep this file with ERP-Uninstall.exe, installer, and manage.py in the ERP package root.
    echo.
    pause
    exit /b 1
)

net session >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Administrator permission is required.
    echo Right-click ERP-Uninstall-Console.cmd and choose Run as administrator.
    echo.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
set "EXITCODE=%ERRORLEVEL%"

echo.
echo ERP uninstall script exited with code: %EXITCODE%
echo If uninstall failed, check the newest log file in:
echo %LOGDIR%
echo.
pause
exit /b %EXITCODE%
