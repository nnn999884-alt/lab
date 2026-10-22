#!/usr/bin/env bash
# 수집기·매매 봇·대시보드를 한 번에. 오류로 꺼지면 30초 뒤 다시 켠다. 끄려면 Ctrl+C. 로그는 state/logs/
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "먼저 bash setup.sh 를 실행하세요."; exit 1; }
mkdir -p state/logs
forever() {
  while true; do
    .venv/bin/python -m pennybot "$@" >> "state/logs/$1.console.log" 2>&1
    echo "$(date) pennybot $1 종료됨 → 30초 뒤 재시작" >> "state/logs/$1.console.log"
    sleep 30
  done
}
forever collect & P1=$!
sleep 5
forever trade & P2=$!
forever dashboard & P3=$!
trap 'kill $P1 $P2 $P3 2>/dev/null; pkill -P $$ 2>/dev/null' EXIT INT TERM
echo "실행 중 — 대시보드 http://127.0.0.1:8050 (로그: state/logs/). 끄려면 Ctrl+C"
wait
