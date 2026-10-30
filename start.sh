#!/usr/bin/env bash
# Idempotent: starts the box-side supervisors only if not already running.
# The trading engine now runs on GitHub Actions (see .github/workflows/engine.yml).
# On the box we run only the dashboard plus a sync loop that pulls the engine's state from GitHub.
# Set OPD_BOX_ENGINE=1 to also run the engine locally (do NOT do this while the GitHub runner is active:
# there must only ever be one writer of data/state.json).
cd "$(dirname "$0")"
mkdir -p data logs
names="dashboard sync"
[ "${OPD_BOX_ENGINE:-0}" = "1" ] && names="engine $names"
for n in $names; do
  pidf="data/${n}.supervisor.pid"
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
    echo "$n already running (supervisor pid $(cat "$pidf"))"
  else
    nohup setsid ./run_forever.sh "$n" >/dev/null 2>&1 < /dev/null &
    sleep 1
    echo "$n started (supervisor pid $(cat "$pidf"))"
  fi
done
