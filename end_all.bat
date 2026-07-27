@echo off
echo ==========================================
echo       Terminating MailMind Services
echo ==========================================

echo [1/3] Terminating API Server (Port 8000)...
FOR /F "tokens=5" %%T IN ('netstat -a -n -o ^| findstr :8000') DO (
    taskkill /F /PID %%T >nul 2>&1
)

echo [2/3] Terminating React Web UI (Port 5173)...
FOR /F "tokens=5" %%T IN ('netstat -a -n -o ^| findstr :5173') DO (
    taskkill /F /PID %%T >nul 2>&1
)

echo [3/3] Terminating Polling Engine...
wmic process where "CommandLine like '%%src\main.py%%' and Name='python.exe'" call terminate >nul 2>&1
wmic process where "CommandLine like '%%src/main.py%%' and Name='python.exe'" call terminate >nul 2>&1
wmic process where "CommandLine like '%%nodemon%%' and Name='node.exe'" call terminate >nul 2>&1

echo.
echo All MailMind services have been shut down successfully!
