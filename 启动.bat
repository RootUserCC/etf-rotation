@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo [1/3] 清理 8001 端口的旧服务器进程...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":8001 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

if exist "server_err.log" del /f /q "server_err.log" >nul 2>&1

echo [2/3] 后台无窗口启动监控服务（按需实时模式，关闭网页自动休眠）...
set "PYW="
for /f "delims=" %%p in ('where pythonw.exe 2^>nul') do (
    if not defined PYW set "PYW=%%p"
)
if not defined PYW (
    for /f "delims=" %%p in ('where python.exe 2^>nul') do (
        if not defined PYW (
            set "PYDIR=%%~dpp"
            if exist "!PYDIR!pythonw.exe" set "PYW=!PYDIR!pythonw.exe"
        )
    )
)

if not defined PYW (
    echo [错误] 未找到 pythonw.exe，请先安装 Python 并加入 PATH。
    pause
    exit /b 1
)

echo 使用解释器: !PYW!
start "" "!PYW!" "%~dp0run_server.pyw"

echo [3/3] 等待 8001 端口就绪...
set "READY=0"
for /l %%i in (1,1,10) do (
    if "!READY!"=="0" (
        netstat -ano | findstr /C:":8001 " | findstr "LISTENING" >nul 2>&1
        if not errorlevel 1 (
            set "READY=1"
        ) else (
            ping 127.0.0.1 -n 2 >nul
        )
    )
)

if "!READY!"=="0" (
    echo.
    echo [失败] 8001 端口未能启动！
    if exist "server_err.log" (
        echo --- server_err.log ---
        type "server_err.log"
    )
    pause
    exit /b 1
)

echo 服务已就绪，正在打开 http://127.0.0.1:8001/
start "" http://127.0.0.1:8001/
