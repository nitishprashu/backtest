# NIFTY Options Lab v5

A local, private options backtesting workstation: Python engine + browser UI. Nothing leaves your machine
(the server listens on 127.0.0.1 only).

    pip install -r requirements.txt
    python -m lab                      (or double-click run_lab.bat)
    -> opens http://127.0.0.1:8765

Defaults read your V3 DuckDB folder (`D:\Nitish Personal\Python\breeze_data`). Override with
`python -m lab --data-dir "<folder>" --credentials "<credential.txt>" --port 8765`
(or the env vars NIFTY_BREEZE_DATA_DIR / BREEZE_CREDENTIAL_FILE). Run history is stored in `lab_runs.db`.
Set `NIFTY_LAB_CACHE_MB` (default 1200) to cap the in-memory session cache. See `CHANGES.md` for review notes and known limitations.

## Screens
| Screen | What it does |
|---|---|
| Strategy | Templates, up to 8 legs, individual-leg SL/target/trail/re-entry, combined strategy SL and portfolio rules, Greek-based strike selection, entry conditions, Supertrend, VWAP, EMA & MACD signals, live payoff preview on a real day |
| Results | KPI cards, equity + drawdown, daily P&L, calendar heat-map, weekday / days-to-expiry / exit-reason breakdowns, entry-Greeks charts, sortable trade table, worst drawdowns, MAE/MFE, diagnostics, trades CSV and a complete PDF report |
| Charts | Independent market chart workspace for NIFTY spot or a selected option, recent 100-candle windows from 1 minute to monthly with scroll-back history, year-filtered expiries, Ichimoku/Supertrend/EMA/SMA/Bollinger/VWAP/RSI/MACD, volume/OI panes, trendlines, gridlines, crosshair OHLCV, Y-axis reset, and optional saved-run trade markers |
| Explorer | select a trading day and either the current or a saved strategy, then replay candles at 1, 5, or 10 candles/sec with live P&L, positions, and exits; replay a saved trade from its entry or inspect underlying EMA signal verification |
| Optimizer | two-parameter sweep (SL %, target %, entry time) over cached sessions, with an out-of-sample split |
| Runs | every backtest saved (SQLite): compare equity curves, rename, delete, load back into the builder |
| Data | coverage, 09:15-15:29 timing check, expiry completeness, optional Breeze repair of one contract |

## Realism (no bid/ask in the data)
Slippage model per fill = max(% of premium, minimum ticks of 0.05), x a stop-exit multiplier, x a thin-candle
multiplier. Presets: none / liquid ATM / conservative / stress. The defaults are a suggested starting point,
not measured values: calibrate against your own contract notes. Lot sizes follow the contract-expiry schedule
(50/25/75/65); STT, exchange, IPFT, SEBI, stamp and GST follow dated schedules; brokerage is yours to set.

## Engine rules
Entry at the entry candle's open (or close); individual-leg stops/targets use candle high/low, stop wins a
same-candle tie, a gap through a stop fills at the open. Combined strategy/portfolio rules use candle closes
and fill at the next open. Combined strategy SL can be measured in rupees, percent of entry premium, or
quantity-weighted combined premium points (one strategy lot). Signals use only closed candles. A day is skipped,
with the reason shown, if data is missing or an expiry looks absent. EMA signals support price-versus-EMA with
an optional independent EMA/SMA slope filter, or fast/slow EMA crossovers. EMA can use NIFTY spot
or option premium as its source. With option premium selected, CE and PE are tracked independently:
above-EMA/bullish signals buy that same option, and below-EMA/bearish signals sell it. The selected
strike rule is re-evaluated for each session, with the resulting CE and PE premiums forming their
own EMA histories. Both positions may overlap; trade-count and daily loss/target controls are shared.
For time-based strategies, ATM and ATM Offset legs can use either NIFTY spot or a synthetic future as
their strike reference. The synthetic future is estimated at the entry price field from the nearest
spot-ATM pair as ATM strike + CE premium − PE premium; if either ATM premium is unavailable, the
strike cannot be resolved for that entry. Other strike rules are unaffected.
The schedule can also be restricted to listed option expiry dates; this filter combines with the
selected weekdays, while the expiry selector continues to choose the contracts traded. For
carry-forward strategies it restricts new entries only; open positions continue to be managed and
rolled on subsequent sessions.
EMA, Supertrend, and VWAP support 1-, 5-, 15-, 30-, and 60-minute, daily, weekly, and monthly timeframes.
MACD supports the same timeframes, uses fast/slow/signal EMA periods (12/26/9 by default), and signals
bullish or bearish when its line is above or below its signal line. Like EMA, MACD carries state across
sessions using only in-session candles. The full-duration NIFTY chart renders MACD, its signal line, and
histogram in a separate oscillator pane so the indicator scale does not affect the price axis.
For MACD and underlying-source EMA strategies, each leg's Buy/Sell action selects the directional option:
bullish signals buy CE or sell PE; bearish signals buy PE or sell CE. The configured CE/PE leg type does
not override this direction mapping.
Periods count bars in the selected timeframe. Signals only use fully closed candles: a daily candle's
signal becomes available on the next session, while weekly and monthly signals become available in the
next calendar week or month. Incomplete trailing 60-minute session bars are ignored. For intraday resolutions, VWAP accumulates
selected-timeframe candles from the session open. Daily, weekly, and monthly VWAP is calculated within
the respective closed period. Premium VWAP uses each session's selected CE/PE contracts, so weekly
and monthly periods can combine different contracts across sessions. Intraday EMA calculations carry
their state across trading sessions using only in-session candles; no overnight or out-of-session bars
are synthesized. Intraday Supertrend can likewise carry its state across sessions when enabled.

Position holding can be set to intraday square-off, carry one open trade at a time, or carry while continuing
to take new eligible entries. Carry-one ignores intraday square-off and leg re-entry. Underlying-driven
indicator strategies hold at most one position at a time, reversing immediately on each opposite signal, and
ignore the daily trade cap and daily loss/target limits. For EMA signals calculated on the underlying, the
configured Buy/Sell action selects the premium expression: bullish Buy uses CE, bullish Sell uses PE, bearish
Buy uses PE, and bearish Sell uses CE.
One-minute carry signals are compared with the prior session's final signal so an overnight direction change
can reverse at the first eligible signal of the next session. Option-premium
EMA allows one active position per option type and applies shared daily trade and loss/target limits.
Configured SL/target/trailing and signal exits remain active. In the fresh-entry mode each entry and its
configured exits are managed independently.
If an option position reaches expiry without another exit, carry modes close that contract at the expiry
session's final bar and roll into the next weekly or monthly expiry with the same option type and side.
The default rollover rule keeps the original strike; the optional re-selection rule applies the configured
strike condition again using the rollover session's closing market data.
Configured SL/target/trailing rules restart from the rolled contract's entry premium. Each contract's
roll-in/roll-out is shown as a separate trade row; positions close at their configured exit or the
backtest/data boundary if no exit fires. Realized P&L is booked on each contract's exit date.
When a held strike moves outside the usual daily spot-based load window, carry mode fetches that exact
contract for subsequent sessions and its rollover target on demand. If a required session is genuinely absent,
the trade exit is labelled `DATA_GAP`, and the results diagnostics list the affected expiry, strike, option type,
and date range to download.
Trade rows include NIFTY spot at entry (the candle open) and exit (the candle close). Results displays
the actual closed-bar EMA level(s) for NIFTY EMA strategies; signed signal differences and signal-time
spot values remain available in the underlying trade record for audit and export.

The Charts workspace is separate from Explorer and does not depend on a backtest date range. It opens
with the most recent 100 candles available for the selected instrument and interval; dragging toward the
left edge loads older history, while loaded data can be panned in either direction. Options are selected
by expiry year, expiry, strike, and type. Intraday options include the 15:39 candle from 3 Aug 2026,
reflecting the 15:40 close. Intervals include 2-hour, 4-hour, daily, weekly, and monthly bars. The
Y-axis can be adjusted by dragging its scale and restored with Reset Y axis. It supports Ichimoku,
Supertrend, volume, open interest where the source provides it, and user-drawn trendlines; a saved run
may optionally be selected for trade markers. Its
interaction model is inspired by OpenAlgo Charts
(https://openalgo.in/charts), adapted for local historical data. The
downloadable PDF report includes run strategy/settings, every stored
performance metric, equity/drawdown and daily P&L charts, every session, the complete trade log, and
skipped-session diagnostics.

## Greeks and entry filters
The Black-76 model estimates implied volatility and Greeks from the option prices in your data. By default,
put-call parity estimates each session's forward/carry; a manual rate/dividend-yield setting is also available.
IV is left unavailable when a price is missing or outside no-arbitrage bounds. Strike selection can target
delta, IV, theta, or vega, with optional minimum/maximum bounds; the selected contract is the closest available
strike to the requested target. Greek-based entry conditions can filter on ATM IV or net position delta, theta,
and vega. Spot-versus-VWAP, spot move, and Supertrend conditions are also available; all configured conditions
must hold, and the engine enters on the first eligible bar within the configured entry window.
Trade rows record entry Greeks when calculable. Results include daily aggregate entry-Greek charts, while the
minute-by-minute Explorer continues to show portfolio Greeks where the underlying contract data permits.

## Layout
`lab/` server, jobs, run history, service, dashboard analytics | `optbt/` engine | `lab_data.py` DuckDB layer |
`web/` UI (plain ES modules + vendored d3, no build step, works offline) | `tests/`

## Tests
    python -m unittest discover -s tests          engine, data layer, API over real HTTP
    python tests/run_e2e.py 8801                  full browser flow (needs `pip install playwright` + Chromium)
The test market in `tests/` is synthetic and used ONLY by tests; the app never simulates data.

## Not verified in the build environment
Reads from your real DuckDB files and Breeze downloads (no DuckDB / credentials there); the data-layer logic is
tested through an in-memory stand-in with the same interface.
#   b a c k t e s t  
 