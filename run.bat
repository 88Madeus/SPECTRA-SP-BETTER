@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv32\Scripts\pythonw.exe (echo Сначала запустите install.bat & pause & exit /b 1)
rem При первом запуске создаются ярлыки на рабочем столе, дальше — только быстрая проверка
.venv32\Scripts\python.exe -m spectra_better.tools.shortcuts --if-needed
start "" .venv32\Scripts\pythonw.exe -m spectra_better %*
