"""Application service: everything the HTTP layer exposes, independent of HTTP and of the data backend.

`provider` must offer:
    coverage()                      -> {"first": date|None, "last": date|None, "spot_rows": int, "option_years": [...]}
    store(start, end, step, warmup_days=10)
                                    -> object with dates, history_dates, build_day(), set_window(), ...
    repair(expiry, typ, strike, d1, d2)   (optional)
"""
from __future__ import annotations

import dataclasses
import io
import math
import threading
import time as time_module
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from optbt import (NIFTY, ReEntryRule, RiskRule, intraday_path, run_backtest, run_signal_backtest)
from optbt.data import C, H, L, N_BARS, O, V, bar_index
from optbt.conditions import find_entry_bar
from optbt.engine import resolve_strike, select_expiry, select_leg_expiry, slippage_model
from optbt.greeks import b76_price, estimate_carry, greeks_at, greeks_series, tte_years
from optbt.schemas import ExpirySelect
from optbt.signals import (EMAParams, EMASource, SignalStrategyConfig, VwapParams, VwapSource,
                           indicator_backtest_chart_data, indicator_display_for_day,
                           indicator_ema_values_for_days, indicator_premium_signal_values_for_days,
                           indicator_signal_values_for_days)

from . import APP_VERSION, insights
from . import strategy as strat
from .jobs import Job, JobManager
from .runs import RunStore
from .serial import clean


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _ensure_trade_contracts(day, trades, store) -> None:
    """Load trade contracts omitted by the day's spot-relative strike window."""
    loader = getattr(store, "load_contract_day", None)
    for row in trades.itertuples(index=False):
        expiry = pd.Timestamp(row.expiry).date()
        option_type = str(row.option_type).upper()
        contract = (expiry, int(row.strike), option_type)
        if contract in day.options:
            continue
        if loader is None:
            raise ApiError(
                f"contract {expiry} {contract[1]} {option_type} is not loaded for replay on {day.trade_date}",
                409,
            )
        prices = loader(day.trade_date, contract)
        if prices is None:
            raise ApiError(
                f"contract {expiry} {contract[1]} {option_type} has no replay data on {day.trade_date}",
                409,
            )
        day.options[contract] = prices


def _date(v: Any) -> date:
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        raise ApiError(f"invalid date {v!r}") from None


def _chart_session_duration(day: date, instrument: str) -> int:
    return 385 if instrument == "option" and day >= date(2026, 8, 3) else 375


def _spot_close_before(day, event_time: pd.Timestamp, history_days) -> float:
    bar = bar_index(event_time.time())
    if 0 < bar < N_BARS:
        return float(day.spot[C, bar - 1])
    if bar == 0:
        if day.prev_spot is not None:
            return float(day.prev_spot[C, -1])
        prior_days = [candidate for candidate in history_days
                      if candidate.trade_date < day.trade_date]
        if prior_days:
            prior_day = max(prior_days, key=lambda candidate: candidate.trade_date)
            return float(prior_day.spot[C, -1])
    return float("nan")


class Service:
    def __init__(self, provider, runs: RunStore, jobs: Optional[JobManager] = None):
        self.provider = provider
        self.runs = runs
        self.jobs = jobs or JobManager()
        # The store carries mutable per-strategy state (strike window, session cache); one user of it at a time.
        self._store_lock = threading.RLock()

    # ------------------------------------------------------------------ config / data
    def config(self) -> Dict[str, Any]:
        cov: Dict[str, Any] = {}
        err = None
        try:
            cov = self.provider.coverage()
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
        last = cov.get("last") or date.today()
        first = cov.get("first")
        end = min(last, date.today())
        start = max(first, end - timedelta(days=90)) if first else end - timedelta(days=90)
        settings = strat.settings_to_json(strat.default_globals(start, end))
        return clean({
            "version": APP_VERSION, "coverage": cov, "coverage_error": err,
            "settings": settings,
            "strategy": strat.strategy_to_json(strat.default_params()),
            "templates": [{"name": n, "legs": strat.template_legs(n)} for n in strat.TEMPLATES if n != "Custom"],
            "expiry_rules": list(strat.EXPIRY_RULES), "weekdays": strat.WEEKDAY_NAMES,
        })

    def data_status(self, start: date, end: date, step: int = 50) -> Dict[str, Any]:
        with self._store_lock:
            return self._data_status(start, end, step)

    def _data_status(self, start: date, end: date, step: int) -> Dict[str, Any]:
        cov = self.provider.coverage()
        out: Dict[str, Any] = {"coverage": cov}
        store = self.provider.store(start, end, step)
        cat = store.catalogue
        per = cat.groupby("expiry").agg(contracts=("table", "count"), strikes=("strike", "nunique"),
                                        lo=("strike", "min"), hi=("strike", "max")).reset_index()
        per["gap_days"] = pd.to_datetime(per["expiry"]).diff().dt.days
        per["flag"] = (per["gap_days"] > 9) | (per["strikes"] < 10)
        out.update({
            "session": store.session_check(), "contracts": int(len(cat)), "expiries": per.to_dict("records"),
            "sessions": len(store.dates), "stats": dict(store.stats),
        })
        return clean(out)

    # ------------------------------------------------------------------ runs
    def start_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        g, params = self._parse(payload)
        job = self.jobs.submit("run", lambda j: self._do_run(j, payload, g, params))
        return job.snapshot()

    def start_replay(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            d = _date(payload["date"])
        except KeyError as exc:
            raise ApiError(f"missing field {exc}") from None
        g, params = self._parse(payload)
        replay_start = g["start"]
        if g["holding_mode"] == "INTRADAY" or replay_start > d:
            replay_start = d
        g["start"], g["end"] = replay_start, d
        errs = strat.validate(params, g)
        if errs:
            raise ApiError(" ".join(errs))
        job = self.jobs.submit("replay", lambda j: self._do_replay(j, d, g, params))
        return job.snapshot()

    def _do_replay(self, job: Job, d: date, g: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        job.update(0.02, "Waiting for the data layer")
        with self._store_lock:
            job.update(0.08, "Opening market data")
            warmup = strat.indicator_warmup_days(params) if params["kind"] == "indicator" else 10
            store = self.provider.store(g["start"], g["end"], int(g["strike_interval"]), warmup_days=warmup)
            if d not in store.dates:
                raise ApiError(f"NIFTY spot data is unavailable for {d.isoformat()}", 404)

            cfg = strat.build_config(params, g)
            store.set_window(*strat.strike_window(params),
                             carry_forward=(cfg.holding_mode.value != "INTRADAY"
                                            or strat.needs_multiple_expiries(params)))
            runner = run_backtest if params["kind"] == "multi" else run_signal_backtest
            result = runner(cfg, store, strat.build_registry(g),
                            progress=lambda i, n: job.update(
                                0.1 + 0.65 * i / max(n, 1), f"Simulating session {i} of {n}"))
            day_trades = result.trades
            if not day_trades.empty:
                entries = pd.to_datetime(day_trades["entry_time"]).dt.date
                exits = pd.to_datetime(day_trades["exit_time"]).dt.date
                day_trades = day_trades.loc[(entries <= d) & (exits >= d)].copy()
                if cfg.holding_mode.value != "INTRADAY":
                    boundary = (pd.to_datetime(day_trades["exit_time"]).dt.date == d) & \
                        day_trades["exit_reason"].eq("BACKTEST_END")
                    day_trades.loc[boundary, "exit_time"] = datetime.combine(d + timedelta(days=1), time.min)

            run = {
                "kind": params["kind"],
                "strategy": strat.strategy_to_json(params),
                "settings": strat.settings_to_json(g),
            }
            job.update(0.82, "Building the replay")
            chart = self._explorer_day(run, d, day_trades)
            skipped = [reason for skipped_date, reason in result.skipped if skipped_date == d]
            return clean({"date": d, "chart": chart, "trade_count": len(day_trades), "skipped": skipped})

    def _parse(self, payload: Dict[str, Any]):
        try:
            g = strat.parse_settings(payload.get("settings") or {})
            params = strat.parse_strategy(payload.get("strategy") or {})
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiError(f"invalid request: {exc}") from None
        errs = strat.validate(params, g)
        if errs:
            raise ApiError(" ".join(errs))
        return g, params

    def _do_run(self, job: Job, payload: Dict[str, Any], g: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        job.update(0.02, "Waiting for the data layer")
        with self._store_lock:
            return self._do_run_locked(job, payload, g, params)

    def _do_run_locked(self, job: Job, payload: Dict[str, Any], g: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        job.update(0.02, "Opening data")
        t0 = time_module.perf_counter()
        if params["kind"] == "indicator":
            store = self.provider.store(g["start"], g["end"], int(g["strike_interval"]),
                                        warmup_days=strat.indicator_warmup_days(params))
        else:
            store = self.provider.store(g["start"], g["end"], int(g["strike_interval"]))
        extra, fixed = strat.strike_window(params)
        cfg, registry = strat.build_config(params, g), strat.build_registry(g)
        store.set_window(extra, fixed, carry_forward=(cfg.holding_mode.value != "INTRADAY"
                                                       or strat.needs_multiple_expiries(params)))
        runner = run_backtest if params["kind"] == "multi" else run_signal_backtest
        res = runner(cfg, store, registry,
                     progress=lambda i, n: job.update(0.05 + 0.88 * i / max(n, 1), f"Simulating session {i} of {n}"))
        self._add_trade_verification_values(res.trades, cfg, store, params["kind"] == "indicator")
        job.update(0.95, "Computing analytics")
        elapsed = time_module.perf_counter() - t0
        m = res.metrics
        curve = res.daily["cum_pnl"].values if not res.daily.empty else np.array([])
        if len(curve) > 80:
            curve = curve[np.linspace(0, len(curve) - 1, 80).astype(int)]
        summary = {"net": m.get("total_net_pnl", 0.0), "days": m.get("total_trades", 0),
                   "win": m.get("win_rate_pct"), "pf": m.get("profit_factor"), "dd": m.get("max_drawdown"),
                   "sharpe": m.get("sharpe"), "curve": curve.tolist(), "start": g["start"], "end": g["end"]}
        label = ("Multi-leg" if params["kind"] == "multi" else f"{params['indicator']['type']} signals")
        name = (payload.get("name") or f"{label} · {g['start']:%d %b %y} → {g['end']:%d %b %y}")
        rid = self.runs.save(name, params["kind"], strat.strategy_to_json(params), strat.settings_to_json(g),
                             m, summary, res.daily, res.trades, [(d, w) for d, w in res.skipped], elapsed)
        return {"run_id": rid}

    def list_runs(self) -> List[Dict[str, Any]]:
        return clean(self.runs.list())

    def run_detail(self, rid: str) -> Dict[str, Any]:
        run = self._run(rid)
        g = strat.parse_settings(run["settings"])
        dash = insights.build_dashboard(run["metrics"], run["daily"], run["trades"], g["capital"] or None)
        sk = pd.DataFrame(run["skipped"], columns=["date", "reason"]) if run["skipped"] else pd.DataFrame(columns=["date", "reason"])
        grouped = (sk.groupby("reason").agg(days=("date", "count"), first=("date", "min")).reset_index()
                   .sort_values("days", ascending=False)) if not sk.empty else sk
        trades = run["trades"]
        trade_event_dates = set()
        if not trades.empty:
            trade_event_dates.update(pd.to_datetime(trades["entry_time"]).dt.date)
            trade_event_dates.update(pd.to_datetime(trades["exit_time"]).dt.date)
        return clean({
            "id": run["id"], "name": run["name"], "created": run["created"], "kind": run["kind"],
            "strategy": run["strategy"], "settings": run["settings"], "metrics": run["metrics"],
            "dashboard": dash, "skipped": grouped.to_dict("records") if not sk.empty else [],
            "skipped_dates": sk.head(300).to_dict("records"), "elapsed": run["elapsed"],
            "trade_dates": sorted({d.isoformat() for d in run["trades"]["date"]}) if not run["trades"].empty else [],
            "trade_event_dates": sorted(d.isoformat() for d in trade_event_dates),
        })

    def _run(self, rid: str) -> Dict[str, Any]:
        run = self.runs.get(rid)
        if run is None:
            raise ApiError("run not found", 404)
        return run

    @staticmethod
    def _add_trade_verification_values(trades: pd.DataFrame, cfg, store, indicator_run: bool) -> None:
        for column in ("signal_value_entry", "signal_value_exit", "signal_ema_entry", "signal_ema_exit",
                       "signal_fast_ema_entry", "signal_fast_ema_exit", "signal_slow_ema_entry",
                       "signal_slow_ema_exit", "signal_spot_entry", "signal_spot_exit", "spot_entry",
                       "spot_exit"):
            if column not in trades:
                trades[column] = np.nan
        if trades.empty:
            return

        event_dates = set(pd.to_datetime(trades["entry_time"]).dt.date)
        event_dates.update(pd.to_datetime(trades["exit_time"]).dt.date)
        history_dates = getattr(store, "history_dates", store.dates)
        dates = ([day for day in history_dates if day <= max(event_dates)]
                 if indicator_run else sorted(event_dates))
        premium_source = (indicator_run and (
            isinstance(cfg.indicator, EMAParams) and cfg.indicator.source is EMASource.OPTION
            or isinstance(cfg.indicator, VwapParams) and cfg.indicator.source is VwapSource.PREMIUM))
        days = []
        for day in dates:
            if hasattr(store, "build_spot_day") and not premium_source:
                spot_day = store.build_spot_day(day)
            else:
                spot_day, _ = store.build_day(day, cfg.expiry, cfg.max_expiry_gap_days, False)
                if spot_day is None:
                    continue
            days.append(spot_day)
        days_by_date = {day.trade_date: day for day in days}
        if premium_source:
            signal_values = indicator_premium_signal_values_for_days(
                days, cfg, NIFTY, resolve_strike)
        elif indicator_run:
            signal_values = indicator_signal_values_for_days(days, cfg)
        else:
            signal_values = {}
        ema_values = indicator_ema_values_for_days(days, cfg) if indicator_run else {}

        for index, trade in trades.iterrows():
            entry_time = pd.Timestamp(trade["entry_time"])
            exit_time = pd.Timestamp(trade["exit_time"])
            entry_day = days_by_date.get(entry_time.date())
            exit_day = days_by_date.get(exit_time.date())
            if entry_day is None or exit_day is None:
                raise RuntimeError("trade verification data is missing an entry or exit session")
            entry_bar = min(N_BARS - 1, max(0, bar_index(entry_time.time())))
            exit_bar = min(N_BARS - 1, max(0, bar_index(exit_time.time())))
            trades.at[index, "spot_entry"] = float(entry_day.spot[O, entry_bar])
            trades.at[index, "spot_exit"] = float(exit_day.spot[C, exit_bar])
            trades.at[index, "signal_spot_entry"] = _spot_close_before(
                entry_day, entry_time, days)
            trades.at[index, "signal_spot_exit"] = _spot_close_before(
                exit_day, exit_time, days)
            if indicator_run:
                entry_values = signal_values[entry_day.trade_date]
                exit_values = signal_values[exit_day.trade_date]
                if isinstance(entry_values, dict):
                    option_type = str(trade["option_type"])
                    entry_values = entry_values[option_type]
                    exit_values = exit_values[option_type]
                trades.at[index, "signal_value_entry"] = entry_values[entry_bar]
                trades.at[index, "signal_value_exit"] = exit_values[exit_bar]
                if ema_values:
                    entry_ema = ema_values[entry_day.trade_date]
                    exit_ema = ema_values[exit_day.trade_date]
                    trades.at[index, "signal_spot_entry"] = entry_ema["spot"][entry_bar]
                    trades.at[index, "signal_spot_exit"] = exit_ema["spot"][exit_bar]
                    if "ema" in entry_ema:
                        trades.at[index, "signal_ema_entry"] = entry_ema["ema"][entry_bar]
                        trades.at[index, "signal_ema_exit"] = exit_ema["ema"][exit_bar]
                    else:
                        trades.at[index, "signal_fast_ema_entry"] = entry_ema["fast_ema"][entry_bar]
                        trades.at[index, "signal_fast_ema_exit"] = exit_ema["fast_ema"][exit_bar]
                        trades.at[index, "signal_slow_ema_entry"] = entry_ema["slow_ema"][entry_bar]
                        trades.at[index, "signal_slow_ema_exit"] = exit_ema["slow_ema"][exit_bar]

    @staticmethod
    def _backfill_spot_ema_values(trades: pd.DataFrame, strategy: Dict[str, Any]) -> None:
        indicator = strategy.get("indicator", {})
        if (indicator.get("type") != "EMA" or indicator.get("ema_source") == "Option premium"
                or indicator.get("ema_mode") == "EMA crossover"
                or indicator.get("ema_timeframe", "1m") != "1m"):
            return
        for suffix in ("entry", "exit"):
            value_col = f"signal_value_{suffix}"
            spot_col = f"signal_spot_{suffix}"
            ema_col = f"signal_ema_{suffix}"
            if value_col not in trades or spot_col not in trades:
                continue
            if ema_col not in trades:
                trades[ema_col] = np.nan
            missing = trades[ema_col].isna() & trades[value_col].notna() & trades[spot_col].notna()
            trades.loc[missing, ema_col] = trades.loc[missing, spot_col] - trades.loc[missing, value_col]

    def run_trades(self, rid: str, offset: int = 0, limit: int = 100, sort: str = "date", order: str = "asc",
                   reason: str = "", result: str = "all", trade_date: Optional[str] = None) -> Dict[str, Any]:
        run = self._run(rid)
        t = run["trades"].copy()
        self._backfill_spot_ema_values(t, run["strategy"])
        if trade_date:
            selected_date = _date(trade_date)
            t = t[t["date"] == selected_date]
        if t.empty:
            return {"total": 0, "rows": [], "sum_net": 0.0}
        t["entry_date"] = pd.to_datetime(t["entry_time"]).dt.date
        t["exit_date"] = pd.to_datetime(t["exit_time"]).dt.date
        t["dte"] = [(e - d).days for d, e in zip(t["date"], t["expiry"])]
        if reason:
            t = t[t["exit_reason"].isin(reason.split(","))]
        if result == "win":
            t = t[t["net_pnl"] > 0]
        elif result == "loss":
            t = t[t["net_pnl"] < 0]
        if sort in t.columns:
            t = t.sort_values([sort, "entry_time"], ascending=(order != "desc"), kind="stable")
        total, sum_net = len(t), float(t["net_pnl"].sum())
        page = t.iloc[max(0, offset):max(0, offset) + max(1, min(limit, 500))].copy()
        page["entry_time"] = pd.to_datetime(page["entry_time"]).dt.strftime("%H:%M")
        page["exit_time"] = pd.to_datetime(page["exit_time"]).dt.strftime("%H:%M")
        if "signal_direction" not in page:
            page["signal_direction"] = ""
        for col in ("entry_iv", "entry_delta", "entry_gamma", "entry_theta", "entry_vega"):
            if col not in page:
                page[col] = np.nan
        for col in ("signal_value_entry", "signal_value_exit", "signal_ema_entry", "signal_ema_exit",
                    "signal_fast_ema_entry", "signal_fast_ema_exit", "signal_slow_ema_entry",
                    "signal_slow_ema_exit", "signal_spot_entry", "signal_spot_exit", "spot_entry",
                    "spot_exit"):
            if col not in page:
                page[col] = np.nan
        cols = ["entry_date", "dte", "leg_id", "signal_direction", "segment", "side", "option_type",
                "strike", "expiry", "qty", "entry_time", "entry_price", "signal_value_entry",
                "signal_ema_entry", "signal_fast_ema_entry", "signal_slow_ema_entry", "signal_spot_entry",
                "spot_entry", "exit_date", "exit_time", "exit_price", "signal_value_exit",
                "signal_ema_exit", "signal_fast_ema_exit", "signal_slow_ema_exit", "signal_spot_exit",
                "spot_exit", "exit_reason",
                "gross_pnl", "slippage_cost", "costs", "net_pnl",
                "entry_iv", "entry_delta", "entry_gamma", "entry_theta", "entry_vega"]
        return clean({"total": total, "sum_net": sum_net, "rows": page[cols].to_dict("records")})

    def trades_csv(self, rid: str) -> bytes:
        run = self._run(rid)
        t = run["trades"].copy()
        self._backfill_spot_ema_values(t, run["strategy"])
        if not t.empty:
            t.insert(0, "entry_date", pd.to_datetime(t["entry_time"]).dt.date)
            t.insert(1, "exit_date", pd.to_datetime(t["exit_time"]).dt.date)
        buf = io.StringIO()
        t.to_csv(buf, index=False)
        return buf.getvalue().encode("utf-8")

    def chart_contracts_global(self, year: str = "", expiry: str = "") -> Dict[str, Any]:
        coverage = self.provider.coverage()
        first, last = coverage.get("first"), coverage.get("last")
        if first is None or last is None:
            raise ApiError("market data coverage is unavailable", 404)
        access = getattr(self.provider, "access", None)
        if access is not None:
            years = [year for year in access.option_years() if first.year <= year <= last.year + 1]
            catalogue = access.catalogue(years)
        else:
            store = self.provider.store(first, last, 50, warmup_days=0)
            catalogue = getattr(store, "catalogue", pd.DataFrame()).copy()
        if catalogue.empty:
            raise ApiError("no option contracts are available", 404)
        expiry_column = "expiry"
        type_column = "type" if "type" in catalogue else "option_type"
        catalogue[expiry_column] = pd.to_datetime(catalogue[expiry_column]).dt.date
        years = sorted({value.year for value in catalogue[expiry_column].unique()}, reverse=True)
        try:
            selected_year = int(year) if year else years[0]
        except (ValueError, TypeError):
            raise ApiError("invalid option expiry year") from None
        if selected_year not in years:
            raise ApiError(f"no option expiries are available for {selected_year}", 404)
        year_expiries = sorted(
            (value for value in catalogue[expiry_column].unique() if value.year == selected_year),
            reverse=True)
        selected_expiry = _date(expiry) if expiry else year_expiries[0]
        if selected_expiry.year != selected_year or selected_expiry not in year_expiries:
            selected_expiry = year_expiries[0]
        contracts = catalogue[catalogue[expiry_column] == selected_expiry]
        return clean({
            "first": first, "last": last,
            "years": years, "expiries": [value for value in year_expiries],
            "selected_year": selected_year,
            "selected_expiry": selected_expiry,
            "contracts": [{"expiry": selected_expiry,
                           "strike": int(row.strike),
                           "option_type": str(getattr(row, type_column)).upper()}
                          for row in contracts[["strike", type_column]].drop_duplicates()
                          .sort_values(["strike", type_column]).itertuples(index=False)],
        })

    def market_chart_history(self, instrument: str = "spot", timeframe: str = "1m",
                             expiry: str = "", strike: str = "", option_type: str = "",
                             before: str = "", limit: int = 100, run_id: str = "") -> Dict[str, Any]:
        intervals = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15,
                     "30m": 30, "60m": 60, "2h": 120, "4h": 240,
                     "1d": "day", "1w": "week", "1mo": "month"}
        if timeframe not in intervals:
            raise ApiError("unsupported chart timeframe")
        if instrument not in ("spot", "option"):
            raise ApiError("instrument must be spot or option")
        try:
            limit = max(10, min(int(limit), 500))
        except (TypeError, ValueError):
            raise ApiError("chart limit must be a whole number") from None
        contract = None
        if instrument == "option":
            try:
                contract = (_date(expiry), int(strike), option_type.upper())
            except (ValueError, AttributeError):
                raise ApiError("a valid option expiry, strike and type are required") from None
            if contract[2] not in ("CE", "PE"):
                raise ApiError("option type must be CE or PE")

        coverage = self.provider.coverage()
        first, last = coverage.get("first"), coverage.get("last")
        if first is None or last is None:
            raise ApiError("market data coverage is unavailable", 404)
        interval = intervals[timeframe]
        cursor = pd.Timestamp(before) if before else None
        if cursor is not None and cursor.tzinfo is not None:
            cursor = cursor.tz_convert("Asia/Kolkata").tz_localize(None)
        if cursor is None:
            end = last
        elif interval == "week":
            end = min(cursor.to_period("W-SUN").start_time.date() - timedelta(days=1), last)
        elif interval == "month":
            end = min(cursor.to_period("M").start_time.date() - timedelta(days=1), last)
        elif interval == "day":
            end = min(cursor.date() - timedelta(days=1), last)
        else:
            end = min(cursor.date(), last)
        if interval == "day":
            span_days = 220
        elif interval == "week":
            span_days = 770
        elif interval == "month":
            span_days = 3650
        else:
            span_days = max(14, int(np.ceil(limit * interval * 7 / 375 * 1.6)) + 7)
        access = getattr(self.provider, "access", None)
        store = self.provider.store(first, last, 50, warmup_days=0) if access is None else None
        frame = pd.DataFrame()
        start = first
        bars = []
        while True:
            start = max(first, end - timedelta(days=span_days))
            if instrument == "spot":
                if access is not None:
                    frame = access.spot_frame(start, end)
                    frame = frame.rename(columns={"datetime": "timestamp"})
                else:
                    frame = store.spot.copy()
                    frame = frame[(frame["timestamp"].dt.date >= start)
                                  & (frame["timestamp"].dt.date <= end)]
            elif access is not None:
                frame = access.chart_contract_frame(*contract, start, end)
            else:
                frame = store.options.copy()
                frame = frame[(frame["expiry"].dt.date == contract[0])
                              & (frame["strike"] == contract[1])
                              & (frame["option_type"].str.upper() == contract[2])
                              & (frame["timestamp"].dt.date >= start)
                              & (frame["timestamp"].dt.date <= end)]
            if not frame.empty:
                frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
                if cursor is not None and interval not in ("day", "week", "month"):
                    frame = frame[frame["timestamp"] < cursor]
                frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
                frame = frame.sort_values("timestamp", kind="stable")
                minute = frame["timestamp"].dt.hour * 60 + frame["timestamp"].dt.minute - 555
                session_duration = frame["timestamp"].dt.date.map(
                    lambda session: _chart_session_duration(
                        session, instrument))
                frame = frame[(minute >= 0) & (minute < session_duration)].copy()
                minute = frame["timestamp"].dt.hour * 60 + frame["timestamp"].dt.minute - 555
                frame["_session"] = frame["timestamp"].dt.date
                if interval == "day":
                    frame["_bucket"] = frame["_session"]
                    group_columns = ["_bucket"]
                elif interval == "week":
                    frame["_bucket"] = frame["timestamp"].dt.to_period("W-SUN").astype(str)
                    group_columns = ["_bucket"]
                elif interval == "month":
                    frame["_bucket"] = frame["timestamp"].dt.to_period("M").astype(str)
                    group_columns = ["_bucket"]
                else:
                    frame["_bucket"] = (minute // interval).astype(int)
                    group_columns = ["_session", "_bucket"]
                bars = []
                for key, rows in frame.groupby(group_columns, sort=True):
                    bucket = key[-1] if isinstance(key, tuple) else key
                    if interval == "day":
                        stamp = pd.Timestamp(bucket).normalize().to_pydatetime()
                    elif interval == "week":
                        stamp = pd.Period(bucket, freq="W-SUN").start_time.to_pydatetime()
                    elif interval == "month":
                        stamp = pd.Period(bucket, freq="M").start_time.to_pydatetime()
                    else:
                        session = key[0]
                        stamp = (datetime.combine(session, time(9, 15))
                                 + timedelta(minutes=int(bucket) * interval))
                    bar = {"time": stamp.isoformat(timespec="minutes"),
                           "open": float(rows.iloc[0]["open"]),
                           "high": float(rows["high"].max()),
                           "low": float(rows["low"].min()),
                           "close": float(rows.iloc[-1]["close"]),
                           "volume": float(rows["volume"].fillna(0).sum()) if "volume" in rows else 0.0}
                    if "open_interest" in rows and rows["open_interest"].notna().any():
                        bar["oi"] = float(rows["open_interest"].dropna().iloc[-1])
                    bars.append(bar)
                if cursor is not None and interval in ("day", "week", "month"):
                    bars = [bar for bar in bars if pd.Timestamp(bar["time"]) < cursor]
            if len(bars) > limit or start == first:
                break
            span_days = min(span_days * 2, max(1, (end - first).days))
        has_older = bool(bars and start > first)
        bars = bars[-limit:]
        title = "NIFTY spot" if contract is None else f"NIFTY {contract[0]} {contract[1]} {contract[2]}"
        events = []
        if run_id and bars:
            run = self._run(run_id)
            index_by_time = {bar["time"]: index for index, bar in enumerate(bars)}
            for row in run["trades"].itertuples(index=False):
                if contract is not None and (
                    pd.Timestamp(row.expiry).date() != contract[0]
                    or int(row.strike) != contract[1]
                    or str(row.option_type).upper() != contract[2]
                ):
                    continue
                for kind, raw_time, option_price, spot_price in (
                    ("ENTRY", row.entry_time, getattr(row, "entry_price", np.nan),
                     getattr(row, "spot_entry", np.nan)),
                    ("EXIT", row.exit_time, getattr(row, "exit_price", np.nan),
                     getattr(row, "spot_exit", np.nan)),
                ):
                    timestamp = pd.Timestamp(raw_time)
                    minute = timestamp.hour * 60 + timestamp.minute - 555
                    session_duration = _chart_session_duration(timestamp.date(), instrument)
                    if not 0 <= minute < session_duration:
                        continue
                    if interval == "day":
                        bucket_time = datetime.combine(timestamp.date(), time.min)
                    elif interval == "week":
                        bucket_time = (pd.Timestamp(timestamp.date()).to_period("W-SUN").start_time.to_pydatetime())
                    elif interval == "month":
                        bucket_time = datetime(timestamp.year, timestamp.month, 1)
                    else:
                        bucket_time = (datetime.combine(timestamp.date(), time(9, 15))
                                       + timedelta(minutes=(minute // interval) * interval))
                    time_key = bucket_time.isoformat(timespec="minutes")
                    index = index_by_time.get(time_key)
                    price = option_price if contract is not None else spot_price
                    if index is None or pd.isna(price):
                        continue
                    events.append({
                        "index": index, "kind": kind, "time": time_key, "price": float(price),
                        "side": str(row.side), "option_type": str(row.option_type),
                        "strike": int(row.strike), "net_pnl": float(row.net_pnl),
                    })
        return clean({"title": title, "instrument": instrument, "timeframe": timeframe,
                      "bars": bars, "events": events, "has_older": has_older,
                      "oi_available": any("oi" in bar for bar in bars),
                      "first": first, "last": last})

    def chart_contracts(self, rid: str, d: date) -> Dict[str, Any]:
        with self._store_lock:
            run = self._run(rid)
            settings = strat.parse_settings(run["settings"])
            store = self.provider.store(settings["start"], settings["end"],
                                        int(settings["strike_interval"]))
            dates = getattr(store, "history_dates", store.dates)
            if d not in dates:
                raise ApiError(f"spot candles unavailable for {d.isoformat()}", 404)
            option_day, _ = store.build_day(d, ExpirySelect.CURRENT_WEEKLY, None, False)
            contracts = sorted({
                (pd.Timestamp(exp).date().isoformat(), int(strike), str(option_type).upper())
                for exp, strike, option_type in (option_day.options if option_day else {})
            })
            return clean({"dates": [x.isoformat() for x in dates],
                          "contracts": [{"expiry": exp, "strike": strike, "option_type": typ}
                                        for exp, strike, typ in contracts]})

    def market_chart(self, rid: str, d: date, instrument: str = "spot", expiry: str = "",
                     strike: str = "", option_type: str = "", timeframe: str = "1m") -> Dict[str, Any]:
        with self._store_lock:
            run = self._run(rid)
            settings = strat.parse_settings(run["settings"])
            intervals = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30,
                         "60m": 60, "2h": 120, "4h": 240,
                         "1d": N_BARS, "1w": N_BARS, "1mo": N_BARS}
            if timeframe not in intervals:
                raise ApiError("unsupported chart timeframe")
            store = self.provider.store(settings["start"], settings["end"],
                                        int(settings["strike_interval"]))
            dates = getattr(store, "history_dates", store.dates)
            if d not in dates:
                raise ApiError(f"spot candles unavailable for {d.isoformat()}", 404)
            day = (store.build_spot_day(d) if hasattr(store, "build_spot_day")
                   else store.build_day(d, ExpirySelect.NEAR, None, False)[0])
            if day is None:
                raise ApiError(f"market data unavailable for {d.isoformat()}", 404)

            option_key = None
            if instrument == "option":
                try:
                    option_key = (_date(expiry), int(strike), option_type.upper())
                except (ValueError, AttributeError):
                    raise ApiError("a valid option expiry, strike and type are required") from None
                if option_key[2] not in ("CE", "PE"):
                    raise ApiError("option type must be CE or PE")
                loader = getattr(store, "load_contract_day", None)
                option = day.options.get(option_key)
                if option is None and loader is not None:
                    option = loader(d, option_key)
                if option is None:
                    loaded_day, _ = store.build_day(d, ExpirySelect.CURRENT_WEEKLY, None, False)
                    option = loaded_day.options.get(option_key) if loaded_day is not None else None
                if option is None:
                    raise ApiError("no option candles for the selected contract and date", 404)
                values = option
                title = f"NIFTY {option_key[0]} {option_key[1]} {option_key[2]}"
            elif instrument == "spot":
                values = day.spot
                title = "NIFTY spot"
            else:
                raise ApiError("instrument must be spot or option")

            interval = intervals[timeframe]
            bars = []
            first_valid = np.flatnonzero(np.isfinite(values[C]))
            if not first_valid.size:
                raise ApiError("no candles for the selected instrument and date", 404)
            first_group = int(first_valid[0]) // interval
            bucket_indices = {}
            for lo in range(first_group * interval, N_BARS, interval):
                hi = min(lo + interval, N_BARS)
                close_values = values[C, lo:hi]
                valid = np.isfinite(close_values)
                if not valid.any():
                    continue
                valid_offsets = np.flatnonzero(valid)
                bar_lo, bar_hi = lo + int(valid_offsets[0]), lo + int(valid_offsets[-1]) + 1
                bucket_indices[lo // interval] = len(bars)
                bars.append({
                    "time": (datetime.combine(d, time(9, 15)) + timedelta(minutes=lo)).strftime("%H:%M"),
                    "open": float(values[O, bar_lo]), "high": float(np.nanmax(values[H, bar_lo:bar_hi])),
                    "low": float(np.nanmin(values[L, bar_lo:bar_hi])), "close": float(values[C, bar_hi - 1]),
                    "volume": float(np.nansum(values[V, lo:hi])) if values.shape[0] > V else 0.0,
                })

            events = []
            trades = run["trades"]
            if not trades.empty:
                for row in trades.itertuples(index=False):
                    for kind, raw_time, option_price, reason in (
                        ("ENTRY", row.entry_time, row.entry_price, ""),
                        ("EXIT", row.exit_time, row.exit_price, row.exit_reason),
                    ):
                        timestamp = pd.Timestamp(raw_time)
                        if timestamp.date() != d:
                            continue
                        if option_key is not None and (
                            pd.Timestamp(row.expiry).date() != option_key[0]
                            or int(row.strike) != option_key[1]
                            or str(row.option_type).upper() != option_key[2]
                        ):
                            continue
                        minute = min(N_BARS - 1, max(0, bar_index(timestamp.time())))
                        chart_index = bucket_indices.get(minute // interval)
                        if chart_index is None:
                            continue
                        events.append({
                            "index": chart_index, "kind": kind, "side": str(row.side),
                            "price": float(option_price) if option_key is not None
                            else float(day.spot[C, minute]),
                            "option_price": float(option_price), "reason": str(reason),
                            "leg_id": str(row.leg_id), "strike": int(row.strike),
                            "option_type": str(row.option_type), "time": timestamp.strftime("%H:%M"),
                            "net_pnl": float(row.net_pnl),
                        })
            return clean({"date": d, "instrument": instrument, "title": title, "timeframe": timeframe,
                          "bars": bars, "events": events})

    def backtest_pdf(self, rid: str) -> tuple[str, bytes, str]:
        from .report import build_backtest_pdf

        with self._store_lock:
            run = dict(self._run(rid))
            settings = strat.parse_settings(run["settings"])
            store = self.provider.store(settings["start"], settings["end"],
                                        int(settings["strike_interval"]))
            available_dates = set(getattr(store, "history_dates", store.dates))
            chart_events = {}
            for row in run["trades"].itertuples(index=False):
                contract = (pd.Timestamp(row.expiry).date(), int(row.strike), str(row.option_type).upper())
                for kind, raw_time, price in (("ENTRY", row.entry_time, row.entry_price),
                                              ("EXIT", row.exit_time, row.exit_price)):
                    timestamp = pd.Timestamp(raw_time)
                    if timestamp.date() not in available_dates:
                        continue
                    chart_date = timestamp.date()
                    key = (chart_date, contract)
                    minute = min(N_BARS - 1, max(0, bar_index(timestamp.time())))
                    chart_events.setdefault(key, []).append({
                        "index": minute, "kind": kind, "time": timestamp.strftime("%H:%M"),
                        "price": float(price),
                    })
            chart_data = []
            loaded_days = {}
            loader = getattr(store, "load_contract_day", None)
            for (chart_date, contract), events in sorted(chart_events.items()):
                prices = loader(chart_date, contract) if loader is not None else None
                if prices is None:
                    if chart_date not in loaded_days:
                        loaded_days[chart_date], _ = store.build_day(
                            chart_date, ExpirySelect.CURRENT_WEEKLY, None, False)
                    day = loaded_days[chart_date]
                    prices = day.options.get(contract) if day is not None else None
                if prices is None:
                    continue
                chart_data.append({
                    "date": chart_date, "contract": f"{contract[1]} {contract[2]} · {contract[0]}",
                    "open": [float(value) for value in prices[O]],
                    "high": [float(value) for value in prices[H]],
                    "low": [float(value) for value in prices[L]],
                    "close": [float(value) for value in prices[C]],
                    "events": events,
                })
            run["trade_charts"] = chart_data
        run["metrics"] = {
            **run["metrics"],
            **insights.extra_metrics(run["metrics"], run["daily"], run["trades"]),
        }
        data = build_backtest_pdf(run)
        filename = f"backtest_{rid}.pdf"
        return "application/pdf", data, filename

    def rename_run(self, rid: str, name: str) -> Dict[str, Any]:
        if not self.runs.rename(rid, name.strip() or "Untitled"):
            raise ApiError("run not found", 404)
        return {"ok": True}

    def delete_run(self, rid: str) -> Dict[str, Any]:
        if not self.runs.delete(rid):
            raise ApiError("run not found", 404)
        return {"ok": True}

    def compare(self, ids: List[str]) -> Dict[str, Any]:
        out = []
        for rid in ids[:8]:
            r = self._run(rid)
            d = r["daily"]
            out.append({"id": rid, "name": r["name"], "metrics": r["metrics"],
                        "curve": [{"date": x.date, "cum": x.cum_pnl} for x in d.itertuples()]})
        return clean({"runs": out})

    # ------------------------------------------------------------------ explorer
    def nifty_backtest_chart(self, rid: str) -> Dict[str, Any]:
        with self._store_lock:
            run = self._run(rid)
            if run["kind"] != "indicator":
                raise ApiError("full-duration NIFTY charts are available for indicator runs only", 400)

            settings = strat.parse_settings(run["settings"])
            params = strat.parse_strategy(run["strategy"])
            cfg = strat.build_config(params, settings)
            store = self.provider.store(
                settings["start"], settings["end"], int(settings["strike_interval"]),
                warmup_days=strat.indicator_warmup_days(params))
            available_dates = getattr(store, "history_dates", store.dates)
            dates = [d for d in available_dates if d <= settings["end"]]
            if not dates:
                raise ApiError("NIFTY spot data is unavailable for this backtest period", 404)

            days = []
            for d in dates:
                if not hasattr(store, "build_spot_day"):
                    raise ApiError("data provider does not support spot-only candles", 501)
                days.append(store.build_spot_day(d))

            chart_data = indicator_backtest_chart_data(days, cfg)
            date_index = {d: i for i, d in enumerate(dates)}

            events = []
            trades = run["trades"]
            for row in trades.itertuples(index=False):
                for kind, timestamp, price, reason in (
                    ("ENTRY", pd.Timestamp(row.entry_time), row.entry_price, ""),
                    ("EXIT", pd.Timestamp(row.exit_time), row.exit_price, row.exit_reason),
                ):
                    session_index = date_index.get(timestamp.date())
                    if session_index is None:
                        continue
                    minute = min(374, max(0, bar_index(timestamp.time())))
                    chart_index = chart_data["minute_bar_map"][session_index][minute]
                    if chart_index < 0:
                        continue
                    events.append({
                        "index": chart_index, "kind": kind, "side": row.side,
                        "option_type": row.option_type, "strike": int(row.strike),
                        "leg_id": row.leg_id, "segment": int(row.segment),
                        "option_price": float(price), "reason": reason,
                        "net_pnl": float(row.net_pnl), "time": timestamp.strftime("%H:%M"),
                    })
            chart_data.pop("minute_bar_map")
            chart_data["events"] = events
            return clean(chart_data)

    def nifty_trade_chart(self, rid: str, d: date) -> Dict[str, Any]:
        with self._store_lock:
            run = self._run(rid)
            settings = strat.parse_settings(run["settings"])
            trades = run["trades"]
            if trades.empty:
                raise ApiError("run has no trades", 404)

            entry_times = pd.to_datetime(trades["entry_time"])
            exit_times = pd.to_datetime(trades["exit_time"])
            on_date = entry_times.dt.date.eq(d) | exit_times.dt.date.eq(d)
            day_trades = trades.loc[on_date]
            if day_trades.empty:
                raise ApiError("no trade entries or exits on that date", 404)

            store = self.provider.store(settings["start"], settings["end"], int(settings["strike_interval"]))
            available_dates = getattr(store, "history_dates", store.dates)
            if d not in available_dates:
                raise ApiError(f"NIFTY spot data unavailable for {d.isoformat()}", 404)
            if hasattr(store, "build_spot_day"):
                day = store.build_spot_day(d)
            else:
                params = strat.parse_strategy(run["strategy"])
                cfg = strat.build_config(params, settings)
                day, why = store.build_day(d, cfg.expiry, cfg.max_expiry_gap_days, False)
                if day is None:
                    raise ApiError(f"could not rebuild NIFTY candles: {why}", 409)

            events = []
            for row in day_trades.itertuples(index=False):
                for kind, timestamp, price, reason in (
                    ("ENTRY", pd.Timestamp(row.entry_time), row.entry_price, ""),
                    ("EXIT", pd.Timestamp(row.exit_time), row.exit_price, row.exit_reason),
                ):
                    if timestamp.date() != d:
                        continue
                    index = min(374, max(0, bar_index(timestamp.time())))
                    events.append({
                        "index": index, "kind": kind, "side": row.side,
                        "option_type": row.option_type, "strike": int(row.strike),
                        "leg_id": row.leg_id, "segment": int(row.segment),
                        "option_price": float(price), "reason": reason,
                        "net_pnl": float(row.net_pnl),
                    })
            return clean({
                "date": d, "bars": [{"open": day.spot[O, i], "high": day.spot[H, i],
                                    "low": day.spot[L, i], "close": day.spot[C, i]}
                                   for i in range(day.spot.shape[1])],
                "events": events,
            })

    def explorer(self, rid: str, d: date) -> Dict[str, Any]:
        with self._store_lock:
            return self._explorer(rid, d)

    def _explorer(self, rid: str, d: date) -> Dict[str, Any]:
        run = self._run(rid)
        tr = run["trades"]
        if not tr.empty:
            entry_dates = pd.to_datetime(tr["entry_time"]).dt.date
            exit_dates = pd.to_datetime(tr["exit_time"]).dt.date
            tr = tr.loc[(entry_dates <= d) & (exit_dates >= d)]
        if tr.empty:
            raise ApiError("no trades on that date", 404)
        return self._explorer_day(run, d, tr)

    def _explorer_day(self, run: Dict[str, Any], d: date, tr: pd.DataFrame) -> Dict[str, Any]:
        g, params = strat.parse_settings(run["settings"]), strat.parse_strategy(run["strategy"])
        cfg = strat.build_config(params, g)
        warmup = strat.indicator_warmup_days(params) if params["kind"] == "indicator" else 10
        store = self.provider.store(g["start"], g["end"], int(g["strike_interval"]), warmup_days=warmup)
        store.set_window(*strat.strike_window(params),
                         carry_forward=(cfg.holding_mode.value != "INTRADAY"
                                        or strat.needs_multiple_expiries(params)))
        need_prev = params["kind"] == "indicator" and params["indicator"]["type"] == "Supertrend" \
            and params["indicator"]["st_prev"]
        day, why = store.build_day(d, cfg.expiry, cfg.max_expiry_gap_days, need_prev)
        if day is None:
            raise ApiError(f"could not rebuild the day: {why}", 409)
        _ensure_trade_contracts(day, tr, store)
        path = intraday_path(day, tr)
        indicator = None
        if params["kind"] == "indicator" and isinstance(cfg, SignalStrategyConfig):
            available_history = getattr(store, "history_dates", store.dates)
            history_dates = [history_date for history_date in available_history if history_date <= d]
            if hasattr(store, "build_spot_day"):
                history_days = [store.build_spot_day(history_date) for history_date in history_dates]
            else:
                history_days = [day]
            indicator = indicator_display_for_day(history_days, cfg, d)
        legs = []
        net_delta = np.zeros(375)
        net_theta = np.zeros(375)
        net_vega = np.zeros(375)
        for r in tr.itertuples():
            arr = day.options[(r.expiry, int(r.strike), r.option_type)]
            key = f"{r.leg_id}#{r.segment}"
            entry_date, exit_date = r.entry_time.date(), r.exit_time.date()
            entry_idx = (0 if entry_date < d else bar_index(r.entry_time.time()))
            closed_today = exit_date == d
            exit_idx = bar_index(r.exit_time.time()) if closed_today else N_BARS - 1
            gseries = greeks_series(day, (r.expiry, int(r.strike), r.option_type), cfg.greek)
            sign = 1.0 if r.side == "BUY" else -1.0
            live = slice(entry_idx, max(entry_idx, exit_idx))
            net_delta[live] += np.nan_to_num(sign * r.lots * gseries["delta"][live])
            net_theta[live] += np.nan_to_num(sign * r.qty * gseries["theta"][live])
            net_vega[live] += np.nan_to_num(sign * r.qty * gseries["vega"][live])
            legs.append({
                "id": key, "side": r.side, "type": r.option_type, "strike": int(r.strike), "qty": int(r.qty),
                "entry_idx": entry_idx, "exit_idx": exit_idx, "closed_today": closed_today,
                "entry_date": entry_date, "exit_date": exit_date,
                "entry_time": r.entry_time.strftime("%H:%M"), "exit_time": r.exit_time.strftime("%H:%M"),
                "entry_price": r.entry_price, "exit_price": r.exit_price, "reason": r.exit_reason,
                "gross": r.gross_pnl, "costs": r.costs, "net": r.net_pnl,
                "close": arr[C], "mtm": path[key].values, "iv": gseries["iv"], "delta": gseries["delta"]})
        closed = tr[pd.to_datetime(tr["exit_time"]).dt.date == d]
        return clean({
            "date": d, "expiry": tr["expiry"].iloc[0] if not tr.empty else None, "base_time": "09:15",
            "spot": {"o": day.spot[0], "h": day.spot[1], "l": day.spot[2], "c": day.spot[3]},
            "indicator": indicator,
            "mtm": path["mtm"].values, "legs": legs,
            "greeks": {"net_delta": net_delta, "net_theta": net_theta, "net_vega": net_vega},
            "totals": {"gross": float(path["mtm"].iloc[-1]), "costs": float(closed["costs"].sum()),
                       "slippage": float(closed["slippage_cost"].sum()), "net": float(closed["net_pnl"].sum()),
                       "peak": float(path["mtm"].max()), "trough": float(path["mtm"].min())},
        })

    # ------------------------------------------------------------------ strategy preview (expiry payoff on a real day)
    def preview(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        g, params = self._parse(payload)
        if params["kind"] != "multi":
            return {"supported": False, "message": "Indicator strategies choose their strikes when a signal fires; "
                                                   "run a backtest to see them."}
        with self._store_lock:
            return self._preview(payload, g, params)

    def _preview(self, payload: Dict[str, Any], g: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        cfg, registry = strat.build_config(params, g), strat.build_registry(g)
        spec = registry.get("NIFTY")
        store = self.provider.store(g["start"], g["end"], int(g["strike_interval"]))
        store.set_window(*strat.strike_window(params),
                         carry_forward=strat.needs_multiple_expiries(params))
        dates = store.dates
        if not dates:
            raise ApiError("no sessions in the selected window", 404)
        want = _date(payload["date"]) if payload.get("date") else dates[-1]
        cands = [d for d in reversed(dates) if d <= want][:12]
        day, why, used = None, "no session", None
        for d in cands:
            day, why = store.build_day(d, cfg.expiry, cfg.max_expiry_gap_days, False)
            if day is not None:
                used = d
                break
        if day is None:
            raise ApiError(f"no usable session near {want}: {why}", 404)
        ei0 = bar_index(cfg.entry_time)
        xi = min(bar_index(cfg.exit_time), 374)
        fidx = O if cfg.entry_price_field == "open" else C
        expiry = select_expiry(day, cfg.expiry)
        ei, condition_message = find_entry_bar(day, cfg, spec, expiry, ei0, xi, fidx, resolve_strike)
        conditions = {"enabled": bool(cfg.entry_conditions), "met": ei is not None,
                      "message": condition_message, "entry_minute": None if ei is None else ei - ei0}
        use_idx = ei if ei is not None else ei0
        sm = slippage_model(cfg)
        lot = spec.lot_size_for(expiry)
        carry, carry_source = estimate_carry(day, expiry, cfg.greek)
        legs, ok = [], True
        net_greek = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        greek_count = 0
        for i, leg in enumerate(cfg.legs):
            leg_expiry = select_leg_expiry(day, expiry, leg.expiry_offset)
            if leg_expiry is None:
                legs.append({"i": i + 1, "error": f"no listed expiry at offset {leg.expiry_offset}"})
                ok = False
                continue
            leg_lot = spec.lot_size_for(leg_expiry)
            key = resolve_strike(leg.strike, leg.option_type, day, leg_expiry, use_idx, spec, fidx, cfg.greek)
            if key is None:
                legs.append({"i": i + 1, "error": "strike unavailable at entry on this day"})
                ok = False
                continue
            arr = day.options[key]
            raw = float(arr[fidx, use_idx])
            s = 1 if leg.side.value == "BUY" else -1
            eff = sm.fill(raw, s == 1, float(arr[4, use_idx]) if np.isfinite(arr[4, use_idx]) else 0.0)
            greeks = greeks_at(day, key, use_idx, fidx, cfg.greek)
            legs.append({"i": i + 1, "side": leg.side.value, "type": key[2], "strike": key[1],
                         "expiry": leg_expiry, "lots": leg.lots, "qty": leg.lots * leg_lot,
                         "premium": raw, "fill": eff, "s": s, **greeks})
            if np.isfinite(greeks["delta"]):
                greek_count += 1
                net_greek["delta"] += s * leg.lots * greeks["delta"]
                net_greek["gamma"] += s * leg.lots * greeks["gamma"]
                net_greek["theta"] += s * leg.lots * leg_lot * greeks["theta"]
                net_greek["vega"] += s * leg.lots * leg_lot * greeks["vega"]
        spot = float(day.spot[fidx, use_idx])
        res = {"supported": True, "date": used, "requested": want, "expiry": expiry, "dte": (expiry - used).days,
               "spot": spot, "lot": lot, "legs": legs, "ok": ok, "conditions": conditions,
               "entry_time": f"{(555 + use_idx) // 60:02d}:{(555 + use_idx) % 60:02d}",
               "carry": {"annual_pct": carry * 100.0, "source": carry_source,
                         "rate_pct": g["greek_rate"]},
               "net": {**net_greek, "complete": greek_count == sum("error" not in x for x in legs)}}
        good = [l for l in legs if "error" not in l]
        if good:
            ks = [l["strike"] for l in good]
            lo = max(1.0, min(min(ks), spot) * 0.93)
            hi = max(max(ks), spot) * 1.07
            step = max(1.0, spec.strike_step / 5.0)
            xs = np.arange(lo, hi + step, step)
            pnl = np.zeros_like(xs)
            today = np.zeros_like(xs)
            payoff_available = True
            today_available = True
            for l in good:
                intrinsic = np.maximum(xs - l["strike"], 0) if l["type"] == "CE" else np.maximum(l["strike"] - xs, 0)
                tte_now = float(tte_years(l["expiry"], used, use_idx, at_close=(fidx == C)))
                tte_at_expiry = float(tte_years(l["expiry"], expiry, N_BARS - 1, at_close=True))
                if tte_at_expiry <= 0:
                    value_at_expiry = intrinsic
                elif np.isfinite(l["iv"]):
                    value_at_expiry = b76_price(xs * np.exp(carry * tte_at_expiry), float(l["strike"]),
                                                tte_at_expiry, l["iv"] / 100.0, l["type"] == "CE",
                                                np.exp(-cfg.greek.rate * tte_at_expiry))
                else:
                    payoff_available = False
                    value_at_expiry = np.zeros_like(xs)
                pnl += l["s"] * (value_at_expiry - l["fill"]) * l["qty"]
                if np.isfinite(l["iv"]):
                    if tte_now > 0:
                        theoretical = b76_price(xs * np.exp(carry * tte_now), float(l["strike"]), tte_now,
                                                l["iv"] / 100.0, l["type"] == "CE",
                                                np.exp(-cfg.greek.rate * tte_now))
                    else:
                        theoretical = intrinsic
                    today += l["s"] * (theoretical - l["fill"]) * l["qty"]
                else:
                    today_available = False
            if not today_available:
                today[:] = np.nan
            if payoff_available:
                sign = np.sign(pnl)
            else:
                res["payoff_error"] = "Expiry payoff estimate needs implied volatility for each later-expiry leg."
            if payoff_available:
                be = []
                for j in np.nonzero(sign[:-1] * sign[1:] < 0)[0]:
                    x0, x1, y0, y1 = xs[j], xs[j + 1], pnl[j], pnl[j + 1]
                    be.append(float(x0 - y0 * (x1 - x0) / (y1 - y0)))
                net_prem = float(sum(-l["s"] * l["fill"] * l["qty"] for l in good))
                up_slope = pnl[-1] - pnl[-2]
                dn_slope = pnl[0] - pnl[1]
                res.update({
                    "payoff": {"x": xs, "y": pnl, "today": today if np.isfinite(today).all() else None},
                    "breakevens": be, "net_premium": net_prem,
                    "max_profit": None if up_slope > 1e-9 or dn_slope > 1e-9 else float(pnl.max()),
                    "max_loss": None if up_slope < -1e-9 or dn_slope < -1e-9 else float(pnl.min()),
                    "unlimited_profit": bool(up_slope > 1e-9 or dn_slope > 1e-9),
                    "unlimited_loss": bool(up_slope < -1e-9 or dn_slope < -1e-9),
                    "premium_notional": float(sum(l["fill"] * l["qty"] for l in good)),
                })
        return clean(res)

    # ------------------------------------------------------------------ optimizer
    def start_optimize(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        g, params = self._parse(payload)
        if params["kind"] != "multi":
            raise ApiError("the optimizer works on multi-leg strategies")
        xs = self._axis(payload.get("x") or {})
        ys = self._axis(payload.get("y") or {})
        if xs["param"] == ys["param"]:
            raise ApiError("choose two different parameters")
        if len(xs["values"]) * len(ys["values"]) > 200:
            raise ApiError("keep the grid to 200 combinations or fewer")
        split = _date(payload["split"]) if payload.get("split") else None
        job = self.jobs.submit("optimize", lambda j: self._do_optimize(j, g, params, xs, ys, split))
        return job.snapshot()

    @staticmethod
    def _axis(a: Dict[str, Any]) -> Dict[str, Any]:
        p = a.get("param")
        if p not in ("sl", "tp", "entry"):
            raise ApiError("axis param must be sl, tp or entry")
        if p == "entry":
            t0, t1 = strat._t(a.get("from", "09:20")), strat._t(a.get("to", "10:30"))
            step = max(1, int(a.get("step", 10)))
            vals, cur = [], datetime.combine(date(2000, 1, 1), t0)
            end = datetime.combine(date(2000, 1, 1), t1)
            while cur <= end:
                vals.append(cur.time())
                cur += timedelta(minutes=step)
            return {"param": p, "values": vals, "labels": [v.strftime("%H:%M") for v in vals]}
        lo, hi, step = float(a.get("from", 10)), float(a.get("to", 50)), float(a.get("step", 10))
        if step <= 0:
            raise ApiError("step must be positive")
        vals = [float(v) for v in np.round(np.arange(lo, hi + 1e-9, step), 4)]
        return {"param": p, "values": vals, "labels": [f"{v:g}%" if v > 0 else "off" for v in vals]}

    @staticmethod
    def _variant(cfg, param: str, value):
        if param == "entry":
            return dataclasses.replace(cfg, entry_time=value)
        legs = []
        for leg in cfg.legs:
            if param == "sl":
                if value > 0:
                    leg = dataclasses.replace(leg, stop_loss=RiskRule.percent(float(value)))
                else:
                    leg = dataclasses.replace(leg, stop_loss=None, trail_sl=None, reentry=ReEntryRule())
            else:
                leg = dataclasses.replace(leg, target=RiskRule.percent(float(value)) if value > 0 else None)
            legs.append(leg)
        return dataclasses.replace(cfg, legs=tuple(legs))

    def _do_optimize(self, job: Job, g, params, xs, ys, split: Optional[date]) -> Dict[str, Any]:
        job.update(0.02, "Waiting for the data layer")
        with self._store_lock:
            return self._do_optimize_locked(job, g, params, xs, ys, split)

    def _do_optimize_locked(self, job: Job, g, params, xs, ys, split: Optional[date]) -> Dict[str, Any]:
        job.update(0.02, "Opening data")
        store = self.provider.store(g["start"], g["end"], int(g["strike_interval"]))
        base, registry = strat.build_config(params, g), strat.build_registry(g)
        store.set_window(*strat.strike_window(params),
                         carry_forward=(base.holding_mode.value != "INTRADAY"
                                        or strat.needs_multiple_expiries(params)))
        n = len(xs["values"]) * len(ys["values"])
        cells, k, t0 = [], 0, time_module.perf_counter()
        for yi, yv in enumerate(ys["values"]):
            for xi, xv in enumerate(xs["values"]):
                k += 1
                job.update(0.03 + 0.95 * k / n, f"Combination {k} of {n}")
                try:
                    cfg = self._variant(self._variant(base, xs["param"], xv), ys["param"], yv)
                    res = run_backtest(cfg, store, registry)
                except Exception as exc:
                    cells.append({"xi": xi, "yi": yi, "error": str(exc)})
                    continue
                m, dly = res.metrics, res.daily
                cell = {"xi": xi, "yi": yi, "net": m.get("total_net_pnl", 0.0), "pf": m.get("profit_factor"),
                        "win": m.get("win_rate_pct"), "dd": m.get("max_drawdown"), "sharpe": m.get("sharpe"),
                        "days": m.get("total_trades", 0)}
                if split is not None and not dly.empty:
                    oos = float(dly[[d >= split for d in dly["date"]]]["net_pnl"].sum())
                    cell["oos"], cell["ins"] = oos, float(dly["net_pnl"].sum()) - oos
                cells.append(cell)
        return clean({"x": {"param": xs["param"], "labels": xs["labels"]}, "y": {"param": ys["param"], "labels": ys["labels"]},
                      "cells": cells, "split": split, "seconds": time_module.perf_counter() - t0})

    # ------------------------------------------------------------------ repair
    def start_repair(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not hasattr(self.provider, "repair"):
            raise ApiError("this data source does not support repairs", 501)
        try:
            args = (_date(payload["expiry"]), str(payload["type"]).upper(), float(payload["strike"]),
                    _date(payload["from"]), _date(payload["to"]))
        except KeyError as exc:
            raise ApiError(f"missing field {exc}") from None
        if args[1] not in ("CE", "PE"):
            raise ApiError("type must be CE or PE")
        job = self.jobs.submit("repair", lambda j: clean(self.provider.repair(*args)))
        return job.snapshot()
