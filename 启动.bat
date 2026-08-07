@echo off
chcp 65001 >nul
cd /d %~dp0site
start "" http://127.0.0.1:8001/
python -m http.server 8001
