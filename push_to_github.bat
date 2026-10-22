@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo =========================================
echo   Gold Alert v2 - GitHub Push
echo =========================================
echo.

where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git not found. Install from https://git-scm.com/download/win
    echo.
    pause
    exit /b 1
)

echo [1/4] Init git repo...
if not exist .git (
    git init
)
git branch -M main

echo.
echo [2/4] Stage files...
git add .
git add -u

echo.
echo [3/4] Commit...
git -c user.name="gold-alert" -c user.email="gold-alert@local" commit -m "v2: cloud run + serverchan + history analysis"

echo.
echo [4/4] Remote URL...
echo.
echo Paste your GitHub repo URL (e.g. https://github.com/USERNAME/REPO.git):
echo.

set /p REPO_URL=Repo URL: 

if "%REPO_URL%"=="" (
    echo [SKIP] No URL entered. Local commit done.
    echo        Run later: git remote add origin ^<URL^> ^&^& git push -u origin main
    pause
    exit /b 0
)

git remote remove origin 2>nul
git remote add origin %REPO_URL%

echo.
echo Pushing to GitHub...
echo.
git push -u origin main

echo.
if errorlevel 1 (
    echo [FAIL] Push error. Check: wrong URL, repo not empty, or browser auth needed.
) else (
    echo.
    echo DONE! Next steps:
    echo  1. Settings -^> Secrets and variables -^> Actions
    echo  2. Add 4 secrets: SMTP_USER, SMTP_AUTH_CODE, MAIL_TO, SERVERCHAN_SENDKEY
    echo  3. Actions -^> Run workflow
)
echo.
pause
