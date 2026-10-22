#!/usr/bin/env bash
# 수집기·매매 봇·대시보드를 한 번에. 끄려면 이 터미널에서 Ctrl+C. 로그는 state/logs/
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "먼저 bash setup.sh 를 실행하세요."; exit 1; }
mkdir -p state/logs
.venv/bin/python -m pennybot collect   >> state/logs/collect.log 2>&1 & P1=$!
sleep 5
.venv/bin/python -m pennybot trade     >> state/logs/trade.log 2>&1 & P2=$!
.venv/bin/python -m pennybot dashboard >> state/logs/dashboard.log 2>&1 & P3=$!
trap 'kill $P1 $P2 $P3 2>/dev/null' EXIT INT TERM
sleep 3
(command -v open >/dev/null && open http://127.0.0.1:8050) || echo "브라우저에서 http://127.0.0.1:8050"
echo "실행 중 (로그: state/logs/). 끄려면 Ctrl+C"
wait
