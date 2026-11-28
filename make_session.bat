@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Run setup_windows.bat first.
  pause
  exit /b 1
)
"%PY%" make_session.py
pause
