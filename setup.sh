#!/usr/bin/env bash
# 맥/리눅스 설치: 터미널에서  bash setup.sh
set -e
cd "$(dirname "$0")"
PY=$(command -v python3 || true)
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "[오류] 파이썬 3.10 이상이 필요합니다. https://www.python.org/downloads/ 에서 3.12 를 설치하세요."
  exit 1
fi
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt -q
[ -f config.yaml ] || cp config.example.yaml config.yaml
if [ ! -f .env ]; then
  cp .env.example .env
  echo ".env 를 엽니다. 토스 키와 SEC_USER_AGENT 를 넣고 저장하세요."
  (command -v open >/dev/null && open -e .env) || "${EDITOR:-nano}" .env
fi
echo "설치 끝. 다음: ./pennybot.sh accounts"
