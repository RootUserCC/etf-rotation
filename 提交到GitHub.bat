@echo off
setlocal
cd /d "%~dp0"

echo Current changes:
git status --short

set "commit_msg="
set /p "commit_msg=Commit message (press Enter for auto): "
if not defined commit_msg set "commit_msg=update %date% %time%"

git add -A
if errorlevel 1 goto error

git diff --cached --quiet
if not errorlevel 1 (
    echo.
    echo No changes to commit.
    pause
    exit /b 0
)

git commit -m "%commit_msg%"
if errorlevel 1 goto error

git push
if errorlevel 1 goto error

echo.
echo Commit pushed to GitHub successfully.
pause
exit /b 0

:error
echo.
echo Commit or push failed. Check the Git messages above.
pause
exit /b 1
