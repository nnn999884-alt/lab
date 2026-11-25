@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === pennybot2 설치 ===

set PY=
where py >nul 2>nul && set PY=py -3
if "%PY%"=="" where python >nul 2>nul && set PY=python
if "%PY%"=="" (
  echo [오류] 파이썬이 없습니다. https://www.python.org/downloads/ 에서 3.12 를 설치하세요.
  echo        설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo [오류] 파이썬 3.10 이상이 필요합니다. 3.12 를 설치하세요.
  pause
  exit /b 1
)

if not exist .venv (
  echo 가상환경 만드는 중...
  %PY% -m venv .venv || (echo [오류] 가상환경 생성 실패 & pause & exit /b 1)
)
echo 패키지 설치 중... (1~2분)
.venv\Scripts\python -m pip install --upgrade pip -q
.venv\Scripts\python -m pip install -r requirements.txt -q || (echo [오류] 패키지 설치 실패 & pause & exit /b 1)

if not exist .env (
  > .env echo LS_APP_KEY=
  >> .env echo LS_APP_SECRET=
  >> .env echo PENNYBOT_WEBHOOK=
  >> .env echo PENNYBOT_TELEGRAM_TOKEN=
  >> .env echo PENNYBOT_TELEGRAM_CHAT=
)
echo.
echo 설치 끝. 메모장이 열리면 LS_APP_KEY, LS_APP_SECRET 을 채우고 저장하세요 (.env 는 절대 공유·커밋 금지).
notepad .env
echo 그다음 start.bat 더블클릭 (상태 확인: pennybot.bat status)
pause
