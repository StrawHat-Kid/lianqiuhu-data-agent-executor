@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM   练秋湖 HC2026 纯问数执行器 - 启动脚本
REM   首次运行自动安装依赖项，之后每次直接启动服务
REM ============================================================

cd /d "%~dp0"

echo ============================================
echo    练秋湖 HC2026 纯问数执行器
echo    DeepSeek API 问答 / 静态知识全文注入
echo ============================================
echo.

REM ---------- 1. 检测 Python ----------
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.9 或更高版本，
    echo        并勾选 "Add python.exe to PATH" 后重新运行本脚本。
    pause
    exit /b 1
)

REM ---------- 2. 创建 / 复用虚拟环境 ----------
set "PY=python"
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    echo [信息] 首次运行，正在创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [提示] 虚拟环境创建失败，改用系统 Python。
    ) else (
        set "PY=.venv\Scripts\python.exe"
    )
)

REM ---------- 3. 自动安装依赖项 ----------
echo [信息] 正在检查并安装依赖项，请稍候 ...
"%PY%" -m pip install --upgrade pip >nul 2>nul
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络连接后重新运行本脚本。
    pause
    exit /b 1
)
echo [信息] 依赖项已经就绪。
echo.

REM ---------- 4. 启动服务 ----------
echo [信息] 正在启动服务，浏览器/Postman 访问 http://127.0.0.1:18034
echo [信息] 实际 host/port 以本机 config.json 为准；按 Ctrl+C 停止服务。
echo.
"%PY%" main.py

pause
