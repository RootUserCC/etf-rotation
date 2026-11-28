@echo off
rem Daily update: fetch data -> export json -> publish to gh-pages. Logs to update.log.
setlocal
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo [%date% %time%] start update >> update.log
"%PY%" fetch_data.py >> update.log 2>&1
if errorlevel 1 (echo [%date% %time%] fetch FAILED >> update.log & exit /b 1)
"%PY%" export_json.py >> update.log 2>&1
if errorlevel 1 (echo [%date% %time%] export FAILED >> update.log & exit /b 1)
echo [%date% %time%] update done >> update.log
if exist publish_site.bat call publish_site.bat >> update.log 2>&1
