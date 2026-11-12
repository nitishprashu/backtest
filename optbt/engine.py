"""Execution engine.

Order of operations for one trading day
---------------------------------------
1. Pick the expiry, resolve every leg's strike at the entry candle from spot.
2. Each leg is simulated independently over the 1-minute bars with vectorised
   NumPy scans (no per-bar Python loop): trailing SL levels are a function of
   the running favourable excursion up to the *previous* bar, SL/TP hits are
   first-True lookups on boolean arrays.
3. SL exits can spawn re-entry segments (RE_EXECUTE / RE_COST).
4. A portfolio mark-to-market series is built from all leg segments; the first
   bar whose close breaches portfolio SL / TP / trailing SL closes everything
   still open at the NEXT bar's open (decision at close, fill one bar later).
5. Whatever is still open is squared off at the exit time.

Conventions
-----------
* Entry fills at the entry candle's ``entry_price_field`` (default: open). With
  "open" the entry candle's own high/low are already in play for SL/TP; with
  "close" evaluation starts on the next bar.
* If SL and TP are both touched inside one bar, SL wins (conservative).
* SL fills at the trigger level, or at the bar open when the bar gaps through
  it. TP fills at the level, or at the open when the bar gaps past it.
* Slippage (percent) is applied adversely to every fill.
* Portfolio MTM uses bar closes and excludes statutory costs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .analytics import build_result
from .costs import leg_costs
from .data import C, H, L, N_BARS, O, V, DataStore, DayData, OptionKey, bar_index
from .conditions import find_entry_bar
from .expiry import choose_expiry
from .greeks import GreekSettings, chain_greeks_at, greeks_at
from .runner import run_days, select_dates
from .instruments import DEFAULT_REGISTRY, InstrumentRegistry, InstrumentSpec
from .schemas import (ATMReference, TRADE_COLUMNS, BacktestResult, ExitReason, ExpirySelect, LegConfig,
                      ReEntryMode, Side, SlippageModel, StrategyConfig, StrikeMode, StrikeSelection,
                      OptionType)

_FIELD = {"open": O, "close": C}


@dataclass
class Segment:
    leg_index: int
    leg_id: str
    seg_no: int
    key: OptionKey
    s: int                # +1 long, -1 short
    qty: int
    entry_idx: int
    entry_raw: float
    exit_idx: int
    exit_raw: float
    reason: ExitReason


# --------------------------------------------------------------------------- #
# selection helpers
# --------------------------------------------------------------------------- #
def select_expiry(day: DayData, mode: ExpirySelect) -> Optional[date]:
    """The expiry the store selected for this day (stores apply the data-completeness guard).
    Hand-built DayData without a selection falls back to a guard-free choice."""
    if day.selected_expiry is not None:
        return day.selected_expiry
    return choose_expiry(day.expiries, day.trade_date, mode, None)[0]


def select_leg_expiry(day: DayData, base_expiry: date, offset: int) -> Optional[date]:
    """Select an expiry relative to the strategy expiry (0 = strategy, 1 = next listed)."""
    expiries = sorted(e for e in day.expiries if e >= base_expiry)
    if base_expiry not in expiries:
        return None
    index = expiries.index(base_expiry) + offset
    return expiries[index] if index < len(expiries) else None


def resolve_strike(sel: StrikeSelection, otype: OptionType, day: DayData, expiry: date,
                   bar: int, spec: InstrumentSpec, fidx: int,
                   greek: Optional[GreekSettings] = None) -> Optional[OptionKey]:
    ot = otype.value
    spot = day.spot[fidx, bar]
    if not np.isfinite(spot):
        return None

    if sel.mode is StrikeMode.ATM_OFFSET:
        atm = int(math.floor(spot / spec.strike_step + 0.5)) * spec.strike_step
        if sel.atm_reference is ATMReference.SYNTHETIC_FUTURE:
            ce = day.options.get((expiry, atm, OptionType.CE.value))
            pe = day.options.get((expiry, atm, OptionType.PE.value))
            if ce is None or pe is None:
                return None
            call_price, put_price = ce[fidx, bar], pe[fidx, bar]
            if not (np.isfinite(call_price) and call_price > 0
                    and np.isfinite(put_price) and put_price > 0):
                return None
            synthetic_future = atm + call_price - put_price
            atm = int(math.floor(synthetic_future / spec.strike_step + 0.5)) * spec.strike_step
        key = (expiry, atm + sel.offset * spec.strike_step, ot)
        arr = day.options.get(key)
        if arr is None:
            return None
        p = arr[fidx, bar]
        return key if np.isfinite(p) and p > 0 else None

    if sel.mode is StrikeMode.GREEK:
        chain = chain_greeks_at(day, expiry, ot, bar, fidx, greek or GreekSettings())
        if not chain["strike"].size:
            return None
        values = chain[sel.greek_metric]
        if sel.greek_metric in ("delta", "theta"):
            values = np.abs(values)
        valid = np.isfinite(values)
        if sel.greek_min is not None:
            valid &= values >= sel.greek_min
        if sel.greek_max is not None:
            valid &= values <= sel.greek_max
        if not valid.any():
            return None
        distance = np.where(valid, np.abs(values - sel.greek_target), np.inf)
        order = np.lexsort((np.abs(chain["strike"] - spot), distance))
        return (expiry, int(round(chain["strike"][order[0]])), ot)

    if sel.mode is StrikeMode.FIXED:
        key = (expiry, int(round(sel.fixed_strike)), ot)
        arr = day.options.get(key)
        if arr is None:
            return None
        p = arr[fidx, bar]
        return key if np.isfinite(p) and p > 0 else None

    target = sel.target_premium
    if target is None:
        target = (sel.premium_min + sel.premium_max) / 2.0
    best: Optional[OptionKey] = None
    best_d = float("inf")
    for key, arr in day.options.items():
        if key[0] != expiry or key[2] != ot:
            continue
        p = arr[fidx, bar]
        if not np.isfinite(p) or p <= 0:
            continue
        if sel.premium_min is not None and p < sel.premium_min:
            continue
        if sel.premium_max is not None and p > sel.premium_max:
            continue
        d = abs(p - target)
        if d < best_d or (d == best_d and best is not None and key[1] < best[1]):
            best, best_d = key, d
    return best


# --------------------------------------------------------------------------- #
# vectorised single-segment scan
# --------------------------------------------------------------------------- #
def _scan_exit(arr: np.ndarray, s: int, ev0: int, xi: int, entry: float, leg: LegConfig,
               exit_f: int) -> Tuple[int, float, ExitReason]:
    """First SL/TP hit in bars [ev0, xi], else EOD square-off at bar xi."""
    if ev0 > xi:
        return xi, float(arr[exit_f, xi]), ExitReason.EOD_SQUARE_OFF
    o, h, l = arr[O, ev0:xi + 1], arr[H, ev0:xi + 1], arr[L, ev0:xi + 1]
    n = o.size
    adverse = -s                                  # +1: price rising hurts (short)
    steps = np.zeros(n)
    sl_lvl = None
    if leg.stop_loss is not None:
        base = entry + adverse * leg.stop_loss.distance(entry)
        if leg.trail_sl is not None:
            trig = leg.trail_sl.trigger_distance(entry)
            stp = leg.trail_sl.step_distance(entry)
            ext = h if s == 1 else l
            cum = np.maximum.accumulate(s * (ext - entry))          # best favourable excursion
            prev = np.maximum(np.concatenate(([0.0], cum[:-1])), 0.0)  # only bars already closed
            steps = np.floor(prev / trig + 1e-12)
            sl_lvl = base + s * steps * stp
        else:
            sl_lvl = np.full(n, base)
    i_sl = n
    if sl_lvl is not None:
        hit = (l <= sl_lvl) if s == 1 else (h >= sl_lvl)
        if hit.any():
            i_sl = int(hit.argmax())
    i_tp = n
    tp_lvl = None
    if leg.target is not None:
        tp_lvl = entry + s * leg.target.distance(entry)
        hit = (h >= tp_lvl) if s == 1 else (l <= tp_lvl)
        if hit.any():
            i_tp = int(hit.argmax())

    if i_sl == n and i_tp == n:
        return xi, float(arr[exit_f, xi]), ExitReason.EOD_SQUARE_OFF
    if i_sl <= i_tp:                              # SL wins ties
        lvl, op = float(sl_lvl[i_sl]), float(o[i_sl])
        fill = min(lvl, op) if s == 1 else max(lvl, op)   # gap through the stop -> open
        reason = ExitReason.TRAILING_SL if steps[i_sl] > 0 else ExitReason.SL
        return ev0 + i_sl, fill, reason
    op = float(o[i_tp])
    fill = max(tp_lvl, op) if s == 1 else min(tp_lvl, op)
    return ev0 + i_tp, float(fill), ExitReason.TP


def _find_recost(arr: np.ndarray, s: int, frm: int, xi: int, cost: float) -> Optional[Tuple[int, float]]:
    """First bar in [frm, xi] where premium returns to `cost` (stop-style entry)."""
    if frm > xi:
        return None
    o, h, l = arr[O, frm:xi + 1], arr[H, frm:xi + 1], arr[L, frm:xi + 1]
    hit = (l <= cost) if s == -1 else (h >= cost)
    if not hit.any():
        return None
    i = int(hit.argmax())
    fill = min(cost, o[i]) if s == -1 else max(cost, o[i])
    return frm + i, float(fill)


def _run_leg(li: int, leg: LegConfig, day: DayData, cfg: StrategyConfig, spec: InstrumentSpec,
             expiry: date, ei: int, xi: int) -> Optional[List[Segment]]:
    fi, xf = _FIELD[cfg.entry_price_field], _FIELD[cfg.exit_price_field]
    s = 1 if leg.side is Side.BUY else -1
    qty = leg.lots * spec.lot_size_for(expiry)
    leg_id = leg.leg_id or f"L{li + 1}"
    key = resolve_strike(leg.strike, leg.option_type, day, expiry, ei, spec, fi, cfg.greek)
    if key is None:
        return None
    entry_idx, entry_raw = ei, float(day.options[key][fi, ei])
    ev0 = ei if cfg.entry_price_field == "open" else ei + 1
    segs: List[Segment] = []
    count = 0
    while True:
        arr = day.options[key]
        x_idx, x_raw, reason = _scan_exit(arr, s, ev0, xi, entry_raw, leg, xf)
        segs.append(Segment(li, leg_id, len(segs) + 1, key, s, qty, entry_idx, entry_raw,
                            x_idx, x_raw, reason))
        stopped = reason in (ExitReason.SL, ExitReason.TRAILING_SL)
        if not (stopped and leg.reentry.mode is not ReEntryMode.NONE
                and count < leg.reentry.max_count and x_idx + 1 <= xi):
            break
        count += 1
        nxt = x_idx + 1
        if leg.reentry.mode is ReEntryMode.RE_EXECUTE:
            nkey = resolve_strike(leg.strike, leg.option_type, day, expiry, nxt, spec, O, cfg.greek)
            if nkey is None:
                break
            key, entry_idx = nkey, nxt
            entry_raw = float(day.options[key][O, nxt])
            ev0 = nxt
        else:  # RE_COST: same strike, wait for premium to come back to the original cost
            found = _find_recost(arr, s, nxt, xi, segs[-1].entry_raw)
            if found is None:
                break
            entry_idx, entry_raw = found
            ev0 = entry_idx + 1          # touch happened somewhere inside the bar: judge SL from next bar
    return segs


# --------------------------------------------------------------------------- #
# one day
# --------------------------------------------------------------------------- #
def _bar_dt(d: date, idx: int) -> datetime:
    return datetime.combine(d, time(9, 15)) + timedelta(minutes=int(idx))


_STOP_REASONS = (ExitReason.SL, ExitReason.TRAILING_SL, ExitReason.PORTFOLIO_SL,
                 ExitReason.PORTFOLIO_TRAILING_SL)


def slippage_model(cfg) -> SlippageModel:
    return cfg.slippage_model if cfg.slippage_model is not None else SlippageModel(pct=cfg.slippage_pct)


def _eff(price: float, is_buy: bool, slip: float) -> float:
    """Legacy flat-percent fill (slip is a fraction); kept for callers that pass a plain number."""
    return price * (1 + slip) if is_buy else price * (1 - slip)


def _vol(arr, idx: int) -> float:
    v = arr[V, idx]
    return float(v) if np.isfinite(v) else 0.0


def simulate_day(day: DayData, cfg: StrategyConfig, spec: InstrumentSpec
                 ) -> Tuple[List[dict], Optional[str]]:
    """Returns (trade rows, skip_reason). skip_reason is set when the day could not be traded."""
    ei = bar_index(cfg.entry_time)
    xi = min(bar_index(cfg.exit_time), N_BARS - 1)
    if ei < 0 or ei >= xi:
        return [], "entry/exit time outside session"
    expiry = select_expiry(day, cfg.expiry)
    if expiry is None:
        return [], f"no {cfg.expiry.value} expiry in data"
    dte = (expiry - day.trade_date).days
    if (cfg.dte_min is not None and dte < cfg.dte_min) or (cfg.dte_max is not None and dte > cfg.dte_max):
        return [], f"filter: {dte} days to expiry is outside {cfg.dte_min}..{cfg.dte_max}"
    ei, condition_error = find_entry_bar(
        day, cfg, spec, expiry, ei, xi, _FIELD[cfg.entry_price_field], resolve_strike)
    if ei is None:
        return [], condition_error

    segs: List[Segment] = []
    for li, leg in enumerate(cfg.legs):
        leg_expiry = select_leg_expiry(day, expiry, leg.expiry_offset)
        if leg_expiry is None:
            return [], f"leg {li + 1}: no expiry at offset {leg.expiry_offset} from {expiry}"
        r = _run_leg(li, leg, day, cfg, spec, leg_expiry, ei, xi)
        if r is None:
            return [], f"leg {li + 1} ({leg.side.value} {leg.option_type.value}): strike/data unavailable at entry"
        segs.extend(r)

    sm = slippage_model(cfg)
    ent_eff = [sm.fill(g.entry_raw, g.s == 1, _vol(day.options[g.key], g.entry_idx)) for g in segs]

    # ---- MTM path (bar-close, gross of costs): used for portfolio rules and day MAE / MFE ----------
    mtm = np.zeros(N_BARS)
    for g, ee in zip(segs, ent_eff):
        arr = day.options[g.key]
        xe = sm.fill(g.exit_raw, g.s == -1, _vol(arr, g.exit_idx), g.reason in _STOP_REASONS)
        mtm[g.entry_idx:g.exit_idx] += g.s * (arr[C, g.entry_idx:g.exit_idx] - ee) * g.qty
        mtm[g.exit_idx:] += g.s * (xe - ee) * g.qty
    notional = sum(ee * g.qty for g, ee in zip(segs, ent_eff) if g.seg_no == 1)
    p_sl = cfg.portfolio_sl if cfg.portfolio_sl else (
        notional * cfg.portfolio_sl_pct / 100.0 if cfg.portfolio_sl_pct else (
            cfg.portfolio_sl_points * spec.lot_size_for(expiry) if cfg.portfolio_sl_points else None))
    p_tp = cfg.portfolio_tp if cfg.portfolio_tp else (
        notional * cfg.portfolio_tp_pct / 100.0 if cfg.portfolio_tp_pct else None)
    pe = None                       # portfolio exit bar (fill bar), when triggered
    p_reason = None
    t_dec = None
    if p_sl or p_tp:
        m = mtm[ei:xi]               # decision bars; bar xi is EOD anyway
        if m.size:
            peak = np.maximum.accumulate(np.maximum(m, 0.0))
            prev_peak = np.concatenate(([0.0], peak[:-1]))
            steps = np.zeros(m.size)
            hit_sl = np.zeros(m.size, dtype=bool)
            if p_sl:
                lvl = np.full(m.size, -p_sl)
                if cfg.portfolio_trail:
                    steps = np.floor(prev_peak / cfg.portfolio_trail.trigger + 1e-12)
                    lvl = lvl + steps * cfg.portfolio_trail.step
                hit_sl = m <= lvl
            hit_tp = (m >= p_tp) if p_tp else np.zeros(m.size, dtype=bool)
            i_sl = int(hit_sl.argmax()) if hit_sl.any() else m.size
            i_tp = int(hit_tp.argmax()) if hit_tp.any() else m.size
            if min(i_sl, i_tp) < m.size:
                if i_sl <= i_tp:
                    t = ei + i_sl
                    p_reason = (ExitReason.PORTFOLIO_TRAILING_SL if steps[i_sl] > 0
                                else ExitReason.PORTFOLIO_SL)
                else:
                    t = ei + i_tp
                    p_reason = ExitReason.PORTFOLIO_TP
                pe = t + 1
                t_dec = t

    # ---- apply portfolio exit, then build rows -------------------------------------------------
    path = mtm[ei:(pe if pe is not None else xi + 1)]
    day_mae = float(min(path.min(), 0.0)) if path.size else 0.0
    day_mfe = float(max(path.max(), 0.0)) if path.size else 0.0
    rows: List[dict] = []
    for g, ee in zip(segs, ent_eff):
        exit_idx, exit_raw, reason = g.exit_idx, g.exit_raw, g.reason
        if pe is not None:
            if g.entry_idx >= pe:
                continue                          # would have entered after the portfolio was closed
            if g.exit_idx > t_dec:
                exit_idx, exit_raw, reason = pe, float(day.options[g.key][O, pe]), p_reason
        xe = sm.fill(exit_raw, g.s == -1, _vol(day.options[g.key], exit_idx), reason in _STOP_REASONS)
        points = g.s * (xe - ee)
        gross = points * g.qty
        costs = 0.0
        if cfg.apply_costs:
            costs = (leg_costs(spec, day.trade_date, ee, g.qty, g.s == 1)
                     + leg_costs(spec, day.trade_date, xe, g.qty, g.s == -1))
        leg = cfg.legs[g.leg_index]
        entry_greek = {"iv": np.nan, "delta": np.nan, "gamma": np.nan, "theta": np.nan, "vega": np.nan}
        if cfg.entry_greeks:
            entry_field = _FIELD[cfg.entry_price_field] if g.seg_no == 1 else O
            entry_greek = greeks_at(day, g.key, g.entry_idx, entry_field, cfg.greek)
        rows.append({
            "date": day.trade_date, "strategy": cfg.name, "leg_id": g.leg_id, "segment": g.seg_no,
            "expiry": g.key[0], "strike": g.key[1], "option_type": g.key[2], "side": leg.side.value,
            "lots": leg.lots, "qty": g.qty,
            "entry_time": _bar_dt(day.trade_date, g.entry_idx), "entry_price": round(ee, 4),
            "exit_time": _bar_dt(day.trade_date, exit_idx), "exit_price": round(xe, 4),
            "points": round(points, 4), "gross_pnl": round(gross, 2),
            "slippage_cost": round(g.qty * (abs(ee - g.entry_raw) + abs(xe - exit_raw)), 2),
            "costs": round(costs, 2), "net_pnl": round(gross - costs, 2),
            "exit_reason": reason.value, "day_mae": round(day_mae, 2), "day_mfe": round(day_mfe, 2),
            "entry_iv": entry_greek["iv"], "entry_delta": entry_greek["delta"],
            "entry_gamma": entry_greek["gamma"], "entry_theta": entry_greek["theta"],
            "entry_vega": entry_greek["vega"],
        })
    return rows, None


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
def run_backtest(cfg: StrategyConfig, store, registry: Optional[InstrumentRegistry] = None,
                 workers: int = 1, progress=None) -> BacktestResult:
    """Run `cfg` over every trading day in `store`. workers>1 parallelises day by day."""
    from .schemas import HoldingMode
    if cfg.holding_mode is not HoldingMode.INTRADAY:
        from .carry import run_carried_backtest
        return run_carried_backtest(cfg, store, registry, workers, progress)
    spec = (registry or DEFAULT_REGISTRY).get(cfg.underlying)
    dates = select_dates(store, cfg)
    results = run_days(store, dates, simulate_day, cfg, spec, need_prev=False, workers=workers,
                      progress=progress)
    rows: List[dict] = []
    skipped: List[Tuple[date, str]] = []
    for d, r, why in results:
        if why:
            skipped.append((d, why))
        rows.extend(r)
    trades = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    return build_result(cfg, trades, skipped, universe=[d for d, _, _ in results])
