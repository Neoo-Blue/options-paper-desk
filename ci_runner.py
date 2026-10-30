"""GitHub Actions entry point: run the paper engine until a deadline, committing state to the repo as it goes.

PAPER ONLY. No broker, no real orders, no secrets: quotes come from Yahoo Finance (delayed ~15 min).

Each job:
  1. hard-syncs the checkout to origin/main, so it resumes from the latest committed data/state.json
     (scheduled jobs that waited in the concurrency queue would otherwise start from an older commit);
  2. runs the normal engine loop until min(start + RUNNER_MAX_JOB_S, today's close + RUNNER_AFTER_CLOSE_MIN);
     outside that window it does a single iteration (settles expirations) and exits;
  3. commits data/state.json, data/dashboard.json, docs/dashboard.html and logs/engine-tail.log every
     RUNNER_SYNC_EVERY_S and at exit. If the push is rejected it re-bases the data commit on the new origin/main
     (this job is the only writer of those files, so its copy wins) and retries.

The workflow's concurrency group guarantees one runner at a time.
"""
import argparse
import json
import os
import subprocess
import sys
import time as _time
from datetime import datetime, timedelta

import config
import engine
import market_calendar as mc
import stats
from market_calendar import ET

log = engine.log
DATA_FILES = ["data/state.json", "data/dashboard.json", "docs/dashboard.html", "logs/engine-tail.log"]
DASH_HTML = os.path.join(config.BASE_DIR, "docs", "dashboard.html")
DASH_JSON = os.path.join(config.BASE_DIR, "data", "dashboard.json")


def git(*args, check=False, timeout=90):
    r = subprocess.run(["git", *args], cwd=config.BASE_DIR, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:500]}")
    return r


def pull_latest():
    git("fetch", "--quiet", "origin", "main", check=True)
    git("reset", "--hard", "origin/main", check=True)
    log.info("runner: synced checkout to origin/main %s", git("rev-parse", "--short", "HEAD").stdout.strip())


def deadline_for(now):
    d = now.date()
    if not mc.is_trading_day(d):
        return None
    end = datetime.combine(d, mc.close_time(d), ET) + timedelta(minutes=config.RUNNER_AFTER_CLOSE_MIN)
    if now >= end:
        return None
    return min(now + timedelta(seconds=config.RUNNER_MAX_JOB_S), end)


class Publisher:
    def __init__(self, old_tail):
        self.old_tail = old_tail          # tail committed by earlier jobs
        self.last = 0.0

    def write_artifacts(self, eng):
        try:
            with open(config.LOG_FILE) as f:
                mine = f.readlines()
        except OSError:
            mine = []
        lines = (self.old_tail + mine)[-config.LOG_TAIL_LINES:]
        os.makedirs(os.path.dirname(config.LOG_TAIL_FILE), exist_ok=True)
        with open(config.LOG_TAIL_FILE, "w") as f:
            f.writelines(lines)
        s = eng.s
        with open(DASH_JSON, "w") as f:
            json.dump({"generated": datetime.now(ET).isoformat(), "label": config.DATA_SOURCE_LABEL,
                       "stats": stats.compute(s)}, f, indent=1)
        import dashboard  # flask only needed to render the static snapshot
        html = dashboard.app.test_client().get("/").get_data(as_text=True)
        os.makedirs(os.path.dirname(DASH_HTML), exist_ok=True)
        with open(DASH_HTML, "w") as f:
            f.write(html)

    def publish(self, eng, now, force=False):
        if not force and _time.monotonic() - self.last < config.RUNNER_SYNC_EVERY_S:
            return
        self.last = _time.monotonic()
        eng.save()
        self.write_artifacts(eng)
        git("add", *DATA_FILES, check=True)
        if git("diff", "--cached", "--quiet").returncode == 0:
            return
        msg = f"state {now:%Y-%m-%d %H:%M} ET: cash ${eng.s['cash']:.2f}, open {len(eng.s['positions'])}"
        git("commit", "--quiet", "-m", msg, check=True)
        for attempt in range(6):
            r = git("push", "--quiet", "origin", "HEAD:main")
            if r.returncode == 0:
                log.info("runner: pushed %s", msg)
                return
            log.warning("runner: push rejected (attempt %d): %s", attempt + 1, r.stderr.strip()[:200])
            # Someone else pushed (e.g. a code change). Equivalent of `git pull --rebase` where our data files win:
            # move onto the new origin/main, keep our working-tree data files, and recommit them.
            git("fetch", "--quiet", "origin", "main", check=True)
            git("reset", "--quiet", "--mixed", "origin/main", check=True)
            git("add", *DATA_FILES, check=True)
            if git("diff", "--cached", "--quiet").returncode == 0:
                return
            git("commit", "--quiet", "-m", msg, check=True)
            _time.sleep(3 + 4 * attempt)
        log.error("runner: could not push state after retries; will try again at the next sync")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-git", action="store_true", help="local test: do not fetch/commit/push")
    a = ap.parse_args()
    engine.setup_logging()
    if not a.no_git:
        pull_latest()
    try:
        with open(config.LOG_TAIL_FILE) as f:
            old_tail = f.readlines()
    except OSError:
        old_tail = []
    from data import YahooProvider
    eng = engine.Engine(YahooProvider())
    now = datetime.now(ET)
    dl = deadline_for(now)
    log.info("runner start (PAPER ONLY, GitHub Actions). %s. cash $%.2f, positions %d, full_stop=%s; %s",
             config.DATA_SOURCE_LABEL, eng.s["cash"], len(eng.s["positions"]), eng.s["full_stop"],
             f"running until {dl:%Y-%m-%d %H:%M} ET" if dl else "outside session window: one iteration then exit")
    pub = Publisher(old_tail)
    hook = None if a.no_git else pub.publish
    try:
        engine.run_loop(eng, once=dl is None, deadline=dl, on_iter=hook)
    finally:
        log.info("runner exit: cash $%.2f, positions %d", eng.s["cash"], len(eng.s["positions"]))
        if a.no_git:
            eng.save()
            pub.write_artifacts(eng)
        else:
            pub.publish(eng, datetime.now(ET), force=True)


if __name__ == "__main__":
    sys.exit(main())
