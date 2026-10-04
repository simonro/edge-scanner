#!/usr/bin/env python
"""Build the scanner universe using a filter-based screener.

Filters applied (all thresholds are tunable via CLI flags):
  1. Symbol list: Alpaca's active US equities, or Nasdaq's public symbol
     directory (--symbols-from). Defaults to Alpaca when the provider is
     Alpaca, Nasdaq otherwise.
  2. Eligibility: tradable, not an ETF/fund/leveraged product, plain ticker
     (scanner/symbols.py, the same rule for every source)
  3. Price >= $15  (latest trade)
  4. 20-day avg volume >= --min-avg-vol shares/day, off by default (daily bars)
  5. 20-day avg dollar volume >= $150M/day  (avg_vol * price); $3.75M on
     Alpaca's free IEX feed, which sees only a few percent of the volume
  6. ATR% (20-day avg daily range / price) >= 1%  (no upper ceiling)

Market data (steps 3-6) comes from the provider: --provider, default the
DATA_PROVIDER setting in .env, else alpaca.

Dollar volume (avg_vol × price) serves as the liquidity / size proxy: a stock
with $50M+ daily dollar volume is inherently large/mid-cap and actively traded.

Usage:
    python scripts/build_universe.py
    python scripts/build_universe.py --min-dollar-vol-m 100
    python scripts/build_universe.py --symbols-from nasdaq --out data/universe_nasdaq.csv
    python scripts/build_universe.py --diagnostics data/universe_diag.csv   # why each symbol passed or failed
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv(override=True)

from scanner import symbols as symbol_sources  # noqa: E402
from scanner.data import FEEDS, make_feed      # noqa: E402

log = logging.getLogger(__name__)

_DEFAULT_OUT = Path("data/universe.csv")
_MIN_SYMBOLS = 100
# Non-batch providers pay one request per symbol whatever the span, so they
# fetch the span the live warmup uses and leave it in the cache for it.
_WARMUP_SPAN_DAYS = 400

_DOLLAR_VOL_M = 150.0
# Alpaca's free IEX feed is one exchange, about 2.5% of consolidated volume, so
# every volume it reports is roughly 40x lower than on SIP. With the SIP floor
# nearly nothing passed and a first start on IEX could not build a universe
# (discussion #19). The floor is scaled to the share the feed sees.
_IEX_VOLUME_SHARE = 0.025


def default_provider() -> str:
    p = (os.environ.get("DATA_PROVIDER") or "alpaca").strip().lower()
    return p if p in FEEDS else "alpaca"


def default_dollar_vol_m(provider: str) -> float:
    """The dollar-volume floor in millions, scaled down on Alpaca's IEX feed."""
    if provider == "alpaca" and (os.environ.get("ALPACA_FEED") or "sip").strip().lower() == "iex":
        return _DOLLAR_VOL_M * _IEX_VOLUME_SHARE
    return _DOLLAR_VOL_M


# ── Step 3: latest prices ─────────────────────────────────────────────────────

def _alpaca_prices(symbols: list[str]) -> dict[str, float]:
    """Latest trade price per symbol from Alpaca snapshots, 500 per request."""
    from scanner.data.alpaca import market_data_feed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockSnapshotRequest

    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    _BATCH = 500
    prices: dict[str, float] = {}
    for i in range(0, len(symbols), _BATCH):
        batch = symbols[i : i + _BATCH]
        try:
            snaps = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=batch, feed=market_data_feed()))
            for sym, snap in snaps.items():
                try:
                    if snap.latest_trade:
                        prices[sym] = float(snap.latest_trade.price)
                except (AttributeError, TypeError, ValueError):
                    pass
        except Exception as exc:
            log.warning("Snapshot batch %d error: %s — skipping batch", i // _BATCH, exc)
    return prices


def _feed_prices(feed, symbols: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for sym, snap in (feed.get_snapshot(symbols) or {}).items():
        try:
            if snap.get("price") is not None:
                out[sym] = float(snap["price"])
        except (TypeError, ValueError):
            pass
    return out


# ── Steps 4-6: daily bars → avg volume, dollar volume, ATR% ──────────────────

def bar_metrics(daily: pd.DataFrame, days: int) -> dict | None:
    """Liquidity and range metrics from one symbol's daily bars, or None when
    there is not enough history."""
    df = daily.sort_index().tail(days)
    if len(df) < max(5, days // 2):
        return None
    last_close = float(df["close"].iloc[-1])
    if last_close <= 0:
        return None
    avg_vol = float(df["volume"].mean())
    avg_range = float((df["high"] - df["low"]).mean())
    return {
        "avg_vol_20d":        round(avg_vol),
        "avg_dollar_vol_20d": round(avg_vol * last_close),
        "atr_pct":            round(avg_range / last_close * 100, 2),
        "last_price":         round(last_close, 2),
    }


def _alpaca_bar_metrics(symbols: list[str], days: int) -> dict[str, dict]:
    """Daily bars straight from Alpaca, 1,000 symbols per request (uncached)."""
    from scanner.data.alpaca import market_data_feed
    from alpaca.data.enums import Adjustment
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    today = date.today()
    end = today - timedelta(days=1)
    start = today - timedelta(days=days * 2 + 10)   # 2x window survives weekends/holidays
    _BATCH = 1000
    metrics: dict[str, dict] = {}
    for i in range(0, len(symbols), _BATCH):
        batch = symbols[i : i + _BATCH]
        try:
            df = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Day,
                start=datetime.combine(start, datetime.min.time()),
                # end of day, not midnight: daily bars are stamped 04:00 UTC,
                # so a midnight-UTC cutoff drops the `end` date's bar
                end=datetime.combine(end, datetime.max.time()),
                feed=market_data_feed(),
                # split-adjusted, or a reverse split inflates the 20-day volume
                adjustment=Adjustment.SPLIT,
            )).df
            if df.empty:
                continue
            for sym in batch:
                try:
                    sym_df = df.xs(sym, level="symbol") if isinstance(df.index, pd.MultiIndex) else df
                except KeyError:
                    continue
                m = bar_metrics(sym_df, days)
                if m:
                    metrics[sym] = m
        except Exception as exc:
            log.warning("Bars batch %d error: %s — skipping batch", i // _BATCH, exc)
    return metrics


def _feed_bar_metrics(feed, symbols: list[str], days: int) -> dict[str, dict]:
    end = date.today() - timedelta(days=1)
    start = date.today() - timedelta(days=_WARMUP_SPAN_DAYS)

    def tick(done: int, of: int) -> None:
        if done % 250 == 0 or done == of:
            log.info("  daily bars %d/%d", done, of)

    got = feed.get_historical_daily_multi(symbols, start, end, progress=tick)
    metrics = {}
    for sym, df in got.items():
        # a provider may include today's partial bar; the screen uses completed days
        df = df[pd.to_datetime(df.index).date <= end] if len(df) else df
        m = bar_metrics(df, days)
        if m:
            metrics[sym] = m
    return metrics


# ── The screen ────────────────────────────────────────────────────────────────

def screen(symbols: list[str], prices: dict[str, float], metrics: dict[str, dict], args) -> tuple[list[dict], list[dict]]:
    """Apply the thresholds. Returns (kept rows, one diagnostics row per symbol
    with the first filter it failed, or "kept")."""
    min_dollar_vol = args.min_dollar_vol_m * 1e6
    rows, diag = [], []
    for sym in symbols:
        price = prices.get(sym)
        m = metrics.get(sym)
        d = {"symbol": sym, "price": price, **(m or {})}
        if price is None:
            d["result"] = "no_price"
        elif price < args.min_price:
            d["result"] = "price"
        elif m is None:
            d["result"] = "no_history"
        elif m["avg_vol_20d"] < args.min_avg_vol:
            d["result"] = "avg_vol"
        elif m["avg_dollar_vol_20d"] < min_dollar_vol:
            d["result"] = "dollar_vol"
        elif m["atr_pct"] < args.min_atr_pct:
            d["result"] = "atr_pct"
        else:
            d["result"] = "kept"
            rows.append({"symbol": sym, "last_price": m["last_price"], "avg_vol_20d": m["avg_vol_20d"],
                         "avg_dollar_vol_20d": m["avg_dollar_vol_20d"], "atr_pct": m["atr_pct"]})
        diag.append(d)
    return rows, diag


def main() -> None:
    parser = argparse.ArgumentParser(description="Build scanner universe CSV (filter-based)")
    parser.add_argument("--out",               default=str(_DEFAULT_OUT))
    parser.add_argument("--provider",          choices=FEEDS, default=default_provider(),
                        help="market data for price, volume and ATR (default: DATA_PROVIDER in .env, else alpaca)")
    parser.add_argument("--symbols-from",      choices=symbol_sources.SOURCES, default=None,
                        help="starting symbol list (default: alpaca for the alpaca provider, nasdaq otherwise)")
    parser.add_argument("--min-price",         type=float, default=15.0,
                        help="Minimum last trade price (default: 15.0)")
    parser.add_argument("--min-avg-vol",       type=int,   default=0,
                        help="Minimum 20-day avg daily share volume (default: 0, off)")
    parser.add_argument("--min-dollar-vol-m",  type=float, default=None,
                        help="Minimum 20-day avg daily dollar volume in millions "
                             "(default: 150, or 3.75 on Alpaca's IEX feed)")
    parser.add_argument("--min-atr-pct",       type=float, default=1.0,
                        help="Minimum ATR%% (default: 1.0)")
    parser.add_argument("--days",              type=int,   default=20,
                        help="Trading days of history for volume/ATR (default: 20)")
    parser.add_argument("--diagnostics",       default=None,
                        help="also write every candidate with its metrics and the filter it failed")
    parser.add_argument("--log-level",         default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(message)s",
    )
    source = args.symbols_from or ("alpaca" if args.provider == "alpaca" else "nasdaq")
    if args.min_dollar_vol_m is None:
        args.min_dollar_vol_m = default_dollar_vol_m(args.provider)
        if args.min_dollar_vol_m != _DOLLAR_VOL_M:
            log.info("ALPACA_FEED=iex: one exchange, about %.1f%% of volume; dollar-volume floor "
                     "scaled to $%.2fM/day (set --min-dollar-vol-m to override)",
                     _IEX_VOLUME_SHARE * 100, args.min_dollar_vol_m)

    # Steps 1+2: symbol list + eligibility
    symbols = symbol_sources.eligible_symbols(symbol_sources.fetch(source))
    log.info("Symbols from %s after eligibility filter: %d", source, len(symbols))

    # Step 3: prices, then drop the ones below the floor before the costly step
    feed = None if args.provider == "alpaca" else make_feed(args.provider)
    prices = _alpaca_prices(symbols) if feed is None else _feed_prices(feed, symbols)
    priced = [s for s in symbols if prices.get(s) is not None and prices[s] >= args.min_price]
    log.info("After price filter (>= $%.2f): %d symbols", args.min_price, len(priced))
    if not priced:
        log.error("No symbols passed the price filter: check the %s credentials and data access", args.provider)
        sys.exit(1)

    # Steps 4-6: bar metrics + volume / dollar-vol / ATR filters
    metrics = _alpaca_bar_metrics(priced, args.days) if feed is None else _feed_bar_metrics(feed, priced, args.days)
    log.info("Bar metrics computed for %d / %d symbols", len(metrics), len(priced))
    rows, diag = screen(symbols, prices, metrics, args)
    counts = pd.Series([d["result"] for d in diag]).value_counts().to_dict()
    log.info("Filter results: %s", counts)

    if args.diagnostics:
        dp = Path(args.diagnostics)
        dp.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(diag).to_csv(dp, index=False)
        log.info("Diagnostics written to %s", dp)

    if len(rows) < _MIN_SYMBOLS:
        log.error("Universe too small: %d symbols (minimum %d). Check network access and loosen filters.",
                  len(rows), _MIN_SYMBOLS)
        sys.exit(1)

    df = pd.DataFrame(rows).sort_values("avg_dollar_vol_20d", ascending=False).reset_index(drop=True)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    print(f"\nUniverse written to {out_path}  ({len(df)} symbols, {args.provider} data, {source} symbol list)")
    print(f"  Price >= ${args.min_price:.0f}  |  "
          f"Avg vol >= {args.min_avg_vol:,}  |  "
          f"Dollar vol >= ${args.min_dollar_vol_m:g}M/day  |  "
          f"ATR% >= {args.min_atr_pct:.0f}%")
    print(f"  Most liquid (by dollar vol): {', '.join(df['symbol'].head(10).tolist())}")


if __name__ == "__main__":
    main()
