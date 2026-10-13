@echo off
rem Daily update: fetch data -> export json -> publish to gh-pages. Logs to update.log.
cd /d %~dp0
echo [%date% %time%] start update >> update.log
.venv\Scripts\python fetch_data.py >> update.log 2>&1
if errorlevel 1 (echo [%date% %time%] fetch FAILED >> update.log & exit /b 1)
.venv\Scripts\python export_json.py >> update.log 2>&1
if errorlevel 1 (echo [%date% %time%] export FAILED >> update.log & exit /b 1)
echo [%date% %time%] update done >> update.log
call publish_site.bat >> update.log 2>&1
