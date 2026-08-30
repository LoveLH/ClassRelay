@echo off
chcp 65001 >nul
echo ===================================================
echo   ClassRelay 教师机依赖中继服务器正在启动...
echo ===================================================

python -V >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.7+ 并勾选 Add to PATH。
    pause
    exit /b 1
)

python classrelay.py %*
if %errorlevel% neq 0 (
    pause
)
