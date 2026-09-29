@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv32\Scripts\pythonw.exe (echo Сначала запустите install.bat & pause & exit /b 1)
start "" .venv32\Scripts\pythonw.exe -m spectra_better --simulate %*
