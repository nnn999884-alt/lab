#!/usr/bin/env bash
# 최신 코드 받기. .env / config.yaml / state 는 그대로 남는다
set -e
cd "$(dirname "$0")"
git pull --ff-only
[ -x .venv/bin/python ] && .venv/bin/python -m pip install -r requirements.txt -q
echo "업데이트 끝. ./start.sh 로 다시 켜세요."
