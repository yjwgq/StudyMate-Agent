@echo off
chcp 65001 >nul
echo ============================================
echo  StudyMate Agent - 启动FastAPI后端（前后端分离模式）
echo ============================================
echo.

cd /d "%~dp0"

echo [1/3] 检查项目结构...
if not exist "api\main.py" (
    echo 错误: api\main.py 不存在，请确认项目结构完整
    pause
    exit /b 1
)

if not exist ".env" (
    echo 错误: .env 配置文件不存在，请先创建
    pause
    exit /b 1
)

echo [2/3] 读取端口配置...
REM 从 .env 读取 FASTAPI_PORT（默认 8000）
for /f "tokens=1,2 delims==" %%a in (.env) do (
    if /i "%%a"=="FASTAPI_PORT" set FASTAPI_PORT=%%b
)
if "%FASTAPI_PORT%"=="" set FASTAPI_PORT=8000
echo 服务端口: %FASTAPI_PORT%

echo [3/3] 启动FastAPI服务（uv run）...
echo 服务地址: http://localhost:%FASTAPI_PORT%
echo 接口文档: http://localhost:%FASTAPI_PORT%/docs
echo 健康检测: http://localhost:%FASTAPI_PORT%/api/health
echo.
echo 按 Ctrl+C 可停止服务
echo.

uv run uvicorn api.main:app --host 0.0.0.0 --port %FASTAPI_PORT% --reload

pause
