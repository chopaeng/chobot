@echo off
setlocal enabledelayedexpansion

:: Set console encoding to UTF-8
chcp 65001 >nul 2>&1

:: Always navigate to the script's directory
cd /d "%~dp0"
title Chobot FlightLogger Watchdog

echo ========================================================
echo         Chobot FlightLogger Supervisor & Watchdog
echo ========================================================
echo Directory: %CD%

:: Ensure logs directory exists
if not exist "logs" mkdir "logs"

:: Determine Python executable
set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

echo Python: %PYTHON_EXE%
echo.

:loop
echo [%DATE% %TIME%] Starting FlightLogger...
echo [%DATE% %TIME%] Starting FlightLogger... >> "logs\watchdog.log"

"%PYTHON_EXE%" main.py flight-logger
set "EXIT_CODE=%ERRORLEVEL%"

echo [%DATE% %TIME%] FlightLogger process exited with code %EXIT_CODE%.
echo [%DATE% %TIME%] FlightLogger process exited with code %EXIT_CODE%. >> "logs\watchdog.log"

echo [%DATE% %TIME%] Restarting FlightLogger in 5 seconds (Press Ctrl+C to abort)...
timeout /t 5 /nobreak >nul
goto loop
