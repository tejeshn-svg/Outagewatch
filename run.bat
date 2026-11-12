@echo off
REM ===================================================================
REM OutageWatch -- double-click launcher for Windows
REM Starts the collector in its own window, then the web app.
REM ===================================================================

cd /d "%~dp0"

echo Checking dependencies...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo Could not install dependencies. Is Python on your PATH?
  echo Try:  python --version
  pause
  exit /b 1
)

echo Running one collection pass...
python collector.py --once
if errorlevel 1 (
  echo.
  echo The collector failed. Scroll up for the reason.
  pause
  exit /b 1
)

echo Starting the collector in a separate window ^(polls every 2 minutes^)...
start "OutageWatch collector" cmd /k python collector.py --interval 120

echo.
echo Starting the web app. Open http://127.0.0.1:5000
echo Press Ctrl+C here to stop the web app.
echo.
python app.py

pause
