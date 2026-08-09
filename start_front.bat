@echo off
chcp 65001 >nul
echo ============================================
echo  StudyMate Agent - 启动Streamlit前端（前后端分离模式）
echo ============================================
echo.

cd /d "%~dp0"

echo [1/3] 检查项目结构...
if not exist "frontend\app.py" (
    echo 错误: frontend\app.py 不存在，请确认项目结构完整
    pause
    exit /b 1
)

if not exist ".env" (
    echo 错误: .env 配置文件不存在，请先创建
    pause
    exit /b 1
)

echo [2/3] 读取端口配置...
REM 从 .env 读取 STREAMLIT_PORT（默认 8501）
for /f "tokens=1,2 delims==" %%a in (.env) do (
    if /i "%%a"=="STREAMLIT_PORT" set STREAMLIT_PORT=%%b
)
if "%STREAMLIT_PORT%"=="" set STREAMLIT_PORT=8501
echo 服务端口: %STREAMLIT_PORT%

REM 设置默认运行模式为前后端分离
set APP_MODE=separated

echo [3/3] 启动Streamlit服务（uv run）...
echo 服务地址: http://localhost:%STREAMLIT_PORT%
echo.
echo 注意：请先启动后端 start_server.bat
echo       或在侧边栏切换为"一体化模式"免后端运行
echo.

uv run streamlit run frontend/app.py --server.port %STREAMLIT_PORT% --server.headless true

pause
