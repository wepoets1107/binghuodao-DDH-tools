@echo off
setlocal
cd /d "%~dp0"
echo Starting Deribit DDH Workbench...
echo Open http://127.0.0.1:8888 in your browser.
powershell -ExecutionPolicy Bypass -File "%~dp0start.ps1"
