# Review notes - v5.2.0

## What was checked
* Read the engine, Greeks, analytics, data layer, server, service and web UI; ran the 116 existing tests.
* New property check: for Supertrend (1/5/15m), EMA (price, crossover, slope filter) and spot VWAP, every
  future candle was perturbed and all earlier signals were compared. 84 combinations, no look-ahead found.
* Browser flow (`tests/run_e2e.py`) passes with no console errors.

## Fixed
| Area | Problem | Fix |
|---|---|---|
| Run history | `sqlite3`'s `with` block only commits; every call leaked a connection | `RunStore._conn` now commits, rolls back and closes |
| HTTP server | A malformed or negative `Content-Length` crashed the handler; a 413 left the body unread and corrupted the next keep-alive request | Returns 400 / 413 and closes the connection |
| Settings | Nothing validated strike interval, lot size, capital, brokerage, slippage or DTE bounds (e.g. strike interval 0 gave a wall of per-day errors) | `strategy.validate_globals`, called from `validate` |
| Memory | Day cache was capped by count only (300 days); wide Greek/premium windows or carry mode could reach several GB | Also capped by size, default 1200 MB (`NIFTY_LAB_CACHE_MB` overrides) |
| Analytics | CAGR / Calmar annualised windows of a few days into absurd (or overflowing) numbers | CAGR is NaN below 30 days and overflow-safe |
| UI | Chart tooltips put run names / labels into `innerHTML` unescaped (a run renamed to HTML would execute) | `esc()` in `tipRow` and label tooltips |
| Tests | `test_config` hard-coded an old version; one test needed DuckDB | Uses `APP_VERSION`; DuckDB test skips when not installed; new `tests/test_hardening.py` |

## Known limitations (not changed, your call)
1. **Pre-schedule dates use today's values.** `instruments._pick` falls back to the LATEST entry for dates before a schedule
   starts (tests encode this). A 2021 backtest therefore uses today's lot size (65) and STT (0.15%). Add the older rows or
   make it raise if you backtest before 2021-08 (lots) or 2023-04 (charges).
2. **Carry estimate uses 09:20-09:40 data for the whole session** (`greeks.estimate_carry`). Greek-based strike selection or
   entry filters before ~09:40 therefore see a slowly varying parameter that was estimated slightly in their future. The effect on
   delta is small, but it is not strictly causal.
3. Forward-filled bars (no print) carry volume 0, so SL/TP can trigger on stale prices; use the thin-volume slippage multiplier to price that in.
