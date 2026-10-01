#!/usr/bin/env bash
# Start the FMS server and the vision process together. Ctrl+C stops both.
#   ./run.sh                                  # real cameras (config/vision.yaml)
#   ./run.sh ../config/vision.mock.yaml       # mock fuel, no model or cameras
#   ./run.sh ../config/vision.yaml --preview  # extra args go to run_vision.py
cd "$(dirname "$0")"
source .venv/bin/activate
[ -f config/event.yaml ] || python -m fms.init || exit 1   # first run: create configs + PINs
[ -f tba_secrets.sh ] && source tba_secrets.sh
VCFG="${1:-../config/vision.yaml}"
shift 2>/dev/null
KEEPAWAKE=""
command -v caffeinate >/dev/null && KEEPAWAKE="caffeinate -dimsu"   # macOS: stop sleep
trap 'kill 0' EXIT
$KEEPAWAKE python -m fms.server &
sleep 2
(cd vision && python run_vision.py --config "$VCFG" "$@") &
wait
