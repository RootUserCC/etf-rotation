@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo ============================================================
echo   ETF 轮动监控 · 一键推送到 GitHub
echo ============================================================
echo.
set "do_sync="
set /p "do_sync=推送前是否先拉取最新行情并导出 data.json？[y/N 直接回车跳过]: "
if /i "%do_sync%"=="y" (
    echo.
    echo [1/4] 更新行情与导出页面数据...
    "%PY%" fetch_data.py
    if errorlevel 1 goto error
    "%PY%" export_json.py
    if errorlevel 1 goto error
)

echo.
echo [2/4] 当前仓库变更:
git status --short
echo.

for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm'"') do set "NOW_STR=%%i"
set "commit_msg="
set /p "commit_msg=请输入提交说明（直接回车使用“更新 %NOW_STR%”）: "
if not defined commit_msg set "commit_msg=更新 %NOW_STR%"

echo.
echo [3/4] 暂存并提交变更...
git add -A
if errorlevel 1 goto error

git diff --cached --quiet
if not errorlevel 1 (
    echo 没有需要提交的新改动。
    goto maybe_pages
)

git commit -m "%commit_msg%"
if errorlevel 1 goto error

echo.
echo [4/4] 推送到 GitHub (origin main)...
git push origin HEAD
if errorlevel 1 goto error

:maybe_pages
if exist ".gh-pages" (
    if exist "publish_site.bat" (
        echo.
        echo [附带] 同步发布静态站到 gh-pages...
        call publish_site.bat
    )
)

echo.
echo [成功] 已推送到 GitHub！
pause
exit /b 0

:error
echo.
echo [失败] 推送过程出错，请查看上方提示信息。
pause
exit /b 1
