@echo off
chcp 65001 >nul
schtasks /delete /tn "pennybot" /f
echo 자동 실행을 해제했습니다.
pause
