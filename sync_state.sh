#!/usr/bin/env bash
# Box side: pull the engine's state from GitHub (the engine runs on GitHub Actions now).
# Only the data files are updated (code in this checkout is never touched), so local edits are safe.
#   ./sync_state.sh          one pull
#   ./sync_state.sh --loop   pull every 60 s (run by run_forever.sh sync)
cd "$(dirname "$0")"
FILES="data/state.json data/dashboard.json docs/dashboard.html logs/engine-tail.log"
once() {
  local err
  if err=$(timeout 60 git fetch --quiet origin main 2>&1) && err=$(git checkout origin/main -- $FILES 2>&1); then
    git reset --quiet -- $FILES >/dev/null 2>&1   # leave them unstaged
    printf '{"ok": true, "last_ok": "%s", "commit": "%s", "repo": "%s"}\n' "$(date -Is)" \
      "$(git rev-parse --short origin/main)" "$(git remote get-url origin | sed 's#https://[^@]*@#https://#')" > data/sync_status.json.tmp
    mv data/sync_status.json.tmp data/sync_status.json
    return 0
  fi
  err=$(echo "$err" | tr '\n"' ' ' | cut -c1-200)
  python3 - "$err" <<'PY'
import json, sys, os
p = "data/sync_status.json"
try:
    d = json.load(open(p))
except Exception:
    d = {}
d.update(ok=False, error=sys.argv[1])
json.dump(d, open(p + ".tmp", "w")); os.replace(p + ".tmp", p)
PY
  echo "$(date -Is) sync failed: $err" >&2
  return 1
}
if [ "$1" = "--loop" ]; then
  while true; do once; sleep 60; done
else
  once
fi
