from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo
import config

ET = ZoneInfo("America/New_York")


def now_et():
    return datetime.now(ET)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in config.NYSE_HOLIDAYS


def close_time(d: date) -> time:
    return config.EARLY_CLOSE if d in config.NYSE_EARLY_CLOSES else config.MARKET_CLOSE


def is_market_open(ts: datetime) -> bool:
    ts = ts.astimezone(ET)
    d = ts.date()
    return is_trading_day(d) and config.MARKET_OPEN <= ts.time() < close_time(d)


def minus_minutes(t: time, m: int) -> time:
    return (datetime.combine(date(2000, 1, 1), t) - timedelta(minutes=m)).time()


def entry_window_open(ts: datetime) -> bool:
    ts = ts.astimezone(ET)
    d = ts.date()
    if not is_market_open(ts):
        return False
    return config.ENTRY_START <= ts.time() < minus_minutes(close_time(d), config.ENTRY_END_MIN_BEFORE_CLOSE)


def time_stop_reached(ts: datetime) -> bool:
    ts = ts.astimezone(ET)
    return ts.time() >= minus_minutes(close_time(ts.date()), config.TIME_STOP_MIN_BEFORE_CLOSE)


def trading_days_between(a: date, b: date) -> int:
    """Number of trading days in (a, b] ."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n


def next_open(ts: datetime) -> datetime:
    ts = ts.astimezone(ET)
    d = ts.date()
    if is_trading_day(d) and ts.time() < config.MARKET_OPEN:
        return datetime.combine(d, config.MARKET_OPEN, ET)
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return datetime.combine(d, config.MARKET_OPEN, ET)


def market_seconds_between(a: datetime, b: datetime) -> float:
    """Seconds of regular NYSE session time inside [a, b]."""
    a, b = a.astimezone(ET), b.astimezone(ET)
    if b <= a:
        return 0.0
    total, d = 0.0, a.date()
    while d <= b.date():
        if is_trading_day(d):
            o = datetime.combine(d, config.MARKET_OPEN, ET)
            c = datetime.combine(d, close_time(d), ET)
            lo, hi = max(a, o), min(b, c)
            if hi > lo:
                total += (hi - lo).total_seconds()
        d += timedelta(days=1)
    return total
