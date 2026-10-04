"""Schwab as a full data provider: rate limiting, retries, batch history,
the range-aware cache and the expired-login check. Offline (fake client)."""
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from scanner.data import schwab as sw
from scanner.data.schwab import SchwabFeed, _RateLimiter


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _candles(start: date, n: int):
    return {"candles": [{"datetime": int(pd.Timestamp(start + timedelta(days=i), tz="UTC").timestamp() * 1000),
                         "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000} for i in range(n)]}


class _Client:
    def __init__(self, fail_first=0):
        self.calls, self.fail_first = [], fail_first

    def price_history(self, symbol, **kw):
        self.calls.append(symbol)
        if self.fail_first:
            self.fail_first -= 1
            return _Resp(429)
        start = kw["startDate"].date()
        return _Resp(200, _candles(start, 10))

    def quotes(self, symbols, fields):
        return _Resp(200, {s: {"quote": {"lastPrice": 20.0}} for s in symbols})


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(sw, "_LIMITER", _RateLimiter(1e9))
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)


def _feed(tmp_path, client):
    return SchwabFeed(cache_dir=tmp_path / "d", intraday_cache_dir=tmp_path / "m", client=client)


def test_rate_limiter_spaces_calls_evenly():
    t = [0.0]
    waits = []
    lim = _RateLimiter(120, clock=lambda: t[0], sleep=waits.append)
    for _ in range(4):
        lim.acquire()
    assert waits == pytest.approx([0.5, 1.0, 1.5])      # 120/min = one every 0.5 s


def test_request_retries_429_then_succeeds(tmp_path):
    c = _Client(fail_first=2)
    df = _feed(tmp_path, c).get_historical_daily("AAA", date(2026, 1, 5), date(2026, 1, 20))
    assert len(c.calls) == 3 and len(df) == 10


def test_daily_multi_fetches_each_symbol_and_reports_progress(tmp_path):
    c, seen = _Client(), []
    got = _feed(tmp_path, c).get_historical_daily_multi(["A", "B", "C"], date(2026, 1, 5), date(2026, 1, 20),
                                                        progress=lambda d, n: seen.append((d, n)))
    assert sorted(got) == ["A", "B", "C"] and sorted(c.calls) == ["A", "B", "C"] and seen[-1] == (3, 3)


def test_cache_is_reused_only_when_it_covers_the_requested_span(tmp_path):
    c = _Client()
    f = _feed(tmp_path, c)
    f.get_historical_daily("AAA", date(2026, 1, 5), date(2026, 1, 20))
    f.get_historical_daily("AAA", date(2026, 1, 7), date(2026, 1, 20))     # inside: cached
    assert c.calls == ["AAA"]
    f.get_historical_daily("AAA", date(2025, 1, 5), date(2026, 1, 20))     # longer: refetch
    assert c.calls == ["AAA", "AAA"]


def test_snapshot_goes_through_the_limiter(tmp_path):
    assert _feed(tmp_path, _Client()).get_snapshot(["A", "B"])["B"]["price"] == 20.0


def _tokens_db(path, issued: datetime):
    with sqlite3.connect(path) as con:
        con.execute("create table schwabdev (access_token_issued text, refresh_token_issued text)")
        con.execute("insert into schwabdev values (?, ?)", (issued.isoformat(), issued.isoformat()))
    return path


def test_expired_login_fails_fast_with_instructions(tmp_path, monkeypatch):
    db = _tokens_db(tmp_path / "t.db", datetime.now(timezone.utc) - timedelta(days=9))
    real = sw.refresh_token_age_days
    monkeypatch.setattr(sw, "refresh_token_age_days", lambda: real(db))
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    with pytest.raises(RuntimeError, match="schwab_auth.py"):
        SchwabFeed(cache_dir=tmp_path / "d", intraday_cache_dir=tmp_path / "m")


def test_refresh_token_age_reads_only_the_timestamp(tmp_path):
    db = _tokens_db(tmp_path / "t.db", datetime.now(timezone.utc) - timedelta(days=3))
    assert sw.refresh_token_age_days(db) == pytest.approx(3.0, abs=0.01)
    assert sw.refresh_token_age_days(tmp_path / "missing.db") is None


def test_four_hour_bars_are_built_from_30_minute_candles(tmp_path):
    class C(_Client):
        def price_history(self, symbol, **kw):
            assert kw["frequency"] == 30
            t0 = pd.Timestamp("2026-01-05 14:30", tz="UTC")
            return _Resp(200, {"candles": [{"datetime": int((t0 + pd.Timedelta(minutes=30 * i)).timestamp() * 1000),
                                            "open": 10 + i, "high": 11 + i, "low": 9, "close": 10 + i, "volume": 100}
                                           for i in range(8)]})
    df = _feed(tmp_path, C()).get_bars_range("AAA", "4Hour", date(2026, 1, 5), date(2026, 1, 5))
    assert len(df) == 2 and df["volume"].sum() == 800 and df["high"].max() == 18


# ── Schwab's 300-symbol cap on the 1-minute bar stream ───────────────────────

def test_parse_symbol_cap_reads_schwabs_code_19():
    from scanner.data.schwab import SchwabFeed
    over = {"response": [{"service": "CHART_EQUITY", "command": "ADD", "content": {
        "code": 19, "msg": "You've reached the maximum number of symbols allowed.  (CHART_EQUITY=300, DISCARDED=250)"}}]}
    ok = {"response": [{"service": "CHART_EQUITY", "command": "ADD", "content": {"code": 0, "msg": "ADD command succeeded"}}]}
    assert SchwabFeed.parse_symbol_cap(over) == 300
    assert SchwabFeed.parse_symbol_cap(ok) is None
    assert SchwabFeed.parse_symbol_cap("not json") is None


def _fake_feed(monkeypatch, sent, quotes=None):
    """A SchwabFeed with no network: a recording stream and a canned quotes API."""
    import sys, threading, types
    from scanner.data.schwab import SchwabFeed

    class FakeStream:
        receiver = None
        def __init__(self, client): pass
        def start(self, receiver, daemon=True): FakeStream.receiver = receiver
        def chart_equity(self, keys, fields): return ("CHART_EQUITY", list(keys))
        def level_one_equities(self, keys, fields): return ("LEVELONE_EQUITIES", list(keys))
        def send(self, req): sent.append(req)
        def stop(self): pass

    class FakeResp:
        status_code = 200
        def __init__(self, syms): self._syms = syms
        def json(self): return {sym: {"quote": dict(quotes or {})} for sym in self._syms}

    monkeypatch.setitem(sys.modules, "schwabdev", types.SimpleNamespace(Stream=FakeStream))
    feed = SchwabFeed.__new__(SchwabFeed)
    feed._client = types.SimpleNamespace(quotes=lambda symbols, fields: FakeResp(symbols))
    feed._stream, feed._stop_evt = None, threading.Event()
    feed.streamed_symbols, feed.unstreamed_symbols = [], []
    feed.quote_streamed_symbols, feed.polled_symbols, feed.quote_bars = [], [], None
    feed._stop_evt.set()                                   # return right after subscribing
    monkeypatch.setattr(feed._stop_evt, "clear", lambda: None)
    return feed, FakeStream


def test_large_universe_is_covered_in_three_tiers_in_the_order_given(monkeypatch, capsys):
    sent = []
    feed, _ = _fake_feed(monkeypatch, sent)
    symbols = [f"S{i}" for i in range(4000)]
    feed.subscribe_minute_bars(symbols, lambda bar: None)

    chart = [k for svc, keys in sent if svc == "CHART_EQUITY" for k in keys]
    quotes = [k for svc, keys in sent if svc == "LEVELONE_EQUITIES" for k in keys]
    assert chart == symbols[:300]                          # real bars: the first 300
    assert quotes == symbols[300:3300]                     # live quotes: the next 3,000
    assert feed.polled_symbols == symbols[3300:]           # polled quotes: the rest
    assert feed.unstreamed_symbols == []                   # nothing is left unscanned
    assert "Schwab coverage: 300 symbols on real 1-minute bars" in capsys.readouterr().out


def test_synthetic_bars_off_falls_back_to_the_first_300_and_says_so(monkeypatch, capsys):
    monkeypatch.setenv("SCHWAB_SYNTHETIC_BARS", "0")
    sent = []
    feed, _ = _fake_feed(monkeypatch, sent)
    symbols = [f"S{i}" for i in range(1000)]
    feed.subscribe_minute_bars(symbols, lambda bar: None)

    assert [k for svc, keys in sent for k in keys] == symbols[:300]
    assert feed.unstreamed_symbols == symbols[300:] and feed.polled_symbols == []
    assert "700 of your 1,000 symbols are NOT being scanned" in capsys.readouterr().out


def test_small_universe_uses_real_bars_only(monkeypatch):
    sent = []
    feed, _ = _fake_feed(monkeypatch, sent)
    feed.subscribe_minute_bars([f"S{i}" for i in range(120)], lambda bar: None)
    assert {svc for svc, _ in sent} == {"CHART_EQUITY"}
    assert feed.quote_streamed_symbols == [] and feed.polled_symbols == []


def test_streamed_quotes_become_bars_through_the_same_callback(monkeypatch):
    from scanner.data.quote_bars import QuoteBarBuilder
    from scanner.data.schwab import SchwabFeed
    bars = []
    t = [600_000 * 60.0]
    b = QuoteBarBuilder(bars.append, lambda: t[0])
    b.track("MU", grace=3.0)
    msg = lambda **f: {"data": [{"service": "LEVELONE_EQUITIES", "content": [{"key": "MU", **f}]}]}
    SchwabFeed.handle_quotes(msg(**{"3": 100.0, "8": 5000, "10": 101.0, "11": 99.0}), b)
    t[0] += 5
    SchwabFeed.handle_quotes(msg(**{"8": 5400}), b)                    # only what changed
    t[0] += 5
    SchwabFeed.handle_quotes(msg(**{"3": 100.6, "8": 5900}), b)
    t[0] += 70
    b.flush()
    assert len(bars) == 1
    assert (bars[0]["open"], bars[0]["close"], bars[0]["volume"]) == (100.0, 100.6, 900)


def test_quote_stream_cap_moves_the_overflow_to_polling(monkeypatch):
    sent = []
    feed, stream_cls = _fake_feed(monkeypatch, sent)
    symbols = [f"S{i}" for i in range(3500)]
    feed.subscribe_minute_bars(symbols, lambda bar: None)
    stream_cls.receiver({"response": [{"service": "LEVELONE_EQUITIES", "command": "ADD", "content": {
        "code": 19, "msg": "You've reached the maximum number of symbols allowed.  (LEVELONE_EQUITIES=2000, DISCARDED=250)"}}]})
    assert feed.quote_streamed_symbols == symbols[300:2300]
    assert feed.polled_symbols == symbols[2300:]          # overflow first, then the original tail


def test_short_history_is_not_downloaded_again_once_that_span_was_asked_for(tmp_path):
    """A recent listing can never reach back to the start asked for. Once that
    span has been requested, what is cached is everything the provider has."""
    c = _Client()
    f = _feed(tmp_path, c)
    far_back = date(2020, 1, 1)                     # long before the fake client's first candle
    f.get_historical_daily("AAA", far_back, date(2026, 1, 20))
    f.get_historical_daily("AAA", far_back, date(2026, 1, 20))
    assert c.calls == ["AAA"]                       # second call reused the cache
    f.get_historical_daily("AAA", date(2019, 1, 1), date(2026, 1, 20))     # longer still: ask again
    assert c.calls == ["AAA", "AAA"]
    assert _feed(tmp_path, c).get_historical_daily("AAA", far_back, date(2026, 1, 20)) is not None
    assert c.calls == ["AAA", "AAA"]                # the record survives a restart


def test_cache_written_after_the_last_close_is_fresh_the_next_morning(tmp_path):
    """Monday morning's file holds Friday's bars. Tuesday asks for Monday: the file
    was written before Monday's close, so it is stale. Monday evening's refresh,
    which asks for Monday and is written after its close, is fresh on Tuesday."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from scanner.data.schwab import _fresh
    et = ZoneInfo("America/New_York")
    idx = pd.date_range("2026-09-14 06:00", "2026-09-18 06:00", freq="B", tz="UTC")
    df = pd.DataFrame({"volume": 1.0}, index=idx)               # data through Fri 09-18
    p = tmp_path / "x.parquet"; p.write_bytes(b"x")
    import os
    os.utime(p, (datetime(2026, 9, 21, 9, 43, tzinfo=et).timestamp(),) * 2)      # written Mon 09:43
    assert _fresh(p, df, date(2026, 9, 18), date(2026, 9, 20))   # asked for Friday: fine
    assert not _fresh(p, df, date(2026, 9, 21), date(2026, 9, 20))   # asked for Monday: stale
    os.utime(p, (datetime(2026, 9, 21, 17, 30, tzinfo=et).timestamp(),) * 2)     # rewritten Mon 17:30
    assert _fresh(p, df, date(2026, 9, 21), date(2026, 9, 21))   # Tuesday morning: nothing more to get
    assert _fresh(p, df, date(2026, 9, 20), date(2026, 9, 21))   # a Sunday rolls back to Friday


def test_an_evening_start_does_not_leave_a_day_short_file_that_reads_as_fresh(tmp_path):
    """Issue #20. A start on Friday evening asks for history through Thursday and
    writes the file after Friday's close. Monday asks through Sunday (target
    Friday): the file was written after Friday's close but never asked for
    Friday, so it must be downloaded again."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import os
    et = ZoneInfo("America/New_York")
    friday_evening = datetime(2026, 9, 25, 18, 30, tzinfo=et).timestamp()
    c = _Client()
    _feed(tmp_path, c).get_historical_daily("AAA", date(2026, 9, 1), date(2026, 9, 24))      # through Thursday
    os.utime(tmp_path / "d" / "AAA.parquet", (friday_evening,) * 2)
    _feed(tmp_path, c).get_historical_daily("AAA", date(2026, 9, 1), date(2026, 9, 27))      # Monday's start
    assert c.calls == ["AAA", "AAA"]                # Friday's bar is fetched, not assumed

    _feed(tmp_path, c).get_historical_bars("BBB", "5Min", date(2026, 9, 1), date(2026, 9, 24))
    os.utime(tmp_path / "m" / "BBB.parquet", (friday_evening,) * 2)
    _feed(tmp_path, c).get_historical_bars("BBB", "5Min", date(2026, 9, 1), date(2026, 9, 27))
    assert c.calls[2:] == ["BBB", "BBB"]            # same for the 5-minute cache


def test_a_file_that_asked_for_the_session_stays_fresh_without_its_bar(tmp_path):
    """The written-after-close shortcut still holds when the request did ask for
    the session: a halted symbol has no bar that day and must not be downloaded
    again on every start."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import os
    et = ZoneInfo("America/New_York")
    c = _Client()
    _feed(tmp_path, c).get_historical_daily("AAA", date(2026, 9, 1), date(2026, 9, 25))      # Friday's refresh
    os.utime(tmp_path / "d" / "AAA.parquet", (datetime(2026, 9, 25, 17, 30, tzinfo=et).timestamp(),) * 2)
    _feed(tmp_path, c).get_historical_daily("AAA", date(2026, 9, 1), date(2026, 9, 27))      # data ends 09-10
    assert c.calls == ["AAA"]


def test_other_bar_sizes_never_replace_the_5_minute_cache(tmp_path):
    # The 5-minute cache is keyed by symbol and feeds the RVOL profile. A
    # 1-minute fetch cached under the same name read as 5-minute history and
    # made RVOL about 5x too high.
    c = _Client()
    f = _feed(tmp_path, c)
    f.get_historical_bars("AAA", "1Min", date(2026, 1, 5), date(2026, 1, 20))
    assert not (tmp_path / "m" / "AAA.parquet").exists()
    f.get_historical_bars("AAA", "5Min", date(2026, 1, 5), date(2026, 1, 20))
    assert (tmp_path / "m" / "AAA.parquet").exists()


def test_a_lower_chart_cap_on_a_small_universe_still_polls_the_overflow(monkeypatch):
    """250 symbols fit under the 300 cap, so nothing was polled at the start and
    the poller never started. Schwab then reports a cap of 200: the 50 moved to
    polling were advertised as polled but nothing polled them (audit 3, C11)."""
    import json
    started = []
    real = sw.threading.Thread
    class Recorded(real):
        def start(self):
            started.append(self.name)
    monkeypatch.setattr(sw.threading, "Thread", Recorded)
    sent = []
    feed, stream = _fake_feed(monkeypatch, sent)
    feed.subscribe_minute_bars([f"S{i}" for i in range(250)], lambda bar: None)
    stream.receiver(json.dumps({"response": [{"service": "CHART_EQUITY", "content": {
        "code": 19, "msg": "max (CHART_EQUITY=200, DISCARDED=50)"}}]}))
    assert len(feed.streamed_symbols) == 200 and len(feed.polled_symbols) == 50
    assert {"schwab-quote-poll", "schwab-quote-bars"} <= set(started)
