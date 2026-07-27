@echo off
echo ==========================================
echo       Starting MailMind V2 Services
echo ==========================================

echo [0/3] Initializing Database Schema...
venv\Scripts\python src\setup_db.py

echo [1/3] Starting Backend API Server (Port 8000)...
start "MM_API_Server" venv\Scripts\uvicorn api.app:app --reload --port 8000

echo [2/3] Starting Polling Engine...
start "MM_Polling_Engine" npx nodemon --watch src -e py --exec "venv\Scripts\python src\main.py"

echo [3/3] Starting React Web UI...
cd frontend
start "MM_Web_UI" npm run dev

echo.
echo All services have been launched in separate windows!
echo You can safely close this window.
