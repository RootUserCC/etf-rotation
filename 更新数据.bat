@echo off
setlocal
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo [1/2] 更新行情数据...
"%PY%" fetch_data.py || (echo 数据更新失败 & pause & exit /b 1)
echo [2/2] 生成网站数据...
"%PY%" export_json.py || (echo 导出失败 & pause & exit /b 1)
echo 完成，刷新网页即可看到最新数据。
pause
