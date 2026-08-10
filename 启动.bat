@echo off
chcp 65001 >nul
cd /d %~dp0

echo [1/3] 清理 8001 端口的旧服务器进程...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:"127.0.0.1:8001 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

echo [2/3] 启动服务器（最小化窗口运行）...
start "ETF轮动服务器" /min .venv\Scripts\python server.py

echo [3/3] 等待服务器就绪...
timeout /t 2 /nobreak >nul

echo 打开监控页面 http://127.0.0.1:8001/
start "" http://127.0.0.1:8001/
