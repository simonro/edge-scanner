# Edge Scanner - User Guide

A real-time intraday stock scanner. It streams 1-minute bars for a universe of US stocks, keeps per-symbol
state (VWAP, relative volume, relative strength vs SPY, prior-day and premarket levels, EMAs and more), and
fires alerts when a setup's conditions are met. Alerts, charts, rankings and news are shown in a
multi-window browser dashboard.

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [First-Time Setup](#2-first-time-setup)
3. [Running the Scanner](#3-running-the-scanner)
4. [The Dashboard](#4-the-dashboard)
5. [Setups](#5-setups)
6. [Configuration](#6-configuration)
7. [Scripts Reference](#7-scripts-reference)
8. [Data and Files](#8-data-and-files)
9. [For Developers: Alert Feed](#9-for-developers-alert-feed)
10. [Adding a data provider](#10-adding-a-data-provider)
11. [Troubleshooting](#11-troubleshooting)
12. [Disclaimer](#12-disclaimer)

---

## 1. Requirements

- **Python 3.11 or newer.**
- **Node.js 20 or newer** (to build the dashboard once).
- **An Alpaca account** (paper is fine) and its API keys. The scanner only reads market data; it never
  places orders.
- **Market data.** Set `ALPACA_FEED` in `.env`:
  - `ALPACA_FEED=sip` (the default) is the full consolidated tape. It needs Alpaca's paid market data
    subscription (Algo Trader Plus).
  - `ALPACA_FEED=iex` is free, but it is a single exchange, so volume and relative volume read far lower
    and volume-based setups fire much less. All default thresholds were set on SIP data; the universe
    builder scales its dollar-volume floor down to match (from $150M to $3.75M a day).
- Windows, macOS or Linux. Launchers are included for both: `start_scanner.bat` and
  `restart_scanner.bat` on Windows, `start_scanner.sh` on macOS and Linux.

All times shown by the scanner are **US Eastern**.

---

## 2. First-Time Setup

Do this once on a new machine.

### Step 1: Install the Python packages

Open a terminal in the project folder (ideally inside a virtual environment) and run:

```
pip install -r requirements.txt
```

### Step 2: Build the dashboard

```
npm --prefix dashboard-v2 install
npm --prefix dashboard-v2 run build
```

This writes `dashboard-v2/dist`, which the scanner serves. Re-run the build after pulling dashboard
changes; the scanner does not need a restart to pick up a new build.

### Step 3: Add your Alpaca keys

Copy `.env.example` to `.env` in the project folder and fill it in:

```
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_FEED=sip
```

Your keys are in the Alpaca dashboard under **API Keys**. Use `ALPACA_FEED=iex` if you do not have the SIP
subscription (see [Requirements](#1-requirements)). The `.env` file is gitignored; never commit or share
it.

#### Optional: Charles Schwab as the data provider

A Schwab brokerage account includes market data, so it can replace the paid Alpaca SIP plan. The
setups, alerts, conditions and parameters are the same on both providers. What differs is how the
1-minute bars arrive, because **Schwab streams real 1-minute bars for at most 300 symbols per
account**. The scanner ranks your universe by dollar volume and covers all of it in three tiers:

| Symbols (most liquid first) | Where the bars come from | How close to a real bar |
|---|---|---|
| 1 to 300 | Schwab's 1-minute bar stream | Real bars |
| 301 to 3,300 | Built from Schwab's live quote stream | Open, close and volume match closely; a spike that appears and reverts inside a fraction of a second can be missed |
| 3,301 and up | Built from quotes polled every 10 seconds | Open, close and volume are good; high and low are approximate, except a new high or low of the day, which is exact |

A universe of 300 symbols or fewer runs entirely on real bars. On a larger one, expect alerts built on
closing prices and volume to agree with Alpaca's, and alerts built on the exact high or low of a bar
(breakouts, wicks) to differ now and then in the lower tiers. Odd-lot trades add to a built bar's
volume but never set its price, the same rule real bars follow.

**Refresh the history after the close.** Schwab allows one symbol per history request, about 120 a
minute, and the scanner needs yesterday's bars for every symbol at startup: on a whole-market universe
that is two hours of requests. Run `python scripts/refresh_schwab_cache.py` after the close, ideally as
a scheduled task at 17:30 ET on weekdays (Windows Task Scheduler or cron), and the morning start finds
everything cached and takes minutes. It is REST only and safe to run while a scanner is live.

Start the scanner **before the open** when you can. Schwab allows one symbol per history request, so
only the 600 most liquid symbols are back-filled with today's bars at startup. On a start after the
open, the rest are caught up from quotes in a few seconds (the day's open, high, low, last and volume
so far), which keeps relative volume right; their VWAP is approximate, and their volume includes
premarket, until the next start before the open. To scan only the first 300 symbols on real bars, set
`SCHWAB_SYNTHETIC_BARS=0` in `.env`.

To run Alpaca and Schwab side by side and measure how closely they agree, start the second scanner
with `start_scanner_schwab.bat` (port 7787, its own alert archive) and run
`python scripts/compare_live_feeds.py`.
It is newer than the Alpaca path. Alpaca keys, if you keep them in `.env`, still supply the Benzinga
news; without them news comes from the free Yahoo and Nasdaq feeds.

Schwab has no official Python library, so this uses
[Schwabdev](https://github.com/tylerebowers/Schwabdev) (MIT, by Tyler Bowers) for the API calls and
the login. It installs with `pip install -r requirements.txt`, so there is nothing extra to fetch.
It is an independent project, not affiliated with Charles Schwab.

1. Create an app at [developer.schwab.com](https://developer.schwab.com) (callback URL
   `https://127.0.0.1`) and wait for it to be approved.
2. Add to `.env`:
   ```
   DATA_PROVIDER=schwab
   SCHWAB_APP_KEY=your_app_key
   SCHWAB_APP_SECRET=your_app_secret
   SCHWAB_CALLBACK_URL=https://127.0.0.1
   ```
3. Log in once: `python scripts/schwab_auth.py`. It opens the Schwab login; after you approve,
   the browser shows a page that fails to load. That is expected: paste the whole address from the
   address bar back into the terminal. Schwabdev saves the tokens under `~/.schwabdev/`, outside
   this folder, so they are never at risk of being committed.
4. **Schwab expires the login every 7 days.** Run `schwab_auth.py` again weekly, or the scanner
   stops at startup with a message telling you to.

Schwab allows one symbol per history request, so the first start takes longer than on Alpaca.
Its cache is separate (`data/schwab/`), so switching back and forth never mixes the two. To see how
closely the two providers agree on your universe: `python scripts/check_schwab_match.py`
(needs both logins; about an hour the first time).

### Step 4: Run it

```
python scripts/run_live.py
```

The first run builds the symbol universe and downloads daily and 5-minute history for every symbol, which
can take 10-20 minutes depending on the universe size. Later runs reuse the local cache and only fetch
what is new.

### Step 5 (optional): Install the sample setups

With the scanner running, install the library of ready-made custom setups:

```
python scripts/install_setup_library.py
```

See [The sample setup library](#the-sample-setup-library).

---

## 3. Running the Scanner

One command starts everything:

```
python scripts/run_live.py
```

Or use a launcher: double-click `start_scanner.bat` on Windows, or run `./start_scanner.sh` on macOS and
Linux. Both scan `data/universe_all.csv` when it exists (otherwise `data/universe.csv`) and load 380 days
of daily history; extra flags are passed through, for example `./start_scanner.sh --log-level INFO`.
`restart_scanner.bat` stops a running scanner and starts it again.

### What happens at startup

1. **Universe**: loads `data/universe.csv`, rebuilding it when it is more than 7 days old.
2. **Sector map**: maps symbols to their sector ETFs (refreshed weekly).
3. **Daily history**: refreshes the daily bar cache.
4. **Intraday history**: refreshes the 5-minute bar cache used for relative volume.
5. **Warmup**: seeds every symbol's state from that history.
6. **Live stream**: opens **one** Alpaca market-data WebSocket and starts scanning 1-minute bars.

When it is live, open **http://localhost:7777** in a browser (it redirects to the dashboard at `/v2/`).

### Stopping

Press **Ctrl+C** once in the terminal. The scanner shuts down cleanly.

### Useful flags

| Flag | Default | Description |
|---|---|---|
| `--universe PATH` | `data/universe.csv` | Universe CSV to scan. An explicit file is used as is, with no age check or rebuild |
| `--refresh-universe` | off | Force a universe rebuild even if it is fresh |
| `--history-days N` | `60` | Calendar days of daily history. Use about 380 if you want 200-day averages and 52-week levels |
| `--intraday-days N` | `20` | Days of 5-minute bars for the relative volume profile |
| `--keep-days N` | `5` | Days of alerts kept on disk |
| `--no-fundamentals` | off | Skip the background fundamentals prefetch for the Stock Info window |
| `--log-level` | `WARNING` | `DEBUG`, `INFO` or `WARNING` |

**Only one Alpaca stream per account.** Alpaca allows a single market-data WebSocket per account. Do not
run two scanners (or another app that streams market data) on the same keys at the same time, or one of
them will be disconnected.

---

## 4. The Dashboard

The dashboard (branded **Edge Scanner**) is a desktop-style workspace of free-floating windows. Drag a
window by its title bar, resize it from any edge, double-click the title bar to maximize, and hold `Alt`
while dragging to turn off snapping.

### Windows

| Window | What it shows |
|---|---|
| **Scanner** | The live alert stream. Each window has its own filters: setups, direction, minimum score and symbols. Column picker, row tint, and a sound or text-to-speech per window. **Hold** freezes the list while you read it; new alerts are still collected and sound, the button counts them (Resume +3), and Resume shows them |
| **Chart** | Intraday and daily candles with extended hours, VWAP, EMAs, daily SMAs, prior-day and premarket levels |
| **Rankings** | Ranked lists: RVOL leaders, gainers and losers (from the close or the open), 5-minute movers, premarket gainers, losers and volume, and a new high / low of day stream |
| **News** | Market-wide news, or news for the linked symbol |
| **Stock Info** | Live per-symbol state plus fundamentals |
| **Watchlist** | Editable symbol lists with live columns |
| **Clock** | Eastern time, session phase, market regime, SPY and feed health |
| **Setup check** | For one symbol, what every setup did over the last few minutes and which condition passed or failed. Use it to answer "why did (or didn't) this alert fire?" |

### Linking windows

Give windows the same link color and they follow each other: click a symbol in a Scanner, Rankings or
Watchlist window and every window of that color (chart, news, stock info, setup check) switches to it.

### Screens

A screen is a saved arrangement of windows. You can keep any number of named screens and switch between
them from the top bar. Screens are saved automatically on the server (`data/layouts/`) and keep their
proportions on any monitor size. Two screens are created on first run:

- **Pre-Market**: pre-market gainers, losers and volume rankings. Click a symbol and the pre-market
  chart, daily chart, news and stock info all follow it.
- **Price Action**: an Alerts scanner beside intraday and daily charts and News (the red
  link group), plus a second, independent group (blue): RVOL leaders and 5-min movers driving a
  1-minute chart. It shows how windows of any size sit side by side and how link groups keep
  two workflows apart.

Add either again at any time from Screens > Starter layouts; it arrives as a new screen and never
replaces one of yours.

### News sources

The News window merges the Alpaca news wire (Benzinga) with keyless Yahoo Finance and Nasdaq per-symbol
RSS feeds. When a symbol has no recent news, it widens the search to the last 30 days and says so. To
change or disable the RSS sources, set `NEWS_RSS_SOURCES` in `.env`, for example `NEWS_RSS_SOURCES=yahoo`
or `NEWS_RSS_SOURCES=` (empty) for Alpaca only.

### The top bar

- **ALERTS** is the connection to the scanner's alert stream, with today's alert counts.
- The chip beside it is the **market data**: the provider (Alpaca SIP, Schwab), the newest bar and how
  long ago it arrived. In regular hours it turns amber after 90 seconds without a bar and red after three
  minutes: the alert connection can be fine while the data has stopped. The browser tab shows the provider
  too, so two scanners side by side are easy to tell apart.
- **⋯** holds the rarer controls: save the layout as a new screen, theme, hide the bar, fullscreen.

### Themes and shortcuts

Two themes ship, a dark default and a light one, under **⋯** in the top bar. Shortcuts: `Ctrl+K` add a window, `Ctrl+L` lock or
unlock the layout, `Ctrl+,` open Config, `Esc` close menus and dialogs.

---

## 5. Setups

A **setup** is a named rule that fires an alert. Every alert says which setup fired, the direction (long
or short), the trigger, the price and a score, plus the conditions it checked. Alerts also carry a
suggested stop (the low of the last few 1-minute bars for a long, the high for a short; see `STOP_BARS` in
[Configuration](#6-configuration)). The stop is information only; the scanner does not trade.

Setups are **edge-triggered**: an alert fires on the bar where the setup first becomes true, not on every
bar while it stays true, and the same symbol and setup do not repeat within a 5-minute cooldown.

### Custom setups

You compose setups yourself in the dashboard from three parts:

- **Triggers** from the trigger catalog (`scanner/trigger_catalog.py`, about 50 of them): the moment worth
  an alert, such as a cross above VWAP, a 5-minute breakout, a new high of day, a candle pattern, or
  relative volume crossing a level. Combine several with OR, AND or AT LEAST logic.
- **Parameters**: conditions that must hold when the trigger fires (for example gap of at least 2%,
  relative volume above 1.5, price above VWAP).
- **A universe filter**: which symbols the setup watches.

Custom setup ids start with `cs_`.

### The sample setup library

A library of ready-made custom setups ships in `scanner/setup_library.json`. With the scanner running,
install it with:

```
python scripts/install_setup_library.py
```

It only adds setups that are missing and never overwrites your edits (`--dry-run` shows what it would
add). The library contains:

**Long**

| Setup | What it looks for |
|---|---|
| Gap Up on Volume | Gapped up hard on real volume. Fires on RVOL crossing 2x, a new high of day, or a 5-minute breakout |
| Gap Up Holding | A gap up that is being bought: above VWAP with the last 15 minutes pointing up |
| Gap Down Recovering | A gap down that is being bought back: above VWAP with 15-minute momentum, working on the fill |
| Strong Stock Pullback | A daily leader that is weak this hour, turning back up: the pullback in a strong stock |
| Relative Strength Rising | Relative strength vs SPY is positive and building over the last 15 minutes |
| RS Leaders | The strongest names vs SPY over the past hour, on a daily chart that agrees, making a new push |
| Above Prior Day Range | Trading above yesterday's entire range with volume: a breakout day in progress |
| Testing Prior Day High | Pressing the high of day right at yesterday's high, before the breakout has happened |

**Short**

| Setup | What it looks for |
|---|---|
| Gap Down on Volume | Gapped down hard on real volume. Fires on RVOL crossing 2x, a new low of day, or a 5-minute breakdown |
| Gap Up Fading | A gap up that is being sold: lost VWAP and the last 15 minutes are pointing down |
| Gap Down Extending | A gap down that is still being sold: below VWAP, momentum down |
| Weak Stock Bounce | A daily underperformer bouncing hard this hour, rolling back over: the rally you short |
| Relative Strength Falling | Relative weakness vs SPY is negative and getting worse over the last 15 minutes |
| RS Laggards | The weakest names vs SPY over the past hour, on a daily chart that agrees, making a new leg down |
| Below Prior Day Range | Trading below yesterday's entire range with volume: a breakdown day in progress |
| Testing Prior Day Low | Pressing the low of day right at yesterday's low, before the breakdown has happened |

Each one is a normal custom setup afterwards: rename it, change its thresholds, or delete it. Open it in
Config to see the exact triggers and parameters it uses.

---

## 6. Configuration

Open **Config** from the top bar or with `Ctrl+,`. Changes apply on the next bar; no restart is needed.

### Setups

Create, edit, enable or disable custom setups, change their triggers and parameters, and assign each one
a universe filter.

### Settings

A small set of shared settings that the setups and universe conditions read:

| Setting | Default | What it does |
|---|---|---|
| `STOP_BARS` | `5` | Suggested stop = low (long) or high (short) of the last N 1-minute bars, including the signal bar |
| `GATE_RVOL_MIN` | `1.00` | Minimum time-of-day relative volume for the relative volume gate |
| `GATE_QUALITY_MIN` | `60` | Minimum chart-quality score (0-100): clean structure, not gappy or over-extended |
| `GATE_VOID_MIN_PCT` | `1.0%` | Minimum clear air to the next 60-day level in the trade direction |
| `GATE_RRS_WARMUP_5M_BARS` | `12` | 5-minute bars a symbol needs before a missing 5-minute relative strength blocks the gate (until then the daily figure is used) |
| `GATE_MARKET_ALIGN_FROM` | `10:00` | The SPY market-alignment gate only applies from this time on |

Each row shows its description, the default with a one-click reset, and today's pass rate for the gate
it drives, so you can see which one is doing the blocking before you touch it.

Settings are saved in `data/settings/` and loaded again at startup. **Presets** save the whole settings
set under a name so you can switch back later, and every save, reset and preset apply is recorded in a
change log (`data/settings/history.jsonl`).

### Rankings

Which universe filter each ranked list uses and how many rows it shows.

### Universe filters

Named symbol filters (for example price, average volume, dollar volume, ATR%) that setups and rankings
point at. Three ship by default (`scanner/universe_profiles_defaults.json`):

- **All symbols**: no filter; the default for every assignment.
- **Liquid movers**: price at least $15, 20-day average volume at least 5M shares, dollar volume at least
  $50M, ATR% at least 1.
- **Small cap runners**: price at least $1, ATR% at least 4, float under 20M shares, relative volume at
  least 3 and session volume at least 500K. The float figure comes from Yahoo Finance and can be stale.

The defaults are only seeded on first run; after that, edit them in the dashboard. They are stored under
`data/universe/profiles/`.

### The base universe

Filters narrow the base universe, the CSV the scanner streams. `scripts/build_universe.py` builds it from
all active US equities (see [Scripts Reference](#7-scripts-reference)). A larger base universe gives the
filters more to choose from but costs more CPU per minute and a longer first download.

---

## 7. Scripts Reference

| Script | What it does |
|---|---|
| `scripts/run_live.py` | The live scanner (section 3) |
| `scripts/build_universe.py` | Builds the base universe CSV. Called automatically by `run_live.py` when the default universe is over 7 days old |
| `scripts/install_setup_library.py` | Installs the sample custom setups into a running scanner |
| `scripts/fetch_history.py` | Seeds or refreshes the daily bar cache for a symbol list |
| `scripts/schwab_auth.py` | Logs in to Schwab (`DATA_PROVIDER=schwab`); repeat weekly |
| `scripts/check_schwab_match.py` | Compares Alpaca and Schwab universes and data side by side |
| `scripts/compare_live_feeds.py` | Compares two running scanners: symbol state and alerts, per data tier |
| `scripts/refresh_schwab_cache.py` | Downloads the day's Schwab history after the close so the next start is fast |
| `scripts/compare_universes.py` | Builds two universes and explains every difference |
| `start_scanner.bat`, `start_scanner.sh` | Launchers for Windows and for macOS / Linux (section 3) |
| `restart_scanner.bat` | Stops a running scanner and starts it again (Windows) |

### build_universe.py

```
python scripts/build_universe.py
python scripts/build_universe.py --out data/universe_wide.csv --min-price 5 --min-avg-vol 1000000
```

| Flag | Default | Description |
|---|---|---|
| `--out` | `data/universe.csv` | Output CSV |
| `--min-price` | `15.0` | Minimum last price ($) |
| `--min-avg-vol` | `0` (off) | Minimum 20-day average daily share volume |
| `--min-dollar-vol-m` | `150.0`, or `3.75` with `ALPACA_FEED=iex` | Minimum 20-day average daily dollar volume (millions) |
| `--min-atr-pct` | `1.0` | Minimum ATR% |
| `--days` | `20` | Trading days used for the volume and ATR averages |

To scan a universe other than the default, pass it to the scanner: `python scripts/run_live.py --universe
data/universe_wide.csv`. A file passed this way is not rebuilt automatically, so rebuild it by hand when
its numbers get stale.

---

## 8. Data and Files

Everything the scanner writes lives under `data/` (gitignored).

| Path | Contents |
|---|---|
| `data/universe.csv` | The default base universe |
| `data/sector_map.csv` | Symbol to sector ETF map |
| `data/daily/`, `data/5m/` | Bar caches |
| `data/alerts/all/` | Every alert, one JSONL file per day |
| `data/settings/` | Settings, presets and change log |
| `data/setups/` | Custom setups and setup assignments |
| `data/universe/profiles/` | Universe filters |
| `data/layouts/` | Dashboard screens |
| `data/watchlists.json` | Watchlists |

Alert files older than 5 days are deleted at startup (change with `--keep-days`). Copy them elsewhere if
you want a longer history.

---

## 9. For Developers: Alert Feed

The scanner serves everything on port **7777**:

- **REST API** under `/api/` (used by the dashboard). `GET /api/alerts` returns recent alerts, newest
  first, and takes the same filters as the WebSocket plus `limit`.
- **Alert WebSocket**: `ws://localhost:7777/ws/alerts`. Every alert, each with a `source` field (`custom`
  for custom setups). Filter with query parameters:

  | Parameter | Example |
  |---|---|
  | `sources` | `custom` |
  | `setups` | a custom setup id (`cs_...`), comma-separated for several |
  | `triggers` | `hod_breakout` |
  | `symbols` | `NVDA,AAPL` |
  | `direction` | `long` or `short` |
  | `min_score` | `50` |
  | `custom` | custom setup ids |

  Example: `ws://localhost:7777/ws/alerts?symbols=NVDA,AAPL&direction=long`

  On connect the server sends one `{"type": "replay", "alerts": [...]}` frame with the recent alerts that
  match your filter (newest first), then one `{"type": "alert", "alert": {...}}` frame per new alert.

Custom setup ids are stable: renaming a setup in the dashboard changes only its display name.

---

## 10. Adding a data provider

Two providers ship: **Alpaca** and **Charles Schwab**. They ship because they are the two this
project actually runs on and is tested against, live, every session. Nothing about the engine is
tied to them: providers sit behind one interface, so any service that can stream 1-minute bars and
answer for history can be plugged in, including Polygon, Databento, Tradier, Interactive Brokers or
your own broker's API.

Adding one is a class and a line in a registry. Judging whether its data is good enough is the part
that takes real work, so this section covers both.

### The interface

Implement `DataFeed` in `scanner/data/interface.py`:

| Method | What it must return |
|---|---|
| `get_historical_daily(symbol, start, end)` | Daily OHLCV as a UTC-indexed DataFrame with `open, high, low, close, volume, vwap, trade_count` |
| `get_historical_bars(symbol, timeframe, start, end)` | The same shape for an intraday timeframe |
| `subscribe_minute_bars(symbols, callback)` | Start the stream; call `callback(bar)` once per closed 1-minute bar |
| `get_snapshot(symbols)` | Latest quote and trade per symbol |
| `stop_stream()` | Stop the stream (optional, default does nothing) |

The warmup also uses batch helpers where a provider has them: `get_historical_daily_multi`,
`get_historical_bars_multi`, `get_todays_bars`, `get_todays_bars_multi`. Without them, a full
universe takes a long time to warm up, as Schwab shows: it allows one symbol per history request, so
it threads and throttles instead of batching.

Register it in `scanner/data/__init__.py`:

```python
_BUILTIN = {"schwab": "scanner.data.schwab:SchwabFeed", "yours": "scanner.data.yours:YourFeed"}
```

Then `DATA_PROVIDER=yours` in `.env`, or `python scripts/run_live.py --feed yours`. No signal code
changes. `scanner/data/schwab.py` is the worked example, rate limiter and all.

### What to get right

These are where the bugs live. Each one has cost this project real time:

- **Split adjustment.** Request adjusted history, and never mix adjusted and raw bars in one cache.
  A reverse split in raw bars made a $3.5M/day stock look like a $194M/day stock and corrupted every
  average, level and ATR built from it.
- **Its own cache folder.** Schwab writes under `data/schwab/`, Alpaca under `data/daily_split` and
  `data/5m_split`. Two providers sharing a cache silently poison each other's history.
- **Bar timestamps are the bar's START, in UTC.** A bar labelled 15:30 covers 15:30:00 to 15:30:59
  and cannot exist before 15:31:00. Deliver it once, when it closes.
- **Extended hours.** Premarket and after-hours bars must be included; the engine decides what to do
  with them. Missing premarket bars break gap and premarket levels.
- **One stream.** The whole process shares one connection. Do not open a second one anywhere.
- **Rate limits and retries.** Back off on 429 and 5xx rather than dropping bars silently.
- **Enough history.** The relative-volume profile wants 20 trading days of 5-minute bars, and the
  200-period moving averages want a year of daily bars. A provider with a short lookback will run,
  but RVOL and the long averages stay empty or wrong.

### Prove it before you trade on it

Run the new provider beside a known one and compare, the way Schwab was checked before it shipped:

```bash
python scripts/compare_universes.py --a alpaca:alpaca --b yours:nasdaq
```

It builds a universe with each provider and explains every difference. What Schwab had to reach
before it was accepted: 97% or better overlap at every threshold set, with the remaining differences
explained, and at least 20 trading days of 5-minute history. Prices agreed to the cent on the median
stock. Then watch a live session side by side and check the alerts line up.

Until a provider passes that, treat it as untested: the thresholds and the sample setups were tuned
on Alpaca's consolidated tape, and a thinner feed changes what fires.

---

## 11. Troubleshooting

**The dashboard page is blank or returns 404.** The dashboard has not been built, or the build is
older than the code you updated to (the built files are not in git, so updating does not refresh them).
Stop the scanner, run `npm --prefix dashboard-v2 install` and `npm --prefix dashboard-v2 run build`,
start it again and reload with Ctrl+Shift+R. If the page stays dark and empty, open the browser
console (F12) and reload: the red error names what failed.

**Screens or setups I made are gone.** Everything you save lives under `data/` in the folder the
scanner was started from: screens in `data/layouts/`, setups in `data/setups/custom/`. Starting from a
different install folder, or with an empty `data/`, shows the starter screens and sample setups
instead; nothing is deleted. Copy the files back from the old folder (or start from it) and reload.

**Startup fails with an authorization or subscription error.** Check the keys in `.env`. If you do not
have Alpaca's SIP subscription, set `ALPACA_FEED=iex`.

**Almost nothing fires on the IEX feed.** Expected: IEX is one exchange, so volume and relative volume
read far lower than on SIP. Lower the volume thresholds in your setups and filters, or use SIP.

**The live stream keeps disconnecting, or another app lost its data.** Something else is streaming market
data on the same Alpaca account. Alpaca allows one stream per account; stop the other one.

**No alerts are firing.** Check that the market is open (Clock window), that you have setups enabled in
Config (install the sample library if you have none), and that their universe filter includes the
symbols you are watching. Then open a **Setup check** window on a symbol to see which condition is
failing. Relative volume needs the 5-minute history from startup; if that step failed, restart the
scanner.

**A setup fires too often or too rarely.** Adjust its parameters in Config, or the shared settings; the
pass rate next to each setting shows how restrictive it is today. Changes apply on the next bar.

**200-day averages or 52-week levels are empty.** Daily history is too short. Run with
`--history-days 380` (the launchers already do).

**Startup is slow or the scanner falls behind during the session.** The universe is too large for the
machine. Use a smaller universe CSV (see `build_universe.py` in section 7).

**The Stock Info window has no fundamentals.** They load in the background after warmup and can take a few
minutes. They are skipped when you run with `--no-fundamentals`.

---

## 12. Disclaimer

This is educational software. It is not financial advice and does not recommend buying or selling any
security. Alerts are the output of mechanical rules and can be wrong, late, or based on bad data. The
software is provided "as is", without warranty of any kind (see the MIT license). You are solely
responsible for your own trading decisions and their results.
