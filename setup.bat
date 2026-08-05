@echo off
REM Double-clickable setup entry (promised by docs/05-dev/setup.md).
REM
REM NOTE: comments here are intentionally ASCII-only.
REM
REM A .bat file is read using the console's OEM code page (936 on Chinese
REM Windows, 437 on English). If this file were saved as UTF-8, every
REM Chinese comment byte would be re-interpreted as commands and cmd would
REM print a screen full of "is not recognized as an internal or external
REM command" -- which is exactly what happened during verification.
REM
REM Keeping the launcher ASCII-only removes the whole class of problem.
REM The Chinese output comes from setup.py itself, which is UTF-8 and
REM prints fine once chcp 65001 is set below.

chcp 65001 >nul 2>&1
cd /d "%~dp0"

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

REM Use "uv run" instead of bare python: bare python may resolve to any
REM interpreter on PATH, while setup.py step 5 imports backend modules
REM that need the project's dependencies.
uv run python scripts\setup.py %*

REM pause keeps the window open on double-click, otherwise error messages
REM flash by and the user sees nothing.
echo.
pause
