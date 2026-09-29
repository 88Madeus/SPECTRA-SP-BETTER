@echo off
chcp 65001 >nul
rem Для компьютера без интернета: запустите на любом ПК с интернетом и Python,
rem затем скопируйте папку целиком (вместе с wheels) на лабораторный компьютер.
cd /d "%~dp0"
py -m pip download --only-binary=:all: --platform win32 --python-version 3.12 -d wheels -r requirements.txt
pause
