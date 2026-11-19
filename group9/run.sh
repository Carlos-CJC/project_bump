#!/usr/bin/env sh
# macOS / Linux 入口：优先项目内 .venv，其次 pyenv，再退回 python3。
set -eu
cd "$(dirname "$0")"

if [ -x .venv/bin/python ]; then
    exec .venv/bin/python run.py "$@"
fi

if command -v pyenv >/dev/null 2>&1; then
    exec pyenv exec python run.py "$@"
fi

exec python3 run.py "$@"
