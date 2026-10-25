@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 먼저 setup.bat 을 실행하세요.
  pause
  exit /b 1
)
echo 수집기 / 매매 봇 / 대시보드를 각각 새 창으로 띄우되, 화면에는 안 보이게 최소화합니다 (작업표시줄에서 확인 가능).
echo 오류로 꺼지면 30초 뒤 자동으로 다시 켜집니다. 그만두려면 작업표시줄에서 창을 열어 닫으세요. 기록: state\logs
start /min "pennybot 수집기" cmd /c run_forever.bat collect
timeout /t 5 /nobreak >nul
start /min "pennybot 매매" cmd /c run_forever.bat trade
start /min "pennybot 대시보드" cmd /c run_forever.bat dashboard
rem tunnel.bat 을 한 번 실행해 cloudflared 를 받아 뒀으면 외부 주소도 연다 (주소는 텔레그램으로)
if exist tools\cloudflared.exe start /min "pennybot 외부 주소" cmd /c tunnel.bat
