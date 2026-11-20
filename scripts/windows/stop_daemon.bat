@echo off
chcp 65001 >nul
set "PIDFILE=%USERPROFILE%\.agenthub\daemon.pid"
if exist "%PIDFILE%" (
    set /p DPID=<"%PIDFILE%"
    echo Stopping AgentHub daemon (PID %DPID%)...
    taskkill /PID %DPID% /F >nul 2>&1
    del /f /q "%PIDFILE%" >nul 2>&1
    echo Stopped.
) else (
    echo No running daemon PID file found at %PIDFILE%.
)
pause
