@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup_windows.bat first.
  pause
  exit /b 1
)
if not defined BOT_SESSION_DIR set "BOT_SESSION_DIR=%~dp0"
call .venv\Scripts\activate
python main.py
pause
