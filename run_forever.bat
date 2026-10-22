@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 사용: run_forever.bat collect  (프로그램이 죽으면 30초 뒤 다시 켠다)
set PYTHONUTF8=1
title pennybot %1
:loop
.venv\Scripts\python -m pennybot %*
echo.
echo [%date% %time%] pennybot %1 종료됨 - 30초 뒤 다시 시작 (그만두려면 이 창을 닫으세요)
timeout /t 30 /nobreak >nul
goto loop
