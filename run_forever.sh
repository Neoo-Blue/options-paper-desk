#!/usr/bin/env bash
# Supervisor loop: restarts the given component if it exits/crashes. Usage: run_forever.sh engine|dashboard|sync
cd "$(dirname "$0")"
NAME="$1"
case "$NAME" in
  engine) CMD=(.venv/bin/python -u engine.py) ;;
  dashboard) CMD=(.venv/bin/python -u dashboard.py) ;;
  sync) CMD=(./sync_state.sh --loop) ;;
  *) echo "usage: $0 engine|dashboard|sync"; exit 1 ;;
esac
echo $$ > "data/${NAME}.supervisor.pid"
while true; do
  echo "$(date -Is) starting $NAME" >> "logs/${NAME}.supervisor.log"
  "${CMD[@]}" >> "logs/${NAME}.out" 2>&1
  echo "$(date -Is) $NAME exited with $? - restarting in 10s" >> "logs/${NAME}.supervisor.log"
  sleep 10
done
