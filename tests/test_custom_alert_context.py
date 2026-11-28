"""Market context on custom-setup alerts: the scanner's regime at fire time and
relative strength vs SPY, under the keys the dashboard's Regime, RS and RRS
columns read. Payload only, all offline: real values pass through, and a value
the engine does not have stays None."""
from __future__ import annotations

import pandas as pd
import pytest

from scanner.alert_sink import AlertSink
from scanner.custom_setups import CustomEvaluator, CustomSetupStore
from scanner.live_scanner import LiveScanner
from scanner.market import MarketRegime
from scanner.trigger_catalog import Fire
from tests.helpers import _bar, _daily, _minutes, _state

_SETUP = {"id": "cs_hod", "name": "New high", "enabled": True, "mode": "or", "direction": "all",
          "sessions": ["rth"], "triggers": [{"id": "hod", "options": ["high"]}]}


def _evaluator(tmp_path) -> CustomEvaluator:
    store = CustomSetupStore(tmp_path / "custom", defaults=tmp_path / "none.json")
    store.save(_SETUP)
    return CustomEvaluator(store)


def _fire(ev: CustomEvaluator, st, **kw) -> dict:
    """Three rising bars through the state and the evaluator: the last new-high alert."""
    out: list[dict] = []
    for et, p in zip(_minutes("09:30", 3), [100.0, 101.0, 101.5]):
        bar = _bar(p, et=et, sym=st.symbol)
        st.on_bar(bar)
        out.extend(ev.on_bar(st, bar, set(), **kw))
    assert out, "the fixture setup should fire"
    return out[-1]


# ── evaluator: what it is handed is what the alert carries ───────────────────

@pytest.mark.parametrize("regime, expected", [
    (MarketRegime.BULLISH, "bullish"),
    (MarketRegime.BEARISH, "bearish"),
    (MarketRegime.NEUTRAL, "neutral"),
    ("bearish", "bearish"),            # the plain string value is accepted too
])
def test_regime_is_passed_through(tmp_path, regime, expected):
    a = _fire(_evaluator(tmp_path), _state(), regime=regime)
    assert a["market_regime"] == expected


def test_rs_fields_carry_the_engine_values(tmp_path):
    st = _state()
    st._rrs_m5 = 1.25                  # the intraday RRS the state would hold after an hour
    a = _fire(_evaluator(tmp_path), st, spy_rth_chg_pct=0.4, regime=MarketRegime.BULLISH)
    # RS column: context.rs_vs_spy_pct = stock RTH % move minus SPY's
    assert st.rth_chg_pct == pytest.approx(1.5)
    assert a["context"]["rs_vs_spy_pct"] == pytest.approx(st.rth_chg_pct - 0.4)
    assert a["context"]["rrs_m5"] == pytest.approx(1.25)
    # RRS column: conditions.rrs_d1.value = the state's daily RRS, not gated
    assert st.rrs_d1 == st.rrs_d1, "the fixture history should give a daily RRS"
    assert a["conditions"]["rrs_d1"] == {"value": pytest.approx(st.rrs_d1), "pass": None, "status": "ok"}


def test_missing_context_stays_none(tmp_path):
    st = _state()
    st.rrs_d1 = float("nan")           # too little daily history for an RRS
    a = _fire(_evaluator(tmp_path), st)   # no regime, no SPY move, no intraday RRS yet
    assert a["market_regime"] == "neutral"
    assert a["context"]["rs_vs_spy_pct"] is None
    assert a["context"]["rrs_m5"] is None
    assert a["conditions"]["rrs_d1"] == {"value": None, "pass": None, "status": "no_data"}


def test_rs_is_none_before_the_stock_has_an_rth_open(tmp_path):
    st = _state()
    assert st.rth_chg_pct is None
    alert = _evaluator(tmp_path)._build_alert(
        st, _bar(100.0, et="2024-01-02 09:30"), pd.Timestamp("2024-01-02 09:30", tz="America/New_York"),
        _SETUP, "hod:high", Fire("long"), ["hod:high"], "rth",
        spy_rth_chg_pct=0.4, regime=MarketRegime.BULLISH)
    assert alert["context"]["rs_vs_spy_pct"] is None


# ── live path: the alert agrees with the scanner (what the clock reports) ────

class _Collect(AlertSink):
    def __init__(self):
        super().__init__()
        self.alerts: list[dict] = []

    def push(self, alert: dict) -> bool:
        self.alerts.append(alert)
        return True


def _live_bar(sym: str, price: float, et: str) -> dict:
    return _bar(price, et=f"2024-01-02 {et}", sym=sym)


def test_live_alert_carries_the_scanners_regime_and_spy_relative_strength(tmp_path):
    daily = _daily()
    sc = LiveScanner(["AAA"], None, AlertSink())
    sc.warmup(_daily(base=450.0, step=0.1), {"AAA": daily})
    sink = _Collect()
    sc.attach_custom(_evaluator(tmp_path), sink)

    # SPY rallies off its open: above session VWAP, so the regime turns bullish.
    sc._on_bar(_live_bar("SPY", 450.0, "09:30"))
    sc._on_bar(_live_bar("AAA", 100.0, "09:30"))
    sc._on_bar(_live_bar("SPY", 452.0, "09:31"))
    assert sc._regime is MarketRegime.BULLISH
    sc._on_bar(_live_bar("AAA", 101.0, "09:31"))
    up = sink.alerts[-1]
    assert up["timestamp"].startswith("2024-01-02T09:31") and up["market_regime"] == "bullish"
    assert up["context"]["rs_vs_spy_pct"] == pytest.approx(
        sc._states["AAA"].rth_chg_pct - sc._spy_state.rth_chg_pct)

    # SPY sells off below VWAP: the next alert reads bearish, not the earlier value.
    sc._on_bar(_live_bar("SPY", 445.0, "09:32"))
    assert sc._regime is MarketRegime.BEARISH
    sc._on_bar(_live_bar("AAA", 102.0, "09:32"))
    down = sink.alerts[-1]
    assert down["timestamp"].startswith("2024-01-02T09:32") and down["market_regime"] == "bearish"
    assert down["context"]["rs_vs_spy_pct"] == pytest.approx(
        sc._states["AAA"].rth_chg_pct - sc._spy_state.rth_chg_pct)
    assert down["context"]["rs_vs_spy_pct"] > 0      # stock up 2%, SPY down about 1.1%
