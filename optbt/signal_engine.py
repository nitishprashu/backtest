"""Simulation for indicator-driven strategies (Supertrend / VWAP / EMA / MACD).

Flow per day
------------
1. Pick the expiry; compute `sig`/`valid` per minute (signals.py, no look-ahead).
2. Signal events = minutes where the state changes to a non-zero value.
3. Walk events in order. Each entry fills at the OPEN of the event minute
   (the signal came from bars closed before it). The open trade's SL / TP /
   trailing exit is found with the same vectorised scan the time-based engine uses.
4. An opposite signal closes the trade at that minute's open (REVERSE also
   opens the new side there; EXIT waits; IGNORE holds).
5. Optional daily loss / target: judged on bar-close MTM of day P&L, exits at
   the next open, and stops trading for the day.

Tie rules: an opposite signal at minute j beats an SL/TP/EOD hit inside bar j
(it happens at the open); a daily-limit exit at j beats the signal.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .analytics import build_result, trade_level_metrics
from .costs import leg_costs
from .data import C, N_BARS, O, OptionKey, bar_index
from .greeks import greeks_at
from .runner import run_days, select_dates
from .engine import (_FIELD, _STOP_REASONS, _bar_dt, _scan_exit, _vol, resolve_strike, select_expiry,
                     slippage_model)
from .instruments import DEFAULT_REGISTRY, InstrumentRegistry, InstrumentSpec
from .schemas import (TRADE_COLUMNS, BacktestResult, ExitReason, HoldingMode, LegConfig, OptionType,
                      RolloverStrikeMode, Side, StrikeSelection)
from .signals import (EMASource, EMAParams, MACDParams, OptionEMASignalData, OppositeSignal, OptionExpression,
                      SignalStrategyConfig, SupertrendParams,
                      VwapParams, VwapSource, compute_signals, compute_signals_for_days,
                      indicator_timeframe)

_LIMIT_REASONS = (ExitReason.PORTFOLIO_SL, ExitReason.PORTFOLIO_TP)


def _require_roll_key(tl, expiry_day, expiry, key, expiry_mode, selection, spec, greek):
    from .carry import _next_roll_expiry, _next_roll_key

    next_expiry = _next_roll_expiry(tl, expiry_day, expiry, expiry_mode)
    roll_key = _next_roll_key(tl, expiry_day, expiry, key, expiry_mode,
                              selection=selection, spec=spec, greek=greek)
    if roll_key is None:
        roll_date = tl.dates[expiry_day]
        if next_expiry is None:
            missing = "no later expiry is present in the loaded data"
        else:
            missing = f"no usable {key[2]} contract for expiry {next_expiry.isoformat()} is available"
        raise RuntimeError(
            f"Cannot roll carried {key[2]} {key[1]} contract expiring {expiry.isoformat()} "
            f"on {roll_date.isoformat()}: {missing}; repair or extend option data and rerun"
        )
    return roll_key


def _expression(expr: OptionExpression, direction: int) -> Tuple[Side, OptionType]:
    if expr is OptionExpression.LONG_PREMIUM:
        return (Side.BUY, OptionType.CE) if direction > 0 else (Side.BUY, OptionType.PE)
    return (Side.SELL, OptionType.PE) if direction > 0 else (Side.SELL, OptionType.CE)


def _option_ema_side(cfg: SignalStrategyConfig, direction: int) -> Side:
    follows_signal = cfg.expression is OptionExpression.LONG_PREMIUM
    return Side.BUY if (direction > 0) == follows_signal else Side.SELL


def _execution_leg(cfg: SignalStrategyConfig, direction: int):
    leg = cfg.execution_leg
    if leg is not None:
        if (isinstance(cfg.indicator, MACDParams)
                or isinstance(cfg.indicator, EMAParams)
                and cfg.indicator.source is EMASource.UNDERLYING):
            expression = (OptionExpression.LONG_PREMIUM if leg.side is Side.BUY
                          else OptionExpression.SHORT_PREMIUM)
            side, option_type = _expression(expression, direction)
        else:
            side, option_type = leg.side, leg.option_type
        return side, option_type, leg.strike, leg.lots, leg.stop_loss, leg.target, leg.trail_sl
    side, option_type = _expression(cfg.expression, direction)
    return side, option_type, cfg.strike, cfg.lots, cfg.stop_loss, cfg.target, cfg.trail_sl


def _build_events_with_prior(sig: np.ndarray, valid: np.ndarray, start_idx: int, last_idx: int, xi: int,
                             initial_trend: bool, prior_state=None) -> List[Tuple[int, int]]:
    lo = max(start_idx, 1)
    chg = (valid[lo:xi] & valid[lo - 1:xi - 1] & (sig[lo:xi] != sig[lo - 1:xi - 1]) & (sig[lo:xi] != 0))
    events = [(int(j) + lo, int(sig[int(j) + lo])) for j in np.nonzero(chg)[0]]
    if prior_state is not None and start_idx < xi:
        previous_signal, previous_valid = prior_state
        current_signal = int(sig[start_idx])
        if previous_valid and valid[start_idx] and current_signal != 0 and current_signal != previous_signal:
            events.append((start_idx, current_signal))
            events.sort()
    if initial_trend:
        cand = np.nonzero(valid[start_idx:last_idx + 1] & (sig[start_idx:last_idx + 1] != 0))[0]
        if cand.size:
            j0 = start_idx + int(cand[0])
            if all(e[0] != j0 for e in events):
                events.append((j0, int(sig[j0])))
                events.sort()
    return events


def _build_events(sig: np.ndarray, valid: np.ndarray, start_idx: int, last_idx: int, xi: int,
                  initial_trend: bool) -> List[Tuple[int, int]]:
    return _build_events_with_prior(sig, valid, start_idx, last_idx, xi, initial_trend)


def _simulate_signal_day(day, cfg: SignalStrategyConfig, spec: InstrumentSpec,
                         signal_data=None, prior_state=None) -> Tuple[List[dict], Optional[str]]:
    start_idx = bar_index(cfg.start_time)
    last_idx = bar_index(cfg.last_entry_time)
    xi = min(bar_index(cfg.exit_time), N_BARS - 1)
    if start_idx < 0 or last_idx >= xi:
        return [], "times outside session"
    expiry = select_expiry(day, cfg.expiry)
    if expiry is None:
        return [], f"no {cfg.expiry.value} expiry in data"
    dte = (expiry - day.trade_date).days
    if (cfg.dte_min is not None and dte < cfg.dte_min) or (cfg.dte_max is not None and dte > cfg.dte_max):
        return [], f"filter: {dte} days to expiry is outside {cfg.dte_min}..{cfg.dte_max}"
    sig, valid, locked, err = (signal_data if signal_data is not None else
                               compute_signals(day, cfg, spec, expiry, start_idx, resolve_strike))
    if err:
        return [], err
    if sig is None or valid is None:
        raise RuntimeError("indicator generation returned no signal arrays without an error")
    events = _build_events_with_prior(sig, valid, start_idx, last_idx, xi,
                                      cfg.trade_initial_trend, prior_state)

    sm = slippage_model(cfg)
    trade_lots = cfg.execution_leg.lots if cfg.execution_leg else cfg.lots
    qty = trade_lots * spec.lot_size_for(expiry)
    xf = _FIELD[cfg.exit_price_field]
    has_limits = bool(cfg.daily_max_loss or cfg.daily_target)

    rows: List[dict] = []
    k, n_trades, day_gross, stopped = 0, 0, 0.0, False
    while k < len(events) and not stopped and n_trades < cfg.max_trades_per_day:
        j, d = events[k]
        if j > last_idx:
            break
        side, otype, strike, lots, stop_loss, target, trail_sl = _execution_leg(cfg, d)
        key = (locked[otype.value] if locked and cfg.execution_leg is None else
               resolve_strike(strike, otype, day, expiry, j, spec, O, cfg.greek))
        if key is None:
            k += 1
            continue
        arr = day.options.get(key)
        if arr is None:
            k += 1
            continue
        entry_raw = float(arr[O, j])
        if not np.isfinite(entry_raw) or entry_raw <= 0:
            k += 1
            continue
        s = 1 if side is Side.BUY else -1
        leg = LegConfig(side, otype, strike, lots, stop_loss=stop_loss, target=target,
                trail_sl=trail_sl)
        x_idx, x_raw, reason = _scan_exit(arr, s, j, xi, entry_raw, leg, xf)
        ent_eff = sm.fill(entry_raw, s == 1, _vol(arr, j))

        limit_hit = False
        if has_limits:
            seg = arr[C, j:x_idx]
            if seg.size:
                m = day_gross + s * (seg - ent_eff) * qty
                hit_sl = (m <= -cfg.daily_max_loss) if cfg.daily_max_loss else np.zeros(m.size, bool)
                hit_tp = (m >= cfg.daily_target) if cfg.daily_target else np.zeros(m.size, bool)
                i_sl = int(hit_sl.argmax()) if hit_sl.any() else m.size
                i_tp = int(hit_tp.argmax()) if hit_tp.any() else m.size
                if min(i_sl, i_tp) < m.size:
                    t = j + min(i_sl, i_tp)
                    x_idx, x_raw = t + 1, float(arr[O, t + 1])
                    reason = ExitReason.PORTFOLIO_SL if i_sl <= i_tp else ExitReason.PORTFOLIO_TP
                    limit_hit = True

        trig = None
        if cfg.on_opposite is not OppositeSignal.IGNORE:
            for kk in range(k + 1, len(events)):
                if events[kk][0] > x_idx:
                    break
                if events[kk][1] != d:
                    trig = kk
                    break
        took_signal = False
        if trig is not None:
            jo = events[trig][0]
            if jo < x_idx or (jo == x_idx and not limit_hit):
                x_idx, x_raw, reason = jo, float(arr[O, jo]), ExitReason.SIGNAL_EXIT
                limit_hit, took_signal = False, True

        n_trades += 1
        xe = sm.fill(x_raw, s == -1, _vol(arr, x_idx), reason in _STOP_REASONS)
        points = s * (xe - ent_eff)
        gross = points * qty
        costs = 0.0
        if cfg.apply_costs:
            costs = (leg_costs(spec, day.trade_date, ent_eff, qty, s == 1)
                     + leg_costs(spec, day.trade_date, xe, qty, s == -1))
        entry_greek = greeks_at(day, key, j, O, cfg.greek) if cfg.entry_greeks else {}
        rows.append({
            "date": day.trade_date, "strategy": cfg.name,
            "leg_id": f"L{cfg.execution_leg_index + 1}" if cfg.execution_leg else f"{side.value[0]}{otype.value}",
            "signal_direction": "BULLISH" if d > 0 else "BEARISH",
            "segment": n_trades, "expiry": key[0], "strike": key[1], "option_type": key[2],
            "side": side.value, "lots": lots, "qty": qty,
            "entry_time": _bar_dt(day.trade_date, j), "entry_price": round(ent_eff, 4),
            "exit_time": _bar_dt(day.trade_date, x_idx), "exit_price": round(xe, 4),
            "points": round(points, 4), "gross_pnl": round(gross, 2),
            "slippage_cost": round(qty * (abs(ent_eff - entry_raw) + abs(xe - x_raw)), 2),
            "costs": round(costs, 2), "net_pnl": round(gross - costs, 2),
            "exit_reason": reason.value, "day_mae": float("nan"), "day_mfe": float("nan"),
            "entry_iv": entry_greek.get("iv", np.nan), "entry_delta": entry_greek.get("delta", np.nan),
            "entry_gamma": entry_greek.get("gamma", np.nan), "entry_theta": entry_greek.get("theta", np.nan),
            "entry_vega": entry_greek.get("vega", np.nan),
        })
        day_gross += gross
        if limit_hit or (cfg.daily_max_loss and day_gross <= -cfg.daily_max_loss) \
                or (cfg.daily_target and day_gross >= cfg.daily_target):
            stopped = True
        if took_signal and trig is not None:
            k = trig if cfg.on_opposite is OppositeSignal.REVERSE else trig + 1
        else:
            k += 1
            while k < len(events) and events[k][0] <= x_idx:
                k += 1
    return rows, None


def simulate_signal_day(day, cfg: SignalStrategyConfig, spec: InstrumentSpec
                        ) -> Tuple[List[dict], Optional[str]]:
    return _simulate_signal_day(day, cfg, spec)


def _simulate_option_ema_day(day, cfg: SignalStrategyConfig, spec: InstrumentSpec,
                             stream_data: OptionEMASignalData,
                             prior_states: Optional[Dict[str, Tuple[int, bool]]] = None
                             ) -> Tuple[List[dict], Optional[str]]:
    """Simulate independent CE/PE option-EMA positions under shared daily limits."""
    start_idx, last_idx = bar_index(cfg.start_time), bar_index(cfg.last_entry_time)
    xi = min(bar_index(cfg.exit_time), N_BARS - 1)
    if start_idx < 0 or last_idx >= xi:
        return [], "times outside session"
    expiry = select_expiry(day, cfg.expiry)
    if expiry is None:
        return [], f"no {cfg.expiry.value} expiry in data"
    dte = (expiry - day.trade_date).days
    if (cfg.dte_min is not None and dte < cfg.dte_min) or (cfg.dte_max is not None and dte > cfg.dte_max):
        return [], f"filter: {dte} days to expiry is outside {cfg.dte_min}..{cfg.dte_max}"

    events: Dict[int, List[Tuple[str, int]]] = {}
    errors = []
    contracts: Dict[str, OptionKey] = {}
    for typ in ("CE", "PE"):
        if cfg.execution_leg and typ != cfg.execution_leg.option_type.value:
            continue
        sig, valid, locked, error = stream_data[typ]
        if error:
            errors.append(error)
            continue
        if locked is None or typ not in locked:
            errors.append(f"option EMA: {typ} contract unavailable")
            continue
        contracts[typ] = locked[typ]
        prior = prior_states.get(typ) if prior_states else None
        for j, direction in _build_events_with_prior(
                sig, valid, start_idx, last_idx, xi, cfg.trade_initial_trend, prior):
            events.setdefault(j, []).append((typ, direction))

    sm, xf = slippage_model(cfg), _FIELD[cfg.exit_price_field]
    trade_lots = cfg.execution_leg.lots if cfg.execution_leg else cfg.lots
    qty = trade_lots * spec.lot_size_for(expiry)
    rows = []
    active = {}
    trade_count = 0
    day_gross = 0.0
    stopped = False
    pending_limit = None

    def finish(typ, j, raw, reason):
        nonlocal day_gross
        position = active.pop(typ)
        arr = position["arr"]
        side = position["side"]
        sign = 1 if side is Side.BUY else -1
        exit_price = sm.fill(raw, sign == -1, _vol(arr, j), reason in _STOP_REASONS)
        points = sign * (exit_price - position["entry_price"])
        gross = points * qty
        costs = 0.0
        if cfg.apply_costs:
            costs = (leg_costs(spec, day.trade_date, position["entry_price"], qty, sign == 1)
                     + leg_costs(spec, day.trade_date, exit_price, qty, sign == -1))
        rows.append({
            "date": day.trade_date, "strategy": cfg.name,
            "leg_id": f"L{cfg.execution_leg_index + 1}" if cfg.execution_leg else f"{side.value[0]}{typ}",
            "signal_direction": "BULLISH" if position["direction"] > 0 else "BEARISH",
            "segment": position["segment"], "expiry": position["key"][0],
            "strike": position["key"][1], "option_type": typ, "side": side.value,
            "lots": trade_lots, "qty": qty,
            "entry_time": _bar_dt(day.trade_date, position["entry_idx"]),
            "entry_price": round(position["entry_price"], 4),
            "exit_time": _bar_dt(day.trade_date, j), "exit_price": round(exit_price, 4),
            "points": round(points, 4), "gross_pnl": round(gross, 2),
            "slippage_cost": round(qty * (abs(position["entry_eff"] - position["entry_raw"])
                                          + abs(exit_price - raw)), 2),
            "costs": round(costs, 2), "net_pnl": round(gross - costs, 2),
            "exit_reason": reason.value, "day_mae": float("nan"), "day_mfe": float("nan"),
            "entry_iv": position.get("entry_greek", {}).get("iv", np.nan),
            "entry_delta": position.get("entry_greek", {}).get("delta", np.nan),
            "entry_gamma": position.get("entry_greek", {}).get("gamma", np.nan),
            "entry_theta": position.get("entry_greek", {}).get("theta", np.nan),
            "entry_vega": position.get("entry_greek", {}).get("vega", np.nan),
        })
        day_gross += gross

    for j in range(start_idx, xi + 1):
        if pending_limit is not None:
            for typ in list(active):
                arr = active[typ]["arr"]
                finish(typ, j, float(arr[O, j]), pending_limit)
            stopped = True
            pending_limit = None
        if stopped:
            continue

        for typ, direction in events.get(j, []):
            position = active.get(typ)
            if position is not None and direction != position["direction"]:
                if cfg.on_opposite is OppositeSignal.IGNORE:
                    continue
                arr = position["arr"]
                finish(typ, j, float(arr[O, j]), ExitReason.SIGNAL_EXIT)
                if cfg.on_opposite is OppositeSignal.EXIT:
                    continue
            if typ in active or j > last_idx or trade_count >= cfg.max_trades_per_day:
                continue
            key = contracts.get(typ)
            arr = day.options.get(key) if key is not None else None
            if arr is None:
                continue
            entry_raw = float(arr[O, j])
            if not np.isfinite(entry_raw) or entry_raw <= 0:
                continue
            if cfg.execution_leg:
                side, _, strike, lots, stop_loss, target, trail_sl = _execution_leg(cfg, direction)
            else:
                side = _option_ema_side(cfg, direction)
                strike, lots = cfg.strike, cfg.lots
                stop_loss, target, trail_sl = cfg.stop_loss, cfg.target, cfg.trail_sl
            sign = 1 if side is Side.BUY else -1
            leg = LegConfig(side, OptionType(typ), strike, lots,
                            stop_loss=stop_loss, target=target, trail_sl=trail_sl)
            exit_idx, exit_raw, reason = _scan_exit(arr, sign, j, xi, entry_raw, leg, xf)
            entry_eff = sm.fill(entry_raw, sign == 1, _vol(arr, j))
            trade_count += 1
            active[typ] = {
                "arr": arr, "key": key, "side": side, "direction": direction,
                "entry_idx": j, "entry_raw": entry_raw, "entry_eff": entry_eff,
                "entry_price": entry_eff, "exit_idx": exit_idx, "exit_raw": exit_raw,
                "exit_reason": reason, "segment": trade_count,
                "entry_greek": greeks_at(day, key, j, O, cfg.greek) if cfg.entry_greeks else {},
            }

        for typ in list(active):
            position = active[typ]
            if position["exit_idx"] == j:
                finish(typ, j, position["exit_raw"], position["exit_reason"])

        if cfg.daily_max_loss or cfg.daily_target:
            mtm = day_gross
            for position in active.values():
                sign = 1 if position["side"] is Side.BUY else -1
                mtm += sign * (float(position["arr"][C, j]) - position["entry_price"]) * qty
            if cfg.daily_max_loss and mtm <= -cfg.daily_max_loss:
                pending_limit = ExitReason.PORTFOLIO_SL
            elif cfg.daily_target and mtm >= cfg.daily_target:
                pending_limit = ExitReason.PORTFOLIO_TP

    why = "; ".join(errors) if errors else None
    return rows, why


def _timeframe_history_days(store, cfg, selected_days, need_prev, spec):
    if not selected_days:
        return []
    selected_dates = {day.trade_date for day in selected_days}
    history_dates = getattr(store, "history_dates", store.dates)
    history_dates = [d for d in history_dates if d <= max(selected_dates)]
    history: List = []
    premium_vwap = ((isinstance(cfg.indicator, VwapParams)
                     and cfg.indicator.source is VwapSource.PREMIUM)
                    or (isinstance(cfg.indicator, EMAParams)
                        and cfg.indicator.source is EMASource.OPTION))
    for d in history_dates:
        if d in selected_dates:
            day = next(day for day in selected_days if day.trade_date == d)
        elif premium_vwap:
            day, _ = store.build_day(d, cfg.expiry, cfg.max_expiry_gap_days, need_prev)
            if day is None:
                if not hasattr(store, "build_spot_day"):
                    continue
                day = store.build_spot_day(d)
            expiry = day.selected_expiry or select_expiry(day, cfg.expiry)
            if expiry is not None:
                ce = resolve_strike(cfg.strike, OptionType.CE, day, expiry, bar_index(cfg.start_time),
                                    spec, O, cfg.greek)
                pe = resolve_strike(cfg.strike, OptionType.PE, day, expiry, bar_index(cfg.start_time),
                                    spec, O, cfg.greek)
                keys = {key for key in (ce, pe) if key is not None}
                day.options = {key: day.options[key] for key in keys if key in day.options}
        elif hasattr(store, "build_spot_day"):
            day = store.build_spot_day(d)
        else:
            day, _ = store.build_day(d, cfg.expiry, cfg.max_expiry_gap_days, need_prev)
            if day is None:
                continue
        history.append(day)
    return history


def _run_timeframe_intraday(cfg, store, spec, selected_dates, need_prev, progress):
    if not selected_dates:
        result = build_result(cfg, pd.DataFrame(columns=TRADE_COLUMNS), [], universe=[])
        result.metrics.update(trade_level_metrics(result.trades))
        return result
    rows: List[dict] = []
    skipped: List[Tuple[date, str]] = []
    built: List = []
    for i, d in enumerate(selected_dates, 1):
        try:
            day, why = store.build_day(d, cfg.expiry, cfg.max_expiry_gap_days, need_prev)
        except Exception as exc:
            day, why = None, f"error: {type(exc).__name__}: {exc}"
        if day is None:
            skipped.append((d, why or "market data unavailable"))
        else:
            built.append(day)
        if progress is not None and (i % 5 == 0 or i == len(selected_dates)):
            progress(i, len(selected_dates))
    if not built:
        result = build_result(cfg, pd.DataFrame(columns=TRADE_COLUMNS), skipped, universe=selected_dates)
        result.metrics.update(trade_level_metrics(result.trades))
        return result

    history = _timeframe_history_days(store, cfg, built, need_prev, spec)
    signals = compute_signals_for_days(history, cfg, spec, resolve_strike)
    history_by_date = {day.trade_date: index for index, day in enumerate(history)}
    for day in built:
        data = signals[day.trade_date]
        index = history_by_date[day.trade_date]
        if isinstance(cfg.indicator, EMAParams) and cfg.indicator.source is EMASource.OPTION:
            prior: Optional[Dict[str, Tuple[int, bool]]] = None
            if index > 0:
                previous = signals[history[index - 1].trade_date]
                if not isinstance(previous, dict):
                    raise RuntimeError("option EMA history returned a single signal stream")
                prior = {typ: (int(previous[typ][0][-1]), bool(previous[typ][1][-1]))
                         for typ in ("CE", "PE")}
            if not isinstance(data, dict):
                raise RuntimeError("option EMA history returned a single signal stream")
            day_rows, why = _simulate_option_ema_day(day, cfg, spec, data, prior)
        else:
            prior_underlying = None
            if index > 0:
                previous = signals[history[index - 1].trade_date]
                if not isinstance(previous, tuple):
                    raise RuntimeError("underlying indicator history returned multiple signal streams")
                prior_underlying = (int(previous[0][-1]), bool(previous[1][-1]))
            if not isinstance(data, tuple):
                raise RuntimeError("underlying indicator history returned multiple signal streams")
            day_rows, why = _simulate_signal_day(day, cfg, spec, data, prior_underlying)
        if why:
            skipped.append((day.trade_date, why))
        rows.extend(day_rows)
    trades = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    result = build_result(cfg, trades, skipped, universe=selected_dates)
    result.metrics.update(trade_level_metrics(trades))
    return result


def run_signal_backtest(cfg: SignalStrategyConfig, store,
                        registry: Optional[InstrumentRegistry] = None, workers: int = 1,
                        progress=None) -> BacktestResult:
    if cfg.signal_legs:
        results = [run_signal_backtest(
            replace(cfg, signal_legs=(), execution_leg=leg, execution_leg_index=i),
            store, registry, workers, progress)
            for i, leg in enumerate(cfg.signal_legs)]
        trades = pd.concat([result.trades for result in results], ignore_index=True)
        skipped = sorted({item for result in results for item in result.skipped})
        universe = sorted({d for result in results for d in result.daily["date"]})
        combined = build_result(cfg, trades, skipped, universe=universe)
        combined.metrics.update(trade_level_metrics(trades))
        combined.metrics["data_gaps"] = sorted({
            (gap["expiry"], gap["strike"], gap["option_type"], gap["from"], gap["to"],
             tuple(gap["missing_dates"]))
            for result in results for gap in result.metrics.get("data_gaps", [])
        })
        combined.metrics["data_gaps"] = [
            {"expiry": gap[0], "strike": gap[1], "option_type": gap[2],
             "from": gap[3], "to": gap[4], "missing_dates": list(gap[5])}
            for gap in combined.metrics["data_gaps"]
        ]
        return combined
    if cfg.holding_mode is not HoldingMode.INTRADAY:
        return _run_carried_signal_backtest(cfg, store, registry, workers, progress)
    spec = (registry or DEFAULT_REGISTRY).get(cfg.underlying)
    need_prev = isinstance(cfg.indicator, SupertrendParams) and cfg.indicator.use_prev_day
    dates = select_dates(store, cfg)
    timeframe = indicator_timeframe(cfg.indicator)
    if (timeframe != "1m"
            or isinstance(cfg.indicator, EMAParams)
            or isinstance(cfg.indicator, SupertrendParams) and cfg.indicator.use_prev_day):
        return _run_timeframe_intraday(cfg, store, spec, dates, need_prev, progress)
    results = run_days(store, dates, simulate_signal_day, cfg, spec, need_prev=need_prev, workers=workers,
                      progress=progress)
    rows: List[dict] = []
    skipped: List[Tuple[date, str]] = []
    for d, r, why in results:
        if why:
            skipped.append((d, why))
        rows.extend(r)
    trades = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    res = build_result(cfg, trades, skipped, universe=[d for d, _, _ in results])
    res.metrics.update(trade_level_metrics(trades))
    return res


def _run_carried_signal_backtest(cfg: SignalStrategyConfig, store,
                                 registry: Optional[InstrumentRegistry], workers: int,
                                 progress=None) -> BacktestResult:
    from .carry import _horizon, _timeline

    if workers != 1:
        raise ValueError("carry-forward positions must be simulated sequentially with workers=1")
    spec = (registry or DEFAULT_REGISTRY).get(cfg.underlying)
    if isinstance(cfg.indicator, EMAParams) and cfg.indicator.source is EMASource.OPTION:
        return _run_carried_option_ema_backtest(cfg, store, spec, workers, progress)
    need_prev = isinstance(cfg.indicator, SupertrendParams) and cfg.indicator.use_prev_day
    tl = _timeline(store, cfg, need_prev, progress)
    selected_dates = set(select_dates(store, cfg))
    signal_history = tl.days
    signal_data_by_date = {}
    signal_index_by_date = {}
    if (indicator_timeframe(cfg.indicator) != "1m"
            or isinstance(cfg.indicator, EMAParams)
            or isinstance(cfg.indicator, SupertrendParams) and cfg.indicator.use_prev_day):
        signal_history = _timeframe_history_days(store, cfg, tl.days, need_prev, spec)
        signal_data_by_date = compute_signals_for_days(signal_history, cfg, spec, resolve_strike)
        signal_index_by_date = {day.trade_date: i for i, day in enumerate(signal_history)}
    events = []
    skipped = list(tl.skipped)
    prior_session_state = None
    start_idx = bar_index(cfg.start_time)
    last_idx = (N_BARS - 1 if cfg.holding_mode is not HoldingMode.INTRADAY
                else bar_index(cfg.last_entry_time))
    for di, day in enumerate(tl.days):
        expiry = select_expiry(day, cfg.expiry)
        if expiry is None:
            continue
        if signal_data_by_date:
            signal = signal_data_by_date[day.trade_date]
            if not isinstance(signal, tuple):
                raise RuntimeError("underlying indicator history returned multiple signal streams")
            sig, valid, locked, err = signal
        else:
            sig, valid, locked, err = compute_signals(day, cfg, spec, expiry, start_idx, resolve_strike)
        if err:
            skipped.append((day.trade_date, err))
            if not signal_data_by_date:
                prior_session_state = None
            continue
        if sig is None or valid is None:
            raise RuntimeError("indicator generation returned no signal arrays without an error")
        prior = None
        if signal_data_by_date:
            signal_index = signal_index_by_date[day.trade_date]
            if signal_index > 0:
                previous = signal_data_by_date[signal_history[signal_index - 1].trade_date]
                if not isinstance(previous, tuple):
                    raise RuntimeError("underlying indicator history returned multiple signal streams")
                prior = (int(previous[0][-1]), bool(previous[1][-1]))
        else:
            prior = prior_session_state
        event_last_idx = (N_BARS - 1 if cfg.holding_mode is not HoldingMode.INTRADAY else last_idx)
        daily_events = _build_events_with_prior(sig, valid, start_idx, event_last_idx, N_BARS,
                                                cfg.trade_initial_trend, prior)
        for local_idx, direction in daily_events:
            events.append((tl.index(di, local_idx), direction, di, expiry, locked,
                           day.trade_date in selected_dates
                           and (cfg.holding_mode is HoldingMode.INTRADAY
                                or expiry > day.trade_date)))
        if not signal_data_by_date:
            prior_session_state = (int(sig[-1]), bool(valid[-1]))

    sm = slippage_model(cfg)
    xf = _FIELD[cfg.exit_price_field]
    rows = []
    active_until = -1
    active_direction = 0
    counts: dict[date, int] = {}
    exit_only_events = set()
    for i, (j, direction, di, expiry, locked, can_enter) in enumerate(events):
        trade_date = tl.dates[di]
        reverse_at_exit = (cfg.holding_mode is HoldingMode.CARRY_SINGLE and j == active_until
                           and direction != active_direction and cfg.on_opposite is OppositeSignal.REVERSE)
        if not can_enter:
            continue
        if j % N_BARS > last_idx:
            continue
        if cfg.holding_mode is HoldingMode.CARRY_FRESH_ENTRIES and j in exit_only_events:
            continue
        if (cfg.holding_mode is not HoldingMode.CARRY_SINGLE
                and counts.get(trade_date, 0) >= cfg.max_trades_per_day):
            continue
        if cfg.holding_mode is HoldingMode.CARRY_SINGLE and j <= active_until and not reverse_at_exit:
            continue
        side, otype, strike, lots, stop_loss, target, trail_sl = _execution_leg(cfg, direction)
        key = (locked[otype.value] if locked and cfg.execution_leg is None else
               resolve_strike(strike, otype, tl.days[di], expiry, j % N_BARS, spec, O, cfg.greek))
        if key is None:
            continue
        tl.ensure_contract(key, di)
        arr = tl.market.options.get(key)
        if arr is None:
            continue
        if (cfg.holding_mode is not HoldingMode.INTRADAY
                and _horizon(tl, di, expiry, key)[0] <= j):
            continue
        entry_raw = float(arr[O, j])
        if not np.isfinite(entry_raw) or entry_raw <= 0:
            continue
        s = 1 if side is Side.BUY else -1
        current_j, current_expiry, current_key = j, expiry, key
        segment_number = 0
        while True:
            current_day = current_j // N_BARS
            current_date = tl.dates[current_day]
            current_arr = tl.market.options[current_key]
            if current_j == j:
                current_entry_raw = entry_raw
            else:
                current_entry_raw = float(current_arr[C, current_j])
            entry_greek = (greeks_at(tl.days[current_day], current_key, current_j % N_BARS,
                                     O if current_j == j else C, cfg.greek)
                           if cfg.entry_greeks else {})
            if not np.isfinite(current_entry_raw) or current_entry_raw <= 0:
                break
            xi, boundary_reason = _horizon(tl, current_day, current_expiry, current_key)
            leg = LegConfig(side, otype, StrikeSelection.fixed(current_key[1]), lots,
                            stop_loss=stop_loss, target=target, trail_sl=trail_sl)
            ev0 = current_j + 1 if segment_number else current_j
            x_idx, x_raw, reason = _scan_exit(current_arr, s, ev0, xi,
                                              current_entry_raw, leg, xf)
            ent_eff = sm.fill(current_entry_raw, s == 1, _vol(current_arr, current_j))
            qty = lots * spec.lot_size_for(current_expiry)

            limit_hit = False
            if cfg.holding_mode is not HoldingMode.CARRY_SINGLE and (cfg.daily_max_loss or cfg.daily_target):
                first_day = current_j // N_BARS
                last_day = min(x_idx, xi) // N_BARS
                for trade_day in range(first_day, last_day + 1):
                    day_start = max(current_j, trade_day * N_BARS)
                    day_end = min(x_idx, (trade_day + 1) * N_BARS - 1)
                    if day_start >= day_end:
                        continue
                    reference = ent_eff if trade_day == first_day else float(current_arr[C, day_start - 1])
                    m = s * (current_arr[C, day_start:day_end] - reference) * qty
                    hit_sl = (m <= -cfg.daily_max_loss) if cfg.daily_max_loss else np.zeros(m.size, bool)
                    hit_tp = (m >= cfg.daily_target) if cfg.daily_target else np.zeros(m.size, bool)
                    i_sl = int(hit_sl.argmax()) if hit_sl.any() else m.size
                    i_tp = int(hit_tp.argmax()) if hit_tp.any() else m.size
                    if min(i_sl, i_tp) < m.size:
                        t = day_start + min(i_sl, i_tp)
                        x_idx = min(t + 1, xi)
                        x_raw = float(current_arr[O, x_idx]) if x_idx > t else float(current_arr[C, t])
                        reason = ExitReason.PORTFOLIO_SL if i_sl <= i_tp else ExitReason.PORTFOLIO_TP
                        limit_hit = True
                        break

            opposite = None
            if cfg.on_opposite is not OppositeSignal.IGNORE:
                opposite = next((event for event in events[i + 1:]
                                 if event[0] > current_j and event[1] != direction and event[0] <= xi), None)
            took_signal = opposite is not None and (
                opposite[0] < x_idx or (opposite[0] == x_idx and not limit_hit))
            if took_signal and opposite is not None:
                x_idx = opposite[0]
                x_raw = float(current_arr[O, x_idx])
                reason = ExitReason.SIGNAL_EXIT
                if cfg.on_opposite is OppositeSignal.EXIT:
                    exit_only_events.add(x_idx)

            roll_key = None
            if (cfg.holding_mode is not HoldingMode.INTRADAY
                    and boundary_reason is ExitReason.EXPIRY
                    and reason is ExitReason.EOD_SQUARE_OFF and x_idx == xi):
                roll_key = _require_roll_key(
                    tl, xi // N_BARS, current_expiry, current_key, cfg.expiry,
                    selection=(strike if cfg.rollover_strike_mode is RolloverStrikeMode.RESELECT else None),
                    spec=spec, greek=cfg.greek)
            if roll_key is not None:
                reason = ExitReason.ROLLED

            xe = sm.fill(x_raw, s == -1, _vol(current_arr, x_idx), reason in _STOP_REASONS)
            points = s * (xe - ent_eff)
            gross = points * qty
            exit_date = tl.timestamp(x_idx).date()
            costs = 0.0
            if cfg.apply_costs:
                costs = (leg_costs(spec, current_date, ent_eff, qty, s == 1)
                         + leg_costs(spec, exit_date, xe, qty, s == -1))
            path = s * (current_arr[C, current_j:x_idx + 1] - ent_eff) * qty
            rows.append({
                "date": exit_date, "strategy": cfg.name,
                "leg_id": f"L{cfg.execution_leg_index + 1}" if cfg.execution_leg else f"{side.value[0]}{otype.value}",
                "signal_direction": "BULLISH" if direction > 0 else "BEARISH",
                "segment": counts.get(trade_date, 0) + segment_number + 1, "expiry": current_key[0],
                "strike": current_key[1], "option_type": current_key[2], "side": side.value,
                "lots": lots, "qty": qty,
                "entry_time": tl.timestamp(current_j), "entry_price": round(ent_eff, 4),
                "exit_time": tl.timestamp(x_idx), "exit_price": round(xe, 4),
                "points": round(points, 4), "gross_pnl": round(gross, 2),
                "slippage_cost": round(qty * (abs(ent_eff - current_entry_raw) + abs(xe - x_raw)), 2),
                "costs": round(costs, 2), "net_pnl": round(gross - costs, 2),
                "exit_reason": (boundary_reason.value if reason is ExitReason.EOD_SQUARE_OFF else reason.value),
                "day_mae": round(float(min(path.min(), 0.0)), 2) if path.size else 0.0,
                "day_mfe": round(float(max(path.max(), 0.0)), 2) if path.size else 0.0,
                "entry_iv": entry_greek.get("iv", np.nan), "entry_delta": entry_greek.get("delta", np.nan),
                "entry_gamma": entry_greek.get("gamma", np.nan), "entry_theta": entry_greek.get("theta", np.nan),
                "entry_vega": entry_greek.get("vega", np.nan),
            })
            segment_number += 1
            if roll_key is None:
                j = x_idx
                break
            current_j, current_expiry, current_key = xi, roll_key[0], roll_key
        counts[trade_date] = counts.get(trade_date, 0) + 1
        if cfg.holding_mode is HoldingMode.CARRY_SINGLE:
            active_until, active_direction = j, direction

    trades = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    result = build_result(cfg, trades, skipped, universe=sorted(selected_dates))
    result.metrics.update(trade_level_metrics(trades))
    result.metrics["data_gaps"] = tl.data_gap_report()
    return result


def _run_carried_option_ema_backtest(cfg, store, spec, workers, progress):
    from .carry import _horizon, _timeline

    if workers != 1:
        raise ValueError("carry-forward positions must be simulated sequentially with workers=1")
    need_prev = False
    tl = _timeline(store, cfg, need_prev, progress)
    selected_dates = set(select_dates(store, cfg))
    history = _timeframe_history_days(store, cfg, tl.days, need_prev, spec)
    signal_data = compute_signals_for_days(history, cfg, spec, resolve_strike)
    history_index = {day.trade_date: i for i, day in enumerate(history)}
    events: Dict[int, List[Tuple[str, int, int, Optional[date], Optional[OptionKey], bool]]] = {}
    skipped = list(tl.skipped)
    start_idx = bar_index(cfg.start_time)
    last_idx = (N_BARS - 1 if cfg.holding_mode is not HoldingMode.INTRADAY
                else bar_index(cfg.last_entry_time))

    for di, day in enumerate(tl.days):
        signal_streams = signal_data.get(day.trade_date)
        if signal_streams is None:
            continue
        if not isinstance(signal_streams, dict):
            raise RuntimeError("option EMA calculation returned a single signal stream")
        stream_errors = []
        for typ in ("CE", "PE"):
            error = signal_streams[typ][3]
            if error is not None:
                stream_errors.append(error)
        if stream_errors:
            skipped.append((day.trade_date, "; ".join(stream_errors)))
        prev = None
        hi = history_index[day.trade_date]
        if hi > 0:
            prev_streams = signal_data[history[hi - 1].trade_date]
            if not isinstance(prev_streams, dict):
                raise RuntimeError("option EMA history returned a single signal stream")
            prev = {typ: (int(prev_streams[typ][0][-1]), bool(prev_streams[typ][1][-1]))
                    for typ in ("CE", "PE")}
        expiry = select_expiry(day, cfg.expiry)
        if expiry is None:
            continue
        for typ in ("CE", "PE"):
            if cfg.execution_leg and typ != cfg.execution_leg.option_type.value:
                continue
            sig, valid, locked, error = signal_streams[typ]
            if error:
                continue
            prior = prev.get(typ) if prev else None
            for local_idx, direction in _build_events_with_prior(
                    sig, valid, start_idx, last_idx, N_BARS, cfg.trade_initial_trend, prior):
                key = locked[typ] if locked else None
                can_enter = (day.trade_date in selected_dates and local_idx <= last_idx
                             and expiry > day.trade_date)
                events.setdefault(tl.index(di, local_idx), []).append(
                    (typ, direction, di, expiry, key, can_enter))

    sm, xf = slippage_model(cfg), _FIELD[cfg.exit_price_field]
    trade_lots = cfg.execution_leg.lots if cfg.execution_leg else cfg.lots
    active = {}
    rows = []
    counts: Dict[date, int] = {}
    day_realized = 0.0
    current_date = None
    pending_limit = None
    stopped_dates = set()
    def close_position(typ, j, raw, reason):
        nonlocal day_realized
        position = active.pop(typ)
        arr = position["arr"]
        sign = position["sign"]
        exit_eff = sm.fill(raw, sign == -1, _vol(arr, j), reason in _STOP_REASONS)
        points = sign * (exit_eff - position["entry_eff"])
        gross = points * position["qty"]
        daily_change = sign * (exit_eff - position["daily_reference"]) * position["qty"]
        day_realized += daily_change
        exit_date = tl.timestamp(j).date()
        costs = 0.0
        if cfg.apply_costs:
            costs = (leg_costs(spec, position["entry_date"], position["entry_eff"],
                               position["qty"], sign == 1)
                     + leg_costs(spec, exit_date, exit_eff, position["qty"], sign == -1))
        path = sign * (arr[C, position["entry_idx"]:j + 1] - position["entry_eff"]) * position["qty"]
        rows.append({
            "date": exit_date, "strategy": cfg.name,
            "leg_id": f"L{cfg.execution_leg_index + 1}" if cfg.execution_leg else f"{position['side'].value[0]}{typ}",
            "signal_direction": "BULLISH" if position["direction"] > 0 else "BEARISH",
            "segment": position["segment"], "expiry": position["key"][0],
            "strike": position["key"][1], "option_type": typ,
            "side": position["side"].value, "lots": trade_lots, "qty": position["qty"],
            "entry_time": tl.timestamp(position["entry_idx"]),
            "entry_price": round(position["entry_eff"], 4),
            "exit_time": tl.timestamp(j), "exit_price": round(exit_eff, 4),
            "points": round(points, 4), "gross_pnl": round(gross, 2),
            "slippage_cost": round(position["qty"] * (
                abs(position["entry_eff"] - position["entry_raw"]) + abs(exit_eff - raw)), 2),
            "costs": round(costs, 2), "net_pnl": round(gross - costs, 2),
            "exit_reason": (position["boundary_reason"].value
                            if reason is ExitReason.EOD_SQUARE_OFF else reason.value),
            "day_mae": round(float(min(path.min(), 0.0)), 2) if path.size else 0.0,
            "day_mfe": round(float(max(path.max(), 0.0)), 2) if path.size else 0.0,
            "entry_iv": position["entry_greek"].get("iv", np.nan),
            "entry_delta": position["entry_greek"].get("delta", np.nan),
            "entry_gamma": position["entry_greek"].get("gamma", np.nan),
            "entry_theta": position["entry_greek"].get("theta", np.nan),
            "entry_vega": position["entry_greek"].get("vega", np.nan),
        })

    total = len(tl.days) * N_BARS
    for j in range(total):
        trade_date = tl.timestamp(j).date()
        if trade_date != current_date:
            current_date, day_realized = trade_date, 0.0
            prior_idx = j - 1
            for position in active.values():
                arr = position["arr"]
                mark = float(arr[C, prior_idx]) if prior_idx >= 0 else float("nan")
                position["daily_reference"] = (mark if np.isfinite(mark)
                                               else position["entry_eff"])
        if pending_limit is not None:
            for typ in list(active):
                arr = active[typ]["arr"]
                close_position(typ, j, float(arr[O, j]), pending_limit)
            pending_limit = None

        for typ, direction, di, expiry, locked_key, can_enter in events.get(j, []):
            if not can_enter:
                continue
            if expiry is None:
                continue
            position = active.get(typ)
            if position is not None and direction != position["direction"]:
                if cfg.on_opposite is OppositeSignal.IGNORE:
                    continue
                arr = position["arr"]
                close_position(typ, j, float(arr[O, j]), ExitReason.SIGNAL_EXIT)
                if cfg.on_opposite is OppositeSignal.EXIT:
                    continue
            if typ in active or not can_enter or trade_date in stopped_dates:
                continue
            if counts.get(trade_date, 0) >= cfg.max_trades_per_day:
                continue
            day = tl.days[di]
            strike = cfg.execution_leg.strike if cfg.execution_leg else cfg.strike
            key = (locked_key if locked_key is not None and cfg.execution_leg is None else
                   resolve_strike(strike, OptionType(typ), day, expiry, j % N_BARS, spec, O, cfg.greek))
            if key is None:
                continue
            tl.ensure_contract(key, di)
            arr = tl.market.options.get(key)
            if arr is None:
                continue
            if _horizon(tl, di, expiry, key)[0] <= j:
                continue
            entry_raw = float(arr[O, j])
            if not np.isfinite(entry_raw) or entry_raw <= 0:
                continue
            if cfg.execution_leg:
                side = cfg.execution_leg.side
                lots = cfg.execution_leg.lots
                stop_loss, target, trail_sl = (cfg.execution_leg.stop_loss,
                                               cfg.execution_leg.target,
                                               cfg.execution_leg.trail_sl)
            else:
                side = _option_ema_side(cfg, direction)
                lots = cfg.lots
                stop_loss, target, trail_sl = cfg.stop_loss, cfg.target, cfg.trail_sl
            sign = 1 if side is Side.BUY else -1
            leg = LegConfig(side, OptionType(typ), strike, lots,
                            stop_loss=stop_loss, target=target, trail_sl=trail_sl)
            horizon, boundary_reason = _horizon(tl, di, expiry, key)
            exit_idx, exit_raw, reason = _scan_exit(arr, sign, j, horizon, entry_raw, leg, xf)
            entry_eff = sm.fill(entry_raw, sign == 1, _vol(arr, j))
            entry_greek = greeks_at(day, key, j % N_BARS, O, cfg.greek) if cfg.entry_greeks else {}
            counts[trade_date] = counts.get(trade_date, 0) + 1
            active[typ] = {
                "arr": arr, "key": key, "side": side, "sign": sign,
                "direction": direction, "entry_idx": j, "entry_date": trade_date,
                "entry_raw": entry_raw, "entry_eff": entry_eff, "exit_idx": exit_idx,
                "exit_raw": exit_raw, "exit_reason": reason,
                "boundary_reason": boundary_reason,
                "daily_reference": entry_eff, "qty": lots * spec.lot_size_for(expiry),
                "segment": counts[trade_date],
                "entry_greek": entry_greek,
            }

        for typ in list(active):
            position = active[typ]
            if position["exit_idx"] != j:
                continue
            if (cfg.holding_mode is HoldingMode.INTRADAY
                    or position["exit_reason"] is not ExitReason.EOD_SQUARE_OFF
                    or position["boundary_reason"] is not ExitReason.EXPIRY):
                close_position(typ, j, position["exit_raw"], position["exit_reason"])
                continue

            roll_selection = cfg.execution_leg.strike if cfg.execution_leg else cfg.strike
            roll_key = _require_roll_key(
                tl, j // N_BARS, position["key"][0], position["key"], cfg.expiry,
                selection=(roll_selection
                           if cfg.rollover_strike_mode is RolloverStrikeMode.RESELECT else None),
                spec=spec, greek=cfg.greek)
            roll_arr = tl.market.options[roll_key]
            entry_raw = float(roll_arr[C, j])
            close_position(typ, j, position["exit_raw"], ExitReason.ROLLED)
            entry_eff = sm.fill(entry_raw, position["sign"] == 1, _vol(roll_arr, j))
            qty = trade_lots * spec.lot_size_for(roll_key[0])
            strike = cfg.execution_leg.strike if cfg.execution_leg else cfg.strike
            stop_loss = cfg.execution_leg.stop_loss if cfg.execution_leg else cfg.stop_loss
            target = cfg.execution_leg.target if cfg.execution_leg else cfg.target
            trail_sl = cfg.execution_leg.trail_sl if cfg.execution_leg else cfg.trail_sl
            leg = LegConfig(position["side"], OptionType(typ), strike, trade_lots,
                            stop_loss=stop_loss, target=target, trail_sl=trail_sl)
            horizon, boundary_reason = _horizon(tl, j // N_BARS, roll_key[0], roll_key)
            exit_idx, exit_raw, reason = _scan_exit(
                roll_arr, position["sign"], j + 1, horizon, entry_raw, leg, xf)
            active[typ] = {
                "arr": roll_arr, "key": roll_key, "side": position["side"],
                "sign": position["sign"], "direction": position["direction"],
                "entry_idx": j, "entry_date": trade_date, "entry_raw": entry_raw,
                "entry_eff": entry_eff, "exit_idx": exit_idx, "exit_raw": exit_raw,
                "exit_reason": reason, "boundary_reason": boundary_reason,
                "daily_reference": entry_eff, "qty": qty,
                "segment": position["segment"] + 1,
                "entry_greek": greeks_at(tl.days[j // N_BARS], roll_key, j % N_BARS, C, cfg.greek)
                if cfg.entry_greeks else {},
            }

        if cfg.daily_max_loss or cfg.daily_target:
            mtm = day_realized
            for position in active.values():
                mark = float(position["arr"][C, j])
                if np.isfinite(mark):
                    mtm += position["sign"] * (mark - position["daily_reference"]) * position["qty"]
            if cfg.daily_max_loss and mtm <= -cfg.daily_max_loss:
                pending_limit = ExitReason.PORTFOLIO_SL
                stopped_dates.add(trade_date)
            elif cfg.daily_target and mtm >= cfg.daily_target:
                pending_limit = ExitReason.PORTFOLIO_TP
                stopped_dates.add(trade_date)

    trades = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    result = build_result(cfg, trades, skipped, universe=sorted(selected_dates))
    result.metrics.update(trade_level_metrics(trades))
    result.metrics["data_gaps"] = tl.data_gap_report()
    return result
