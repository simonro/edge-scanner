"""Live scanner — pre-market warmup and per-bar evaluation loop.

Lifecycle:
  1. Instantiate LiveScanner with a symbol list, DataFeed, and AlertSink.
  2. Call warmup() with pre-fetched history DataFrames to build SymbolStates.
  3. Call connect() to subscribe to live 1-min bars (blocks until stopped).
  4. Call reset_session() at end-of-day to clear intraday state for next session.

Bar routing inside _on_bar():
  • SPY bar  → update SPY intraday state → recompute market regime
  • Other bar → update symbol state (passing latest SPY bar for M5 RRS) →
                run Evaluator → push any alerts to AlertSink
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd

from scanner.alert_sink import AlertSink
from scanner.data.interface import DataFeed
from scanner.conditions import ConditionCtx
from scanner.market import MarketRegime, classify_market
from scanner.profiles import BarProfileCache, ProfileResult, profile_stats
from scanner.recent_activity import RecentActivity, describe_check
from scanner.state import SymbolState
from scanner.timing import bar_timer, perf_counter_ns
from scanner.trigger_catalog import SymbolSeries

log = logging.getLogger(__name__)


class LiveScanner:
    """Orchestrates warmup, live bar routing, evaluation, and alerting."""

    def __init__(
        self,
        symbols: list[str],
        feed: DataFeed,
        sink: Optional[AlertSink] = None,
        sector_map: Optional[dict[str, str]] = None,
    ) -> None:
        """
        Args:
            symbols:    list of ticker symbols to scan (exclude SPY — added automatically)
            feed:       DataFeed implementation (Alpaca or fake for tests)
            sink:       optional general-purpose sink, cleared on session reset.
                        Alerts go out through attach_system()'s sink.
            sector_map: optional mapping of symbol -> sector-ETF symbol for stacked RS
        """
        self.symbols = [s.upper() for s in symbols]
        self.feed = feed
        self.sink = sink if sink is not None else AlertSink()
        self._sector_map: dict[str, str] = sector_map or {}

        self._states: dict[str, SymbolState] = {}
        # Multi-timeframe candle rings, one per symbol. Owned here rather than
        # by CustomEvaluator because the universe conditions read them too, and
        # they must be available whether or not custom setups are attached. The
        # cost is not new: CustomEvaluator.on_bar already advanced these for
        # every symbol on every bar, before its own `not plan.keys` early out.
        self._series: dict[str, SymbolSeries] = {}
        # Universe profiles. None until attach_profiles(); when absent every
        # alert passes, so this is inert unless deliberately wired up.
        self._profiles = None
        self._spy_state: Optional[SymbolState] = None
        self._latest_spy_bar: Optional[dict] = None
        self._regime: MarketRegime = MarketRegime.NEUTRAL
        # Data freshness for the dashboard: the newest bar's own time, and the
        # wall clock when a bar last arrived. A connected socket says nothing
        # about whether bars are still flowing; these do.
        self.last_bar_ts: Optional[pd.Timestamp] = None
        self.last_bar_wall: Optional[float] = None

        # System setups from an optional engine plugin (attach_system). Every
        # evaluator runs inside this one process on the same states and bars,
        # so they all share ONE Alpaca stream (Alpaca allows a single
        # concurrent data websocket per account). Gets SPY's RTH % move for
        # relative strength vs SPY.
        self._system_evaluator = None
        self._system_sink: Optional[AlertSink] = None
        self._custom_sink: Optional[AlertSink] = None
        # A second, optional plugin evaluator (attach_de). Shares the system
        # sink but is a separate evaluator, so nothing about it can touch what
        # the first one emits.
        self._de_evaluator = None
        # Custom setups (universe + triggers composed in the dashboard). Runs
        # LAST, after every other evaluator, and emits through its own sink
        # with setup=<custom id>, custom=true. See scanner/custom_setups.py.
        self._custom_evaluator = None
        # What every setup did per symbol in the last few minutes, for the Setup
        # check window. Handed to each evaluator as it is attached.
        self.activity = RecentActivity()

    # ── System setups ────────────────────────────────────────────────────────

    def attach_system(self, evaluator, sink: AlertSink) -> None:
        """Attach a plugin's system-setup evaluator + sink to run on every non-SPY bar."""
        self._system_evaluator = evaluator
        self._system_sink = sink
        evaluator.activity = self.activity

    def attach_de(self, evaluator) -> None:
        """Attach a second plugin evaluator. It emits through the system sink, so
        attach_system() must have been called first; it has no feed of its own."""
        if self._system_sink is None:
            raise RuntimeError("attach_de() requires attach_system() first: it emits on the system feed")
        self._de_evaluator = evaluator
        evaluator.activity = self.activity

    def attach_custom(self, evaluator, sink: Optional[AlertSink] = None) -> None:
        """Attach a CustomEvaluator. Its alerts go to `sink`, or to the system
        sink when one is attached and no sink is given."""
        sink = sink if sink is not None else self._system_sink
        if sink is None:
            raise RuntimeError("attach_custom() needs a sink (or attach_system() first)")
        self._custom_sink = sink
        self._custom_evaluator = evaluator
        evaluator.activity = self.activity
        # Hand it this scanner's series store so both read the same rings. Any
        # series the evaluator already built are merged in, then it stops owning
        # them: _on_bar advances them from here on.
        if getattr(evaluator, "owns_series", False):
            for sym, s in evaluator._series.items():
                self._series.setdefault(sym, s)
            evaluator._series = self._series
            evaluator.owns_series = False
        for sym, s in self._series.items():
            for tf, period in evaluator.plan.emas:
                s.want_ema(tf, period)

    # ── Warmup ────────────────────────────────────────────────────────────────

    def warmup(
        self,
        spy_daily: pd.DataFrame,
        symbol_daily: dict[str, pd.DataFrame],
        sector_daily: Optional[dict[str, pd.DataFrame]] = None,
        bars_5m: Optional[dict[str, pd.DataFrame]] = None,
    ) -> None:
        """Build SymbolStates from pre-fetched history DataFrames.

        Separating data-fetch from state-build keeps this method testable
        without a live DataFeed.

        Args:
            spy_daily:      SPY daily OHLCV DataFrame (UTC index)
            symbol_daily:   {symbol: daily_df} for every symbol in self.symbols
            sector_daily:   {etf_symbol: daily_df} for sector ETFs (optional)
            bars_5m:        {symbol: 5m_df} for RVOL profile (optional)
        """
        # SPY gets its own SymbolState for intraday VWAP / regime tracking
        self._spy_state = SymbolState.from_history("SPY", spy_daily, spy_daily)
        log.debug("SPY state built")

        built = 0
        for sym in self.symbols:
            daily = symbol_daily.get(sym)
            if daily is None or daily.empty:
                log.warning("No daily history for %s — skipping", sym)
                continue

            sector_sym = self._sector_map.get(sym)
            sec_df = (sector_daily or {}).get(sector_sym) if sector_sym else None
            bars5m = (bars_5m or {}).get(sym)

            # One symbol's bad history must not stop the whole scanner.
            try:
                self._states[sym] = SymbolState.from_history(
                    sym,
                    daily,
                    spy_daily,
                    sector_daily=sec_df,
                    bars_5m_history=bars5m,
                )
            except Exception as exc:
                log.warning("Warmup failed for %s (%s), skipping", sym, exc)
                continue
            # Candle rings for the same symbol. seed_intraday fills 5/15/30/60
            # from the 5-min history; the 1- and 2-min rings are session-only by
            # design, so conditions on those timeframes have no baseline until
            # the session provides one.
            series = self._series.setdefault(sym, SymbolSeries(sym))
            series.seed_daily(daily)
            series.seed_intraday(bars5m)
            built += 1

        log.info("Warmup complete: %d / %d symbols loaded", built, len(self.symbols))

        if not self._sector_map:
            log.info(
                "No sector_map provided — sector_rrs is informational only "
                "(gate removed); the sector RS score bonus will be 0. "
                "Pass sector_map={sym: etf} to enable the bonus + Gate Check value."
            )
        no_profile = [sym for sym, st in self._states.items() if st.volume_profile.empty]
        if no_profile:
            log.warning(
                "RVOL volume profile missing for %d symbol(s): %s … "
                "Pass bars_5m to warmup() to enable the rvol gate.",
                len(no_profile),
                ", ".join(no_profile[:10]) + (" …" if len(no_profile) > 10 else ""),
            )

    def attach_profiles(self, engine) -> None:
        """Attach a ProfileEngine so alerts are screened before they are emitted.

        The check runs post-trigger, pre-emit: a profile is consulted only on
        the handful of would-be alerts per bar, never on every symbol, so it
        costs effectively nothing against the per-bar budget.
        """
        self._profiles = engine

    def _passes_profile(self, alert: dict, state: SymbolState, bar: dict,
                        session: str, cache, source: str) -> bool:
        """The choke point. Returns True when the alert may be emitted.

        Every evaluator's alerts pass through here, so screening is uniform and
        no evaluator needed changing. Failing open on an internal error is
        deliberate: a bug in this layer must not silence the scanner.

        Args:
            source: which evaluator produced this, "system" or "custom".
                Passed explicitly rather than sniffed from the payload.
        """
        eng = self._profiles
        if eng is None:
            return True
        try:
            if source == "custom":
                code = alert.get("setup", "")
                cp = eng.for_setup(code, self._custom_profile_ids.get(code))
            else:
                code = alert.get("setup", "")          # a system setup code
                cp = eng.for_setup(code)
            # Shared set first, then the setup's own. ANDed, so listing a
            # condition in both is not a conflict: the tighter threshold wins.
            params = (list(eng.params_for(self._custom_param_sets.get(code)))
                      + list(self._custom_params.get(code, ()))) if source == "custom" else []
            if (cp is None or cp.is_empty) and not params:
                return True
            ctx = ConditionCtx(state=state, series=self._series.get(state.symbol),
                               bar=bar, session=session,
                               direction=alert.get("direction"),
                               fundamentals=self._fundamentals_for(state.symbol),
                               regime=self._regime)
            res = eng.check(cp, ctx, cache)
            alert["universe_profile"] = res.to_json()
            alert["profile_hash"] = res.hash
            checks = list(res.checks)
            passed = res.passed
            if params:
                # The setup's own dynamic conditions: "what is it doing right
                # now", as opposed to the universe's "what kind of stock is it".
                # Evaluated even when the setup has no universe filter, and
                # folded into one stats record so the panel reports a single
                # blocked count per setup rather than two competing ones.
                pres = eng.check_conditions(params, ctx, cache)
                alert["parameters"] = pres.to_json()["checks"]
                checks += pres.checks
                passed = passed and pres.passed
            combined = ProfileResult(res.profile_id, res.name, res.hash, passed, checks)
            profile_stats.record(code or source, combined)
            if not passed:
                log.debug("screen blocked %s %s: %s", state.symbol, code,
                          "; ".join(f"{c.name}={c.reason}" for c in checks if not c.passed))
                why = [f"{describe_check(c)} ({res.name} universe)" for c in res.checks if not c.passed]
                if params:
                    why += [describe_check(c) for c in pres.checks if not c.passed]
                self.activity.add(state.symbol, bar.get("timestamp"), source, code,
                                  alert.get("direction", ""), "blocked",
                                  alert.get("entry_trigger") or alert.get("trigger", ""), why)
            return passed
        except Exception as exc:
            log.error("profile check failed for %s: %s", state.symbol, exc, exc_info=True)
            return True

    def _record_push(self, accepted, alert: dict, source: str, code: str):
        """Note what the sink did with an alert that passed the screen, and hand
        its answer straight back. A sink returns False when its own don't-repeat
        window swallows the alert, which is otherwise invisible."""
        try:
            self.activity.add(alert.get("symbol", ""), alert.get("timestamp"), source, code,
                              alert.get("direction", ""), "sent" if accepted is not False else "repeat",
                              alert.get("entry_trigger") or alert.get("trigger", ""),
                              [] if accepted is not False else ["the feed already sent this alert on this stock recently"])
        except Exception:
            pass
        return accepted

    def _fundamentals_for(self, symbol: str) -> Optional[dict]:
        try:
            from scanner.fundamentals import get_cache
            return get_cache().get(symbol)
        except Exception:
            return None

    @property
    def _custom_profile_ids(self) -> dict[str, str]:
        """{custom setup id: profile id} from the live compiled plan."""
        ce = self._custom_evaluator
        if ce is None:
            return {}
        return {s["id"]: s.get("universe_profile") or "" for s in ce.plan.setups}

    @property
    def _custom_param_sets(self) -> dict[str, str]:
        """{custom setup id: parameter set id} from the live compiled plan."""
        ce = self._custom_evaluator
        if ce is None:
            return {}
        return {s["id"]: (s.get("parameter_set") or "") for s in ce.plan.setups}

    @property
    def _custom_params(self) -> dict[str, list[dict]]:
        """{custom setup id: its own dynamic conditions} from the live plan."""
        ce = self._custom_evaluator
        if ce is None:
            return {}
        return {s["id"]: (s.get("parameters") or []) for s in ce.plan.setups}

    # ── Series ────────────────────────────────────────────────────────────────

    def series(self, symbol: str) -> SymbolSeries:
        """The candle rings for one symbol, created on first use."""
        s = self._series.get(symbol)
        if s is None:
            s = self._series[symbol] = SymbolSeries(symbol)
            ce = self._custom_evaluator
            if ce is not None:
                for tf, period in ce.plan.emas:
                    s.want_ema(tf, period)
        return s

    def _advance_series(self, state: SymbolState, bar: dict) -> str:
        """Push one bar into the symbol's candle rings. Returns the session tag."""
        ts = pd.Timestamp(bar["timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        et = ts.tz_convert("America/New_York")
        vwap = state.vwap
        return self.series(state.symbol).on_bar(
            bar, et.hour * 60 + et.minute, et.strftime("%Y-%m-%d"),
            float(vwap) if vwap is not None else None,
        )

    def _roll_session_if_new_day(self, bar: dict) -> bool:
        """Reset intraday state when the first bar of a new ET date arrives.

        Returns False for a bar that belongs to an EARLIER date than the session
        in progress (a late or replayed bar): the caller must drop it, because it
        would add yesterday's volume and prices to today's VWAP, volume and HOD/LOD.

        A process left running overnight used to carry yesterday's VWAP, volume,
        HOD/LOD and opening price into the new session. Startup seeding calls
        state.on_bar directly, so this only ever sees live bars.
        """
        try:
            ts = pd.Timestamp(bar["timestamp"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            day = ts.tz_convert("America/New_York").strftime("%Y-%m-%d")
        except Exception:
            return True
        prev = getattr(self, "_session_day", None)
        if prev is None:
            self._session_day = day
            return True
        if day < prev:
            log.warning("Dropped a %s bar dated %s: the session in progress is %s",
                        bar.get("symbol", "?"), day, prev)
            return False
        if day == prev:
            return True
        log.warning("New session %s (was %s): intraday state reset. Daily context "
                    "(prior close, ADV, volume profile) is from the last warmup; "
                    "restart to refresh it.", day, prev)
        self.reset_session()
        self._session_day = day
        return True

    # ── Bar routing ───────────────────────────────────────────────────────────

    def _on_bar(self, bar: dict) -> None:
        """Process one incoming 1-min bar.

        Called by the feed's streaming callback.  All state mutations and
        evaluations happen synchronously inside this method.

        Args:
            bar: dict with keys symbol, timestamp, open, high, low, close, volume
        """
        symbol: str = bar.get("symbol", "")
        if not self._roll_session_if_new_day(bar):
            return
        self.last_bar_wall = time.time()
        try:
            ts = pd.Timestamp(bar["timestamp"])
            if self.last_bar_ts is None or ts > self.last_bar_ts:
                self.last_bar_ts = ts
        except Exception:
            pass

        if symbol == "SPY":
            if self._spy_state is None:
                return
            self._spy_state.on_bar(bar)
            self._latest_spy_bar = bar
            self._regime = classify_market(bar["close"], self._spy_state.vwap)
            log.debug("SPY: close=%.2f vwap=%.2f regime=%s",
                      bar["close"], self._spy_state.vwap or 0, self._regime.value)
            return

        state = self._states.get(symbol)
        if state is None:
            return

        # Capacity instrumentation (scanner/timing.py). Off by default; when on,
        # a boolean read plus two perf_counter_ns calls per stage.
        _t = bar_timer.on
        if _t:
            _bar0 = perf_counter_ns()
            _t0 = _bar0

        state.on_bar(bar, self._latest_spy_bar)
        if _t:
            _t1 = perf_counter_ns(); bar_timer.record("state", _t1 - _t0); _t0 = _t1

        # Advance the candle rings once, here, so every consumer (custom setups
        # and the universe conditions) reads the same series. Must come after
        # state.on_bar: series.on_bar records the bar's VWAP, and this is the
        # same post-update value CustomEvaluator used to read.
        session = self._advance_series(state, bar)
        if _t:
            _t1 = perf_counter_ns(); bar_timer.record("series", _t1 - _t0); _t0 = _t1

        # What the existing evaluators fired on this bar, for custom setups that
        # reuse them as triggers ("setup:<code>").
        #
        # Recorded BEFORE the profile check, on purpose. ext_fired means "this
        # trigger fired", not "this alert was published". A custom setup using
        # setup:<code> as a trigger is screened by its OWN profile; letting that
        # system setup's profile also gate it would be surprising action at a distance.
        ext_fired: set[str] = set()
        # One memo per (symbol, bar) so setups sharing a profile, and profiles
        # sharing a condition, resolve it once. Dropped when the bar is done.
        pcache = BarProfileCache()

        if self._system_evaluator is not None and self._system_sink is not None:
            try:
                spy_chg = self._spy_state.rth_chg_pct if self._spy_state is not None else None
                spy_mom15 = self._spy_state.mom_15m_pct if self._spy_state is not None else None
                for alert in self._system_evaluator.on_bar(state, bar, spy_chg, spy_mom15):
                    ext_fired.add(f"setup:{alert['setup']}")
                    if not self._passes_profile(alert, state, bar, session, pcache, "system"):
                        continue
                    if self._record_push(self._system_sink.push(alert), alert, "system", alert["setup"]):
                        log.info(
                            "SYS ALERT %-6s  %-5s  %-8s  tier=%s  stop=%.2f (%.2f%%)  price=%.2f",
                            alert["symbol"], alert["direction"], alert["setup"],
                            alert.get("tier"), alert.get("suggested_stop") or 0,
                            alert.get("stop_pct") or 0, alert["price"],
                        )
            except Exception as exc:
                log.error("System evaluator error for %s: %s", symbol, exc, exc_info=True)
        if _t:
            _t1 = perf_counter_ns(); bar_timer.record("system", _t1 - _t0); _t0 = _t1

        # The second plugin evaluator runs after the first, never interleaved
        # with it. Its setups use their own codes, so their sink keys never
        # collide with the first evaluator's.
        if self._de_evaluator is not None and self._system_sink is not None:
            try:
                for alert in self._de_evaluator.on_bar(state, bar):
                    ext_fired.add(f"setup:{alert['setup']}")
                    if not self._passes_profile(alert, state, bar, session, pcache, "system"):
                        continue
                    if self._record_push(self._system_sink.push(alert), alert, "system", alert["setup"]):
                        log.info(
                            "DE ALERT  %-6s  %-5s  %-8s  slot=%s  stop=%.2f (%.2f%%)  price=%.2f",
                            alert["symbol"], alert["direction"], alert["setup"],
                            alert["context"].get("slot"), alert.get("suggested_stop") or 0,
                            alert.get("stop_pct") or 0, alert["price"],
                        )
            except Exception as exc:
                log.error("DE evaluator error for %s: %s", symbol, exc, exc_info=True)
        if _t:
            _t1 = perf_counter_ns(); bar_timer.record("de", _t1 - _t0); _t0 = _t1

        # Custom setups last: they may reuse anything above as a trigger and
        # never influence it. Best-effort: an error here never stops the rest.
        if self._custom_evaluator is not None and self._custom_sink is not None:
            try:
                spy_chg = self._spy_state.rth_chg_pct if self._spy_state is not None else None
                spy_mom15 = self._spy_state.mom_15m_pct if self._spy_state is not None else None
                for alert in self._custom_evaluator.on_bar(state, bar, ext_fired, spy_mom15,
                                                           session=session, spy_rth_chg_pct=spy_chg,
                                                           regime=self._regime):
                    if not self._passes_profile(alert, state, bar, session, pcache, "custom"):
                        continue
                    if self._record_push(self._custom_sink.push(alert), alert, "custom", alert["setup"]):
                        log.info("CS ALERT  %-6s  %-5s  %-18s  %s  price=%.2f",
                                 alert["symbol"], alert["direction"], alert["setup"],
                                 alert.get("trigger_note") or alert.get("entry_trigger"), alert["price"])
            except Exception as exc:
                log.error("Custom evaluator error for %s: %s", symbol, exc, exc_info=True)
        if _t:
            _end = perf_counter_ns()
            bar_timer.record("custom", _end - _t0)
            # Attribute this symbol's whole cost to its bar minute. The sum over
            # a minute is the number that has to fit inside the bar cadence.
            bar_timer.record_bar(str(bar.get("timestamp", ""))[:16], _end - _bar0)

    def ranked_symbols(self) -> list[str]:
        """Loaded symbols, most liquid first (20-day average dollar volume).

        A provider that caps its best data tier, as Schwab does at 300 real-bar
        symbols, is served in this order, so the names that matter get the best
        data. The universe loader returns symbols alphabetically, so the order
        has to be made here.
        """
        def _dollar_volume(sym: str) -> float:
            st = self._states[sym]
            try:
                v = float(st.adv20 or 0.0) * float(st.prior_close or 0.0)
            except (TypeError, ValueError):
                return 0.0
            return v if v == v else 0.0
        return sorted(self._states.keys(), key=lambda sym: (-_dollar_volume(sym), sym))

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Subscribe to 1-min bars and block until the stream ends.

        Calls feed.subscribe_minute_bars() which is expected to block.
        To stop cleanly, call feed.stop_stream() from a signal handler or
        another thread.
        """
        if not self._states:
            log.warning("connect() called before warmup — no symbols loaded")
        all_symbols = ["SPY"] + [sym for sym in self.ranked_symbols() if sym != "SPY"]
        log.info("Connecting to live feed for %d symbols", len(all_symbols))
        self.feed.subscribe_minute_bars(all_symbols, self._on_bar)

    # ── Session reset ─────────────────────────────────────────────────────────

    def reset_session(self) -> None:
        """Reset all intraday state for the next trading session.

        Call this after market close to prepare for tomorrow's session.
        """
        # An explicit reset (replay, the demo) already did the rollover: forget the
        # date so the next bar starts a session instead of triggering a second reset.
        self._session_day = None
        for state in self._states.values():
            state._reset_intraday()
        if self._spy_state is not None:
            self._spy_state._reset_intraday()
        self._latest_spy_bar = None
        self._regime = MarketRegime.NEUTRAL
        self.sink.clear()
        if self._system_evaluator is not None:
            self._system_evaluator.reset()
        if self._system_sink is not None:
            self._system_sink.clear()
        if self._custom_sink is not None and self._custom_sink is not self._system_sink:
            self._custom_sink.clear()
        if self._de_evaluator is not None:
            self._de_evaluator.reset()
        if self._custom_evaluator is not None:
            self._custom_evaluator.reset()
        try:
            from scanner.settings import gate_stats
            gate_stats.reset()
        except Exception:
            pass
        profile_stats.reset()
        # Static membership was resolved against yesterday's adv20 / ATR /
        # prior close. Those are only rebuilt at the next warmup, so drop the
        # member sets rather than carry a stale screen into a new session.
        if self._profiles is not None:
            self._profiles.invalidate_members()
        log.info("Session reset complete")
