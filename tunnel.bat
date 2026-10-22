@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 대시보드를 외부 주소(https://….trycloudflare.com)로 연다. 주소는 켤 때마다 바뀌고 텔레그램으로 온다.
if not exist .venv\Scripts\python.exe (
  echo 먼저 setup.bat 을 실행하세요.
  pause
  exit /b 1
)
if not exist tools\cloudflared.exe (
  echo cloudflared 받는 중...
  if not exist tools mkdir tools
  powershell -NoProfile -Command "Invoke-WebRequest https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe -OutFile tools\cloudflared.exe -UseBasicParsing"
)
run_forever.bat tunnel
