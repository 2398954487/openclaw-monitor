@echo off
chcp 65001 >nul
title OpenClaw Monitor

echo ========================================
echo   OpenClaw Monitor
echo ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.9+
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 安装依赖
echo [1/2] 安装依赖...
pip install -r requirements.txt >nul 2>&1
if errorlevel 1 (
    pip install flask psutil
)

REM 启动
echo [2/2] 启动监控面板...
echo.
echo 面板地址: http://localhost:19100
echo 按 Ctrl+C 停止
echo.
python app.py

pause
