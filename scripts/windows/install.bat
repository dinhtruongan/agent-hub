@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
title AgentHub Local — 1-Click Installer
echo Installing agenthub-local...
python -m pip install -e .
if errorlevel 1 (
    echo Install failed. Check Python 3.12+ installation.
    pause
    exit /b 1
)
agenthub-local init
pause
