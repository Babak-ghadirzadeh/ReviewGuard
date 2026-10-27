#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  printf 'Run Setup ReviewGuard.command first.\n'
  read -r answer
  exit 1
fi
open http://127.0.0.1:8765
exec .venv/bin/python -m reviewguard serve
