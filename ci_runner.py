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


def session_end(d):
    return datetime.combine(d, mc.close_time(d), ET) + timedelta(minutes=config.RUNNER_AFTER_CLOSE_MIN)


def settlement_pending(eng, now):
    """An open position has expired (expiry today after the close, or earlier) and is not settled yet."""
    d = now.date()
    after_close = now >= datetime.combine(d, mc.close_time(d), ET) if mc.is_trading_day(d) else True
    return any(p["expiry"] < d.isoformat() or (p["expiry"] == d.isoformat() and after_close)
               for p in eng.s["positions"])


def should_chain(now, ran_s):
    """Start the next run (self-chaining) when this job stops at its time cap while the session window is still
    open. Never on weekends/holidays/after the close (deadline_for is None there), never after a short run."""
    if ran_s < config.RUNNER_CHAIN_MIN_RUN_S:
        return False
    return deadline_for(now + timedelta(minutes=config.RUNNER_CHAIN_LEAD_MIN)) is not None


def chain_next(reason):
    """Queue the next run of this workflow. Uses the job's GITHUB_TOKEN (workflow_dispatch is the one event a
    GITHUB_TOKEN may trigger). The concurrency group queues it until this job exits."""
    if not os.environ.get("GH_TOKEN"):
        log.warning("runner: cannot self-chain (%s): GH_TOKEN not set", reason)
        return False
    r = subprocess.run(["gh", "workflow", "run", config.RUNNER_WORKFLOW_FILE, "--ref", "main"],
                       cwd=config.BASE_DIR, capture_output=True, text=True, timeout=60)
    if r.returncode:
        log.error("runner: self-chain failed (%s): %s", reason, (r.stderr or r.stdout).strip()[:300])
        return False
    log.info("runner: self-chained next run (%s)", reason)
    return True


def deadline_for(now):
    d = now.date()
    if not mc.is_trading_day(d):
        return None
    end = session_end(d)
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
    t0 = _time.monotonic()
    try:
        engine.run_loop(eng, once=dl is None, deadline=dl, on_iter=hook)
        # Last job of the day: if an expiry still needs the official close, keep waiting for it a while.
        now = datetime.now(ET)
        if settlement_pending(eng, now) and mc.is_trading_day(now.date()):
            grace = datetime.combine(now.date(), mc.close_time(now.date()), ET) + timedelta(minutes=config.RUNNER_SETTLE_GRACE_MIN)
            cap = datetime.now(ET) + timedelta(seconds=config.RUNNER_MAX_JOB_S - (_time.monotonic() - t0))
            until = min(grace, cap)
            if now < until:
                log.info("runner: expiry settlement pending; waiting for the official close until %s ET", f"{until:%H:%M}")
                engine.run_loop(eng, deadline=until, on_iter=hook, stop_fn=lambda e, n: not settlement_pending(e, n))
    finally:
        log.info("runner exit: cash $%.2f, positions %d", eng.s["cash"], len(eng.s["positions"]))
        if a.no_git:
            eng.save()
            pub.write_artifacts(eng)
        else:
            pub.publish(eng, datetime.now(ET), force=True)
            now = datetime.now(ET)
            if should_chain(now, _time.monotonic() - t0):
                chain_next(f"time cap reached at {now:%H:%M} ET; session window open until "
                           f"{session_end(now.date()):%H:%M} ET")


if __name__ == "__main__":
    sys.exit(main())
