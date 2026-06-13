@echo off
setlocal
title ERP Console Setup

set "ROOT=%~dp0"
set "SCRIPT=%ROOT%installer\erp-setup-launcher.ps1"
set "LOGDIR=%ROOT%installer\logs"

if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>nul

echo.
echo ERP Console Setup
echo.
echo Root: %ROOT%
echo Script: %SCRIPT%
echo Logs: %LOGDIR%
echo.

if not exist "%SCRIPT%" (
    echo [ERROR] Installer script not found.
    echo Keep this file with ERP-Setup.exe, installer, and manage.py in the ERP package root.
    echo.
    pause
    exit /b 1
)

net session >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Administrator permission is required.
    echo Right-click ERP-Setup-Console.cmd and choose Run as administrator.
    echo.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
set "EXITCODE=%ERRORLEVEL%"

echo.
echo ERP setup script exited with code: %EXITCODE%
echo If setup failed, check the newest log file in:
echo %LOGDIR%
echo.
pause
exit /b %EXITCODE%
