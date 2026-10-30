"""Yahoo Finance (yfinance) data access. Quotes are ~15 minutes delayed. Nothing here invents prices."""
import logging
import math
import warnings
from datetime import date, datetime, timedelta

warnings.filterwarnings("ignore")
import threading  # noqa: E402
import yfinance as yf  # noqa: E402

import config  # noqa: E402
from market_calendar import ET  # noqa: E402


class FetchTimeout(Exception):
    pass


def _force_short_request_timeout():
    """Cap yfinance's per-request HTTP timeout (it defaults to 30s for option chains)."""
    try:
        from yfinance.data import YfData
    except Exception:  # pragma: no cover
        return
    if getattr(YfData.get, "_opd_patched", False):
        return
    orig = YfData.get

    def get(self, url, params=None, timeout=30, **kw):
        return orig(self, url, params=params, timeout=min(timeout or config.REQUEST_TIMEOUT_S, config.REQUEST_TIMEOUT_S), **kw)
    get._opd_patched = True
    YfData.get = get


_force_short_request_timeout()


def guarded(fn, *args, timeout=None, **kwargs):
    """Run fn in a daemon thread; raise FetchTimeout if it doesn't finish within `timeout` seconds.
    A stuck call is abandoned (it ends on its own via the request timeout); we never wait 30s+."""
    timeout = config.HARD_FETCH_TIMEOUT_S if timeout is None else timeout
    box = {}

    def run():
        try:
            box["v"] = fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise FetchTimeout(f"{getattr(fn, '__name__', fn)} exceeded {timeout}s")
    if "e" in box:
        raise box["e"]
    return box.get("v")

log = logging.getLogger("opd")


def clean_num(x):
    """Return float or None (NaN/None/inf -> None). Never substitutes a value."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


class YahooProvider:
    def __init__(self):
        self._cache = {}
        self.last_success = None   # wall-clock time of last successful fetch

    def new_tick(self):
        self._cache = {}

    def _ok(self):
        self.last_success = datetime.now(ET)

    def intraday_bars(self, ticker):
        key = ("bars", ticker)
        if key not in self._cache:
            df = guarded(lambda: yf.Ticker(ticker).history(period="1d", interval="1m", prepost=False, auto_adjust=False,
                                                          timeout=config.REQUEST_TIMEOUT_S))
            if df is not None and len(df):
                df.index = df.index.tz_convert(ET)
                self._ok()
            self._cache[key] = df
        return self._cache[key]

    def expiries(self, ticker):
        key = ("exp", ticker)
        if key not in self._cache:
            self._cache[key] = list(guarded(lambda: yf.Ticker(ticker).options))
            self._ok()
        return self._cache[key]

    def chain(self, ticker, expiry):
        """Returns list of dict rows for calls and puts: {symbol, kind, strike, bid, ask, last, volume, oi, expiry}."""
        key = ("chain", ticker, expiry)
        if key not in self._cache:
            oc = guarded(lambda: yf.Ticker(ticker).option_chain(expiry))  # only this one expiry
            rows = []
            for kind, df in (("call", oc.calls), ("put", oc.puts)):
                for r in df.itertuples(index=False):
                    rows.append({
                        "symbol": r.contractSymbol, "kind": kind, "underlying": ticker, "expiry": expiry,
                        "strike": clean_num(r.strike), "bid": clean_num(r.bid), "ask": clean_num(r.ask),
                        "last": clean_num(r.lastPrice), "volume": clean_num(r.volume),
                        "oi": clean_num(r.openInterest),
                    })
            self._cache[key] = rows
            self._ok()
        return self._cache[key]

    def option_quote(self, ticker, expiry, symbol, fresh=False):
        """Fetch only the chain for the position's own expiry. fresh=True bypasses the per-tick cache."""
        if fresh:
            self._cache.pop(("chain", ticker, expiry), None)
        for r in self.chain(ticker, expiry):
            if r["symbol"] == symbol:
                return r
        return None

    def close_on(self, ticker, d: date):
        """Official daily close of the underlying on date d, or None if not available yet."""
        df = guarded(lambda: yf.Ticker(ticker).history(start=d.isoformat(), end=(d + timedelta(days=1)).isoformat(),
                                                       interval="1d", auto_adjust=False, timeout=config.REQUEST_TIMEOUT_S))
        if df is None or not len(df):
            return None
        idx = [i for i in df.index if i.date() == d]
        if not idx:
            return None
        self._ok()
        return clean_num(df.loc[idx[-1], "Close"])
