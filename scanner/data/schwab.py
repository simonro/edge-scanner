"""DataFeed backed by the Charles Schwab API (via the schwabdev wrapper).

Selected with DATA_PROVIDER=schwab in .env (or run_live.py --feed schwab);
AlpacaFeed remains the default. Needs `pip install schwabdev` and a one-time
login with scripts/schwab_auth.py (repeat weekly: Schwab expires it after 7 days).

Why this exists: Alpaca allows ONE concurrent market-data websocket per
account, and costs $99/mo. Schwab is free with a brokerage account and gives
an independent stream. Whether the DATA is equivalent is a separate question,
answered by scripts/check_schwab_match.py, not by this file.

Known differences from AlpacaFeed, all deliberate:
  * Cache lives under data/schwab/** so it can never mix with Alpaca's parquet
    cache. Mixing them would silently corrupt the live scanner's history.
  * Schwab's price-history endpoint is ONE SYMBOL PER REQUEST (no batch
    variant), so multi-symbol fetches are threaded and throttled instead of
    batched. Expect slower cold warmups.
  * Schwab returns no vwap / trade_count on candles; those columns are present
    but NaN so the DataFrame schema still matches AlpacaFeed.
  * Minute history has a shorter lookback than Alpaca's. Verify empirically
    with check_schwab_match.py before relying on it for the 20-day RVOL profile.
  * The refresh token expires every 7 days and re-auth opens a browser
    (Schwab's rule, not schwabdev's). Unattended runs WILL eventually stop.
  * SCHWAB STREAMS 1-MINUTE BARS FOR AT MOST 300 SYMBOLS PER ACCOUNT
    (CHART_EQUITY; measured 2026-09-20, the streamer answers code 19 and
    discards the rest). Alpaca's paid feed has no such cap. Symbols past the cap
    get no live bars, so subscribe_minute_bars takes them in the order given
    (the universe file is sorted by dollar volume, most liquid first), says
    loudly how many were left out, and exposes them as `unstreamed_symbols`.

Credentials (add to .env yourself, never commit):
    SCHWAB_APP_KEY=...
    SCHWAB_APP_SECRET=...
    SCHWAB_CALLBACK_URL=https://127.0.0.1
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

from scanner.cache import parquet
from scanner.data.interface import DataFeed, Timeframe

load_dotenv(override=True)

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Isolated from Alpaca's data/daily + data/5m. Do NOT point these at the Alpaca
# dirs: the two providers disagree on volume and would poison the live cache.
_DEFAULT_DAILY_CACHE    = Path("data/schwab/daily")
_DEFAULT_INTRADAY_CACHE = Path("data/schwab/5m")

_BAR_COLS = ["open", "high", "low", "close", "volume", "vwap", "trade_count"]

# (periodType, frequencyType, frequency). periodType is NOT optional: Schwab
# defaults it to "day", and "day" only permits frequencyType="minute", so
# asking for daily candles without it returns 400 Bad Request. Valid pairs:
#     day -> minute        month -> daily, weekly
#     year -> daily, weekly, monthly     ytd -> daily, weekly
# Minute frequencies are limited to 1, 5, 10, 15, 30 (no 60), so 1Hour is
# resampled from 30-minute candles.
_FREQ: dict[str, tuple[str, str, int]] = {
    "1Min":  ("day",  "minute", 1),
    "5Min":  ("day",  "minute", 5),
    "15Min": ("day",  "minute", 15),
    "30Min": ("day",  "minute", 30),
    "Day":   ("year", "daily",  1),
    "1Week": ("year", "weekly", 1),
}

_MAX_WORKERS = 4      # concurrency; the RATE is set by _LIMITER, not by this

# Schwab's market-data API allows about 120 requests a minute per app. Every
# request in this module goes through one shared limiter so that threads
# together stay under it. Override with SCHWAB_MAX_RPM if Schwab raises yours.
_MAX_RPM = float(os.environ.get("SCHWAB_MAX_RPM", "110"))
_RETRIES = 4                 # on HTTP 429 / 5xx, with exponential backoff
_REFRESH_TOKEN_DAYS = 7      # Schwab's rule; after this a browser login is required
_TOKENS_DB = Path(os.path.expanduser("~/.schwabdev/tokens.db"))


class _RateLimiter:
    """Evenly spaced, thread-safe: at most `rpm` acquisitions per minute."""

    def __init__(self, rpm: float, clock=time.monotonic, sleep=time.sleep) -> None:
        self._interval = 60.0 / max(rpm, 1.0)
        self._next = 0.0
        self._lock = threading.Lock()
        self._clock, self._sleep = clock, sleep

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            wait = self._next - now
            self._next = max(now, self._next) + self._interval
        if wait > 0:
            self._sleep(wait)


_LIMITER = _RateLimiter(_MAX_RPM)


def refresh_token_age_days(db: Path = _TOKENS_DB) -> float | None:
    """Days since Schwab last issued the refresh token, or None if unknown.
    Reads only the timestamp column, never a token."""
    try:
        import sqlite3
        with sqlite3.connect(db) as con:
            row = con.execute("select refresh_token_issued from schwabdev").fetchone()
        issued = datetime.fromisoformat(row[0])
        return (datetime.now(issued.tzinfo) - issued).total_seconds() / 86400
    except Exception:
        return None


def _covers(df: pd.DataFrame, start: date, slack_days: int = 6) -> bool:
    """True when a cached frame reaches back to `start` (allowing for weekends
    and holidays at the front of the range)."""
    if df is None or df.empty:
        return False
    first = pd.Timestamp(df.index.min()).tz_convert(_ET).date() if df.index.tz is not None \
        else pd.Timestamp(df.index.min()).date()
    return (first - start).days <= slack_days


def _num(v) -> float | None:
    """A quote field as a float, or None when it is missing or not a number."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _last_session_before(moment: datetime) -> date:
    """The most recent weekday whose regular session had closed by `moment` (ET)."""
    d = moment.date()
    if moment.hour * 60 + moment.minute < 16 * 60:      # today's session not over yet
        d = date.fromordinal(d.toordinal() - 1)
    while d.weekday() >= 5:
        d = date.fromordinal(d.toordinal() - 1)
    return d


def _fresh(path: Path, df: pd.DataFrame, end: date, asked_end: date | None = None) -> bool:
    """True when a cached frame does not need re-downloading.

    Schwab serves ONE symbol per history request at about 120 a minute, so
    re-downloading a whole-market universe costs two hours. The question is not
    "does the data reach `end`" (a file written Monday morning holds Friday's
    bars, and Tuesday asks for Monday: that rule re-downloaded everything every
    morning) but "could Schwab give more than this file has?" It could not if
    the file was written after the close of the last session on or before `end`,
    plus a margin for late final bars, AND the request that wrote it asked for
    data through that session (`asked_end`). Without the second half, a start
    on Friday evening (which asks for history through Thursday) left a file
    written after Friday's close that held only Thursday, and Monday's start
    trusted it: prior close, gap % and prior-day levels came from Thursday
    (issue #20). An unknown `asked_end` (a file from before it was recorded)
    falls back to checking the data itself.
    """
    if df is None or df.empty:
        return False
    written = datetime.fromtimestamp(path.stat().st_mtime, tz=_ET)
    target = end
    while target.weekday() >= 5:
        target = date.fromordinal(target.toordinal() - 1)
    # Written after that session's close (with an hour for the tape to settle),
    # by a request that asked for that session?
    close = datetime(target.year, target.month, target.day, 17, 0, tzinfo=_ET)
    if written >= close and asked_end is not None and asked_end >= target:
        return True
    # Or the data itself already reaches that session.
    last = pd.Timestamp(df.index.max())
    last_day = last.tz_convert(_ET).date() if last.tzinfo is not None else last.date()
    return last_day >= target


class _Asked:
    """Earliest start date already requested from Schwab, per symbol, per cache.

    A symbol whose history is shorter than the window asked for (a recent
    listing) can never "cover" the start, so without a record of the request it
    was downloaded again on every start: about 1,800 extra requests on a
    6,456-symbol universe, fifteen minutes at Schwab's 120 a minute. If the same
    span or a longer one was already asked for, the cached frame IS everything
    Schwab has. Kept in one small JSON file beside the parquet files.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._path = Path(cache_dir) / "_asked.json"
        self._lock = threading.Lock()
        self._dirty = False
        try:
            self._map: dict[str, str] = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._map = {}

    def covers(self, symbol: str, start: date) -> bool:
        got = self._map.get(symbol)
        return got is not None and got <= start.isoformat()

    def note(self, symbol: str, start: date) -> None:
        iso = start.isoformat()
        with self._lock:
            if self._map.get(symbol, "9999") > iso:
                self._map[symbol] = iso
                self._dirty = True

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._map), encoding="utf-8")
                os.replace(tmp, self._path)
                self._dirty = False
            except OSError as exc:
                log.debug("could not save %s: %s", self._path, exc)


class _AskedEnd(_Asked):
    """End date of the request that wrote each cached file, per symbol, per cache.

    _fresh() needs it: a file written after a session's close holds that
    session only if the request asked for it. A start in the evening asks for
    history through the day before, so its file is a day short (issue #20).
    The latest request wins, not the latest end: a later, shorter request
    rewrote the file, and the record must describe what the file holds.
    """

    def __init__(self, cache_dir: Path) -> None:
        super().__init__(cache_dir)
        self._path = Path(cache_dir) / "_asked_end.json"
        try:
            self._map = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._map = {}

    def end(self, symbol: str) -> date | None:
        got = self._map.get(symbol)
        try:
            return date.fromisoformat(got) if got else None
        except ValueError:
            return None

    def note(self, symbol: str, end: date) -> None:
        iso = end.isoformat()
        with self._lock:
            if self._map.get(symbol) != iso:
                self._map[symbol] = iso
                self._dirty = True


class SchwabFeed(DataFeed):
    """DataFeed implementation over the Schwab market-data API."""

    def __init__(
        self,
        cache_dir: Path = _DEFAULT_DAILY_CACHE,
        intraday_cache_dir: Path = _DEFAULT_INTRADAY_CACHE,
        client=None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._intraday_cache_dir = Path(intraday_cache_dir)
        self._asked_daily = _Asked(self._cache_dir)
        self._asked_intraday = _Asked(self._intraday_cache_dir)
        self._ended_daily = _AskedEnd(self._cache_dir)
        self._ended_intraday = _AskedEnd(self._intraday_cache_dir)
        self._in_batch = False
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._intraday_cache_dir.mkdir(parents=True, exist_ok=True)

        if client is not None:
            self._client = client            # injected (tests)
        else:
            age = refresh_token_age_days()
            if age is not None and age >= _REFRESH_TOKEN_DAYS:
                # schwabdev would otherwise stop at a console prompt waiting
                # for a browser login, which hangs an unattended start.
                raise RuntimeError(
                    f"Schwab login expired ({age:.0f} days since the last login; Schwab allows "
                    f"{_REFRESH_TOKEN_DAYS}). Run: python scripts/schwab_auth.py")
            if age is not None and age >= _REFRESH_TOKEN_DAYS - 1:
                log.warning("Schwab login expires within a day; run scripts/schwab_auth.py soon")
            import schwabdev
            key    = os.environ.get("SCHWAB_APP_KEY")
            secret = os.environ.get("SCHWAB_APP_SECRET")
            if not key or not secret:
                raise RuntimeError(
                    "SCHWAB_APP_KEY / SCHWAB_APP_SECRET missing from .env. "
                    "Register an app at developer.schwab.com, then add them."
                )
            self._client = schwabdev.Client(
                app_key=key,
                app_secret=secret,
                callback_url=os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1"),
            )
        self._stream = None
        self._stop_evt = threading.Event()
        self.streamed_symbols: list[str] = []      # live 1-min bars (Schwab caps these)
        self.unstreamed_symbols: list[str] = []    # asked for, but past Schwab's cap
        self.quote_streamed_symbols: list[str] = []   # bars built from the live quote stream
        self.polled_symbols: list[str] = []           # bars built from polled quotes
        self.quote_bars = None                        # the QuoteBarBuilder, once streaming

    # ── Candle parsing ────────────────────────────────────────────────────────

    @staticmethod
    def _candles_to_df(payload: dict) -> pd.DataFrame:
        """Schwab candle JSON -> UTC-indexed OHLCV frame matching AlpacaFeed."""
        candles = (payload or {}).get("candles") or []
        if not candles:
            return pd.DataFrame(columns=_BAR_COLS).rename_axis("timestamp")
        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
        df = df.set_index("timestamp").sort_index()
        # Schwab gives no vwap / trade_count; keep the columns so downstream code
        # that reindexes on _BAR_COLS behaves identically to Alpaca.
        for col in ("vwap", "trade_count"):
            if col not in df.columns:
                df[col] = float("nan")
        return df.reindex(columns=_BAR_COLS)

    def _price_history(self, symbol: str, timeframe: Timeframe,
                       start: date, end: date) -> pd.DataFrame:
        if timeframe in ("1Hour", "4Hour"):
            # Schwab has no hourly candles: build them from 30-minute ones.
            half = self._price_history(symbol, "30Min", start, end)
            if half.empty:
                return half
            return half.resample("1h" if timeframe == "1Hour" else "4h").agg({
                "open": "first", "high": "max", "low": "min", "close": "last",
                "volume": "sum", "vwap": "mean", "trade_count": "sum",
            }).dropna(subset=["open"])

        ptype, ftype, freq = _FREQ[timeframe]
        # period is deliberately omitted: Schwab rejects it alongside
        # startDate/endDate, which is how this feed always queries.
        resp = self._request(lambda: self._client.price_history(
            symbol=symbol,
            periodType=ptype,
            frequencyType=ftype,
            frequency=freq,
            startDate=datetime.combine(start, datetime.min.time()),
            endDate=datetime.combine(end, datetime.max.time()),
            needExtendedHoursData=(ftype == "minute"),   # premarket levels need it
        ))
        return self._candles_to_df(resp.json())

    @staticmethod
    def _request(call):
        """Rate-limited call with retry on 429 and 5xx. Raises on anything else."""
        delay = 2.0
        for attempt in range(_RETRIES + 1):
            _LIMITER.acquire()
            resp = call()
            status = getattr(resp, "status_code", 200)
            if status == 429 or status >= 500:
                if attempt == _RETRIES:
                    resp.raise_for_status()
                log.debug("Schwab HTTP %s, retrying in %.0fs", status, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            resp.raise_for_status()
            return resp
        return resp

    # ── DataFeed interface ────────────────────────────────────────────────────

    def get_historical_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        # Reuse today's file only if it reaches back far enough: the universe
        # build and the warmup ask for different spans on the same morning.
        p = self._cache_dir / f"{symbol}.parquet"
        if p.exists():
            cached = parquet.load(symbol, self._cache_dir)
            if (_fresh(p, cached, end, self._ended_daily.end(symbol))
                    and (_covers(cached, start) or self._asked_daily.covers(symbol, start))):
                return cached
        df = self._price_history(symbol, "Day", start, end)
        parquet.save(symbol, df, self._cache_dir)
        self._asked_daily.note(symbol, start)
        self._ended_daily.note(symbol, end)
        if not self._in_batch:
            self._asked_daily.save()
            self._ended_daily.save()
        return df

    def get_historical_bars(self, symbol: str, timeframe: Timeframe,
                            start: date, end: date) -> pd.DataFrame:
        # The cache is keyed by symbol alone and holds the 5-minute history the
        # volume profile is built from. Any other bar size is fetched uncached:
        # cached under the same name it would replace that history, which is how
        # a 1-minute replay fetch inflated Alpaca's RVOL about 5x on 2026-09-23.
        if timeframe != "5Min":
            return self._price_history(symbol, timeframe, start, end)
        p = self._intraday_cache_dir / f"{symbol}.parquet"
        if p.exists():
            cached = parquet.load(symbol, self._intraday_cache_dir)
            if (_fresh(p, cached, end, self._ended_intraday.end(symbol))
                    and (_covers(cached, start) or self._asked_intraday.covers(symbol, start))):
                return cached
        df = self._price_history(symbol, timeframe, start, end)
        parquet.save(symbol, df, self._intraday_cache_dir)
        self._asked_intraday.note(symbol, start)
        self._ended_intraday.note(symbol, end)
        if not self._in_batch:
            self._asked_intraday.save()
            self._ended_intraday.save()
        return df

    def get_bars_range(self, symbol: str, timeframe: Timeframe,
                       start: date, end: date) -> pd.DataFrame:
        """Uncached range fetch (used by the chart API)."""
        return self._price_history(symbol, timeframe, start, end)

    def get_todays_bars(self, symbol: str, timeframe: Timeframe) -> pd.DataFrame:
        today = datetime.now(_ET).date()
        return self._price_history(symbol, timeframe, today, today)

    def get_todays_bars_multi(self, symbols: list[str],
                              timeframe: Timeframe = "1Min") -> dict[str, pd.DataFrame]:
        """One request per symbol (Schwab has no batch endpoint), threaded.

        At about 120 requests a minute a whole-market universe would hold the
        start up for close to an hour, so only the first SEED_MAX_SYMBOLS are
        seeded (the caller passes them most liquid first). The rest build their
        session from the live feed: complete when the scanner starts before the
        open, and from the start time onward when it starts mid-session.
        """
        if len(symbols) > self.SEED_MAX_SYMBOLS:
            print(f"       Schwab: seeding today's bars for the {self.SEED_MAX_SYMBOLS} most liquid symbols "
                  f"only (one request each). The other {len(symbols) - self.SEED_MAX_SYMBOLS:,} build VWAP, "
                  f"volume and levels from the live feed, so start before the open.", flush=True)
            symbols = symbols[: self.SEED_MAX_SYMBOLS]
        today = datetime.now(_ET).date()
        out: dict[str, pd.DataFrame] = {}

        def _one(sym: str):
            return sym, self._price_history(sym, timeframe, today, today)

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futs = [pool.submit(_one, s) for s in symbols]
            for fut in as_completed(futs):
                try:
                    sym, df = fut.result()
                    if not df.empty:
                        out[sym] = df[["open", "high", "low", "close", "volume"]]
                except Exception as exc:
                    log.warning("Schwab today's bars failed: %s", exc)
        return out

    def _multi(self, fetch, symbols: list[str], progress=None) -> dict[str, pd.DataFrame]:
        """Run fetch(symbol) for many symbols on a few threads. The shared rate
        limiter, not the thread count, sets the pace; failures are logged and
        skipped so one bad symbol never stops a warmup."""
        out: dict[str, pd.DataFrame] = {}
        done = 0
        self._in_batch = True             # one write of the request record, at the end
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futs = {pool.submit(fetch, s): s for s in symbols}
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    df = fut.result()
                    if df is not None and not df.empty:
                        out[sym] = df
                except Exception as exc:
                    log.warning("Schwab history failed for %s: %s", sym, exc)
                done += 1
                if progress is not None:
                    progress(done, len(symbols))
        self._in_batch = False
        self._asked_daily.save()
        self._asked_intraday.save()
        self._ended_daily.save()
        self._ended_intraday.save()
        return out

    def get_historical_daily_multi(self, symbols: list[str], start: date, end: date,
                                   workers: int = 0, progress=None) -> dict[str, pd.DataFrame]:
        """Daily bars for many symbols, one request each (Schwab has no batch
        endpoint): about len(symbols) / SCHWAB_MAX_RPM minutes when not cached."""
        return self._multi(lambda s: self.get_historical_daily(s, start, end), symbols, progress)

    def get_historical_bars_multi(self, symbols: list[str], timeframe: Timeframe,
                                  start: date, end: date, workers: int = 0,
                                  progress=None) -> dict[str, pd.DataFrame]:
        return self._multi(lambda s: self.get_historical_bars(s, timeframe, start, end), symbols, progress)

    def get_session_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """The session so far, from quotes: open, high, low, last and total volume.

        For a mid-session start. Bars for today can only be back-filled for the
        most liquid symbols (one request each), which would leave every other
        symbol starting the day at zero volume, so its relative volume reads far
        too low until the close. Quotes come 500 to a request, so the whole
        universe takes seconds. What this cannot give is the session VWAP (it is
        approximated by the day's typical price) or the split between premarket
        and regular volume (total volume includes both).
        """
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), self.QUOTES_PER_REQUEST):
            batch = symbols[i : i + self.QUOTES_PER_REQUEST]
            try:
                resp = self._request(lambda b=batch: self._client.quotes(symbols=b, fields="quote"))
                for sym, payload in (resp.json() or {}).items():
                    q = (payload or {}).get("quote") or {}
                    row = {"open": _num(q.get("openPrice")), "high": _num(q.get("highPrice")),
                           "low": _num(q.get("lowPrice")), "last": _num(q.get("lastPrice")),
                           "volume": _num(q.get("totalVolume"))}
                    if all(v is not None and v > 0 for v in row.values()):
                        out[sym] = row
            except Exception as exc:
                log.warning("Schwab session quotes failed for %d symbols: %s", len(batch), exc)
        return out

    def get_snapshot(self, symbols: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        _BATCH = 100   # quotes() IS batched, unlike price history
        for i in range(0, len(symbols), _BATCH):
            batch = symbols[i : i + _BATCH]
            try:
                resp = self._request(lambda b=batch: self._client.quotes(symbols=b, fields="quote"))
                for sym, payload in (resp.json() or {}).items():
                    q = (payload or {}).get("quote") or {}
                    result[sym] = {
                        "price":        q.get("lastPrice"),
                        "bid":          q.get("bidPrice"),
                        "ask":          q.get("askPrice"),
                        "daily_volume": q.get("totalVolume"),
                    }
            except Exception as exc:
                log.warning("Schwab quotes batch failed: %s", exc)
        return result

    # ── Streaming ─────────────────────────────────────────────────────────────

    @staticmethod
    def handle_message(raw, callback: Callable[[dict], None]) -> None:
        """Decode one raw streamer message and emit any CHART_EQUITY bars.

        Split out from subscribe_minute_bars so it can be tested without
        opening a socket. Field order is 0 key, 1 sequence, 2 open, 3 high,
        4 low, 5 close, 6 volume, 7 chart time (epoch ms), 8 chart day --
        this is schwabdev's CORRECTED order; Schwab's own docs are wrong.
        Malformed payloads are skipped rather than killing the stream.
        """
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (TypeError, ValueError):
            return
        for block in (msg or {}).get("data", []) or []:
            if block.get("service") != "CHART_EQUITY":
                continue
            for c in block.get("content", []) or []:
                try:
                    callback({
                        "symbol":    c.get("key"),
                        "timestamp": pd.to_datetime(int(c["7"]), unit="ms", utc=True),
                        "open":      float(c["2"]),
                        "high":      float(c["3"]),
                        "low":       float(c["4"]),
                        "close":     float(c["5"]),
                        "volume":    float(c["6"]),
                    })
                except (KeyError, TypeError, ValueError) as exc:
                    log.debug("Skipping malformed CHART_EQUITY payload: %s", exc)

    # Schwab's per-account limits, measured against the live streamer 2026-09-20.
    # The streamer's own answer (code 19) overrides these if Schwab changes them.
    CHART_EQUITY_CAP = 300        # real 1-minute bars
    LEVELONE_CAP = 3000           # live quotes
    QUOTES_PER_REQUEST = 500      # REST quotes
    POLL_SECONDS = 10.0           # one pass over the polled symbols
    # No batch history endpoint: callers that would re-download bars for the whole
    # universe (the premarket lists) must use the scanner's live state instead.
    per_symbol_history = True
    SEED_MAX_SYMBOLS = 600        # today's-bars seeding at startup: about 5 minutes

    @staticmethod
    def parse_symbol_cap(raw, service: str = "CHART_EQUITY") -> int | None:
        """The cap Schwab reports when a subscription exceeds it, else None.

        The streamer answers an over-limit ADD with code 19 and a message like
        "You've reached the maximum number of symbols allowed.  (CHART_EQUITY=300,
        DISCARDED=250)". It does NOT raise and keeps streaming the symbols it
        had already accepted, which is why this has to be read explicitly.
        """
        import re
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (TypeError, ValueError):
            return None
        for r in (msg or {}).get("response", []) or []:
            content = r.get("content") or {}
            if r.get("service") == service and content.get("code") == 19:
                m = re.search(service + r"=(\d+)", str(content.get("msg") or ""))
                if m:
                    return int(m.group(1))
                return SchwabFeed.CHART_EQUITY_CAP if service == "CHART_EQUITY" else SchwabFeed.LEVELONE_CAP
        return None

    # Schwab's minute bars carry about this share of the cumulative volume its
    # quotes report, evenly through the day. Measured against a consolidated feed
    # on 12 symbols across the tiers: 0.64 to 0.82, median 0.77. Built volume is
    # scaled by it so that relative volume, which divides by a baseline made from
    # those minute bars, compares like with like. A per-symbol factor from cached
    # history was tried (regular-session 5-minute volume over daily volume) and was
    # WORSE than this constant: daily volume includes the closing auction and
    # after-hours trading, which the intraday total never sees.
    BAR_VOLUME_SHARE = 0.77

    @staticmethod
    def handle_quotes(raw, builder) -> None:
        """Feed LEVELONE_EQUITIES updates to a QuoteBarBuilder. Fields: 3 last
        price, 8 total volume, 9 last size (shares), 10 day high, 11 day low. The streamer sends only
        the fields that changed, so any of them may be missing."""
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (TypeError, ValueError):
            return
        for block in (msg or {}).get("data", []) or []:
            if block.get("service") != "LEVELONE_EQUITIES":
                continue
            for c in block.get("content", []) or []:
                try:
                    builder.on_quote(c.get("key"), last=_num(c.get("3")), total_volume=_num(c.get("8")),
                                     day_high=_num(c.get("10")), day_low=_num(c.get("11")),
                                     last_size=_num(c.get("9")))
                except Exception as exc:
                    log.debug("Skipping malformed LEVELONE payload: %s", exc)

    def subscribe_minute_bars(self, symbols: list[str],
                              callback: Callable[[dict], None]) -> None:
        """Stream 1-minute bars for every symbol. Blocks until stop_stream().

        Schwab serves real 1-minute bars (CHART_EQUITY) for at most 300 symbols
        per account, so a larger universe is covered in three tiers, in the
        order given (the universe file is sorted most liquid first):

            first 300     real bars from CHART_EQUITY
            next 3,000    bars built from the live quote stream (LEVELONE_EQUITIES)
            the rest      bars built from REST quotes polled every POLL_SECONDS

        See scanner/data/quote_bars.py for how a bar is built from quotes and how
        close it is to a real one. Set SCHWAB_SYNTHETIC_BARS=0 to turn the second
        and third tier off: only the first 300 symbols are then scanned.

        CHART_EQUITY field order (schwabdev translate.py, corrected against
        Schwab's own docs which are wrong): 0 key, 1 sequence, 2 open, 3 high,
        4 low, 5 close, 6 volume, 7 chart time (epoch ms), 8 chart day.
        """
        from scanner.data.quote_bars import QuoteBarBuilder
        # schwabdev 4.0.0 exposes no Client.stream property; construct it.
        import schwabdev
        stream = schwabdev.Stream(self._client)
        self._stream = stream
        self._stop_evt.clear()

        # Bars now arrive from three threads (stream, quote flush, poller) and the
        # scanner's bar handler is not re-entrant: one bar at a time.
        emit_lock = threading.Lock()

        def _emit(bar: dict) -> None:
            with emit_lock:
                callback(bar)

        synthetic = os.environ.get("SCHWAB_SYNTHETIC_BARS", "1").strip().lower() not in ("0", "false", "no", "off")
        cap = self.CHART_EQUITY_CAP
        self.streamed_symbols = list(symbols[:cap])
        rest = list(symbols[cap:])
        self.quote_streamed_symbols = rest[: self.LEVELONE_CAP] if synthetic else []
        self.polled_symbols = rest[self.LEVELONE_CAP:] if synthetic else []
        self.unstreamed_symbols = [] if synthetic else rest
        self.quote_bars = builder = QuoteBarBuilder(_emit)
        tiers_lock = threading.Lock()
        for sym in self.quote_streamed_symbols:
            builder.track(sym, grace=3.0)
        for sym in self.polled_symbols:
            builder.track(sym, grace=self.POLL_SECONDS + 5.0)
        for sym in self.quote_streamed_symbols + self.polled_symbols:
            builder.set_scale(sym, self.BAR_VOLUME_SHARE)

        def _receiver(raw) -> None:
            chart_cap = self.parse_symbol_cap(raw, "CHART_EQUITY")
            if chart_cap is not None and chart_cap < len(self.streamed_symbols):
                # Schwab accepted fewer real-bar symbols than expected: the overflow
                # moves down a tier (or is reported, with synthetic bars off).
                with tiers_lock:
                    over = self.streamed_symbols[chart_cap:]
                    self.streamed_symbols = self.streamed_symbols[:chart_cap]
                    if synthetic:
                        for sym in over:
                            builder.track(sym, grace=self.POLL_SECONDS + 5.0)
                            builder.set_scale(sym, self.BAR_VOLUME_SHARE)
                        self.polled_symbols = over + self.polled_symbols
                    else:
                        self.unstreamed_symbols = over + self.unstreamed_symbols
                        self._warn_cap(chart_cap)
            quote_cap = self.parse_symbol_cap(raw, "LEVELONE_EQUITIES")
            if quote_cap is not None and quote_cap < len(self.quote_streamed_symbols):
                with tiers_lock:
                    over = self.quote_streamed_symbols[quote_cap:]
                    self.quote_streamed_symbols = self.quote_streamed_symbols[:quote_cap]
                    for sym in over:
                        builder.track(sym, grace=self.POLL_SECONDS + 5.0)
                    self.polled_symbols = over + self.polled_symbols
                log.warning("Schwab accepted %d quote-stream symbols; %d moved to polling", quote_cap, len(over))
            self.handle_message(raw, _emit)
            if synthetic:
                self.handle_quotes(raw, builder)

        if self.unstreamed_symbols:
            self._warn_cap(cap)

        stream.start(receiver=_receiver, daemon=True)
        # Subscribe in chunks; Schwab caps the key list per request.
        _CHUNK = 250
        for i in range(0, len(self.streamed_symbols), _CHUNK):
            stream.send(stream.chart_equity(self.streamed_symbols[i : i + _CHUNK], "0,1,2,3,4,5,6,7,8"))
        for i in range(0, len(self.quote_streamed_symbols), _CHUNK):
            stream.send(stream.level_one_equities(self.quote_streamed_symbols[i : i + _CHUNK], "0,3,8,9,10,11"))
        log.info("Schwab: %d symbols on real bars, %d on streamed quotes, %d on polled quotes",
                 len(self.streamed_symbols), len(self.quote_streamed_symbols), len(self.polled_symbols))
        if synthetic and rest:
            print(f"       Schwab coverage: {len(self.streamed_symbols):,} symbols on real 1-minute bars, "
                  f"{len(self.quote_streamed_symbols):,} on bars built from live quotes, "
                  f"{len(self.polled_symbols):,} on bars built from quotes polled every "
                  f"{self.POLL_SECONDS:g}s (high/low approximate). SCHWAB_SYNTHETIC_BARS=0 turns "
                  f"the last two off.", flush=True)

        def _poll() -> None:
            while not self._stop_evt.is_set():
                t0 = time.monotonic()
                with tiers_lock:
                    todo = list(self.polled_symbols)
                for i in range(0, len(todo), self.QUOTES_PER_REQUEST):
                    if self._stop_evt.is_set():
                        return
                    batch = todo[i : i + self.QUOTES_PER_REQUEST]
                    try:
                        resp = self._request(lambda b=batch: self._client.quotes(symbols=b, fields="quote"))
                        for sym, payload in (resp.json() or {}).items():
                            q = (payload or {}).get("quote") or {}
                            builder.on_quote(sym, last=_num(q.get("lastPrice")), total_volume=_num(q.get("totalVolume")),
                                             day_high=_num(q.get("highPrice")), day_low=_num(q.get("lowPrice")),
                                             last_size=_num(q.get("lastSize")))
                    except Exception as exc:
                        log.warning("Schwab quote poll failed for %d symbols: %s", len(batch), exc)
                self._stop_evt.wait(max(0.5, self.POLL_SECONDS - (time.monotonic() - t0)))

        def _flush() -> None:
            while not self._stop_evt.wait(1.0):
                try:
                    builder.flush()
                except Exception as exc:
                    log.error("quote bar flush failed: %s", exc, exc_info=True)

        # Whenever synthetic bars are on, even with nothing to poll yet: a lower
        # cap reported by Schwab moves symbols to polling after this point, and
        # without these threads they were advertised as polled but went dead
        # (audit 3, C11). An empty poll list costs no requests.
        if synthetic:
            threading.Thread(target=_flush, daemon=True, name="schwab-quote-bars").start()
            threading.Thread(target=_poll, daemon=True, name="schwab-quote-poll").start()

        while not self._stop_evt.wait(1.0):
            pass

    def _warn_cap(self, cap: int) -> None:
        n, total = len(self.unstreamed_symbols), len(self.unstreamed_symbols) + len(self.streamed_symbols)
        text = (f"Schwab streams live 1-minute bars for at most {cap} symbols per account, and "
                f"SCHWAB_SYNTHETIC_BARS is off. {n:,} of your {total:,} symbols are NOT being scanned "
                f"live: only the first {cap} in the universe file (the most liquid) are.")
        log.error(text)
        print("\n" + "!" * 72 + f"\n  {text}\n" + "!" * 72 + "\n", flush=True)

    def stop_stream(self) -> None:
        self._stop_evt.set()
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as exc:
                log.debug("Schwab stream stop: %s", exc)
            log.info("Schwab stream stopped")
