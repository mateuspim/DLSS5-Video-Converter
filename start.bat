@echo off
setlocal
cd /d "%~dp0"
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "GRADIO_ANALYTICS_ENABLED=False"
"%~dp0bin\python-3.13.15-embed-amd64\python.exe" "%~dp0app.py"
if errorlevel 1 pause
