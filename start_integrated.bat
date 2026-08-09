@echo off
chcp 65001 >nul
echo ============================================
echo  StudyMate Agent - 启动一体化模式（Streamlit直接调用工作流，免后端）
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
for /f "tokens=1,2 delims==" %%a in (.env) do (
    if /i "%%a"=="STREAMLIT_PORT" set STREAMLIT_PORT=%%b
)
if "%STREAMLIT_PORT%"=="" set STREAMLIT_PORT=8501
echo 服务端口: %STREAMLIT_PORT%

REM 设置运行模式为一体化（Streamlit 直接导入 graph 工作流）
set APP_MODE=integrated

echo [3/3] 启动Streamlit一体化模式（uv run）...
echo 服务地址: http://localhost:%STREAMLIT_PORT%
echo.
echo 一体化模式说明：
echo   - Streamlit 直接调用 graph 工作流，无需启动 FastAPI 后端
echo   - Agent 与 Chroma 在 Streamlit 进程内运行
echo   - 首次请求时加载 Chroma 索引，可能较慢
echo   - 适合单机学生使用，资源占用较低
echo.

uv run streamlit run frontend/app.py --server.port %STREAMLIT_PORT% --server.headless true

pause
