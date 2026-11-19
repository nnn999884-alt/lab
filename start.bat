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
rem 휴대폰용 외부 주소: cloudflared 가 없으면 공식 배포 주소에서 받아 온다 (주소는 텔레그램으로)
if not exist tools\cloudflared.exe (
  echo [안내] 외부 주소 프로그램을 Cloudflare 공식 배포 주소에서 받는 중... 처음 한 번만 1분 정도 걸립니다.
  if not exist tools mkdir tools
  powershell -NoProfile -Command "$f='tools\cloudflared.exe'; try { Invoke-WebRequest https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe -OutFile $f -UseBasicParsing; if ((Get-Item $f).Length -lt 10MB) { throw 'too small' } } catch { Remove-Item $f -ErrorAction SilentlyContinue; exit 1 }"
)
if exist tools\cloudflared.exe (
  start /min "pennybot 외부 주소" cmd /c run_forever.bat tunnel
) else (
  echo.
  echo [안내] 외부 주소 프로그램을 받지 못해 휴대폰용 외부 주소는 꺼져 있습니다. 인터넷 연결이나 백신을 확인하고
  echo        tunnel.bat 을 실행해 보세요. 매매와 수집은 정상으로 돕니다.
  timeout /t 10 >nul
)
