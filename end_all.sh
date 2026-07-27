#!/bin/bash
echo "=========================================="
echo "      Terminating MailMind Services"
echo "=========================================="

echo "[1/3] Terminating API Server (Port 8000)..."
API_PID=$(netstat -ano | grep :8000 | awk '{print $5}' | head -n 1)
if [ ! -z "$API_PID" ]; then
    taskkill //F //PID $API_PID >/dev/null 2>&1
fi

echo "[2/3] Terminating React Web UI (Port 5173)..."
UI_PID=$(netstat -ano | grep :5173 | awk '{print $5}' | head -n 1)
if [ ! -z "$UI_PID" ]; then
    taskkill //F //PID $UI_PID >/dev/null 2>&1
fi

echo "[3/3] Terminating Polling Engine..."
wmic process where "CommandLine like '%src\main.py%' and Name='python.exe'" call terminate >/dev/null 2>&1
wmic process where "CommandLine like '%src/main.py%' and Name='python.exe'" call terminate >/dev/null 2>&1
wmic process where "CommandLine like '%nodemon%' and Name='node.exe'" call terminate >/dev/null 2>&1

echo ""
echo "All MailMind services have been shut down successfully!"
