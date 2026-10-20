@echo off
chcp 65001 >nul
echo ===================================================
echo   ClassRelay 教师机依赖中继服务器正在启动...
echo ===================================================

REM 切换到脚本所在目录，防止从其他位置运行/双击导致找不到 classrelay.py
cd /d "%~dp0"

REM 依次探测可用的 Python 命令：py(启动器) > python > python3
set "PYCMD="
py -V >nul 2>&1 && set "PYCMD=py"
if not defined PYCMD (
    python -V >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
    python3 -V >nul 2>&1 && set "PYCMD=python3"
)
if not defined PYCMD (
    echo [错误] 未检测到 Python。
    echo        请安装 Python 3.7+ 并勾选 "Add Python to PATH"：
    echo        https://www.python.org/downloads/
    echo        注意：请勿使用 Microsoft Store 版 Python 的占位别名。
    pause
    exit /b 1
)

echo 使用 Python 命令: %PYCMD%
%PYCMD% -u classrelay.py %*
if %errorlevel% neq 0 (
    pause
)
