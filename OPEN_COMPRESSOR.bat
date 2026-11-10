@echo off
rem Compressor MP4 - opens the UI in the browser. Close this window to stop.
rem Needs Python 3 (python.org). Installs ffmpeg (imageio-ffmpeg package) if missing.
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python not found. Install it from https://www.python.org/downloads/ and tick "Add to PATH".
  pause
  exit /b 1
)
where ffmpeg >nul 2>nul || (%PY% -c "import imageio_ffmpeg" 2>nul || (
  echo Installing ffmpeg ^(first run only^)...
  %PY% -m pip install --user imageio-ffmpeg
))
%PY% app\server.py %*
if errorlevel 1 pause
