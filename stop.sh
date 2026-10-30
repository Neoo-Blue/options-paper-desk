#!/usr/bin/env bash
cd "$(dirname "$0")"
for n in engine dashboard sync; do
  pidf="data/${n}.supervisor.pid"
  if [ -f "$pidf" ]; then
    pid=$(cat "$pidf")
    kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null
    rm -f "$pidf"; echo "$n stopped"
  fi
done
true
