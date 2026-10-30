"""All tunable constants for the options paper desk.

RISK LIMITS are hard constants. The engine reads them; it never writes or changes them.
This is a PURE SIMULATION: no broker logins, no real orders, no real money.
"""
from datetime import date, time
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("OPD_STATE_FILE", os.path.join(BASE_DIR, "data", "state.json"))
LOG_FILE = os.environ.get("OPD_LOG_FILE", os.path.join(BASE_DIR, "logs", "engine.log"))
LOCK_FILE = os.path.join(BASE_DIR, "data", "engine.lock")

DATA_SOURCE_LABEL = "Quotes delayed ~15 min (Yahoo Finance via yfinance)"

# ---------------------------------------------------------------- account
STARTING_CASH = 50.00            # USD
CONTRACT_MULTIPLIER = 100

# ---------------------------------------------------------------- RISK (hard, never modified by engine)
MAX_POSITION_FRAC = 0.40         # max 40% of current total equity in one position (cost incl. fees)
DAILY_LOSS_CAP_FRAC = 0.15       # stop for the day once equity <= start-of-day equity * (1 - 0.15)
FULL_STOP_FRAC = 0.50            # full stop forever (until manual reset) once equity <= 50% of start
FULL_STOP_EQUITY = STARTING_CASH * FULL_STOP_FRAC   # = $25.00
MAX_OPEN_POSITIONS = 1
CONTRACTS_PER_TRADE = 1

# ---------------------------------------------------------------- Robinhood fees (options, non-index)
# Source (fetched 2026-10-27): Robinhood Financial Standard Pricing Fee Schedule (doc id 20260929-5969628-19014374)
#   https://cdn.robinhood.com/assets/robinhood/legal/RHF+Fee+Schedule.pdf
# and https://robinhood.com/us/en/support/articles/trading-fees-on-robinhood/
FEE_SOURCE_URLS = [
    "https://cdn.robinhood.com/assets/robinhood/legal/RHF+Fee+Schedule.pdf",
    "https://robinhood.com/us/en/support/articles/trading-fees-on-robinhood/",
]
FEES = {
    "commission_per_contract": 0.00,          # $0 commission on stock/ETF options
    "orf_occ_per_contract": 0.04,             # Options Regulatory + OCC Clearing Fee, buys AND sells
    "cat_per_contract": 0.0003,               # Consolidated Audit Trail, buys AND sells; <$0.01 rounds down to 0, else nearest penny
    "sec_fee_per_dollar_sold": 20.60 / 1_000_000,  # SEC Section 31 fee on SELLS, $20.60 per $1M principal, rounded UP to the penny
    "taf_per_contract_sold": 0.00329,         # FINRA TAF on option SELLS, rounded to nearest penny (<$0.01 -> 0), max $9.79
    "taf_max": 9.79,
    # Robinhood: "The trading activity fee (TAF) is temporarily paused from October 1, 2026, through December 31, 2026 per FINRA."
    "taf_paused_from": date(2026, 10, 1),
    "taf_paused_to": date(2026, 12, 31),
    "expiry_settlement_fee": 0.00,            # no transaction fees applied on automatic expiry settlement
}

# ---------------------------------------------------------------- market calendar (NYSE, source: nyse.com/markets/hours-calendars)
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
NYSE_HOLIDAYS = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
}
NYSE_EARLY_CLOSES = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}  # 1:00 pm ET close

# ---------------------------------------------------------------- strategy
UNIVERSE = ["SPY", "QQQ", "IWM", "AMD", "PLTR", "SOFI", "F", "NIO"]
ETFS = {"SPY", "QQQ", "IWM"}
MOVE_THRESHOLD_ETF = 0.0040      # |last/open - 1| >= 0.40% for ETFs
MOVE_THRESHOLD_STOCK = 0.0100    # >= 1.00% for single stocks
EMA_BARS = 20                    # 20 x 1-min bar EMA as the "short moving average"
ENTRY_START = time(10, 0)        # no entries before 10:00 ET
ENTRY_END_MIN_BEFORE_CLOSE = 60  # no new entries in the last 60 min (15:00 ET normally)
TIME_STOP_MIN_BEFORE_CLOSE = 15  # positions expiring today are closed at 15:45 ET (12:45 on early-close days)
MAX_DTE = 7                      # nearest expiry with 0..7 calendar days to expiry
MAX_OTM_STRIKES = 10             # consider up to 10 OTM strikes per expiry, nearest-the-money first...
MAX_OTM_PCT = 0.05               # ...and only strikes within 5% of spot (first one that passes filters AND fits cap wins)
MIN_VOLUME = 100
MIN_OPEN_INTEREST = 100
MAX_SPREAD_FRAC_OF_ASK = 0.30
MIN_ASK = 0.05                   # skip $0.01-$0.04 lottery tickets
TAKE_PROFIT_FRAC = 0.50          # exit when net liquidation value >= +50% vs total entry cost
STOP_LOSS_FRAC = 0.40            # exit when net liquidation value <= -40% vs total entry cost
MAX_HOLD_TRADING_DAYS = 3        # multi-day positions: close at time stop on 3rd trading day held, or expiry day
MAX_ENTRIES_PER_DAY = 3
REENTRY_COOLDOWN_MIN = 30        # no re-entry in the same underlying within 30 min of an exit

# ---------------------------------------------------------------- loop
POLL_SECONDS_MARKET = 90
POLL_SECONDS_IDLE = 300
EQUITY_HISTORY_MAX = 20000

# ---------------------------------------------------------------- robustness (operational only; NOT risk or strategy)
REQUEST_TIMEOUT_S = 8            # per-HTTP-request timeout forced onto yfinance (default was 30s)
HARD_FETCH_TIMEOUT_S = 12        # wall-clock cap per data call (thread guard), whatever yfinance does internally
QUOTE_RETRY_BACKOFF_S = [10, 12, 15]  # fast retries while an open position has no fresh quote (then stays at 15s)
GAP_FACTOR = 2                   # a loop gap > 2x POLL_SECONDS_MARKET during market hours is a "missed check"
STALE_QUOTE_SECONDS = 180        # open position's last good quote older than this -> flagged stale
SLEEP_CHUNK_S = 5                # sleep in short chunks; re-check wall clock + monotonic each chunk
GAP_HISTORY_MAX = 500

# ---------------------------------------------------------------- GitHub Actions runner / box sync (operational only)
LOG_TAIL_FILE = os.path.join(BASE_DIR, "logs", "engine-tail.log")   # committed rolling log tail (runner writes it)
LOG_TAIL_LINES = 1500
SYNC_STATUS_FILE = os.path.join(BASE_DIR, "data", "sync_status.json")  # box only: when state was last pulled
RUNNER_SYNC_EVERY_S = 180         # runner commits + pushes state at most this often (and at exit)
RUNNER_MAX_JOB_S = 5 * 3600 + 40 * 60   # hand off before GitHub's 6 h job limit
RUNNER_AFTER_CLOSE_MIN = 15       # keep running this long after the close (expiry settlement needs the official close)
