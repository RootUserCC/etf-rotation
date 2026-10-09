@echo off
rem Publish site/ to gh-pages branch (GitHub Pages). Non-fatal on failure.
cd /d %~dp0
if not exist .gh-pages (
  echo [site] .gh-pages worktree missing, skip
  exit /b 0
)
copy /y site\index.html .gh-pages\ >nul
copy /y site\data.json .gh-pages\ >nul
copy /y site\echarts.min.js .gh-pages\ >nul
cd .gh-pages
git add -A
git diff --cached --quiet
if not errorlevel 1 (
  echo [site] no change
  exit /b 0
)
git commit -q -m "update site data"
git push origin gh-pages
if errorlevel 1 (echo [site] push FAILED) else (echo [site] published)
exit /b 0
