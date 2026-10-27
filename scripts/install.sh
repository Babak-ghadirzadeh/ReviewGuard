#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[pdf]'
.venv/bin/python -m reviewguard doctor
printf '\nSetup complete. Run a journal pass with .venv/bin/reviewguard monitor --config config/monitor.json\n'
