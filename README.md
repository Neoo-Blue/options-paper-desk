# Options Paper Desk ($50 short-dated options simulator)

**Pure simulation.** It has no broker login, places no real orders, and never touches real money.
Data: Yahoo Finance through `yfinance`. **Quotes are delayed about 15 minutes.**

## Files
| Path | Purpose |
|---|---|
| `config.py` | All constants: risk limits, fees and their source URLs, NYSE 2026/2027 holidays and early closes, strategy parameters |
| `engine.py` | Engine loop (`--once`, `--reset`) |
| `fees.py` | Robinhood fee model |
| `market_calendar.py` | Market hours, holidays, entry window, and time stop |
| `data.py` | yfinance wrapper. Never fills in or guesses a missing number |
| `dashboard.py` | Flask dashboard on `0.0.0.0:8050`, auto-refreshes every 30 s. `/api/state` returns JSON |
| `report.py` | Prints a 3-line daily summary |
| `stats.py` | Equity, P&L, win rate, and max drawdown math |
| `dryrun.py` | Runs selection, fills, fees, and risk checks against real Yahoo chains. Uses a test engine with `persist=False`, so it never writes `data/state.json` |
| `tests/test_engine.py` | Unit checks using a fake data provider and a temporary state file |
| `ci_runner.py`, `.github/workflows/engine.yml` | GitHub Actions runner: run until hand-off time, commit state |
| `sync_state.sh` | Local viewer: pull state files from the repo |
| `run_forever.sh`, `start.sh`, `stop.sh` | Supervisor scripts |
| `data/state.json` | Saved state: cash, positions, trades, equity history, and daily records. Written atomically |
| `logs/engine-tail.log` | Committed rolling engine log (last 1500 lines). `logs/engine.log` is the per-run log. `logs/*.supervisor.log` and `logs/*.out` hold restart history and stdout |

## Where it runs: GitHub Actions (free for public repos)
The engine runs in `.github/workflows/engine.yml` on GitHub-hosted runners, so it watches the whole session
(9:30-16:00 ET) without depending on any personal machine staying awake. No secrets are used: Yahoo data is free.
- **Schedule:** weekday cron starts (UTC; see the table in the workflow) for a morning job and a queued afternoon job.
  Each job runs `ci_runner.py`, which resumes from the latest committed `data/state.json` and runs until
  min(start + 5h40m, close + 15 min) ET, so it always hands off before GitHub's 6-hour limit. Outside that window
  (late start, weekend, holiday, manual run after the close) it does one iteration (settles expirations) and exits.
- **One writer:** the workflow's concurrency group (`cancel-in-progress: false`) lets exactly one job run; later
  triggers wait in the queue and start as soon as the running job exits. Redundant triggers cover late/skipped cron.
- **Persistence:** every 3 minutes and at exit the job commits `data/state.json`, `data/dashboard.json`,
  `docs/dashboard.html` (static dashboard snapshot) and `logs/engine-tail.log`. A rejected push is retried on top
  of the new `main` (the runner's data files win; it is their only writer).
- **Manual run:** Actions tab -> paper-engine -> Run workflow (or `gh workflow run engine.yml`).
- **Missed checks** across hand-offs or runner delays are still recorded (`missed_checks` in the state).

## Local copy (viewer only)
```
./start.sh            # idempotent; starts the dashboard (port 8050) and sync_state.sh --loop (pulls state every 60 s)
./stop.sh             # stops them
.venv/bin/python report.py          # pulls the latest state first; --no-pull to skip
.venv/bin/python -m unittest discover -s tests
.venv/bin/python dryrun.py
.venv/bin/python engine.py --reset  # back up state and reset to $50 (then commit+push). Only way to clear a FULL STOP
```
- `sync_state.sh` only checks out the data files from `origin/main`; it never touches code or pushes.
- **Never run a second engine** while the GitHub workflow is enabled (`OPD_BOX_ENGINE=1 ./start.sh` exists for
  running locally *instead*; disable the workflow first).
- **Crash recovery:** `run_forever.sh` restarts a component 10 s after it exits. An `fcntl` lock blocks a second local engine.

## Fees (Robinhood, stock and ETF options, per contract, per side)
Sources were fetched on 2026-10-27. The fee schedule PDF is document id 20260929-5969628-19014374.
- https://cdn.robinhood.com/assets/robinhood/legal/RHF+Fee+Schedule.pdf
- https://robinhood.com/us/en/support/articles/trading-fees-on-robinhood/

| Fee | Amount | Side |
|---|---|---|
| Commission | $0 | — |
| Options Regulatory + OCC Clearing Fee | $0.04/contract | buys and sells |
| CAT fee | $0.0003/contract. Under $0.01 rounds down to $0; otherwise rounds to the nearest penny | buys and sells |
| SEC fee | $20.60 per $1M of sale principal, rounded **up** to the penny. Applies to every stock/ETF option sale regardless of size | sells |
| FINRA TAF | $0.00329/contract, rounded to the nearest penny (under $0.01 becomes $0), max $9.79. **Paused 2026-10-01 through 2026-12-31 per FINRA**; the engine handles the pause by date | sells |
| Expiry settlement | No transaction fee charged | — |

**Example for 1 contract:**
- Buy at $0.12: cost = $12.00 + $0.04 = **$12.04**
- Sell at $0.18: proceeds = $18.00 − $0.04 − $0.01 SEC = **$17.95**

## Risk limits (hard constants in `config.py`; the engine never changes them)
- **Starting cash:** $50.00.
- **Position size:** one contract, long calls or puts only, at most one open position. The total cost (ask×100 + fees) must be ≤ cash **and** ≤ 40% of current equity.
- **Daily loss cap:** once equity ≤ 85% of start-of-day equity, the engine sells the open position at the bid if a real bid exists, and opens nothing else that day.
- **Full stop:** once equity ≤ $25.00, the engine sells any open position and opens nothing again until `engine.py --reset`.
- **How equity is measured:** cash plus the value of open positions if sold now (bid×100 minus sell fees). This is P&L after spreads and fees.
- **When there's no quote:** the position keeps its last real bid and is flagged "no quote" in the log and the dashboard. If the bid is zero, the position is valued at $0. The engine never invents a mark.

## Strategy (intraday momentum, ~0–7 DTE)
**Universe:** SPY, QQQ, IWM, AMD, PLTR, SOFI, F, NIO. All have weekly expiries.

**Polling:** every 90 s during NYSE hours (9:30–16:00 ET). Every ≤5 min when the market is closed. Weekends, holidays, and early closes (13:00 ET) come from the hardcoded calendar.

**Entry signal.** Entries are allowed from 10:00 ET until 60 minutes before the close (15:00 ET, or 12:00 on early-close days). The engine uses today's 1-minute bars:
- **Call signal:** the move from today's open is ≥ +0.40% for ETFs or ≥ +1.00% for stocks, **and** the last price is above the session VWAP **and** above the 20-bar EMA.
- **Put signal:** the mirror image.
- The strongest signal (move ÷ threshold) is tried first.

**Contract choice.**
- Expiries 0–7 calendar days out, nearest expiry first.
- OTM strikes within 5% of spot, up to 10 per expiry, nearest-the-money first.
- The first contract that passes **all** of these filters and fits the cap is bought at the **ask**:
  - bid > 0 and ask > 0
  - ask ≥ $0.05
  - volume ≥ 100
  - open interest ≥ 100
  - (ask − bid) / ask ≤ 30%

**Exits.** Sells are at the **bid** and need a real bid > 0. The engine sells when any of these happens:
- **Take profit:** net value ≥ +50% of entry cost.
- **Stop loss:** net value ≤ −40%.
- **Time stop:** at 15:45 ET (12:45 on early-close days) on the expiry day.
- **Multi-day limit:** at the time stop on the 3rd trading day held.

**Expiry.** If the engine couldn't sell (no bid), the contract settles at intrinsic value using the underlying's official daily close on the expiry date. If Yahoo doesn't have that close yet, the engine waits and retries rather than guessing.

**Other limits:** at most 3 entries per day, and a 30-minute cooldown before re-entering the same underlying.

**Honest notes.**
- With $50 and a 40% cap, a position can cost at most about $20, so the premium must be ≤ about $0.19. On the 2026-10-27 close, near-the-money 0–3 DTE SPY, QQQ, and AMD calls last traded at $1.70–$6.49 ($170–$649 per contract). The engine will mostly end up in **cheap, further-OTM contracts**: ETFs a few strikes out, or SOFI and F, where calls last traded at $0.08–$0.34. These are low-probability contracts that often expire worthless.
- Spreads and fees take a large share of a $0.10–$0.20 option: a $0.02 spread is 10–20% of the premium.
- Expect many days with no trade because no contract passes the filters and fits the cap.
- Yahoo quotes are delayed, so real fills would differ.
- Yahoo's open interest after hours was 0 on almost every contract. If it also comes back as 0 during the session, the OI ≥ 100 filter will block entries. Check `logs/engine.log` for lines like `skip ...: OI` and lower `MIN_OPEN_INTEREST` if needed.

## Robustness (added 2026-10-28; no risk limits or strategy parameters changed)
- **Quote fetches:** yfinance's HTTP timeout is capped at 8 s (it was 30 s). Each data call also has a 12 s hard limit (`data.guarded`).
- **Managing an open position:** the engine fetches only that contract's own expiry chain, bypassing the cache.
- **Fast retries:** if a quote fetch fails or comes back with no bid/ask while a position is open, the next loop runs after 10 s, then 12 s, then every 15 s until a good quote arrives. Normally it waits 90 s. The first good quote immediately re-runs take profit, stop loss, the daily cap and the full stop. The last real mark is kept and flagged; no price is invented.
- **Missed checks:** the loop saves the wall-clock time of every pass. If a pass comes more than 2× the 90 s poll late (or more than the requested sleep + 90 s), and over 90 s of market time was missed, the engine logs `MISSED CHECKS` with start and end times. It records the gap in `state.missed_checks`. This also catches time the process was down, because the last loop time is saved.
- **Stale quotes:** an open position whose last good quote is older than 3 minutes during market hours is flagged STALE on the dashboard and in the tick log. When a stale period ends (a quote recovers or the position closes), it is saved to `state.stale_periods` if it lasted more than 3 minutes.
- **Sleeping:** sleeps happen in 5 s chunks and stop when either the wall-clock or the monotonic deadline passes. A paused or frozen box therefore resumes within about 5 s of waking.
- **Where it shows up:** the dashboard has "Missed checks today" and "Stale-quote periods today" tables. `report.py` prints a 4th "Data health" line.
- **Settings:** all of these are operational constants at the bottom of `config.py`.
