@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 먼저 setup.bat 을 실행하세요.
  pause
  exit /b 1
)
set PYTHONUTF8=1
title pennybot2
echo pennybot2 시작: 허브 + 일꾼(시세·일봉·감지·알림·감시)을 각각 따로 켭니다. 끄려면 이 창에서 Ctrl+C.
echo 일꾼 하나가 죽어도 이 창이 그것만 다시 켭니다. 상태 확인은 다른 창에서  pennybot.bat status
:loop
.venv\Scripts\python -m pennybot2 run
if errorlevel 3 (
  echo [안내] 이미 실행 중입니다. 새 창을 닫으세요.
  pause
  exit /b 0
)
echo 감독자가 종료됨 (코드 %errorlevel%) — 10초 뒤 다시 시작. 멈추려면 Ctrl+C.
timeout /t 10 >nul
goto loop
