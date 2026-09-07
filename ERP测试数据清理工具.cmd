@echo off
setlocal
set "APP_DIR=%~dp0"
set "ERP_APP_DIR=%APP_DIR%"
set "EXE=%APP_DIR%ERP测试数据清理工具.exe"
if exist "%EXE%" (
    start "ERP测试数据清理工具" "%EXE%"
    exit /b 0
)
set "PYTHONW=%APP_DIR%.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=pythonw.exe"
start "ERP测试数据清理工具" "%PYTHONW%" "%APP_DIR%tools\erp_test_data_cleanup.py"
endlocal
