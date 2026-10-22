@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 윈도우에 로그인하면 start.bat 이 자동으로 실행되게 등록 (작업 스케줄러)
schtasks /create /tn "pennybot" /sc onlogon /tr "\"%~dp0start.bat\"" /rl limited /f
if errorlevel 1 (
  echo [오류] 등록 실패. 이 파일을 마우스 오른쪽 버튼 - 관리자 권한으로 실행해 보세요.
) else (
  echo 등록 완료: 다음 로그인부터 자동으로 켜집니다. 끄려면 autostart_off.bat
)
pause
