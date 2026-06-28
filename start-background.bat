@echo off
setlocal
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0start-background.ps1"
echo Open http://127.0.0.1:8888 in your browser.
