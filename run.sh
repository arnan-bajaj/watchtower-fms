#!/usr/bin/env bash
cd "$(dirname "$0")"
source .venv/bin/activate
[ -f tba_secrets.sh ] && source tba_secrets.sh
VCFG="${1:-../config/vision.yaml}"
trap 'kill 0' EXIT            # Ctrl+C stops both
caffeinate -dimsu python -m fms.server &
sleep 2
(cd vision && python run_vision.py --config "$VCFG") &
wait