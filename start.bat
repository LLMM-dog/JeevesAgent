@echo off
REM ============================================================
REM  Jeeves -- double-click to start (Windows)
REM
REM  One-click launcher. First run auto-initialises the project.
REM ============================================================

chcp 65001 >nul 2>&1
cd /d "%~dp0"

REM -- uv check --
where uv >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] uv not found. Install it first:
    echo.
    echo     powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo.
    pause
    exit /b 1
)

REM -- First run / missing .env? Auto-setup --
if not exist ".env" (
    echo.
    echo First run detected -- initialising project...
    echo.
    uv run python scripts\setup.py %*
    if errorlevel 1 (
        echo.
        echo [ERROR] Setup failed. Check the output above.
        pause
        exit /b 1
    )
    echo.
    echo Done.
    echo.
)

REM -- Mode --
REM Production is the default for double-click users.
REM Pass -Dev for the dev server with hot-reload.
set MODE=-Prod
if /i "%~1"=="-Dev" set MODE=
if /i "%~1"=="dev"  set MODE=

REM -- Launch --
if "%MODE%"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %MODE% %*
)

echo.
pause
