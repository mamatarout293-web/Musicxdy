@echo off
setlocal
cd /d "%~dp0"
echo === Musicxdy Windows setup ===
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo Python not found.
  echo Install Python 3.11 (64-bit) from https://www.python.org/downloads/release/python-3119/
  echo Tick "Add python.exe to PATH" during installation.
  pause
  exit /b 1
)

py -3.11 -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 not found.
  echo Install Python 3.11 (64-bit) from https://www.python.org/downloads/release/python-3119/
  echo Do not use Python 3.12+; the audio crypto package has no Windows wheel there.
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating virtual environment...
  py -3.11 -m venv .venv
)
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg not found. Trying winget...
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
  echo Close this window, open it again, and run setup_windows.bat once more.
  pause
  exit /b 1
)

if not exist .env (
  copy .env.example .env >nul
  echo .env created. Fill API_ID, API_HASH, BOT_TOKEN and SESSION_STRING.
  notepad .env
)

echo.
echo Setup finished. Double-click start_windows.bat to run the bot.
pause
