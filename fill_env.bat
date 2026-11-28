@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo ============================================
echo  Musicxdy - .env setup
echo ============================================
echo.
echo Values kisi ko mat bhejo.
echo.
set /p API_ID=API_ID: 
if "%API_ID%"=="" goto missing
set /p API_HASH=API_HASH: 
if "%API_HASH%"=="" goto missing
set /p BOT_TOKEN=BOT_TOKEN: 
if "%BOT_TOKEN%"=="" goto missing
set /p SESSION_STRING=SESSION_STRING: 
if "%SESSION_STRING%"=="" goto missing
(
 echo API_ID=%API_ID%
 echo API_HASH=%API_HASH%
 echo BOT_TOKEN=%BOT_TOKEN%
 echo SESSION_STRING=%SESSION_STRING%
 echo.
 echo AUDIO_QUALITY=high
 echo AUDIO_NORMALIZE=0
 echo START_WATCHDOG=0
 echo START_DELAY=10
 echo.
 echo YOUTUBE_FIRST=1
 echo YOUTUBE_FIRST_TIMEOUT=20
 echo MIN_AUDIO_KBPS=120
 echo ENABLE_ALTERNATE_SOURCES=1
) > .env
echo.
echo .env saved. Notepad opening...
start "" notepad .env
pause
exit /b 0
:missing
echo.
echo ERROR: koi value blank hai. Dobara chalao.
pause
exit /b 1
