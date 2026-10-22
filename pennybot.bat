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
  echo   pennybot.bat dashboard --demo   예시 데이터로 대시보드 미리보기
  echo   pennybot.bat accounts           계좌번호 확인
  echo   pennybot.bat check              토스 연결 점검
  echo   pennybot.bat backtest           백테스트
  echo 수집기+매매+대시보드를 한 번에 켜려면 start.bat 더블클릭
  pause
  exit /b 0
)
set PYTHONUTF8=1
.venv\Scripts\python -m pennybot %*
