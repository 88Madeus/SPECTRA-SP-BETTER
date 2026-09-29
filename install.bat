@echo off
chcp 65001 >nul
rem Установка SpectraSP Better: виртуальное окружение с 32-битным Python 3.12
cd /d "%~dp0"
set PY=
py -3.12-32 -c "import sys" >nul 2>&1 && set PY=py -3.12-32
if "%PY%"=="" (
  echo Не найден 32-битный Python 3.12.
  echo Скачайте "Windows installer (32-bit)" для Python 3.12 с https://www.python.org/downloads/windows/
  echo и при установке отметьте "Add python.exe to PATH" и "py launcher".
  pause
  exit /b 1
)
if not exist .venv32\Scripts\python.exe (
  %PY% -m venv .venv32 || (echo Ошибка создания окружения & pause & exit /b 1)
)
.venv32\Scripts\python.exe -m pip install --upgrade pip
if exist wheels (
  .venv32\Scripts\python.exe -m pip install --no-index --find-links wheels -r requirements.txt
) else (
  .venv32\Scripts\python.exe -m pip install -r requirements.txt
)
if errorlevel 1 (echo Ошибка установки пакетов & pause & exit /b 1)
echo.
echo Готово. Запуск: run.bat   Проверка приборов: probe.bat   Без приборов: run_simulation.bat
echo При первом запуске run.bat на рабочем столе появятся ярлыки «SpectraSP Better».
pause
