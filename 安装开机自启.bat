@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   ETF 轮动监控 · 安装开机自启 与 收盘定时更新
echo ============================================================
echo   1. ETFRotationWeb        : 开机无窗口自启轻量本地服务
echo                              （按需实时更新：仅网页打开时抓行情）
echo   2. ETFRotationDataUpdate : 工作日 15:10 自动更新日线与页面数据
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_tasks.ps1"
if errorlevel 1 (
    echo.
    echo [失败] 计划任务注册失败，如提示权限不足请右键“以管理员身份运行”。
    pause
    exit /b 1
)

echo.
echo [启动] 正在立即拉起后台服务...
schtasks /run /tn ETFRotationWeb >nul 2>&1
ping 127.0.0.1 -n 3 >nul
echo [完成] 现在可随时访问 http://127.0.0.1:8001/ （双击“启动.bat”可直接打开）
echo        如需卸载自启，运行: powershell -ExecutionPolicy Bypass -File register_tasks.ps1 -Uninstall
echo.
pause
