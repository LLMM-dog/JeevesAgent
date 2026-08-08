@echo off
REM ============================================================
REM  Jeeves -- double-click to start (Windows)
REM
REM  One-click launcher. First run auto-initialises the project
REM  (dependencies, .env, database). Subsequent runs skip setup.
REM
REM  NOTE: comments here are intentionally ASCII-only.
REM  A .bat file is read using the console's OEM code page (936
REM  on Chinese Windows). UTF-8 comments would print as garbage.
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
    echo (This installs dependencies and creates .env. One-time, ~60 seconds.)
    echo.
    uv run python scripts\setup.py %*
    if errorlevel 1 (
        echo.
        echo [ERROR] Setup failed. Check the output above.
        pause
        exit /b 1
    )
    echo.
    echo Setup complete. Starting Jeeves...
    echo.
)

REM -- Mode --
REM Production is the default for double-click users (single process,
REM single URL). Pass -Dev for the dev server with hot-reload.
set MODE=-Prod
if /i "%~1"=="-Dev" set MODE=
if /i "%~1"=="dev"  set MODE=

REM -- Launch --
REM start.bat exists because Windows blocks unsigned .ps1 by default.
REM Double-clicking start.ps1 gives "cannot be loaded because running
REM scripts is disabled on this system". This wrapper uses -ExecutionPolicy
REM Bypass for this one process only -- nothing persists.
if "%MODE%"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %MODE% %*
)

REM Keep the window open on double-click so error messages don't flash by.
echo.
pause
