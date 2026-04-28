@echo off
REM ===========================================================
REM NHL Props Model — GitHub Push Helper
REM
REM Run this AFTER you've created the empty repo on github.com
REM ===========================================================

echo.
echo === NHL Props Model: GitHub Setup ===
echo.

REM Prompt for username
set /p GH_USER="Your GitHub username: "
if "%GH_USER%"=="" (
    echo ERROR: username required
    pause
    exit /b 1
)

REM Default repo name
set REPO_NAME=nhl-props-model
set /p CONFIRM="Repo name [%REPO_NAME%]: "
if not "%CONFIRM%"=="" set REPO_NAME=%CONFIRM%

echo.
echo Will push to: https://github.com/%GH_USER%/%REPO_NAME%
echo.
set /p OK="Continue? (y/N): "
if /i not "%OK%"=="y" (
    echo Aborted.
    pause
    exit /b 0
)

cd /d "%~dp0"

echo.
echo [1/6] Initializing git repo...
git init >nul 2>&1
if errorlevel 1 (
    echo ERROR: git not installed. Get it from https://git-scm.com/download/win
    pause
    exit /b 1
)

echo [2/6] Building dashboard locally first (so Pages works on first deploy)...
python build_dashboard.py
if errorlevel 1 (
    echo WARNING: local build failed. Continuing anyway — GitHub Action will retry.
)

echo [3/6] Adding files...
git add .
git status --short

echo [4/6] Creating initial commit...
git commit -m "initial NHL props model"
if errorlevel 1 (
    echo (no changes to commit, or commit failed — that's OK if already committed)
)

echo [5/6] Setting branch + remote...
git branch -M main
git remote remove origin >nul 2>&1
git remote add origin https://github.com/%GH_USER%/%REPO_NAME%.git

echo [6/6] Pushing to GitHub...
echo (If asked for password, use a Personal Access Token from
echo  GitHub Settings -^> Developer settings -^> Personal access tokens)
echo.
git push -u origin main

echo.
echo === DONE ===
echo.
echo Now in your browser:
echo   1. Add ODDS_API_KEY secret:
echo      https://github.com/%GH_USER%/%REPO_NAME%/settings/secrets/actions/new
echo      Name: ODDS_API_KEY
echo      Value: 5cce14f3242989037557db8157e2db7f
echo.
echo   2. Enable GitHub Pages:
echo      https://github.com/%GH_USER%/%REPO_NAME%/settings/pages
echo      Source: Deploy from a branch
echo      Branch: main / folder: /docs
echo      Save
echo.
echo   3. Trigger first build:
echo      https://github.com/%GH_USER%/%REPO_NAME%/actions
echo      Click "Daily NHL Props Build" -^> "Run workflow"
echo.
echo   4. After ~2 min your dashboard is at:
echo      https://%GH_USER%.github.io/%REPO_NAME%/
echo.
pause
