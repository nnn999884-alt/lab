@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === pennybot 설치 ===

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

if not exist config.yaml copy config.example.yaml config.yaml >nul
if not exist .env (
  copy .env.example .env >nul
  echo.
  echo .env 파일을 메모장으로 엽니다. 토스 키와 SEC_USER_AGENT 를 넣고 저장하세요.
  notepad .env
)
echo.
echo 설치 끝. 다음: pennybot.bat accounts  (계좌번호 확인)
pause
