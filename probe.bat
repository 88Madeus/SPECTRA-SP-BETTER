@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv32\Scripts\python.exe (echo Сначала запустите install.bat & pause & exit /b 1)
echo Проверка приборов (только чтение). Закройте SpectraSP перед запуском!
.venv32\Scripts\python.exe -m spectra_better.tools.probe %*
pause
