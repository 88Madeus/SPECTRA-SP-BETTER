@echo off
chcp 65001 >nul
rem Создать (заново) ярлыки SpectraSP Better на рабочем столе
cd /d "%~dp0"
if not exist .venv32\Scripts\python.exe (echo Сначала запустите install.bat & pause & exit /b 1)
.venv32\Scripts\python.exe -m spectra_better.tools.shortcuts
pause
