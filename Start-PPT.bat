@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo PowerPoint helper could not start: the project Python environment is missing.
  echo Expected: "%~dp0.venv\Scripts\python.exe"
  echo Set up this application's .venv and install requirements.txt, then try again.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0helper_launcher.py" --message-box
if errorlevel 1 (
  echo PowerPoint helper failed to start. See logs\ppt-helper-startup.log.
  echo Run Start-PPT.bat normally, not with Run as administrator.
  pause
  exit /b 1
)
exit /b 0
