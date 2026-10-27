#!/bin/sh
set -eu
cd "$(dirname "$0")"
./scripts/install.sh
printf '\nPress Return to close.\n'
read -r answer
