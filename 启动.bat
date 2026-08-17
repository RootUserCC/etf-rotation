@echo off
cd /d %~dp0

echo [1/3] 清理 8001 端口的旧服务器进程...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:"127.0.0.1:8001 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

echo [2/3] 通过计划任务后台启动服务器（无窗口，关闭任何终端都不影响）...
schtasks /run /tn ETFRotationWeb >nul

echo [3/3] 等待服务器就绪...
ping 127.0.0.1 -n 4 >nul

echo 打开监控页面 http://127.0.0.1:8001/
start "" http://127.0.0.1:8001/
