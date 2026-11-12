#!/bin/sh
# Thin wrapper: ./run.sh [options]  ==  python3 run_local.py [options]
cd "$(dirname "$0")" && exec "${PYTHON:-python3}" run_local.py "$@"
