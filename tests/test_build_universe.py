"""The universe screen (scripts/build_universe.py) and the side-by-side
comparison (scripts/compare_universes.py), offline."""
import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bu = _load("build_universe")
cu = _load("compare_universes")

ARGS = SimpleNamespace(min_price=15.0, min_avg_vol=5_000_000, min_dollar_vol_m=50.0, min_atr_pct=1.0)


def _daily(n: int, close: float, vol: float, rng: float) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp(date.today() - timedelta(days=1), tz="UTC"), periods=n, freq="B")
    return pd.DataFrame({"open": close, "high": close + rng / 2, "low": close - rng / 2,
                         "close": close, "volume": vol}, index=idx)


def test_bar_metrics_and_short_history():
    m = bu.bar_metrics(_daily(30, 50.0, 6e6, 1.0), 20)
    assert m == {"avg_vol_20d": 6_000_000, "avg_dollar_vol_20d": 300_000_000, "atr_pct": 2.0, "last_price": 50.0}
    assert bu.bar_metrics(_daily(5, 50.0, 6e6, 1.0), 20) is None


def test_screen_reports_the_first_filter_each_symbol_failed():
    metrics = {"OK": bu.bar_metrics(_daily(25, 50, 6e6, 1.0), 20),
               "THIN": bu.bar_metrics(_daily(25, 50, 1e6, 1.0), 20),
               "CALM": bu.bar_metrics(_daily(25, 50, 6e6, 0.1), 20)}
    prices = {"OK": 50, "THIN": 50, "CALM": 50, "CHEAP": 5, "NEW": 50}
    rows, diag = bu.screen(["OK", "THIN", "CALM", "CHEAP", "NEW", "GONE"], prices, metrics, ARGS)
    assert [r["symbol"] for r in rows] == ["OK"]
    assert {d["symbol"]: d["result"] for d in diag} == {
        "OK": "kept", "THIN": "avg_vol", "CALM": "atr_pct", "CHEAP": "price", "NEW": "no_history", "GONE": "no_price"}


def test_a_non_batch_feed_goes_through_the_generic_interface():
    class Feed:
        def get_snapshot(self, symbols):
            return {s: {"price": 50.0} for s in symbols}

        def get_historical_daily_multi(self, symbols, start, end, progress=None):
            self.span = (start, end)
            return {s: _daily(25, 50, 6e6, 1.0) for s in symbols}

    f = Feed()
    assert bu._feed_prices(f, ["A", "B"]) == {"A": 50.0, "B": 50.0}
    m = bu._feed_bar_metrics(f, ["A"], 20)
    assert m["A"]["avg_vol_20d"] == 6_000_000
    assert (date.today() - f.span[0]).days >= 380          # primes the warmup cache


def _diag(path: Path, rows: list[dict]) -> Path:
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_compare_classifies_list_data_and_near_cutoff(tmp_path):
    kept = {"price": 50, "avg_vol_20d": 6e6, "avg_dollar_vol_20d": 3e8, "atr_pct": 2.0, "result": "kept"}
    a = _diag(tmp_path / "a.csv", [
        {"symbol": "BOTH", **kept}, {"symbol": "ONLYA", **kept}, {"symbol": "VOLA", **kept},
        {"symbol": "NA", **kept}])
    b = _diag(tmp_path / "b.csv", [
        {"symbol": "BOTH", **kept},
        {"symbol": "VOLA", **{**kept, "avg_vol_20d": 4.8e6, "result": "avg_vol"}},
        {"symbol": "NA", **kept}, {"symbol": "ONLYB", **kept}])
    report = cu.compare(a, b, "x:1", "y:2", "default")
    assert "| in both | 2 |" in report                      # BOTH and NA (a real ticker)
    assert "| ONLYA | list | not in the other symbol list |" in report
    assert "| VOLA | data | avg_vol: 4,800,000 there vs 6,000,000 here (threshold 5,000,000) | yes |" in report
    assert "| ONLYB | list |" in report


def test_iex_scales_the_dollar_volume_floor(monkeypatch):
    """IEX sees about 2.5% of consolidated volume: with the SIP floor a first
    start on the free feed screened out nearly everything (discussion #19)."""
    monkeypatch.delenv("ALPACA_FEED", raising=False)
    assert bu.default_dollar_vol_m("alpaca") == 150.0            # SIP, the default
    monkeypatch.setenv("ALPACA_FEED", "iex")
    assert bu.default_dollar_vol_m("alpaca") == 3.75
    assert bu.default_dollar_vol_m("schwab") == 150.0            # the setting is Alpaca's alone
    # $20 x 250k shares = $5M a day: out on the SIP floor, in on the IEX one
    metrics = {"ABC": {"last_price": 20.0, "avg_vol_20d": 250_000, "avg_dollar_vol_20d": 5_000_000, "atr_pct": 2.0}}
    args = SimpleNamespace(min_price=15.0, min_avg_vol=0, min_dollar_vol_m=3.75, min_atr_pct=1.0)
    assert bu.screen(["ABC"], {"ABC": 20.0}, metrics, args)[1][0]["result"] == "kept"
    args.min_dollar_vol_m = 150.0
    assert bu.screen(["ABC"], {"ABC": 20.0}, metrics, args)[1][0]["result"] == "dollar_vol"
