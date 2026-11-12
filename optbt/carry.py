"""Sequential multi-session simulation for positions held beyond the entry session."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Callable, List, Optional, Tuple

import numpy as np
import pandas as pd

from .analytics import build_result
from .costs import leg_costs
from .data import C, N_BARS, O, DayData, bar_index
from .conditions import find_entry_bar
from .engine import (_FIELD, _STOP_REASONS, _run_leg, _vol, resolve_strike, select_expiry,
                     select_leg_expiry, slippage_model)
from .greeks import GreekSettings, greeks_at
from .instruments import DEFAULT_REGISTRY, InstrumentRegistry, InstrumentSpec
from .runner import select_dates
from .schemas import (TRADE_COLUMNS, ATMReference, BacktestResult, ExitReason, ExpirySelect, HoldingMode,
                      OptionType, ReEntryRule, RolloverStrikeMode, StrategyConfig, StrikeMode, StrikeSelection)


class _TimelineOptionArray:
    """Expose a contract across the timeline without padding absent sessions into a dense array."""

    def __init__(self, days: List[DayData], key: tuple):
        self.days = days
        self.key = key
        self.shape = (5, len(days) * N_BARS)

    def __getitem__(self, index):
        if isinstance(index, tuple):
            rows, columns = index
        else:
            rows, columns = index, slice(None)

        if isinstance(columns, (int, np.integer)):
            column = int(columns)
            if column < 0:
                column += self.shape[1]
            if not 0 <= column < self.shape[1]:
                raise IndexError("timeline option index out of range")
            day_idx, minute = divmod(column, N_BARS)
            arr = self.days[day_idx].options.get(self.key)
            values = arr[:, minute] if arr is not None else np.full(5, np.nan)
        else:
            start, stop, step = columns.indices(self.shape[1])
            if step != 1:
                selected = [self[:, column] for column in range(start, stop, step)]
                values = np.stack(selected, axis=1) if selected else np.empty((5, 0))
            else:
                chunks = []
                first_day = start // N_BARS
                last_day = (stop - 1) // N_BARS if stop > start else first_day - 1
                for day_idx in range(first_day, last_day + 1):
                    lo = max(start - day_idx * N_BARS, 0)
                    hi = min(stop - day_idx * N_BARS, N_BARS)
                    arr = self.days[day_idx].options.get(self.key)
                    chunks.append(arr[:, lo:hi] if arr is not None
                                  else np.full((5, hi - lo), np.nan))
                values = np.concatenate(chunks, axis=1) if chunks else np.empty((5, 0))
        return values[rows]


@dataclass
class Timeline:
    days: List[DayData]
    dates: List[date]
    market: DayData
    date_to_day: dict[date, int]
    key_presence: dict[tuple, set[int]]
    skipped: List[Tuple[date, str]]
    contract_loader: Optional[Callable[[date, tuple], Optional[np.ndarray]]] = None
    all_expiries: Optional[List[date]] = None
    checked_contract_days: set[tuple] = field(default_factory=set)
    data_gap_dates: dict[tuple, set[date]] = field(default_factory=dict)
    contracts_by_expiry: dict[date, List[tuple]] = field(default_factory=dict)

    def index(self, day: int, minute: int) -> int:
        return day * N_BARS + minute

    def timestamp(self, index: int) -> datetime:
        day, minute = divmod(index, N_BARS)
        return datetime.combine(self.dates[day], time(9, 15)) + timedelta(minutes=minute)

    def ensure_contract(self, key: tuple, start_day: int) -> None:
        """Fetch one held contract outside the normal strike window for the rest of its life."""
        start_date = self.dates[start_day]
        last_date = min(key[0], self.dates[-1])
        for skipped_date, _ in self.skipped:
            if start_date <= skipped_date <= last_date and skipped_date not in self.date_to_day:
                self.data_gap_dates.setdefault(key, set()).add(skipped_date)
        for di in range(start_day, len(self.days)):
            d = self.dates[di]
            if d > key[0]:
                break
            if di in self.key_presence.get(key, set()):
                continue
            check = (key, d)
            if check in self.checked_contract_days:
                continue
            self.checked_contract_days.add(check)
            arr = self.contract_loader(d, key) if self.contract_loader is not None else None
            if arr is None:
                self.data_gap_dates.setdefault(key, set()).add(d)
                continue
            self.days[di].options[key] = arr
            self.days[di].expiries = sorted(set(self.days[di].expiries) | {key[0]})
            self.market.options.setdefault(key, _TimelineOptionArray(self.days, key))
            self.key_presence.setdefault(key, set()).add(di)

    def data_gap_report(self) -> List[dict]:
        report = []
        for (expiry, strike, option_type), dates in sorted(self.data_gap_dates.items()):
            missing = sorted(dates)
            report.append({
                "expiry": expiry.isoformat(), "strike": strike, "option_type": option_type,
                "from": missing[0].isoformat(), "to": missing[-1].isoformat(),
                "missing_dates": [d.isoformat() for d in missing],
            })
        return report

    def last_index_for(self, entry_day: int, expiry: date, key: tuple) -> tuple[int, ExitReason]:
        """Stop at expiry, the data boundary, or the first session without this contract."""
        present = self.key_presence.get(key, set())
        if self.dates[entry_day] >= expiry:
            return self.index(entry_day, N_BARS - 1), ExitReason.EXPIRY
        end_day = len(self.days) - 1
        reason = ExitReason.BACKTEST_END
        for di in range(entry_day + 1, len(self.days)):
            d = self.dates[di]
            gaps = [gap for gap, _ in self.skipped if self.dates[di - 1] < gap <= d]
            if gaps:
                end_day, reason = di - 1, ExitReason.DATA_GAP
                break
            if d > expiry:
                end_day, reason = di - 1, ExitReason.EXPIRY
                break
            if di not in present:
                end_day, reason = di - 1, ExitReason.DATA_GAP
                break
            if d == expiry:
                end_day, reason = di, ExitReason.EXPIRY
                break
        else:
            if end_day not in present:
                available = [di for di in present if entry_day <= di < end_day]
                if available:
                    end_day, reason = max(available), ExitReason.DATA_GAP
            trailing_gaps = [gap for gap, _ in self.skipped if gap > self.dates[end_day]]
            if trailing_gaps:
                reason = ExitReason.DATA_GAP
            elif self.dates[end_day] >= expiry:
                reason = ExitReason.EXPIRY
        if end_day < entry_day:
            end_day = entry_day
        return self.index(end_day, N_BARS - 1), reason


def _timeline(store, cfg, need_prev: bool, progress=None) -> Timeline:
    dates = [d for d in store.dates if cfg.start_date is None or d >= cfg.start_date]
    dates = [d for d in dates if cfg.end_date is None or d <= cfg.end_date]
    days: List[DayData] = []
    valid_dates: List[date] = []
    skipped: List[Tuple[date, str]] = []
    for i, d in enumerate(dates, 1):
        try:
            day, why = store.build_day(d, cfg.expiry, cfg.max_expiry_gap_days, need_prev)
        except Exception as exc:
            day, why = None, f"error: {type(exc).__name__}: {exc}"
        if day is None:
            skipped.append((d, why or "market data unavailable"))
        else:
            days.append(day)
            valid_dates.append(d)
        if progress is not None and (i % 5 == 0 or i == len(dates)):
            progress(i, len(dates))
    if not days:
        empty_spot = np.empty((4, 0), dtype=float)
        market = DayData(date.min, empty_spot, {}, [], selected_expiry=None)
        return Timeline([], [], market, {}, {}, skipped)

    spot = np.concatenate([day.spot for day in days], axis=1)
    keys = set().union(*(day.options.keys() for day in days))
    options = {}
    presence = {}
    for key in keys:
        present_days = set()
        for di, day in enumerate(days):
            if key in day.options:
                present_days.add(di)
        options[key] = _TimelineOptionArray(days, key)
        presence[key] = present_days
    market = DayData(valid_dates[0], spot, options, sorted({k[0] for k in keys}))
    contract_loader = getattr(store, "load_contract_day", None)
    all_expiries = getattr(store, "_expiries", None)
    if all_expiries is None:
        all_expiries = sorted({expiry for day in days for expiry in day.expiries})
    contracts_by_expiry: dict[date, List[tuple]] = {}
    catalogue = getattr(store, "catalogue", None)
    if catalogue is not None and not catalogue.empty:
        type_column = "type" if "type" in catalogue.columns else "option_type"
        for row in catalogue.itertuples(index=False):
            row_expiry = getattr(row, "expiry")
            if not isinstance(row_expiry, date):
                row_expiry = pd.Timestamp(row_expiry).date()
            strike = float(getattr(row, "strike"))
            option_type = str(getattr(row, type_column)).upper()
            contracts_by_expiry.setdefault(row_expiry, []).append(
                (row_expiry, strike, option_type))
    return Timeline(days, valid_dates, market, {d: i for i, d in enumerate(valid_dates)}, presence, skipped,
                    contract_loader, all_expiries, set(), {}, contracts_by_expiry)


def _horizon(tl: Timeline, day_idx: int, expiry: date, key: tuple) -> tuple[int, ExitReason]:
    """Honor contract-data gaps and close expiring contracts on the prior trading session."""
    boundary, reason = tl.last_index_for(day_idx, expiry, key)
    known_dates = set(tl.dates)
    known_dates.update(d for d, _ in tl.skipped)
    if not any(d >= expiry for d in known_dates):
        return boundary, reason

    prior_sessions = [d for d in known_dates if d < expiry]
    if not prior_sessions:
        return boundary, reason
    roll_date = max(prior_sessions)
    roll_day = tl.date_to_day.get(roll_date)
    if (roll_day is None or roll_day < day_idx
            or roll_day not in tl.key_presence.get(key, set())):
        return boundary, reason

    roll_boundary = tl.index(roll_day, N_BARS - 1)
    if boundary < roll_boundary:
        return boundary, reason
    return roll_boundary, ExitReason.EXPIRY


def _next_roll_expiry(tl: Timeline, expiry_day: int, expiry: date,
                      expiry_mode: ExpirySelect) -> Optional[date]:
    expiries = tl.all_expiries or sorted({candidate[0] for candidate in tl.days[expiry_day].options})
    candidates = sorted(candidate for candidate in expiries if candidate > expiry)
    if expiry_mode is ExpirySelect.MONTHLY:
        later_months = sorted({(candidate.year, candidate.month) for candidate in candidates
                               if (candidate.year, candidate.month) > (expiry.year, expiry.month)})
        if not later_months:
            return None
        month = later_months[0]
        return max(candidate for candidate in candidates
                   if (candidate.year, candidate.month) == month)
    return candidates[0] if candidates else None


def _load_roll_candidate(tl: Timeline, expiry_day: int, key: tuple) -> None:
    day = tl.days[expiry_day]
    if key in day.options:
        return
    if tl.contract_loader is None:
        return
    prices = tl.contract_loader(tl.dates[expiry_day], key)
    if prices is not None:
        day.options[key] = prices


def _prepare_roll_selection(tl: Timeline, expiry_day: int, roll_expiry: date, key: tuple,
                            selection: StrikeSelection, spec: InstrumentSpec,
                            greek: GreekSettings) -> Optional[tuple]:
    day = tl.days[expiry_day]
    bar = N_BARS - 1
    option_type = key[2]
    if selection.mode is StrikeMode.ATM_OFFSET:
        spot = float(day.spot[C, bar])
        if not np.isfinite(spot):
            return None
        atm = int(np.floor(spot / spec.strike_step + 0.5)) * spec.strike_step
        if selection.atm_reference is ATMReference.SYNTHETIC_FUTURE:
            call_key = (roll_expiry, atm, "CE")
            put_key = (roll_expiry, atm, "PE")
            _load_roll_candidate(tl, expiry_day, call_key)
            _load_roll_candidate(tl, expiry_day, put_key)
            call = day.options.get(call_key)
            put = day.options.get(put_key)
            if call is None or put is None:
                return None
            call_price, put_price = float(call[C, bar]), float(put[C, bar])
            if not (np.isfinite(call_price) and call_price > 0
                    and np.isfinite(put_price) and put_price > 0):
                return None
            synthetic = atm + call_price - put_price
            atm = int(np.floor(synthetic / spec.strike_step + 0.5)) * spec.strike_step
        target_key = (roll_expiry, atm + selection.offset * spec.strike_step, option_type)
        _load_roll_candidate(tl, expiry_day, target_key)
    elif selection.mode is StrikeMode.FIXED:
        _load_roll_candidate(
            tl, expiry_day, (roll_expiry, int(round(selection.fixed_strike)), option_type))
    else:
        candidates = tl.contracts_by_expiry.get(roll_expiry, [])
        if not candidates:
            candidates = [candidate for candidate in day.options if candidate[0] == roll_expiry]
        for candidate in candidates:
            if candidate[2] == option_type:
                _load_roll_candidate(tl, expiry_day, candidate)

    selected = resolve_strike(selection, OptionType(option_type), day, roll_expiry,
                              bar, spec, C, greek)
    if selected is None:
        return None
    _load_roll_candidate(tl, expiry_day, selected)
    prices = day.options.get(selected)
    if prices is None or not np.isfinite(prices[C, bar]) or prices[C, bar] <= 0:
        return None
    return selected


def _next_roll_key(tl: Timeline, expiry_day: int, expiry: date, key: tuple,
                   expiry_mode: ExpirySelect, selection: Optional[StrikeSelection] = None,
                   spec: Optional[InstrumentSpec] = None,
                   greek: Optional[GreekSettings] = None) -> Optional[tuple]:
    roll_expiry = _next_roll_expiry(tl, expiry_day, expiry, expiry_mode)
    if roll_expiry is None:
        return None
    if selection is not None:
        if spec is None:
            raise ValueError("instrument specification is required to reselect a rollover strike")
        roll_key = _prepare_roll_selection(
            tl, expiry_day, roll_expiry, key, selection, spec, greek or GreekSettings())
        if roll_key is None:
            return None
    else:
        roll_key = (roll_expiry, key[1], key[2])
    tl.ensure_contract(roll_key, expiry_day)
    if roll_key not in tl.market.options:
        return None
    roll_price = tl.market.options[roll_key][C, tl.index(expiry_day, N_BARS - 1)]
    return roll_key if np.isfinite(roll_price) and roll_price > 0 else None


def _simulate_batch(tl: Timeline, cfg: StrategyConfig, spec: InstrumentSpec, entry_day: int
                    ) -> tuple[list[dict], int, Optional[str]]:
    day = tl.days[entry_day]
    expiry = select_expiry(day, cfg.expiry)
    if expiry is None:
        return [], tl.index(entry_day, bar_index(cfg.entry_time)), f"no {cfg.expiry.value} expiry in data"
    local_ei = bar_index(cfg.entry_time)
    local_xi = min(bar_index(cfg.exit_time), N_BARS - 1)
    ei = tl.index(entry_day, local_ei)
    if (cfg.holding_mode is not HoldingMode.INTRADAY
            and expiry <= tl.dates[entry_day]):
        return [], ei, "cannot carry a position opened on expiry day"
    if ei < 0 or ei >= tl.index(entry_day, N_BARS - 1):
        return [], ei, "entry/exit time outside session"
    local_ei, condition_error = find_entry_bar(
        day, cfg, spec, expiry, local_ei, local_xi, _FIELD[cfg.entry_price_field], resolve_strike)
    if local_ei is None:
        return [], ei, condition_error
    ei = tl.index(entry_day, local_ei)

    segs = []
    for li, leg in enumerate(cfg.legs):
        leg_expiry = select_leg_expiry(day, expiry, leg.expiry_offset)
        if leg_expiry is None:
            return [], ei, f"leg {li + 1}: no expiry at offset {leg.expiry_offset} from {expiry}"
        leg_start = len(segs)
        key = resolve_strike(leg.strike, leg.option_type, day, leg_expiry, local_ei, spec,
                             _FIELD[cfg.entry_price_field], cfg.greek)
        if key is None:
            return [], ei, "entry leg or contract data unavailable"
        if leg.strike.mode.value == "GREEK":
            leg = replace(leg, strike=StrikeSelection.fixed(key[1]))
        carried_leg = (replace(leg, reentry=ReEntryRule())
                       if cfg.holding_mode is HoldingMode.CARRY_SINGLE else leg)
        current_expiry, current_key, entry_idx = leg_expiry, key, ei
        current_cfg = cfg
        while True:
            tl.ensure_contract(current_key, entry_idx // N_BARS)
            horizon, boundary_reason = _horizon(tl, entry_idx // N_BARS, current_expiry, current_key)
            contract_leg = (carried_leg if entry_idx == ei else
                            replace(carried_leg, strike=StrikeSelection.fixed(current_key[1])))
            leg_segs = _run_leg(li, contract_leg, tl.market, current_cfg, spec, current_expiry,
                                entry_idx, horizon)
            if leg_segs is None:
                return [], ei, "entry leg or contract data unavailable"
            current_key = leg_segs[-1].key
            rolled = (cfg.holding_mode is not HoldingMode.INTRADAY
                      and boundary_reason is ExitReason.EXPIRY
                      and leg_segs[-1].exit_idx == horizon
                      and leg_segs[-1].reason is ExitReason.EOD_SQUARE_OFF)
            if not rolled:
                segs.extend(leg_segs)
                break
            roll_key = _next_roll_key(
                tl, horizon // N_BARS, current_expiry, current_key, cfg.expiry,
                selection=(cfg.legs[li].strike
                           if cfg.rollover_strike_mode is RolloverStrikeMode.RESELECT else None),
                spec=spec, greek=cfg.greek)
            if roll_key is None:
                segs.extend(leg_segs)
                break
            roll_arr = tl.market.options[roll_key]
            roll_price = float(roll_arr[C, horizon])
            if not np.isfinite(roll_price) or roll_price <= 0:
                segs.extend(leg_segs)
                break
            leg_segs[-1] = replace(leg_segs[-1], reason=ExitReason.ROLLED)
            segs.extend(leg_segs)
            current_expiry, current_key, entry_idx = roll_key[0], roll_key, horizon
            current_cfg = replace(cfg, entry_price_field="close")
        segs[leg_start:] = [replace(segment, seg_no=i + 1)
                            for i, segment in enumerate(segs[leg_start:])]
    if not segs:
        return [], ei, "entry leg or contract data unavailable"

    # Cap the basket at the earliest expiry/data boundary among its selected contracts.
    final_leg_segments = {}
    for segment in segs:
        final_leg_segments[segment.leg_index] = segment
    boundaries = [_horizon(tl, segment.entry_idx // N_BARS, segment.key[0], segment.key)
                  for segment in final_leg_segments.values()]
    basket_boundary, boundary_reason = min(boundaries, key=lambda result: result[0])
    basket_end = basket_boundary
    segs = [g if g.exit_idx <= basket_end else _close_segment_at(
        g, basket_end, tl.market.options[g.key], _FIELD[cfg.exit_price_field])
            for g in segs]
    sm = slippage_model(cfg)
    ent_eff = [sm.fill(g.entry_raw, g.s == 1, _vol(tl.market.options[g.key], g.entry_idx)) for g in segs]
    mtm = np.zeros(tl.market.spot.shape[1], dtype=float)
    for g, ee in zip(segs, ent_eff):
        arr = tl.market.options[g.key]
        xe = sm.fill(g.exit_raw, g.s == -1, _vol(arr, g.exit_idx), g.reason in _STOP_REASONS)
        mtm[g.entry_idx:g.exit_idx] += g.s * (arr[C, g.entry_idx:g.exit_idx] - ee) * g.qty
        mtm[g.exit_idx:] += g.s * (xe - ee) * g.qty

    notional = sum(ee * g.qty for g, ee in zip(segs, ent_eff) if g.seg_no == 1)
    p_sl = cfg.portfolio_sl or (
        notional * cfg.portfolio_sl_pct / 100.0 if cfg.portfolio_sl_pct else (
            cfg.portfolio_sl_points * spec.lot_size_for(expiry) if cfg.portfolio_sl_points else None))
    p_tp = cfg.portfolio_tp or (
        notional * cfg.portfolio_tp_pct / 100.0 if cfg.portfolio_tp_pct else None)
    pe = t_dec = None
    p_reason = None
    stop_decision_end = min(basket_end, max(g.exit_idx for g in segs))
    if p_sl or p_tp:
        m = mtm[ei:stop_decision_end]
        if m.size:
            peak = np.maximum.accumulate(np.maximum(m, 0.0))
            prev_peak = np.concatenate(([0.0], peak[:-1]))
            steps = np.zeros(m.size)
            hit_sl = np.zeros(m.size, dtype=bool)
            if p_sl:
                level = np.full(m.size, -p_sl)
                if cfg.portfolio_trail:
                    steps = np.floor(prev_peak / cfg.portfolio_trail.trigger + 1e-12)
                    level += steps * cfg.portfolio_trail.step
                hit_sl = m <= level
            hit_tp = (m >= p_tp) if p_tp else np.zeros(m.size, dtype=bool)
            i_sl = int(hit_sl.argmax()) if hit_sl.any() else m.size
            i_tp = int(hit_tp.argmax()) if hit_tp.any() else m.size
            if min(i_sl, i_tp) < m.size:
                if i_sl <= i_tp:
                    t_dec = ei + i_sl
                    p_reason = (ExitReason.PORTFOLIO_TRAILING_SL if steps[i_sl] > 0
                                else ExitReason.PORTFOLIO_SL)
                else:
                    t_dec, p_reason = ei + i_tp, ExitReason.PORTFOLIO_TP
                pe = min(t_dec + 1, basket_end)

    end_idx = max(g.exit_idx for g in segs)
    if pe is not None:
        if t_dec is None or p_reason is None:
            raise RuntimeError("portfolio exit is missing its decision bar or exit reason")
        end_idx = pe
    path = mtm[ei:end_idx + 1]
    day_mae = float(min(path.min(), 0.0)) if path.size else 0.0
    day_mfe = float(max(path.max(), 0.0)) if path.size else 0.0
    rows = []
    for g, ee in zip(segs, ent_eff):
        exit_idx, exit_raw, reason = g.exit_idx, g.exit_raw, g.reason
        if pe is not None and g.entry_idx > t_dec:
            continue
        if pe is not None and g.exit_idx > t_dec:
            exit_idx = pe
            exit_raw = float(tl.market.options[g.key][O, pe])
            reason = p_reason
        elif g.exit_idx == basket_end and g.reason is ExitReason.EOD_SQUARE_OFF:
            reason = boundary_reason
        if reason is None:
            raise RuntimeError("carried leg is missing its exit reason")
        arr = tl.market.options[g.key]
        xe = sm.fill(exit_raw, g.s == -1, _vol(arr, exit_idx), reason in _STOP_REASONS)
        points = g.s * (xe - ee)
        gross = points * g.qty
        exit_date = tl.timestamp(exit_idx).date()
        costs = 0.0
        if cfg.apply_costs:
            costs = (leg_costs(spec, tl.dates[entry_day], ee, g.qty, g.s == 1)
                     + leg_costs(spec, exit_date, xe, g.qty, g.s == -1))
        greek_day = tl.days[g.entry_idx // N_BARS]
        greek_bar = g.entry_idx % N_BARS
        greek_field = _FIELD[cfg.entry_price_field] if g.entry_idx == ei else O
        entry_greek = greeks_at(greek_day, g.key, greek_bar, greek_field, cfg.greek) if cfg.entry_greeks else {}
        rows.append({
            "date": exit_date, "strategy": cfg.name, "leg_id": g.leg_id, "segment": g.seg_no,
            "expiry": g.key[0], "strike": g.key[1], "option_type": g.key[2],
            "side": cfg.legs[g.leg_index].side.value, "lots": cfg.legs[g.leg_index].lots, "qty": g.qty,
            "entry_time": tl.timestamp(g.entry_idx), "entry_price": round(ee, 4),
            "exit_time": tl.timestamp(exit_idx), "exit_price": round(xe, 4),
            "points": round(points, 4), "gross_pnl": round(gross, 2),
            "slippage_cost": round(g.qty * (abs(ee - g.entry_raw) + abs(xe - exit_raw)), 2),
            "costs": round(costs, 2), "net_pnl": round(gross - costs, 2),
            "exit_reason": reason.value, "day_mae": round(day_mae, 2), "day_mfe": round(day_mfe, 2),
            "entry_iv": entry_greek.get("iv", np.nan), "entry_delta": entry_greek.get("delta", np.nan),
            "entry_gamma": entry_greek.get("gamma", np.nan), "entry_theta": entry_greek.get("theta", np.nan),
            "entry_vega": entry_greek.get("vega", np.nan),
        })
    return rows, end_idx, None


def _close_segment_at(segment, idx: int, arr: np.ndarray, exit_field: int):
    from dataclasses import replace
    return replace(segment, exit_idx=idx, exit_raw=float(arr[exit_field, idx]), reason=ExitReason.EOD_SQUARE_OFF)


def run_carried_backtest(cfg: StrategyConfig, store,
                         registry: Optional[InstrumentRegistry] = None,
                         workers: int = 1, progress=None) -> BacktestResult:
    """Simulate entries sequentially while each position is monitored through later sessions."""
    if workers != 1:
        raise ValueError("carry-forward positions must be simulated sequentially with workers=1")
    spec = (registry or DEFAULT_REGISTRY).get(cfg.underlying)
    tl = _timeline(store, cfg, need_prev=False, progress=progress)
    entry_dates = select_dates(store, cfg)
    rows: List[dict] = []
    skipped = list(tl.skipped)
    next_allowed = -1
    for d in entry_dates:
        di = tl.date_to_day.get(d)
        if di is None:
            continue
        ei = tl.index(di, bar_index(cfg.entry_time))
        if cfg.holding_mode is HoldingMode.CARRY_SINGLE and ei <= next_allowed:
            continue
        batch, end_idx, why = _simulate_batch(tl, cfg, spec, di)
        if not batch:
            skipped.append((d, why or "entry leg or contract data unavailable"))
            continue
        rows.extend(batch)
        next_allowed = max(next_allowed, end_idx)
    trades = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    result = build_result(cfg, trades, skipped, universe=entry_dates)
    result.metrics["data_gaps"] = tl.data_gap_report()
    return result
