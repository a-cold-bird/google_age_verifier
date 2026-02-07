@echo off
chcp 65001 >nul
title Age Verification API

echo ========================================
echo   Age Verification API 启动脚本
echo ========================================
echo.

:: 设置端口
set PORT=5001

:: 清理占用端口的进程
echo [1/3] 检查并清理端口 %PORT% ...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    echo       发现进程 PID: %%a 占用端口 %PORT%
    taskkill /F /PID %%a >nul 2>&1
    if !errorlevel! == 0 (
        echo       已终止进程 %%a
    )
)
echo       端口 %PORT% 已清理

:: 清理残留的 Python 进程（可选，只清理本脚本相关的）
echo.
echo [2/3] 检查残留进程...
for /f "tokens=2" %%a in ('tasklist /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq Age Verification API" /NH 2^>nul ^| findstr /i "python"') do (
    echo       发现残留 Python 进程 PID: %%a
    taskkill /F /PID %%a >nul 2>&1
)
echo       残留进程检查完成

:: 启动 API 服务
echo.
echo [3/3] 启动 API 服务...
echo.
echo ----------------------------------------
echo   端口: %PORT%
echo   模式: 无头模式（不显示浏览器窗口）
echo   前端: http://localhost:%PORT%/
echo ----------------------------------------
echo.

:: 切换到脚本所在目录
cd /d "%~dp0"

:: 启动服务（默认无头模式）
:: python age_verification_api.py --port %PORT%

:: 如果需要有头模式（显示浏览器窗口），使用下面这行：
python age_verification_api.py --port %PORT% --show-browser

pause
