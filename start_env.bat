@echo off
chcp 65001 >nul
echo ============================================
echo  StudyMate Agent - 环境配置脚本
echo ============================================
echo.

cd /d "%~dp0"

echo [1/3] 检查uv是否已安装...
where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo 错误: uv 未安装，请先安装 uv
    echo 安装命令: powershell -Command "Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -OutFile 'install.ps1'; .\install.ps1"
    pause
    exit /b 1
)
echo uv 已安装

echo.
echo [2/3] 同步依赖并创建虚拟环境...
uv sync
if %errorlevel% neq 0 (
    echo 错误: uv sync 失败
    pause
    exit /b 1
)
echo 依赖同步完成

echo.
echo [3/3] 激活虚拟环境...
call .venv\Scripts\activate.bat
if %errorlevel% neq 0 (
    echo 错误: 激活虚拟环境失败
    pause
    exit /b 1
)
echo 虚拟环境已激活

echo.
echo ============================================
echo 环境配置完成！
echo 当前目录: %cd%
echo Python版本: 
python --version
echo ============================================
echo.
echo 后续操作:
echo   - 启动后端: start_server.bat
echo   - 启动前端: start_front.bat
echo   - 环境检查: python env_check.py
echo.
pause