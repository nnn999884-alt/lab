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
if exist tools\cloudflared.exe (
  start /min "pennybot 외부 주소" cmd /c run_forever.bat tunnel
) else (
  echo.
  echo [안내] 휴대폰용 외부 주소는 꺼져 있습니다. 이 폴더에 tools\cloudflared.exe 가 없어서입니다.
  echo        폴더를 새로 받았다면 tunnel.bat 을 한 번 실행하세요 ^(받아 오고 외부 주소를 텔레그램으로 보냅니다^).
  echo        이후로는 start.bat 이 외부 주소도 같이 켭니다.
  timeout /t 10 >nul
)
