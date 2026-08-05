@echo off
REM Double-clickable launcher for Windows. Defaults to production mode.
REM
REM NOTE: comments here are intentionally ASCII-only.
REM
REM A .bat file is read using the console's OEM code page (936 on Chinese
REM Windows, 437 on English). If this file were saved as UTF-8, every
REM Chinese comment byte would be re-interpreted as commands and cmd would
REM print a screen full of "is not recognized as an internal or external
REM command". Keeping the launcher ASCII-only removes that whole class of
REM problem -- the Chinese output comes from start.ps1, which prints fine
REM once chcp 65001 is set below.

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

if not exist ".env" (
    echo.
    echo [ERROR] .env not found. Run setup first: double-click setup.bat
    echo.
    pause
    exit /b 1
)

REM Production mode is the default here, unlike start.ps1 which defaults to
REM dev. Reason: this file is the entry point for people who just want to
REM use the app, and dev mode is wrong for them in four ways --
REM two processes instead of one, --reload watching files nobody edits,
REM two ports to understand (5173 vs 9000), and a slower first paint.
REM
REM Production serves the built frontend from the backend, so there is one
REM process and one URL. start.ps1 skips the rebuild when nothing changed,
REM so repeat launches are instant.
REM
REM Pass -Dev to get the dev server instead:  start.bat -Dev
set MODE=-Prod
if /i "%~1"=="-Dev" set MODE=
if /i "%~1"=="dev" set MODE=

REM -ExecutionPolicy Bypass is the whole point of this wrapper.
REM
REM Windows blocks unsigned .ps1 by default, so double-clicking start.ps1
REM gives "cannot be loaded because running scripts is disabled on this
REM system". That error names the file, not the fix, and sends people to
REM run Set-ExecutionPolicy machine-wide -- a much bigger change than this
REM task needs. Bypass applies to this one process only; nothing persists.
REM
REM -NoProfile skips the user's PowerShell profile: it can print banners,
REM change the working directory, or override Write-Host, any of which
REM would corrupt the launcher's output.
if "%MODE%"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %MODE% %*
)

REM pause keeps the window open on double-click, otherwise the error
REM messages from Fail() flash by and the user sees an empty screen.
echo.
pause
