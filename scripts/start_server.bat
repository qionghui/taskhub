@echo off
REM TaskHub 服务端启动脚本（Windows）
REM 用法：start_server.bat

cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] 创建虚拟环境...
    C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m venv .venv
)

echo [2/2] 启动 TaskHub，访问 http://127.0.0.1:8787
.venv\Scripts\python.exe -m uvicorn server.app:app --host 0.0.0.0 --port 8787