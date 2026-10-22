@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 먼저 setup.bat 을 실행하세요.
  pause
  exit /b 1
)
echo 수집기 / 매매 봇 / 대시보드를 각각 새 창으로 띄웁니다.
echo 오류로 꺼지면 30초 뒤 자동으로 다시 켜집니다. 그만두려면 창을 닫으세요. 기록: state\logs
start "pennybot 수집기" cmd /c run_forever.bat collect
timeout /t 5 /nobreak >nul
start "pennybot 매매" cmd /c run_forever.bat trade
start "pennybot 대시보드" cmd /c run_forever.bat dashboard
