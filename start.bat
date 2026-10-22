@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 먼저 setup.bat 을 실행하세요.
  pause
  exit /b 1
)
echo 수집기 / 매매 봇 / 대시보드를 각각 새 창으로 띄웁니다. 창을 닫으면 그 프로그램이 멈춥니다.
start "pennybot 수집기" cmd /k "set PYTHONUTF8=1&& .venv\Scripts\python -m pennybot collect"
timeout /t 5 /nobreak >nul
start "pennybot 매매" cmd /k "set PYTHONUTF8=1&& .venv\Scripts\python -m pennybot trade"
start "pennybot 대시보드" cmd /k "set PYTHONUTF8=1&& .venv\Scripts\python -m pennybot dashboard"
