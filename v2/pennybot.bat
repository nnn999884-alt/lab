@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 먼저 setup.bat 을 실행하세요.
  pause
  exit /b 1
)
if "%~1"=="" (
  echo 사용법: 이 폴더에서 명령 프롬프트를 열고  pennybot.bat 명령
  echo   pennybot.bat status               지금 상태 (일꾼 응답, 마지막 체결)
  echo   pennybot.bat grade ..\state\market.db   기존 알림을 같은 기준으로 채점
  echo   pennybot.bat replay               저장된 1분봉 재생
  echo 전체를 켜려면 start.bat
  pause
  exit /b 0
)
set PYTHONUTF8=1
.venv\Scripts\python -m pennybot2 %*
