@echo off
cd /d %~dp0
echo [%date% %time%] 开始更新 >> update.log
.venv\Scripts\python fetch_data.py >> update.log 2>&1
if errorlevel 1 (echo [%date% %time%] 数据更新失败 >> update.log & exit /b 1)
.venv\Scripts\python export_json.py >> update.log 2>&1
if errorlevel 1 (echo [%date% %time%] 导出失败 >> update.log & exit /b 1)
.venv\Scripts\python gen_signals.py >> update.log 2>&1
if errorlevel 1 (echo [%date% %time%] 信号生成失败 >> update.log & exit /b 1)
echo [%date% %time%] 完成 >> update.log
