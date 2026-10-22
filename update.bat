@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 최신 코드 받기 (git clone 으로 받은 폴더에서만). .env / config.yaml / state 는 그대로 남는다
where git >nul 2>nul || (echo [오류] Git 이 없습니다. https://git-scm.com/download/win 에서 설치하세요. & pause & exit /b 1)
if not exist .git (
  echo [오류] 이 폴더는 ZIP 으로 받은 것 같습니다. README 의 'git 으로 받기' 를 따라 다시 받아 주세요.
  pause
  exit /b 1
)
echo 실행 중인 수집기·매매·대시보드 창을 먼저 닫아 주세요.
pause
git pull --ff-only || (echo [오류] 업데이트 실패. 코드 파일을 직접 고쳤다면 되돌린 뒤 다시 시도하세요. & pause & exit /b 1)
if exist .venv\Scripts\python.exe .venv\Scripts\python -m pip install -r requirements.txt -q
echo.
echo 업데이트 끝. start.bat 으로 다시 켜세요.
pause
