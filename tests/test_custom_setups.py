"""Custom setups: trigger catalog, evaluator, store and the /api/v2/setups routes.

All offline. States are driven through SymbolState.on_bar with synthetic
1-min bars so the candle series, VWAP and levels are real.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scanner.api import AppState
from scanner.custom_setups import (
    _DEFAULTS_FILE, CustomEvaluator, CustomSetupStore, SetupError, SetupNames, normalize_setup, summary_lines,
)
from scanner.trigger_catalog import BY_ID, CATALOG, SymbolSeries, candle_key, catalog_json
from tests.test_api_v2 import FakeScanner
from tests.helpers import _bar, _feed, _state


# ── helpers ──────────────────────────────────────────────────────────────────

def _setup(sid: str, triggers: list[dict], **kw) -> dict:
    d = {"id": sid, "name": sid, "enabled": True, "mode": "or", "direction": "all", "sessions": ["rth"],
         "triggers": triggers}
    d.update(kw)
    return d


def _evaluator(tmp_path: Path, *setups: dict) -> CustomEvaluator:
    store = CustomSetupStore(tmp_path / "custom", defaults=tmp_path / "none.json")
    for s in setups:
        store.save(s)
    return CustomEvaluator(store)


def _run(ev: CustomEvaluator, state, prices, start="09:30", external=None, **kw) -> list[dict]:
    """Feed bars through the state AND the evaluator (the LiveScanner order)."""
    out: list[dict] = []
    from tests.helpers import _minutes
    for et, p in zip(_minutes(start, len(prices)), prices):
        bar = _bar(p, et=et, sym=state.symbol, **kw)
        state.on_bar(bar)
        out.extend(ev.on_bar(state, bar, external or set()))
    return out


# ── catalog ──────────────────────────────────────────────────────────────────

def test_catalog_is_well_formed():
    from scanner import plugins
    ids = [t.id for t in CATALOG]
    native = [t for t in CATALOG if t.source == "native"]
    assert len(ids) == len(set(ids)) and len(native) >= 45
    js = catalog_json()
    assert all({"id", "name", "category", "desc", "direction", "options", "params", "sessions", "source"} <= set(t) for t in js)
    # every option-bearing native trigger has a default option
    for t in CATALOG:
        if t.options and t.source == "native":
            assert t.default_options, t.id
    for name in ("hod", "cross_above", "bull_engulfing", "orb_breakout", "through_vwap"):
        assert name in BY_ID
    # the only external triggers are the installed plugin's system setup pass-throughs
    assert all(t.source == "native" or t.id.startswith("setup:") for t in CATALOG)
    assert sorted(t.id for t in CATALOG if t.source == "system") == sorted(f"setup:{c}" for c in plugins.SYSTEM_CODES)


def test_candle_key_anchors():
    assert candle_key(9 * 60 + 30, 5) == ("rth", 0)
    assert candle_key(9 * 60 + 34, 5) == ("rth", 0)
    assert candle_key(9 * 60 + 35, 5) == ("rth", 1)
    assert candle_key(10 * 60 + 30, 60) == ("rth", 1)
    assert candle_key(4 * 60 + 7, 5) == ("pre", 1)


def test_series_aggregates_and_completes_candles():
    s = SymbolSeries("X")
    for i in range(11):
        em = 9 * 60 + 30 + i
        s.on_bar({"open": 100 + i, "high": 100.5 + i, "low": 99.5 + i, "close": 100.2 + i, "volume": 1000}, em, "2024-01-02", 100.0)
    assert len(s.candles[5]) == 2 and s.completed[5] is True            # 09:40 bar closed the 09:35 candle
    assert s.candles[5][0]["high"] == pytest.approx(104.5) and s.candles[5][0]["volume"] == 5000
    assert len(s.candles[1]) == 10 and s.day_high == pytest.approx(110.5)
    assert s.partial[15]["key"] == ("2024-01-02", "rth", 0)


# ── native triggers ──────────────────────────────────────────────────────────

def test_hod_trigger_fires_on_new_high_only(tmp_path):
    ev = _evaluator(tmp_path, _setup("s1", [{"id": "hod", "options": ["high"]}]))
    st = _state()
    alerts = _run(ev, st, [100.0, 100.5, 100.2, 101.0, 100.9])
    # bar2 (100.5 > 100.0 high), bar4 (101.0 > 100.5). Sink-level dedupe is not applied here.
    assert [a["price"] for a in alerts] == [100.5, 101.0]
    a = alerts[0]
    assert a["setup"] == "s1" and a["custom"] is True and a["direction"] == "long"
    assert a["trigger"].startswith("CS_s1:hod:high") and a["setup_label"] == "s1"
    assert "suggested_stop" in a and "context" in a


def test_direction_filter_and_sessions(tmp_path):
    ev = _evaluator(tmp_path, _setup("s1", [{"id": "hod", "options": ["high", "low"]}], direction="short"))
    st = _state()
    alerts = _run(ev, st, [100.0, 100.5, 99.0, 99.5, 98.5])
    assert all(a["direction"] == "short" for a in alerts) and len(alerts) == 2
    # premarket bars: RTH-only setup stays silent
    ev2 = _evaluator(tmp_path / "b", _setup("s2", [{"id": "hod_ext", "options": ["high"]}], sessions=["rth"]))
    st2 = _state(symbol="MSFT")
    assert _run(ev2, st2, [100.0, 101.0, 102.0], start="09:00") == []
    ev3 = _evaluator(tmp_path / "c", _setup("s3", [{"id": "hod_ext", "options": ["high"]}], sessions=["pre", "rth"]))
    st3 = _state(symbol="NVDA")
    assert len(_run(ev3, st3, [100.0, 101.0, 102.0], start="09:00")) == 2


def test_cross_above_vwap_and_prior_close(tmp_path):
    ev = _evaluator(tmp_path, _setup("s1", [{"id": "cross_above", "options": ["prior_close", "vwap"]}]))
    st = _state(prior_close=100.0)
    # closes: 99, 98.8 (below prior close and VWAP), 100.4 (crosses prior close 100 and VWAP ~98.9)
    alerts = _run(ev, st, [99.0, 98.8, 100.4])
    assert len(alerts) == 1                       # one alert per setup per bar
    assert set(alerts[0]["triggers_fired"]) == {"cross_above:prior_close", "cross_above:vwap"}
    assert alerts[0]["score"] > 55                # multi-trigger bonus


def test_new_candle_high_5min_once_per_candle(tmp_path):
    ev = _evaluator(tmp_path, _setup("s1", [{"id": "new_candle_high", "options": ["5"], "params": {"since": 1}}]))
    st = _state()
    prices = [100.0] * 5 + [101.0, 101.5, 101.8, 101.9, 102.0]     # 2nd candle keeps making highs
    alerts = _run(ev, st, prices)
    assert len(alerts) == 1 and alerts[0]["price"] == 101.0


def test_bull_engulfing_on_completed_5min_candle(tmp_path):
    ev = _evaluator(tmp_path, _setup("s1", [{"id": "bull_engulfing", "options": ["5"]}]))
    st = _state()
    # candle 1 red 100 -> 99 ; candle 2 green 98.9 -> 100.4 engulfs; candle 3 starts -> fires on its first bar
    prices = [100.0, 99.8, 99.5, 99.2, 99.0, 98.9, 99.5, 99.9, 100.2, 100.4, 100.3]
    opens = [None] * 5 + [98.9] + [None] * 5
    from tests.helpers import _minutes
    out = []
    for i, (et, p) in enumerate(zip(_minutes("09:30", len(prices)), prices)):
        bar = _bar(p, et=et, o=opens[i] if opens[i] is not None else (prices[i - 1] if i else p))
        st.on_bar(bar)
        out.extend(ev.on_bar(st, bar, set()))
    assert len(out) == 1 and out[0]["direction"] == "long" and "engulfing" in out[0]["trigger_note"]


def test_orb_breakout_once_per_day(tmp_path):
    ev = _evaluator(tmp_path, _setup("s1", [{"id": "orb_breakout", "options": ["5"]}]))
    st = _state()
    prices = [100.0, 100.4, 100.2, 100.3, 100.1] + [100.2, 100.6, 100.8, 100.3, 100.7]
    alerts = _run(ev, st, prices)
    # opening candle high = 100.45 (bar high = close + 0.05); first close above it is 100.6
    assert [a["price"] for a in alerts] == [100.6]


def test_running_up_and_repeat_seconds(tmp_path):
    ev = _evaluator(tmp_path, _setup("s1", [{"id": "running", "options": ["up"], "params": {"min_pct": 0.5}}], repeat_sec=180))
    st = _state()
    alerts = _run(ev, st, [100.0, 100.6, 101.3, 101.2, 102.0])   # +0.6%, +0.7%, -, +0.8%
    # 2nd fire at 09:32 is inside the 180 s repeat window of 09:31; 09:34 is 180 s later -> allowed
    assert [a["price"] for a in alerts] == [100.6, 102.0]


def test_and_mode_requires_all_triggers_in_window(tmp_path):
    ev = _evaluator(tmp_path, _setup("s1", [{"id": "hod", "options": ["high"]},
                                            {"id": "running", "options": ["up"], "params": {"min_pct": 0.5}}],
                                     mode="and", and_window_min=5))
    st = _state()
    # bar2: new HOD + running up (+1%) -> both same bar -> fires. bar3: only HOD -> no.
    alerts = _run(ev, st, [100.0, 101.0, 101.2])
    assert [a["price"] for a in alerts] == [101.0]


def test_external_triggers_pass_through(tmp_path, fake_plugin):
    ev = _evaluator(tmp_path, _setup("s1", [{"id": "setup:X1"}, {"id": "setup:X2"}]))
    st = _state()
    bar = _bar(100.0, et="2024-01-02 10:00")
    st.on_bar(bar)
    assert ev.on_bar(st, bar, set()) == []
    out = ev.on_bar(st, bar, {"setup:X1", "setup:unrelated"})
    assert len(out) == 1 and out[0]["triggers_fired"] == ["setup:X1"]
    out = ev.on_bar(st, _bar(100.1, et="2024-01-02 10:01"), {"setup:X1", "setup:X2"})
    assert set(out[0]["triggers_fired"]) == {"setup:X1", "setup:X2"}


def test_a_system_passthrough_trigger_is_unknown_without_the_plugin(monkeypatch):
    from tests.helpers import install_fake_plugin
    install_fake_plugin(monkeypatch, setups=())
    with pytest.raises(SetupError):
        normalize_setup({"id": "s1", "name": "x", "triggers": [{"id": "setup:X1"}]})


def test_disabled_setup_is_silent_and_reload_picks_up_changes(tmp_path):
    store = CustomSetupStore(tmp_path / "custom", defaults=tmp_path / "none.json")
    store.save(_setup("s1", [{"id": "hod", "options": ["high"]}], enabled=False))
    ev = CustomEvaluator(store)
    st = _state()
    assert _run(ev, st, [100.0, 101.0]) == []
    store.save(_setup("s1", [{"id": "hod", "options": ["high"]}], enabled=True))
    ev.reload()
    assert len(_run(ev, st, [102.0], start="09:32")) == 1
    assert ev.stats()["setups"]["s1"]["hod:high"] == 1


def test_stock_check_reports_levels_and_last_eval(tmp_path):
    ev = _evaluator(tmp_path, _setup("s1", [{"id": "cross_above", "options": ["prior_close"]}, {"id": "hod", "options": ["high"]}]))
    st = _state(prior_close=100.0)
    _run(ev, st, [99.0, 100.5])
    chk = ev.check(ev.store.get("s1"), st)
    rows = {r["key"]: r for r in chk["triggers"]}
    assert chk["in_universe"] and chk["bars_1m"] == 2
    assert rows["cross_above:prior_close"]["level"] == 100.0 and rows["cross_above:prior_close"]["fired_last_bar"] is True
    assert rows["hod:high"]["fired_last_bar"] is True and rows["hod:high"]["fires_today"] == 1


# ── store / validation ───────────────────────────────────────────────────────

def test_normalize_setup_validates_and_fills_defaults(fake_plugin):
    s = normalize_setup({"id": "x1", "name": "X", "triggers": [{"id": "volume_spike", "options": ["5", "99"], "params": {"ratio": "3"}}]})
    t = s["triggers"][0]
    assert t["options"] == ["5"] and t["params"] == {"ratio": 3.0, "lookback": 10.0}
    assert s["mode"] == "or" and s["sessions"] == ["rth"] and s["color"].startswith("#")
    with pytest.raises(SetupError):
        normalize_setup({"id": "X1", "name": "clash", "triggers": []})    # a system setup code
    with pytest.raises(SetupError):
        normalize_setup({"id": "x2", "name": "bad", "triggers": [{"id": "nope"}]})
    with pytest.raises(SetupError):
        normalize_setup({"id": "x3", "name": ""})
    lines = summary_lines(s)
    assert lines["alerts"] == ["Volume spike · 5 Min (Min relative volume 3.0 x, Average of 10.0 candles)"]


def test_store_seeds_defaults_once(tmp_path):
    store = CustomSetupStore(tmp_path / "custom")            # real defaults file
    seeded = store.load_all()
    shipped = json.loads(_DEFAULTS_FILE.read_text(encoding="utf-8"))["setups"]
    assert len(seeded) == len(shipped)
    assert all(s["source"] == "sample" and s["pending_filters"] == [] for s in seeded)
    assert all(BY_ID.get(t["id"]) for s in seeded for t in s["triggers"])
    store.delete(seeded[0]["id"])
    assert len(CustomSetupStore(tmp_path / "custom").load_all()) == len(seeded) - 1   # no re-seed


def test_setup_names_roundtrip(tmp_path, fake_plugin):
    n = SetupNames(tmp_path / "names.json")
    assert n.load() == {"X1": "Fake Long", "X2": "Fake Short"} == SetupNames.defaults()
    assert n.save({"X1": "Renamed", "ZZ": "ignored"})["X1"] == "Renamed"
    assert SetupNames(tmp_path / "names.json").load()["X1"] == "Renamed"
    assert json.loads((tmp_path / "names.json").read_text(encoding="utf-8")) == {"X1": "Renamed"}
    assert n.save({"X1": ""})["X1"] == "Fake Long"


def test_setup_names_are_empty_without_a_plugin(tmp_path, monkeypatch):
    from tests.helpers import install_fake_plugin
    install_fake_plugin(monkeypatch, setups=())
    n = SetupNames(tmp_path / "names.json")
    assert n.load() == {} and n.save({"X1": "ignored"}) == {}


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path: Path):
    st = _state(symbol="AAA", prior_close=100.0)
    _feed(st, [99.0, 100.5, 101.0], start="09:30")
    scanner = FakeScanner({"AAA": st})
    app_state = AppState(scanner=scanner, feed=None)  # type: ignore[arg-type]
    ev = CustomEvaluator(CustomSetupStore(tmp_path / "setups" / "custom", defaults=tmp_path / "none.json"))
    app_state.custom_eval = ev
    from scanner.api_v2 import register_v2_routes
    app = FastAPI()
    register_v2_routes(app, app_state, layouts_dir=tmp_path / "layouts", watchlists_path=tmp_path / "wl.json",
                       fundamentals_path=tmp_path / "fund.json", universe_csv=tmp_path / "nope.csv",
                       sector_csv=tmp_path / "nope2.csv", setups_dir=tmp_path / "setups")
    return TestClient(app), ev, st


def test_setups_api_crud_and_hot_reload(client, fake_plugin):
    from scanner import trigger_catalog
    c, ev, st = client
    r = c.get("/api/v2/setups").json()
    assert r["live"] is True and r["system"] == [
        {"code": "X1", "name": "Fake Long", "default_name": "Fake Long", "direction": "long"},
        {"code": "X2", "name": "Fake Short", "default_name": "Fake Short", "direction": "short"}]
    assert len(r["catalog"]) == len(trigger_catalog.CATALOG)
    assert {"setup:X1", "setup:X2"} <= {t["id"] for t in r["catalog"]}
    assert r["custom"] == []
    body = _setup("cs_test", [{"id": "hod", "options": ["high"]}])
    r = c.put("/api/v2/setups/cs_test", json=body)
    assert r.status_code == 200 and r.json()["setup"]["summary"]["alerts"] == ["High/Low of the day · High"]
    assert [s["id"] for s in ev.plan.setups] == ["cs_test"]           # evaluator reloaded
    assert c.put("/api/v2/setups/other", json=body).status_code == 400   # id mismatch
    assert c.put("/api/v2/setups/cs_bad", json={"id": "cs_bad", "name": "x", "triggers": [{"id": "nope"}]}).status_code == 400
    assert c.put("/api/v2/setups/names", json={"X2": "Renamed"}).json()["names"]["X2"] == "Renamed"
    assert [s["name"] for s in c.get("/api/v2/setups").json()["system"] if s["code"] == "X2"] == ["Renamed"]
    assert c.delete("/api/v2/setups/cs_test").json()["ok"] is True
    assert ev.plan.setups == []


def test_setups_api_stock_check(client, fake_plugin):
    c, ev, st = client
    c.put("/api/v2/setups/cs_chk", json=_setup("cs_chk", [{"id": "cross_above", "options": ["prior_close"]}]))
    bar = _bar(101.5, et="2024-01-02 09:33", sym="AAA")
    st.on_bar(bar)
    ev.on_bar(st, bar, set())
    r = c.get("/api/v2/setups/cs_chk/check?symbol=aaa").json()
    assert r["in_universe"] is True and r["triggers"][0]["level"] == 100.0
    # a system code goes to the plugin's check, case-insensitively
    r = c.get("/api/v2/setups/x1/check?symbol=AAA").json()
    assert r["setup"] == "X1" and any(g["name"] == "gap" for g in r["gates"]) and "fired" in r
    r = c.get("/api/v2/setups/X1/check?symbol=ZZZ").json()
    assert r["in_universe"] is False
    assert c.get("/api/v2/setups/nope/check?symbol=AAA").status_code == 404


# ── parameters: the setup's own dynamic conditions ───────────────────────────

def test_a_static_condition_is_rejected_from_parameters():
    """Parameters answer "what is it doing right now". Anything fixed for the
    session is a universe condition and belongs in a shared filter."""
    with pytest.raises(SetupError, match="universe condition"):
        normalize_setup({"id": "cs_x", "name": "X", "triggers": [],
                         "parameters": [{"id": "avg_vol_20d", "op": "gte", "value": 1e6}]})


def test_parameters_normalize_and_round_trip():
    s = normalize_setup({"id": "cs_x", "name": "X", "triggers": [], "parameters": [
        {"id": "rvol", "op": "gte", "value": 1.5},
        {"id": "rel_vol", "option": "2", "op": "gte", "value": 200},
    ]})
    assert [c["id"] for c in s["parameters"]] == ["rvol", "rel_vol"]
    assert s["parameters"][1]["option"] == "2"
    assert s["parameters"][0]["params"] == {}


def test_the_same_parameter_twice_is_rejected():
    with pytest.raises(SetupError, match="twice"):
        normalize_setup({"id": "cs_x", "name": "X", "triggers": [], "parameters": [
            {"id": "rvol", "op": "gte", "value": 1.0},
            {"id": "rvol", "op": "gte", "value": 2.0}]})


def test_the_same_parameter_on_different_options_is_allowed():
    s = normalize_setup({"id": "cs_x", "name": "X", "triggers": [], "parameters": [
        {"id": "rel_vol", "option": "2", "value": 200},
        {"id": "rel_vol", "option": "5", "value": 150}]})
    assert len(s["parameters"]) == 2


def test_a_time_window_from_two_operators_is_allowed():
    s = normalize_setup({"id": "cs_x", "name": "X", "triggers": [], "parameters": [
        {"id": "time_et", "op": "gte", "value": 935},
        {"id": "time_et", "op": "lte", "value": 1530}]})
    assert [(c["op"], c["value"]) for c in s["parameters"]] == [("gte", 935.0), ("lte", 1530.0)]


def test_a_setup_with_no_parameters_is_still_valid():
    assert normalize_setup({"id": "cs_x", "name": "X", "triggers": []})["parameters"] == []


# ── EMA vs EMA, and the "at least N of" mode ─────────────────────────────────
#
# Every other composable cross compares PRICE to a level; a 3/9 or 3/8 EMA
# crossover compares two moving averages, and a trend-filtered trigger adds an
# EMA(8)-over-EMA(21) filter on top of an ordinary VWAP event. These make both
# expressible as ordinary setups.

def _setup(sid, triggers, **kw):
    return {"id": sid, "name": sid, "enabled": True, "mode": kw.pop("mode", "or"),
            "direction": kw.pop("direction", "all"), "sessions": ["rth"],
            "triggers": triggers, **kw}


def _ema_cross(fast=3, slow=9, tf=1, opt="up"):
    return [{"id": "ema_cross_ema", "options": [opt],
             "params": {"fast": fast, "slow": slow, "tf": tf}}]


def test_ema_cross_ema_fires_on_the_crossing_bar_only(tmp_path):
    ev = _evaluator(tmp_path, _setup("cs_x", _ema_cross(), direction="long"))
    st = _state(symbol="AAA", prior_close=100.0)
    # down then up: the fast EMA must cross the slow one exactly once
    prices = [100 - i * 0.5 for i in range(14)] + [94 + i * 1.5 for i in range(14)]
    out = _run(ev, st, prices)
    assert len(out) == 1, f"expected one crossing, got {[a['trigger_note'] for a in out]}"
    assert "EMA(3) crossed above EMA(9)" in out[0]["trigger_note"]


def test_ema_cross_ema_ignores_a_fast_slower_than_the_slow(tmp_path):
    ev = _evaluator(tmp_path, _setup("cs_x", _ema_cross(fast=21, slow=9), direction="long"))
    st = _state(symbol="AAA", prior_close=100.0)
    assert _run(ev, st, [100 + i for i in range(20)]) == []


def test_the_plan_preregisters_both_emas_so_they_seed_from_history(tmp_path):
    """An EMA created on first use starts cold and is wrong until it converges."""
    ev = _evaluator(tmp_path, _setup("cs_x", _ema_cross(fast=3, slow=9, tf=5)))
    assert (5, 3) in ev.plan.emas and (5, 9) in ev.plan.emas


def test_the_plan_preregisters_emas_named_by_a_parameter(tmp_path):
    ev = _evaluator(tmp_path, _setup(
        "cs_x", [{"id": "hod", "options": ["high"], "params": {}}],
        parameters=[{"id": "ema_stack", "op": "gte", "value": 0.0, "option": "trade",
                     "params": {"fast": 8, "slow": 21, "tf": 1}}]))
    assert (1, 8) in ev.plan.emas and (1, 21) in ev.plan.emas


def test_at_least_n_of_fires_on_two_of_three(tmp_path):
    trigs = [{"id": "hod", "options": ["high"], "params": {}},
             {"id": "cross_above", "options": ["prior_close"], "params": {}},
             {"id": "cross_above", "options": ["vwap"], "params": {}}]
    ev = _evaluator(tmp_path, _setup("cs_x", trigs, mode="atleast", min_triggers=2,
                                     direction="long", and_window_min=30))
    st = _state(symbol="AAA", prior_close=100.0)
    out = _run(ev, st, [99.0, 99.5, 101.0, 102.0, 103.0])
    assert out, "a new high plus a prior-close cross is two of the three"


def test_at_least_n_of_does_not_fire_on_one_of_three(tmp_path):
    trigs = [{"id": "hod", "options": ["high"], "params": {}},
             {"id": "cross_above", "options": ["prior_close"], "params": {}},
             {"id": "cross_above", "options": ["vwap"], "params": {}}]
    ev = _evaluator(tmp_path, _setup("cs_x", trigs, mode="atleast", min_triggers=3,
                                     direction="long", and_window_min=30))
    st = _state(symbol="AAA", prior_close=100.0)
    # Rising but never reaching the prior close, so that cross can never happen:
    # a new high of day and a VWAP cross are only two of the three.
    assert _run(ev, st, [95.0, 95.5, 96.0, 96.5, 97.0]) == []


def test_and_is_atleast_with_the_count_pinned_to_every_alert(tmp_path):
    trigs = [{"id": "hod", "options": ["high"], "params": {}},
             {"id": "cross_above", "options": ["prior_close"], "params": {}}]
    st_a, st_b = _state(symbol="AAA", prior_close=100.0), _state(symbol="AAA", prior_close=100.0)
    prices = [99.0, 99.5, 101.0, 102.0]
    a = _run(_evaluator(tmp_path / "a", _setup("cs_x", trigs, mode="and",
                                               direction="long", and_window_min=30)), st_a, prices)
    b = _run(_evaluator(tmp_path / "b", _setup("cs_x", trigs, mode="atleast", min_triggers=2,
                                               direction="long", and_window_min=30)), st_b, prices)
    assert len(a) == len(b)


def test_min_triggers_is_capped_at_the_number_of_alerts(tmp_path):
    """Asking for 5 of 2 would otherwise make a setup that can never fire."""
    trigs = [{"id": "hod", "options": ["high"], "params": {}},
             {"id": "cross_above", "options": ["prior_close"], "params": {}}]
    ev = _evaluator(tmp_path, _setup("cs_x", trigs, mode="atleast", min_triggers=5,
                                     direction="long", and_window_min=30))
    st = _state(symbol="AAA", prior_close=100.0)
    assert _run(ev, st, [99.0, 99.5, 101.0, 102.0])


def test_mode_summary_says_how_many(tmp_path):
    from scanner.custom_setups import summary_lines
    s = normalize_setup(_setup("cs_x", [{"id": "hod", "options": ["high"], "params": {}}],
                               mode="atleast", min_triggers=2))
    assert "At least 2 of the alerts" in summary_lines(s)["mode"]


def test_a_lazily_created_ema_matches_a_preregistered_one():
    """Pre-registration is an optimisation, not a correctness fix: ema() seeds
    from the candle ring when it is created. Asserted so nobody later 'fixes'
    a bug that does not exist, or removes the seeding that makes it true."""
    def feed(s, n):
        for i in range(n):
            p = 100 + i * 0.3
            s.on_bar({"open": p, "high": p + 0.1, "low": p - 0.1, "close": p, "volume": 1000},
                     570 + i, "2026-09-09", None)
    early = SymbolSeries("A"); early.want_ema(1, 21); feed(early, 60)
    late = SymbolSeries("B"); feed(late, 60)
    assert early.ema(1, 21).value == late.ema(1, 21).value


# ── range break (the general form of an opening-range break) ─────────────────

def _rb(bars=5, tf=1, max_range_pct=1.5, vol_mult=1.5, opt="up"):
    return [{"id": "range_break", "options": [opt],
             "params": {"bars": bars, "tf": tf,
                        "max_range_pct": max_range_pct, "vol_mult": vol_mult}}]


def _feed_bars(ev, state, rows, start="09:30"):
    """rows = (price, volume) pairs, one per 1-min bar."""
    from tests.helpers import _minutes
    out = []
    for et, (p, v) in zip(_minutes(start, len(rows)), rows):
        bar = _bar(p, et=et, sym=state.symbol, vol=v)
        state.on_bar(bar)
        out.extend(ev.on_bar(state, bar, set()))
    return out


def test_range_break_fires_when_a_tight_range_breaks_on_volume(tmp_path):
    ev = _evaluator(tmp_path, _setup("cs_x", _rb(), direction="long"))
    st = _state(symbol="AAA", prior_close=100.0)
    quiet = [(100.0, 1000.0), (100.1, 1000.0), (100.0, 1000.0),
             (100.1, 1000.0), (100.0, 1000.0), (100.05, 1000.0)]
    out = _feed_bars(ev, st, quiet + [(101.0, 5000.0)])
    assert out, "a break above a tight range on 5x volume should fire"
    assert "range high" in out[0]["trigger_note"]


def test_range_break_ignores_a_break_without_volume(tmp_path):
    ev = _evaluator(tmp_path, _setup("cs_x", _rb(vol_mult=3.0), direction="long"))
    st = _state(symbol="AAA", prior_close=100.0)
    quiet = [(100.0, 1000.0)] * 6
    assert _feed_bars(ev, st, quiet + [(101.0, 1100.0)]) == []


def test_range_break_ignores_a_range_that_was_never_tight(tmp_path):
    ev = _evaluator(tmp_path, _setup("cs_x", _rb(max_range_pct=0.2), direction="long"))
    st = _state(symbol="AAA", prior_close=100.0)
    wide = [(100.0, 1000.0), (103.0, 1000.0), (99.0, 1000.0),
            (102.0, 1000.0), (99.5, 1000.0), (101.0, 1000.0)]
    assert _feed_bars(ev, st, wide + [(105.0, 9000.0)]) == []


def test_range_break_does_not_repeat_on_the_same_range(tmp_path):
    ev = _evaluator(tmp_path, _setup("cs_x", _rb(), direction="long"))
    st = _state(symbol="AAA", prior_close=100.0)
    quiet = [(100.0, 1000.0)] * 6
    out = _feed_bars(ev, st, quiet + [(101.0, 5000.0), (101.2, 5000.0)])
    assert len(out) == 1, "a broken range is no longer a range"


def test_range_break_down_needs_the_down_option(tmp_path):
    ev = _evaluator(tmp_path, _setup("cs_x", _rb(opt="down"), direction="short"))
    st = _state(symbol="AAA", prior_close=100.0)
    quiet = [(100.0, 1000.0)] * 6
    out = _feed_bars(ev, st, quiet + [(99.0, 5000.0)])
    assert out and "range low" in out[0]["trigger_note"]


# ── V off VWAP: a bounce, not a stock parked on the line ─────────────────────

def _v_ctx(closes, touch, atr=2.0, vwap=100.0):
    """Five-minute candles at `closes`, then a touch candle (o, h, l, c), all
    recorded against a flat VWAP. Returns an EvalCtx on the bar that completes
    the touch candle, which is when the trigger evaluates."""
    from types import SimpleNamespace
    from scanner.trigger_catalog import EvalCtx
    s = SymbolSeries("X")
    m = 600
    for c in closes:
        for _ in range(5):
            s.on_bar({"open": c, "high": c + 0.05, "low": c - 0.05, "close": c, "volume": 1000},
                     m, "2026-09-11", vwap)
            m += 1
    o, h, l, c = touch
    for i in range(5):
        s.on_bar({"open": o if i == 0 else c, "high": h, "low": l, "close": c, "volume": 1000},
                 m, "2026-09-11", vwap)
        m += 1
    bar = {"open": c, "high": c, "low": c, "close": c, "volume": 1000}
    s.on_bar(bar, m, "2026-09-11", vwap)            # opens the next candle: touch is now COMPLETED
    return EvalCtx(state=SimpleNamespace(symbol="X", atr_d1=atr), series=s, bar=bar,
                   et_min=m, session="rth", external=set())


def _v(opt="support", **kw):
    from scanner.trigger_catalog import _IMPL
    p = {"tf": 5, "lookback": 6, "away_atr": 0.3, "band_atr": 0.1, "max_dwell": 1, **kw}
    return lambda ctx: _IMPL["vwap_v"](ctx, opt, p)


def test_a_v_off_vwap_fires():
    # well above VWAP (1 ATR = $2, so +$1.20 is 0.6 ATR), then a touch at VWAP
    # and a strong green close back above it
    ctx = _v_ctx([101.2, 101.0, 100.8, 100.6, 100.5], touch=(100.1, 100.7, 99.95, 100.6))
    f = _v()(ctx)
    assert f is not None and f.direction == "long"


def test_a_stock_parked_on_vwap_does_not_fire():
    """The alerts you are seeing: every candle closes on the line."""
    ctx = _v_ctx([100.02, 99.98, 100.03, 100.01, 99.99], touch=(100.0, 100.5, 99.95, 100.45))
    assert _v()(ctx) is None


def test_it_must_have_come_from_distance():
    ctx = _v_ctx([100.3, 100.3, 100.3, 100.3, 100.3], touch=(100.2, 100.7, 99.95, 100.6))
    assert _v(max_dwell=5)(ctx) is None         # never more than 0.15 ATR away


def test_a_weak_touch_candle_does_not_count():
    """Came from distance, but the touch candle closed red: not a rejection."""
    ctx = _v_ctx([101.2, 101.0, 100.8, 100.6, 100.5], touch=(100.7, 100.8, 99.95, 100.3))
    assert _v()(ctx) is None


def test_the_short_side_mirrors_it():
    ctx = _v_ctx([98.8, 99.0, 99.2, 99.4, 99.5], touch=(99.9, 100.05, 99.3, 99.4))
    f = _v("resistance")(ctx)
    assert f is not None and f.direction == "short"


# ── Crossing above / below on a chosen candle size ───────────────────────────

def _cross(tid, opt, tf):
    from scanner.trigger_catalog import _IMPL
    return lambda ctx: _IMPL[tid](ctx, opt, {"tf": tf})


def _fires(minutes, tid, opt, tf):
    """Evaluate on each bar as it arrives; the series must be read live, so the
    trigger runs inside the feed loop rather than after it."""
    from types import SimpleNamespace
    from scanner.trigger_catalog import EvalCtx
    s = SymbolSeries("X")
    f = _cross(tid, opt, tf)
    hits = []
    for i, (c, v) in enumerate(minutes):
        bar = {"open": c, "high": c + 0.02, "low": c - 0.02, "close": c, "volume": 1000}
        s.on_bar(bar, 600 + i, "2026-09-14", v)
        ctx = EvalCtx(state=SimpleNamespace(symbol="X", vwap=v, prior_close=100.0), series=s,
                      bar=bar, et_min=600 + i, session="rth", external=set())
        got = f(ctx)
        if got is not None:
            hits.append((i, got))
    return hits


def test_a_5_min_close_through_vwap_fires_once_when_the_candle_completes():
    below = [(99.5, 100.0)] * 5                   # 10:00-10:04 closes below VWAP
    above = [(100.5, 100.0)] * 5                  # 10:05-10:09 closes above
    hits = _fires(below + above + [(100.5, 100.0)], "cross_above", "vwap", 5)
    assert [i for i, _ in hits] == [10]           # the bar that completes 10:05-10:09
    assert hits[0][1].direction == "long" and "5 Min close" in hits[0][1].note


def test_a_1_min_poke_that_fails_inside_the_candle_does_not_count_on_5_min():
    poke = [(99.5, 100.0), (99.5, 100.0), (100.4, 100.0), (99.6, 100.0), (99.5, 100.0)]
    minutes = [(99.5, 100.0)] * 5 + poke + [(99.5, 100.0)]
    assert _fires(minutes, "cross_above", "vwap", 5) == []
    assert len(_fires(minutes, "cross_above", "vwap", 1)) == 1   # the 1-min rule still sees it


def test_3_min_candles_are_built_for_the_cross():
    minutes = [(100.5, 100.0)] * 3 + [(99.5, 100.0)] * 3 + [(99.5, 100.0)]
    hits = _fires(minutes, "cross_below", "vwap", 3)
    assert [i for i, _ in hits] == [6] and hits[0][1].direction == "short"


def test_vwap_is_read_off_each_candle():
    """The first candle closed at 100.10, ABOVE its own VWAP of 100.00; VWAP then
    rose to 100.15. Measured against today's VWAP that earlier close looks below
    it, which would invent a cross from a stock that never left the upper side."""
    minutes = [(100.1, 100.0)] * 5 + [(100.2, 100.15)] * 5 + [(100.2, 100.15)]
    assert _fires(minutes, "cross_above", "vwap", 5) == []


def test_other_levels_cross_on_candles_too():
    minutes = [(99.8, 100.0)] * 5 + [(100.3, 100.0)] * 5 + [(100.3, 100.0)]
    hits = _fires(minutes, "cross_above", "prior_close", 5)
    assert [i for i, _ in hits] == [10]


def test_the_overnight_gap_is_not_a_cross():
    from types import SimpleNamespace
    from scanner.trigger_catalog import EvalCtx, _IMPL
    s = SymbolSeries("X")
    for i in range(5):
        s.on_bar({"open": 99.0, "high": 99.0, "low": 99.0, "close": 99.0, "volume": 1}, 955 + i, "2026-09-11", 100.0)
    hit = None
    for i in range(6):
        bar = {"open": 101.0, "high": 101.0, "low": 101.0, "close": 101.0, "volume": 1}
        s.on_bar(bar, 570 + i, "2026-09-14", 100.0)
        ctx = EvalCtx(state=SimpleNamespace(symbol="X", vwap=100.0), series=s, bar=bar,
                      et_min=570 + i, session="rth", external=set())
        hit = hit or _IMPL["cross_above"](ctx, "vwap", {"tf": 5})
    assert hit is None


@pytest.mark.parametrize("raw,expected", [(None, 1.0), (3, 3.0), (5, 5.0), (15, 15.0), (7, 5.0), (60, 15.0)])
def test_candle_size_saves_as_one_of_the_offered_sizes(raw, expected):
    t = {"id": "cross_above", "options": ["vwap"]}
    if raw is not None:
        t["params"] = {"tf": raw}
    got = normalize_setup(_setup("s1", [t]))
    assert got["triggers"][0]["params"]["tf"] == expected


# ── range break: width in % of price, x daily ATR, or x the average candle ───

def _rbu(unit, width, **kw):
    t = _rb(max_range_pct=width, **kw)
    t[0]["params"]["width_unit"] = unit
    return t


def test_range_width_in_daily_atr_scales_with_the_stock(tmp_path):
    """A $0.20 range on a $100 stock: 0.2% of price. Whether that is tight
    depends on how much the stock moves in a day, which % of price cannot see."""
    st = _state(symbol="AAA", prior_close=100.0)
    atr = st.atr_d1
    quiet = [(100.0, 1000.0), (100.1, 1000.0), (100.0, 1000.0),
             (100.1, 1000.0), (100.0, 1000.0), (100.05, 1000.0)]
    brk = [(101.0, 5000.0)]
    rng = 0.2                                      # 99.95-100.15: closes plus the 0.05 bar wicks
    ev = _evaluator(tmp_path / "fits", _setup("cs_x", _rbu(1, 2 * rng / atr), direction="long"))
    assert _feed_bars(ev, st, quiet + brk), "range inside the ATR multiple should fire"
    st2 = _state(symbol="AAA", prior_close=100.0)
    tight = 0.6 * rng / atr                        # a limit narrower than the range itself
    ev2 = _evaluator(tmp_path / "tight", _setup("cs_x", _rbu(1, max(0.1, tight)), direction="long"))
    assert _feed_bars(ev2, st2, quiet + brk) == []


def test_range_width_in_average_candles_needs_a_quieter_range_than_before(tmp_path):
    busy = [(100.0 + (1.0 if i % 2 else -1.0), 1000.0) for i in range(20)]   # ~2.1-wide candles
    quiet = [(100.0, 1000.0), (100.1, 1000.0), (100.0, 1000.0),
             (100.1, 1000.0), (100.0, 1000.0), (100.05, 1000.0)]             # 0.2-wide
    brk = [(101.5, 9000.0)]
    ev = _evaluator(tmp_path / "a", _setup("cs_x", _rbu(2, 2.0), direction="long"))
    st = _state(symbol="AAA", prior_close=100.0)
    assert _feed_bars(ev, st, busy + quiet + brk), "a range far quieter than the candles before it"
    # the same quiet range with no busy history before it has no baseline, so it cannot qualify
    ev2 = _evaluator(tmp_path / "b", _setup("cs_x", _rbu(2, 2.0), direction="long"))
    st2 = _state(symbol="AAA", prior_close=100.0)
    assert _feed_bars(ev2, st2, quiet + brk) == []


def test_percent_stays_the_default_unit():
    got = normalize_setup(_setup("s1", _rb()))
    assert got["triggers"][0]["params"]["width_unit"] == 0.0


def test_two_range_breaks_with_different_widths_do_not_share_a_latch(tmp_path):
    """Both setups see the same break. With one latch key per candle count, the
    first to reach it marked the break as seen and the second never fired."""
    ev = _evaluator(tmp_path, _setup("cs_wide", _rb(max_range_pct=5.0), direction="long"),
                    _setup("cs_tight", _rb(max_range_pct=1.0), direction="long"))
    st = _state(symbol="AAA", prior_close=100.0)
    quiet = [(100.0, 1000.0), (100.1, 1000.0), (100.0, 1000.0),
             (100.1, 1000.0), (100.0, 1000.0), (100.05, 1000.0)]
    out = _feed_bars(ev, st, quiet + [(101.0, 5000.0)])
    assert {a["setup"] for a in out} == {"cs_wide", "cs_tight"}


# ── running up / down in % of price, x daily ATR, or x the average 1-min candle ──

def _running_hits(minutes, unit, thr, atr=2.0):
    """minutes = (close, high-low half width) per 1-min bar; evaluates on each bar."""
    from types import SimpleNamespace
    from scanner.trigger_catalog import EvalCtx, _IMPL
    s = SymbolSeries("X")
    hits = []
    for i, (c, half) in enumerate(minutes):
        bar = {"open": c, "high": c + half, "low": c - half, "close": c, "volume": 1000}
        s.on_bar(bar, 600 + i, "2026-09-14", 100.0)
        ctx = EvalCtx(state=SimpleNamespace(symbol="X", atr_d1=atr), series=s, bar=bar,
                      et_min=600 + i, session="rth", external=set())
        f = _IMPL["running"](ctx, "up", {"min_pct": thr, "width_unit": unit})
        if f is not None:
            hits.append(i)
    return hits


def test_running_in_percent_is_unchanged():
    assert _running_hits([(100.0, 0.05), (100.6, 0.05)], 0, 0.5) == [1]
    assert _running_hits([(100.0, 0.05), (100.4, 0.05)], 0, 0.5) == []


def test_running_in_daily_atr_scales_with_the_stock():
    move = [(100.0, 0.05), (100.6, 0.05)]                  # +$0.60
    assert _running_hits(move, 1, 0.25, atr=2.0) == [1]      # 0.30 ATR on a quiet stock
    assert _running_hits(move, 1, 0.25, atr=6.0) == []       # 0.10 ATR on a wild one


def test_running_in_average_candles_compares_with_the_last_20_minutes():
    calm = [(100.0, 0.05)] * 20                              # 0.10-wide candles
    assert _running_hits(calm + [(100.4, 0.05)], 2, 3.0) == [20]      # 4x a normal minute
    busy = [(100.0, 0.5)] * 20                               # 1.00-wide candles
    assert _running_hits(busy + [(100.4, 0.05)], 2, 3.0) == []        # 0.4x a normal minute


def test_running_in_average_candles_waits_for_a_baseline():
    assert _running_hits([(100.0, 0.05)] * 5 + [(101.0, 0.05)], 2, 3.0) == []


def test_running_accepts_the_small_values_an_atr_threshold_needs():
    """0.5% of price matches about 0.09 x daily ATR in alert count (24-26 Aug)."""
    t = {"id": "running", "options": ["up"], "params": {"min_pct": 0.09, "width_unit": 1}}
    got = normalize_setup(_setup("s1", [t]))["triggers"][0]["params"]
    assert got == {"min_pct": 0.09, "width_unit": 1.0}


# ── N-day high / low ─────────────────────────────────────────────────────────

def _nday_ctx(n_sessions, close, prev_close):
    from types import SimpleNamespace
    from scanner.trigger_catalog import EvalCtx
    idx = pd.date_range("2026-06-01", periods=n_sessions, freq="B", tz="UTC")
    highs = [100.0 + (i % 7) for i in range(n_sessions)]        # 100..106 repeating
    highs[-20:] = [104.0] * 20                                   # the last 20 sessions top out at 104
    daily = pd.DataFrame({"open": highs, "high": highs, "low": [h - 5 for h in highs],
                          "close": highs, "volume": 1e6}, index=idx)
    s = SymbolSeries("X")
    s.seed_daily(daily)
    s.on_bar({"open": prev_close, "high": prev_close, "low": prev_close, "close": prev_close, "volume": 1}, 600, "2026-09-14", None)
    bar = {"open": close, "high": close, "low": close, "close": close, "volume": 1}
    s.on_bar(bar, 601, "2026-09-14", None)
    return EvalCtx(state=SimpleNamespace(symbol="X"), series=s, bar=bar, et_min=601, session="rth", external=set())


def _nday(ctx, days, opt="high"):
    from scanner.trigger_catalog import _IMPL
    return _IMPL["hi_lo_60d"](ctx, opt, {"days": days})


def test_n_day_high_uses_the_number_of_sessions_asked_for():
    ctx = _nday_ctx(60, close=105.0, prev_close=103.5)
    f = _nday(ctx, 20)
    assert f is not None and f.value == 104.0 and "20-day high" in f.note   # above the 20-day high
    assert _nday(ctx, 60) is None                                           # still below the 60-day 106


def test_n_day_does_not_fire_without_enough_history():
    """41 sessions loaded (60 calendar days) cannot answer a 60-day high."""
    ctx = _nday_ctx(41, close=200.0, prev_close=103.5)
    assert _nday(ctx, 60) is None
    assert _nday(ctx, 41) is not None


def test_saved_60_day_setups_keep_60_days():
    got = normalize_setup(_setup("s1", [{"id": "hi_lo_60d", "options": ["high"]}]))
    assert got["triggers"][0]["params"] == {"days": 60.0}


# ── VWAP support / resistance: candle size and touch tolerance unit ─────────

def _vs_hits(minutes, tf, tol, unit, atr=2.0, vwap=100.0):
    """minutes = (open, high, low, close) per 1-min bar at a flat VWAP."""
    from types import SimpleNamespace
    from scanner.trigger_catalog import EvalCtx, _IMPL
    s = SymbolSeries("X")
    hits = []
    for i, (o, h, l, c) in enumerate(minutes):
        bar = {"open": o, "high": h, "low": l, "close": c, "volume": 1000}
        s.on_bar(bar, 600 + i, "2026-09-15", vwap)
        ctx = EvalCtx(state=SimpleNamespace(symbol="X", atr_d1=atr), series=s, bar=bar,
                      et_min=600 + i, session="rth", external=set())
        if _IMPL["vwap_support"](ctx, str(tf), {"tol_pct": tol, "tol_unit": unit}) is not None:
            hits.append(i)
    return hits


_ABOVE = [(100.5, 100.6, 100.4, 100.5)] * 3                     # a 3-min candle closing above VWAP
_DIP = [(100.12, 100.2, 100.05, 100.1), (100.1, 100.2, 100.04, 100.15), (100.15, 100.35, 100.1, 100.3)]  # green: opens 100.12, closes 100.30
_NEXT = [(100.3, 100.3, 100.3, 100.3)]


def test_vwap_support_on_3_min_fires_when_the_touch_candle_closes():
    hits = _vs_hits(_ABOVE + _DIP + _NEXT, 3, 3.0, 1)            # 3% of a $2 ATR = $0.06 band
    assert hits == [6]                                           # the bar that completes the dip candle


def test_vwap_touch_in_atr_scales_with_the_stock():
    """Low of 100.04 is $0.04 from VWAP: inside 3% of a $2 ATR, outside 3% of $1."""
    assert _vs_hits(_ABOVE + _DIP + _NEXT, 3, 3.0, 1, atr=2.0) == [6]
    assert _vs_hits(_ABOVE + _DIP + _NEXT, 3, 3.0, 1, atr=1.0) == []


def test_vwap_touch_in_percent_of_price_still_works():
    assert _vs_hits(_ABOVE + _DIP + _NEXT, 3, 0.05, 0) == [6]    # 0.05% of $100 = $0.05
    assert _vs_hits(_ABOVE + _DIP + _NEXT, 3, 0.02, 0) == []


def test_new_vwap_alerts_default_to_3_min_and_atr():
    got = normalize_setup(_setup("s1", [{"id": "vwap_support"}]))["triggers"][0]
    assert got["options"] == ["3"] and got["params"] == {"tol_pct": 3.0, "tol_unit": 1.0}


def test_saved_vwap_tolerances_keep_meaning_percent_of_price():
    """Saved before tol_unit existed: 0.1 was 0.1% of price and must stay that,
    not turn into 0.1% of ATR under the new default unit."""
    got = normalize_setup(_setup("s1", [{"id": "vwap_resistance", "options": ["1"], "params": {"tol_pct": 0.1}}]))
    assert got["triggers"][0]["params"] == {"tol_pct": 0.1, "tol_unit": 0.0}


def test_validation_errors_keep_their_own_message():
    """SetupError is a ValueError, so it used to be re-wrapped as "a numeric field
    has an invalid value (unknown trigger ...)"."""
    from scanner.custom_setups import SetupError, normalize_setup
    base = {"id": "cs_x", "name": "x", "direction": "long",
            "triggers": [{"id": "range_break", "options": ["up"], "params": {}}]}
    with pytest.raises(SetupError) as exc:
        normalize_setup(dict(base, triggers=[{"id": "no_such_trigger", "options": [], "params": {}}]))
    assert str(exc.value) == "unknown trigger 'no_such_trigger'"
    for bad in (1e999, float("nan"), "1e999", 10 ** 400):             # still a clean 400, not a 500
        with pytest.raises(SetupError, match="numeric field"):
            normalize_setup(dict(base, repeat_sec=bad))
