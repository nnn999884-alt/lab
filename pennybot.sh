#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "먼저 bash setup.sh 를 실행하세요."; exit 1; }
exec .venv/bin/python -m pennybot "$@"
