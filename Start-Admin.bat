@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1"
if errorlevel 1 (
  echo Remote Helper failed to start. See startup-error.log or the message above.
  pause
  exit /b 1
)
