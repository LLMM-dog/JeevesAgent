@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Jeeves 卸载

echo ============================================================
echo   Jeeves 一键卸载
echo   清理项目文件夹之外的东西，并可整体删除本项目
echo ============================================================
echo.
echo 本工具会：
echo   1. 清理便携版 Tailscale（.tailscale/ + tailscaled 进程）
echo   2. 清理项目创建的 Docker 容器
echo   3. 检查残留进程
echo   4. 之后可选删除整个项目文件夹（含源码）
echo.

REM -- 调 Python 卸载脚本（清项目外残留）--
if exist ".venv\Scripts\python.exe" (
    echo 使用项目内置 Python...
    call ".venv\Scripts\python.exe" scripts\uninstall.py %*
    if errorlevel 1 goto end
) else (
    where uv >nul 2>&1
    if errorlevel 1 (
        echo [提示] 未找到 uv，无法运行卸载脚本。
        echo        你可以直接手动删除本文件夹，然后：
        echo        - 结束 tailscaled.exe 进程（若在跑）
        goto end
    )
    uv run python scripts\uninstall.py %*
    if errorlevel 1 goto end
)

echo.
echo ============================================================
set /p DEL=是否删除【整个项目文件夹】（含源码，不可恢复）？[y/N]: 
if /i "%DEL%"=="y" goto delfolder
if /i "%DEL%"=="yes" goto delfolder
echo.
echo 已取消删除文件夹，项目文件保留。卸载完成。
goto end

:delfolder
echo.
echo 正在安排删除整个项目文件夹...
set "DELBAT=%TEMP%\jeeves-selfdelete.bat"
>"%DELBAT%" echo @echo off
>>"%DELBAT%" echo timeout /t 1 /nobreak ^>nul
>>"%DELBAT%" echo rmdir /s /q "%~dp0"
cd /d "%USERPROFILE%"
start "" /b "%DELBAT%"
echo 项目文件夹将在 1 秒后删除，本窗口即将关闭。
timeout /t 2 >nul
exit

:end
echo.
pause