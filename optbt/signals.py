"""Indicator-driven strategy config and signal generation (Supertrend, VWAP, EMA).

Every signal source reduces to two per-minute arrays:
    sig[j]   in {-1, 0, +1}: indicator state known at the START of minute j
    valid[j] : whether the indicator is usable at minute j

State at minute j only uses bars that were fully CLOSED before j, so an entry
at the open of minute j has no look-ahead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum
from typing import Dict, List, Mapping, Optional, Tuple, Union

import numpy as np

from .data import C, H, L, N_BARS, V, DayData, O, OptionKey, bar_index
from .engine import select_expiry
from .indicators import resample_ohlc, supertrend, vwap
from .instruments import InstrumentSpec
from .greeks import GreekSettings
from .schemas import (ExpirySelect, HoldingMode, LegConfig, OptionType, RiskRule, RolloverStrikeMode,
                      StrikeSelection, TrailRule)


class OptionExpression(str, Enum):
    LONG_PREMIUM = "LONG_PREMIUM"     # bullish -> BUY CE, bearish -> BUY PE
    SHORT_PREMIUM = "SHORT_PREMIUM"   # bullish -> SELL PE, bearish -> SELL CE


class OppositeSignal(str, Enum):
    REVERSE = "REVERSE"   # close and open the opposite position (stop-and-reverse)
    EXIT = "EXIT"         # close only, wait for the next signal
    IGNORE = "IGNORE"     # hold until SL / TP / square-off


class VwapSource(str, Enum):
    SPOT = "SPOT"         # spot close vs spot VWAP (index has no volume -> mean of typical price)
    PREMIUM = "PREMIUM"   # ATM CE / PE premium vs each option's own volume-weighted VWAP


class EMASignalMode(str, Enum):
    PRICE_VS_EMA = "PRICE_VS_EMA"
    EMA_CROSSOVER = "EMA_CROSSOVER"


class MovingAverageType(str, Enum):
    EMA = "EMA"
    SMA = "SMA"


class EMASource(str, Enum):
    UNDERLYING = "UNDERLYING"
    OPTION = "OPTION"


SignalData = Tuple[np.ndarray, np.ndarray, Optional[Dict[str, OptionKey]], Optional[str]]
OptionEMASignalData = Dict[str, SignalData]
IndicatorSignalData = Union[SignalData, OptionEMASignalData]


@dataclass(frozen=True)
class SupertrendParams:
    period: int = 10
    multiplier: float = 3.0
    timeframe_min: int = 5
    use_prev_day: bool = True        # continue ATR / trend state across available sessions
    timeframe: str = ""

    def __post_init__(self):
        if self.period < 1 or self.multiplier <= 0 or self.timeframe_min < 1:
            raise ValueError("invalid Supertrend parameters")
        if self.timeframe and self.timeframe not in _TIMEFRAME_MINUTES:
            raise ValueError("invalid indicator timeframe")


@dataclass(frozen=True)
class VwapParams:
    source: VwapSource = VwapSource.SPOT
    buffer_pct: float = 0.0          # close must clear VWAP by this % to count as above/below
    confirm_bars: int = 1            # consecutive 1-min closes required
    timeframe: str = "1m"

    def __post_init__(self):
        if self.buffer_pct < 0 or self.confirm_bars < 1:
            raise ValueError("invalid VWAP parameters")
        if self.timeframe not in _TIMEFRAME_MINUTES:
            raise ValueError("invalid indicator timeframe")


@dataclass(frozen=True)
class EMAParams:
    mode: EMASignalMode = EMASignalMode.PRICE_VS_EMA
    period: int = 20
    fast_period: int = 9
    slow_period: int = 21
    slope_filter: bool = False
    slope_type: MovingAverageType = MovingAverageType.EMA
    slope_period: int = 20
    timeframe: str = "1m"
    source: EMASource = EMASource.UNDERLYING

    def __post_init__(self):
        if self.period < 1 or self.fast_period < 1 or self.slow_period < 1 or self.slope_period < 1:
            raise ValueError("EMA periods must be >= 1")
        if self.mode is EMASignalMode.EMA_CROSSOVER and self.fast_period >= self.slow_period:
            raise ValueError("fast EMA period must be less than slow EMA period")
        if self.timeframe not in _TIMEFRAME_MINUTES:
            raise ValueError("invalid indicator timeframe")


@dataclass(frozen=True)
class MACDParams:
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    timeframe: str = "1m"

    def __post_init__(self):
        if self.fast_period < 1 or self.slow_period < 1 or self.signal_period < 1:
            raise ValueError("MACD periods must be >= 1")
        if self.fast_period >= self.slow_period:
            raise ValueError("fast MACD period must be less than slow MACD period")
        if self.timeframe not in _TIMEFRAME_MINUTES:
            raise ValueError("invalid indicator timeframe")


_TIMEFRAME_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60,
    "1d": None, "1w": None, "1mo": None,
}


def indicator_timeframe(indicator) -> str:
    if isinstance(indicator, SupertrendParams):
        return indicator.timeframe or f"{indicator.timeframe_min}m"
    return indicator.timeframe


@dataclass(frozen=True)
class SignalStrategyConfig:
    name: str
    underlying: str
    indicator: Union[SupertrendParams, VwapParams, EMAParams, MACDParams]
    expression: OptionExpression = OptionExpression.LONG_PREMIUM
    start_time: time = time(9, 30)         # earliest signal that may open a trade
    last_entry_time: time = time(14, 45)   # no new entries after this
    exit_time: time = time(15, 15)         # mandatory square-off
    expiry: ExpirySelect = ExpirySelect.CURRENT_WEEKLY
    strike: StrikeSelection = field(default_factory=StrikeSelection.atm)
    lots: int = 1
    stop_loss: Optional[RiskRule] = None
    target: Optional[RiskRule] = None
    trail_sl: Optional[TrailRule] = None
    on_opposite: OppositeSignal = OppositeSignal.REVERSE
    max_trades_per_day: int = 10
    trade_initial_trend: bool = False      # also enter at start_time if a trend is already in force
    daily_max_loss: Optional[float] = None  # Rs; closes the open trade and stops trading for the day
    daily_target: Optional[float] = None    # Rs
    slippage_pct: float = 0.0
    slippage_model: Optional[object] = None    # SlippageModel; overrides slippage_pct when given
    dte_min: Optional[int] = None
    dte_max: Optional[int] = None
    apply_costs: bool = True
    exit_price_field: str = "close"
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    weekdays: Optional[Tuple[int, ...]] = None
    capital: Optional[float] = None            # None: no % / ratio metrics (they need a capital base)
    max_expiry_gap_days: Optional[int] = 7     # skip a day if an expiry looks missing; None disables
    holding_mode: HoldingMode = HoldingMode.INTRADAY
    rollover_strike_mode: RolloverStrikeMode = RolloverStrikeMode.ORIGINAL
    greek: GreekSettings = GreekSettings()
    entry_greeks: bool = True
    signal_legs: Tuple[LegConfig, ...] = ()
    execution_leg: Optional[LegConfig] = None
    execution_leg_index: int = 0
    expiry_day_only: bool = False

    def __post_init__(self):
        if not self.start_time <= self.last_entry_time < self.exit_time:
            raise ValueError("need start_time <= last_entry_time < exit_time")
        if self.lots < 1 or self.max_trades_per_day < 1:
            raise ValueError("lots and max_trades_per_day must be >= 1")
        if self.trail_sl is not None and self.stop_loss is None:
            raise ValueError("trail_sl requires stop_loss")
        if self.exit_price_field not in ("open", "close"):
            raise ValueError("exit_price_field must be 'open' or 'close'")
        for v in (self.daily_max_loss, self.daily_target):
            if v is not None and v <= 0:
                raise ValueError("daily limits are positive Rs amounts")
        if self.slippage_pct < 0:
            raise ValueError("slippage_pct must be >= 0")


# --------------------------------------------------------------------------- #
# signal generation
# --------------------------------------------------------------------------- #
def _shift_to_minutes(state_bar: np.ndarray, valid_bar: np.ndarray):
    """State computed at the close of 1-minute bar b is usable from minute b+1."""
    sig = np.zeros(N_BARS, dtype=np.int8)
    valid = np.zeros(N_BARS, dtype=bool)
    sig[1:] = state_bar[:-1]
    valid[1:] = valid_bar[:-1]
    return sig, valid


def _supertrend_signals(day: DayData, p: SupertrendParams):
    tf = _TIMEFRAME_MINUTES[p.timeframe] if p.timeframe else p.timeframe_min
    nb = N_BARS // tf
    if not np.isfinite(day.spot).all():
        return np.zeros(N_BARS, np.int8), np.zeros(N_BARS, bool)
    _, h, l, c = resample_ohlc(day.spot, tf)
    off = 0
    if p.use_prev_day and day.prev_spot is not None and np.isfinite(day.prev_spot).all():
        _, ph, pl, pc = resample_ohlc(day.prev_spot, tf)
        h, l, c = np.concatenate([ph, h]), np.concatenate([pl, l]), np.concatenate([pc, c])
        off = ph.size
    _, d = supertrend(h, l, c, p.period, p.multiplier)
    j = np.arange(N_BARS)
    k = np.minimum(off + j // tf - 1, off + nb - 1)      # last bar closed before minute j
    ok = k >= 0
    kk = np.clip(k, 0, None)
    sig = np.where(ok, d[kk], 0).astype(np.int8)
    valid = ok & (d[kk] != 0)
    return sig, valid


def _carry_state(raw: np.ndarray) -> np.ndarray:
    """Forward-fill the last non-zero state (price dithering inside the band keeps the trend)."""
    idx = np.where(raw != 0, np.arange(raw.size), -1)
    np.maximum.accumulate(idx, out=idx)
    return np.where(idx >= 0, raw[np.clip(idx, 0, None)], 0).astype(np.int8)


def _confirmed(mask: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return mask
    return np.convolve(mask.astype(int), np.ones(n, dtype=int))[:mask.size] >= n


def _moving_average(close: np.ndarray, period: int, ma_type: MovingAverageType) -> np.ndarray:
    out = np.full(close.size, np.nan)
    if ma_type is MovingAverageType.EMA:
        alpha = 2.0 / (period + 1)
        for i in range(period - 1, close.size):
            window = close[i - period + 1:i + 1]
            if not np.isfinite(window).all():
                continue
            if i == period - 1 or not np.isfinite(out[i - 1]):
                out[i] = window.mean()
            else:
                out[i] = alpha * close[i] + (1.0 - alpha) * out[i - 1]
        return out
    if close.size >= period:
        finite = np.isfinite(close)
        sums = np.concatenate(([0.0], np.cumsum(np.where(finite, close, 0.0))))
        counts = np.concatenate(([0], np.cumsum(finite)))
        window_counts = counts[period:] - counts[:-period]
        means = (sums[period:] - sums[:-period]) / period
        valid = window_counts == period
        out[period - 1:] = np.where(valid, means, np.nan)
    return out


def _ema_bar_signals(close: np.ndarray, p: EMAParams):
    if p.mode is EMASignalMode.EMA_CROSSOVER:
        fast = _moving_average(close, p.fast_period, MovingAverageType.EMA)
        slow = _moving_average(close, p.slow_period, MovingAverageType.EMA)
        valid = np.isfinite(fast) & np.isfinite(slow)
        raw = np.where(fast > slow, 1, np.where(fast < slow, -1, 0)).astype(np.int8)
        return raw, valid

    reference = _moving_average(close, p.period, MovingAverageType.EMA)
    valid = np.isfinite(close)
    above = valid & (close > reference)
    below = valid & (close < reference)
    if p.slope_filter:
        slope = _moving_average(close, p.slope_period, p.slope_type)
        rising = np.isfinite(slope) & (slope > np.roll(slope, 1))
        falling = np.isfinite(slope) & (slope < np.roll(slope, 1))
        rising[0] = falling[0] = False
        above &= rising
        below &= falling
    raw = np.where(above, 1, np.where(below, -1, 0)).astype(np.int8)
    return raw, valid


def _macd_curves(close: np.ndarray, params: MACDParams):
    fast = _moving_average(close, params.fast_period, MovingAverageType.EMA)
    slow = _moving_average(close, params.slow_period, MovingAverageType.EMA)
    macd = fast - slow
    macd[~(np.isfinite(fast) & np.isfinite(slow))] = np.nan
    signal = _moving_average(macd, params.signal_period, MovingAverageType.EMA)
    return macd, signal


def _macd_bar_signals(close: np.ndarray, params: MACDParams):
    macd, signal = _macd_curves(close, params)
    valid = np.isfinite(macd) & np.isfinite(signal)
    raw = np.where(macd > signal, 1, np.where(macd < signal, -1, 0)).astype(np.int8)
    return _carry_state(raw), valid


def _ema_signals(day: DayData, p: EMAParams):
    return _ema_close_signals(day.spot[C], p)


def _ema_close_signals(close: np.ndarray, p: EMAParams):
    raw, valid = _ema_bar_signals(close, p)
    return _shift_to_minutes(raw, valid)


def _above_below(close: np.ndarray, vw: np.ndarray, buf_pct: float, n: int):
    ok = np.isfinite(close) & np.isfinite(vw)
    buf = buf_pct / 100.0
    above = _confirmed(ok & (close > vw * (1 + buf)), n)
    below = _confirmed(ok & (close < vw * (1 - buf)), n)
    return above, below, ok


def _vwap_spot_signals(day: DayData, p: VwapParams):
    h, l, c = day.spot[H], day.spot[L], day.spot[C]
    vw = vwap(h, l, c, np.zeros(N_BARS))          # no index volume -> equal weights
    above, below, ok = _above_below(c, vw, p.buffer_pct, p.confirm_bars)
    raw = np.where(above, 1, np.where(below, -1, 0)).astype(np.int8)
    return _shift_to_minutes(_carry_state(raw), ok)


def _vwap_premium_signals(day: DayData, p: VwapParams, ce: np.ndarray, pe: np.ndarray):
    def flags(arr):
        vw = vwap(arr[H], arr[L], arr[C], arr[V])
        return _above_below(arr[C], vw, p.buffer_pct, p.confirm_bars)
    ca, _, cok = flags(ce)
    pa, _, pok = flags(pe)
    raw = np.where(ca & ~pa, 1, np.where(pa & ~ca, -1, 0)).astype(np.int8)
    return _shift_to_minutes(_carry_state(raw), cok & pok)


def compute_signals(day: DayData, cfg: SignalStrategyConfig, spec: InstrumentSpec, expiry: date,
                    start_idx: int, resolve):
    """Returns (sig, valid, locked, error). `locked` maps 'CE'/'PE' -> OptionKey for the PREMIUM
    VWAP source (the contracts being monitored are also the contracts traded)."""
    ind = cfg.indicator
    if isinstance(ind, SupertrendParams):
        sig, valid = _supertrend_signals(day, ind)
        return sig, valid, None, None
    if isinstance(ind, EMAParams):
        sig, valid = _ema_signals(day, ind)
        return sig, valid, None, None
    if isinstance(ind, MACDParams):
        raw, valid = _macd_bar_signals(day.spot[C], ind)
        sig, valid = _shift_to_minutes(raw, valid)
        return sig, valid, None, None
    if ind.source is VwapSource.SPOT:
        sig, valid = _vwap_spot_signals(day, ind)
        return sig, valid, None, None
    ce = resolve(cfg.strike, OptionType.CE, day, expiry, start_idx, spec, O)
    pe = resolve(cfg.strike, OptionType.PE, day, expiry, start_idx, spec, O)
    if ce is None or pe is None:
        return None, None, None, "premium VWAP: CE/PE contracts unavailable at start_time"
    sig, valid = _vwap_premium_signals(day, ind, day.options[ce], day.options[pe])
    return sig, valid, {"CE": ce, "PE": pe}, None


def _timeframe_groups(days: List[DayData], timeframe: str, include_partial: bool = False):
    groups: List[List[Tuple[int, int, int]]] = []
    bucket_map = [np.full(N_BARS, -1, dtype=int) for _ in days]
    if timeframe.endswith("m"):
        minutes = _TIMEFRAME_MINUTES[timeframe]
        for di in range(len(days)):
            for lo in range(0, N_BARS, minutes):
                hi = lo + minutes
                if lo >= N_BARS or (hi > N_BARS and not include_partial):
                    break
                hi = min(hi, N_BARS)
                bucket_map[di][lo:hi] = len(groups)
                groups.append([(di, lo, hi)])
        return groups, bucket_map

    previous_key = None
    for di, day in enumerate(days):
        if timeframe == "1d":
            key = day.trade_date
        elif timeframe == "1w":
            iso = day.trade_date.isocalendar()
            key = (iso.year, iso.week)
        else:
            key = (day.trade_date.year, day.trade_date.month)
        if key != previous_key:
            groups.append([])
            previous_key = key
        groups[-1].append((di, 0, N_BARS))
        bucket_map[di][:] = len(groups) - 1
    return groups, bucket_map


def compute_signals_for_days(days: List[DayData], cfg: SignalStrategyConfig,
                             spec: InstrumentSpec, resolve
                             ) -> Mapping[date, IndicatorSignalData]:
    """Compute timeframe-close signals over a sequence of sessions with no future-bar access."""
    timeframe = indicator_timeframe(cfg.indicator)
    ind = cfg.indicator
    carries_session_history = (isinstance(ind, (EMAParams, MACDParams))
                               or isinstance(ind, SupertrendParams) and ind.use_prev_day)
    if timeframe == "1m" and not carries_session_history:
        result = {}
        for day in days:
            expiry = day.selected_expiry or select_expiry(day, cfg.expiry)
            if expiry is None:
                result[day.trade_date] = (np.zeros(N_BARS, np.int8), np.zeros(N_BARS, bool),
                                          None, f"no {cfg.expiry.value} expiry in data")
            else:
                result[day.trade_date] = compute_signals(day, cfg, spec, expiry,
                                                         bar_index(cfg.start_time), resolve)
        return result

    groups, bucket_map = _timeframe_groups(days, timeframe)
    if isinstance(ind, EMAParams) and ind.source is EMASource.OPTION:
        return compute_option_ema_signals_for_days(days, cfg, spec, resolve)
    agg_high, agg_low, agg_close = [], [], []
    for group in groups:
        last_day, _, last_bar = group[-1]
        agg_high.append(max(float(days[di].spot[H, lo:hi].max()) for di, lo, hi in group))
        agg_low.append(min(float(days[di].spot[L, lo:hi].min()) for di, lo, hi in group))
        agg_close.append(float(days[last_day].spot[C, last_bar - 1]))
    close = np.asarray(agg_close, dtype=float)
    locked_by_date: Dict[date, Optional[dict]] = {}
    errors: Dict[date, Optional[str]] = {day.trade_date: None for day in days}

    if isinstance(ind, SupertrendParams):
        high, low = np.asarray(agg_high), np.asarray(agg_low)
        if timeframe.endswith("m") and not ind.use_prev_day:
            state = np.zeros(close.size, dtype=np.int8)
            groups_by_day: Dict[int, List[int]] = {}
            for gi, group in enumerate(groups):
                groups_by_day.setdefault(group[0][0], []).append(gi)
            for di, indices in groups_by_day.items():
                _, local_state = supertrend(high[indices], low[indices], close[indices],
                                            ind.period, ind.multiplier)
                state[indices] = local_state
        else:
            _, state = supertrend(high, low, close, ind.period, ind.multiplier)
        bar_valid = state != 0
        bar_signal = state
    elif isinstance(ind, EMAParams):
        bar_signal, bar_valid = _ema_bar_signals(close, ind)
    elif isinstance(ind, MACDParams):
        bar_signal, bar_valid = _macd_bar_signals(close, ind)
    else:
        if timeframe.endswith("m"):
            above = np.zeros(len(groups), dtype=bool)
            below = np.zeros(len(groups), dtype=bool)
            bar_valid = np.zeros(len(groups), dtype=bool)
            groups_by_day: Dict[int, List[int]] = {}
            for gi, group in enumerate(groups):
                groups_by_day.setdefault(group[0][0], []).append(gi)
            for di, indices in groups_by_day.items():
                day = days[di]
                if ind.source is VwapSource.SPOT:
                    highs, lows, closes = [], [], []
                    for gi in indices:
                        _, lo, hi = groups[gi][0]
                        prices = day.spot[:, lo:hi]
                        highs.append(float(prices[H].max()))
                        lows.append(float(prices[L].min()))
                        closes.append(float(prices[C, -1]))
                    vw = vwap(np.asarray(highs), np.asarray(lows), np.asarray(closes),
                              np.zeros(len(indices)))
                    for local, gi in enumerate(indices):
                        close_value = closes[local]
                        bar_valid[gi] = np.isfinite(vw[local]) and np.isfinite(close_value)
                        above[gi] = close_value > vw[local] * (1 + ind.buffer_pct / 100.0)
                        below[gi] = close_value < vw[local] * (1 - ind.buffer_pct / 100.0)
                else:
                    expiry = day.selected_expiry or select_expiry(day, cfg.expiry)
                    ce = (resolve(cfg.strike, OptionType.CE, day, expiry, bar_index(cfg.start_time),
                                  spec, O) if expiry is not None else None)
                    pe = (resolve(cfg.strike, OptionType.PE, day, expiry, bar_index(cfg.start_time),
                                  spec, O) if expiry is not None else None)
                    if expiry is None or ce is None or pe is None or ce not in day.options or pe not in day.options:
                        errors[day.trade_date] = ("no expiry in data" if expiry is None else
                                                  "premium VWAP: CE/PE contracts unavailable at start_time")
                        locked_by_date[day.trade_date] = None
                        continue
                    locked_by_date[day.trade_date] = {"CE": ce, "PE": pe}
                    bars = {"CE": [], "PE": []}
                    for gi in indices:
                        _, lo, hi = groups[gi][0]
                        for typ, key in (("CE", ce), ("PE", pe)):
                            arr = day.options[key]
                            bars[typ].append((
                                float(arr[O, lo]), float(arr[H, lo:hi].max()),
                                float(arr[L, lo:hi].min()), float(arr[C, hi - 1]),
                                float(arr[V, lo:hi].sum())))
                    vwap_series = {}
                    for typ in ("CE", "PE"):
                        values = np.asarray(bars[typ], dtype=float)
                        vwap_series[typ] = vwap(values[:, 1], values[:, 2], values[:, 3], values[:, 4])
                    for local, gi in enumerate(indices):
                        ce_close, pe_close = bars["CE"][local][3], bars["PE"][local][3]
                        ce_above = ce_close > vwap_series["CE"][local] * (1 + ind.buffer_pct / 100.0)
                        pe_above = pe_close > vwap_series["PE"][local] * (1 + ind.buffer_pct / 100.0)
                        bar_valid[gi] = (np.isfinite(ce_close) and np.isfinite(pe_close)
                                         and np.isfinite(vwap_series["CE"][local])
                                         and np.isfinite(vwap_series["PE"][local]))
                        above[gi] = ce_above and not pe_above
                        below[gi] = pe_above and not ce_above
            bar_signal = np.zeros(len(groups), dtype=np.int8)
            for indices in groups_by_day.values():
                bull = _confirmed(above[indices], ind.confirm_bars)
                bear = _confirmed(below[indices], ind.confirm_bars)
                raw = np.where(bull, 1, np.where(bear, -1, 0))
                bar_signal[indices] = _carry_state(raw)
        else:
            above, below, valid = [], [], []
            for group in groups:
                if ind.source is VwapSource.SPOT:
                    h_parts = [days[di].spot[H, lo:hi] for di, lo, hi in group]
                    l_parts = [days[di].spot[L, lo:hi] for di, lo, hi in group]
                    c_parts = [days[di].spot[C, lo:hi] for di, lo, hi in group]
                    h, l, c = np.concatenate(h_parts), np.concatenate(l_parts), np.concatenate(c_parts)
                    vw = vwap(h, l, c, np.zeros(c.size))[-1]
                    last_close = c[-1]
                    valid.append(np.isfinite(vw) and np.isfinite(last_close))
                    above.append(last_close > vw * (1 + ind.buffer_pct / 100.0))
                    below.append(last_close < vw * (1 - ind.buffer_pct / 100.0))
                else:
                    source_arrays = {"CE": [], "PE": []}
                    complete_group = True
                    for di, _, _ in group:
                        day = days[di]
                        expiry = day.selected_expiry or select_expiry(day, cfg.expiry)
                        if expiry is None:
                            errors[day.trade_date] = f"no {cfg.expiry.value} expiry in data"
                            locked_by_date[day.trade_date] = None
                            complete_group = False
                            continue
                        ce = resolve(cfg.strike, OptionType.CE, day, expiry, bar_index(cfg.start_time),
                                     spec, O)
                        pe = resolve(cfg.strike, OptionType.PE, day, expiry, bar_index(cfg.start_time),
                                     spec, O)
                        if ce is None or pe is None or ce not in day.options or pe not in day.options:
                            errors[day.trade_date] = "premium VWAP: CE/PE contracts unavailable at start_time"
                            locked_by_date[day.trade_date] = None
                            complete_group = False
                            continue
                        locked_by_date[day.trade_date] = {"CE": ce, "PE": pe}
                        _, lo, hi = next((x for x in group if x[0] == di), (di, 0, N_BARS))
                        source_arrays["CE"].append(day.options[ce][:, lo:hi])
                        source_arrays["PE"].append(day.options[pe][:, lo:hi])
                    if not complete_group or any(not source_arrays[t] for t in ("CE", "PE")):
                        valid.append(False)
                        above.append(False)
                        below.append(False)
                        continue
                    flags = {}
                    for typ in ("CE", "PE"):
                        values = np.concatenate(source_arrays[typ], axis=1)
                        vw = vwap(values[H], values[L], values[C], values[V])[-1]
                        last_close = values[C, -1]
                        flags[typ] = (last_close > vw * (1 + ind.buffer_pct / 100.0),
                                      last_close < vw * (1 - ind.buffer_pct / 100.0))
                    valid.append(True)
                    above.append(flags["CE"][0] and not flags["PE"][0])
                    below.append(flags["PE"][0] and not flags["CE"][0])
            above_arr, below_arr = _confirmed(np.asarray(above), ind.confirm_bars), \
                _confirmed(np.asarray(below), ind.confirm_bars)
            bar_signal = _carry_state(np.where(above_arr, 1, np.where(below_arr, -1, 0)))
            bar_valid = np.asarray(valid, dtype=bool)

    last_intraday_group = ({group[0][0]: index for index, group in enumerate(groups)}
                           if timeframe.endswith("m") else {})
    output: Dict[date, IndicatorSignalData] = {}
    for di, day in enumerate(days):
        bucket_indices = bucket_map[di] - 1
        last_group_index = last_intraday_group.get(di)
        if last_group_index is not None:
            tail_start = groups[last_group_index][0][2]
            # The last complete candle's signal remains active in any shorter session tail.
            bucket_indices[tail_start:] = last_group_index
        has_closed = bucket_indices >= 0
        sig = np.zeros(N_BARS, dtype=np.int8)
        valid = np.zeros(N_BARS, dtype=bool)
        sig[has_closed] = bar_signal[bucket_indices[has_closed]]
        valid[has_closed] = bar_valid[bucket_indices[has_closed]]
        output[day.trade_date] = (sig, valid, locked_by_date.get(day.trade_date), errors[day.trade_date])
    return output


def _indicator_group_curves(days: List[DayData], indicator, timeframe: str, groups,
                            high: np.ndarray, low: np.ndarray, close: np.ndarray):
    curves: List[Tuple[str, np.ndarray]] = []
    if isinstance(indicator, MACDParams):
        return curves, f"MACD({indicator.fast_period}, {indicator.slow_period}, {indicator.signal_period}) · {timeframe}"
    if isinstance(indicator, EMAParams):
        if indicator.source is EMASource.OPTION:
            return curves, f"Option-premium EMA · {timeframe}"
        if indicator.mode is EMASignalMode.EMA_CROSSOVER:
            curves.extend([
                (f"EMA({indicator.fast_period})", _moving_average(
                    close, indicator.fast_period, MovingAverageType.EMA)),
                (f"EMA({indicator.slow_period})", _moving_average(
                    close, indicator.slow_period, MovingAverageType.EMA)),
            ])
        else:
            curves.append((f"EMA({indicator.period})", _moving_average(
                close, indicator.period, MovingAverageType.EMA)))
            if indicator.slope_filter:
                curves.append((f"{indicator.slope_type.value}({indicator.slope_period})",
                               _moving_average(close, indicator.slope_period, indicator.slope_type)))
        return curves, f"EMA · {timeframe}"
    if isinstance(indicator, SupertrendParams):
        line = np.full(len(groups), np.nan)
        if timeframe.endswith("m") and not indicator.use_prev_day:
            groups_by_day: Dict[int, List[int]] = {}
            for gi, group in enumerate(groups):
                groups_by_day.setdefault(group[0][0], []).append(gi)
            for indices in groups_by_day.values():
                _, local_line = supertrend(high[indices], low[indices], close[indices],
                                           indicator.period, indicator.multiplier)
                line[indices] = local_line
        else:
            line, _ = supertrend(high, low, close, indicator.period, indicator.multiplier)
        curves.append((f"Supertrend({indicator.period}, x{indicator.multiplier:g})", line))
        return curves, f"Supertrend · {timeframe}"
    if indicator.source is VwapSource.PREMIUM:
        return curves, f"Option-premium VWAP · {timeframe}"
    line = np.full(len(groups), np.nan)
    if timeframe.endswith("m"):
        groups_by_day: Dict[int, List[int]] = {}
        for gi, group in enumerate(groups):
            groups_by_day.setdefault(group[0][0], []).append(gi)
        for indices in groups_by_day.values():
            line[indices] = vwap(high[indices], low[indices], close[indices],
                                 np.zeros(len(indices)))
    else:
        for gi, group in enumerate(groups):
            h = np.concatenate([days[di].spot[H, lo:hi] for di, lo, hi in group])
            l = np.concatenate([days[di].spot[L, lo:hi] for di, lo, hi in group])
            c = np.concatenate([days[di].spot[C, lo:hi] for di, lo, hi in group])
            line[gi] = vwap(h, l, c, np.zeros(c.size))[-1]
    curves.append(("VWAP", line))
    return curves, f"VWAP · {timeframe}"


def indicator_signal_values_for_days(days: List[DayData], cfg: SignalStrategyConfig
                                     ) -> Dict[date, np.ndarray]:
    """Return the signed indicator reading available at each minute, using closed bars only."""
    if not days:
        return {}

    indicator = cfg.indicator
    timeframe = indicator_timeframe(indicator)
    groups, bucket_map = _timeframe_groups(days, timeframe)
    highs, lows, closes = [], [], []
    for group in groups:
        last_day, _, last_bar = group[-1]
        highs.append(max(float(days[di].spot[H, lo:hi].max()) for di, lo, hi in group))
        lows.append(min(float(days[di].spot[L, lo:hi].min()) for di, lo, hi in group))
        closes.append(float(days[last_day].spot[C, last_bar - 1]))
    high, low, close = np.asarray(highs), np.asarray(lows), np.asarray(closes)

    if isinstance(indicator, MACDParams):
        macd, signal = _macd_curves(close, indicator)
        readings = macd - signal
    elif isinstance(indicator, EMAParams):
        if indicator.source is EMASource.OPTION:
            return {day.trade_date: np.full(N_BARS, np.nan) for day in days}
        if indicator.mode is EMASignalMode.EMA_CROSSOVER:
            fast = _moving_average(close, indicator.fast_period, MovingAverageType.EMA)
            slow = _moving_average(close, indicator.slow_period, MovingAverageType.EMA)
            readings = fast - slow
        else:
            average = _moving_average(close, indicator.period, MovingAverageType.EMA)
            readings = close - average
    elif isinstance(indicator, SupertrendParams):
        if timeframe.endswith("m") and not indicator.use_prev_day:
            line = np.full(len(groups), np.nan)
            groups_by_day: Dict[int, List[int]] = {}
            for gi, group in enumerate(groups):
                groups_by_day.setdefault(group[0][0], []).append(gi)
            for indices in groups_by_day.values():
                line[indices], _ = supertrend(high[indices], low[indices], close[indices],
                                              indicator.period, indicator.multiplier)
        else:
            line, _ = supertrend(high, low, close, indicator.period, indicator.multiplier)
        readings = close - line
    elif indicator.source is VwapSource.SPOT:
        lines, _ = _indicator_group_curves(days, indicator, timeframe, groups,
                                           high, low, close)
        readings = close - lines[0][1]
    else:
        return {day.trade_date: np.full(N_BARS, np.nan) for day in days}

    output = {}
    for day_index, day in enumerate(days):
        buckets = bucket_map[day_index]
        available_indices = buckets - 1
        if timeframe.endswith("m") and np.any(buckets < 0) and np.any(buckets >= 0):
            available_indices[buckets < 0] = int(buckets[buckets >= 0].max())
        values = np.full(N_BARS, np.nan)
        valid = (available_indices >= 0) & (available_indices < readings.size)
        values[valid] = readings[available_indices[valid]]
        output[day.trade_date] = values
    return output


def indicator_ema_values_for_days(days: List[DayData], cfg: SignalStrategyConfig
                                  ) -> Dict[date, Dict[str, np.ndarray]]:
    """Return the closed-bar EMA line values available at each minute for spot EMA signals."""
    indicator = cfg.indicator
    if not days or not isinstance(indicator, EMAParams) or indicator.source is EMASource.OPTION:
        return {}

    timeframe = indicator_timeframe(indicator)
    groups, bucket_map = _timeframe_groups(days, timeframe)
    closes = []
    for group in groups:
        last_day, _, last_bar = group[-1]
        closes.append(float(days[last_day].spot[C, last_bar - 1]))
    close = np.asarray(closes)
    lines = {"spot": close}
    if indicator.mode is EMASignalMode.EMA_CROSSOVER:
        lines.update({
            "fast_ema": _moving_average(close, indicator.fast_period, MovingAverageType.EMA),
            "slow_ema": _moving_average(close, indicator.slow_period, MovingAverageType.EMA),
        })
    else:
        lines["ema"] = _moving_average(close, indicator.period, MovingAverageType.EMA)

    output = {}
    for day_index, day in enumerate(days):
        buckets = bucket_map[day_index]
        available_indices = buckets - 1
        if timeframe.endswith("m") and np.any(buckets < 0) and np.any(buckets >= 0):
            available_indices[buckets < 0] = int(buckets[buckets >= 0].max())
        valid = (available_indices >= 0) & (available_indices < close.size)
        day_values = {}
        for name, line in lines.items():
            values = np.full(N_BARS, np.nan)
            values[valid] = line[available_indices[valid]]
            day_values[name] = values
        output[day.trade_date] = day_values
    return output


def indicator_premium_signal_values_for_days(days: List[DayData], cfg: SignalStrategyConfig,
                                            spec: InstrumentSpec, resolve
                                            ) -> Dict[date, Dict[str, np.ndarray]]:
    """Return option-premium indicator readings for the CE and PE streams."""
    indicator = cfg.indicator
    is_option_ema = isinstance(indicator, EMAParams) and indicator.source is EMASource.OPTION
    is_premium_vwap = isinstance(indicator, VwapParams) and indicator.source is VwapSource.PREMIUM
    if not (is_option_ema or is_premium_vwap):
        raise ValueError("premium signal readings require option EMA or premium VWAP")
    if not days:
        return {}

    timeframe = indicator_timeframe(indicator)
    groups, bucket_map = _timeframe_groups(days, timeframe)
    contracts: Dict[int, Dict[str, Optional[OptionKey]]] = {}
    for di, day in enumerate(days):
        expiry = day.selected_expiry or select_expiry(day, cfg.expiry)
        contracts[di] = {}
        for typ, opt_type in (("CE", OptionType.CE), ("PE", OptionType.PE)):
            key = (resolve(cfg.strike, opt_type, day, expiry, bar_index(cfg.start_time),
                           spec, O, cfg.greek) if expiry is not None else None)
            contracts[di][typ] = key if key in day.options else None

    readings = {typ: np.full(len(groups), np.nan) for typ in ("CE", "PE")}
    if is_option_ema:
        closes = {typ: np.full(len(groups), np.nan) for typ in ("CE", "PE")}
        for gi, group in enumerate(groups):
            for di, _, hi in group:
                for typ in ("CE", "PE"):
                    key = contracts[di][typ]
                    if key is not None:
                        closes[typ][gi] = float(days[di].options[key][C, hi - 1])
        for typ in ("CE", "PE"):
            close = closes[typ]
            if indicator.mode is EMASignalMode.EMA_CROSSOVER:
                fast = _moving_average(close, indicator.fast_period, MovingAverageType.EMA)
                slow = _moving_average(close, indicator.slow_period, MovingAverageType.EMA)
                readings[typ] = fast - slow
            else:
                average = _moving_average(close, indicator.period, MovingAverageType.EMA)
                readings[typ] = close - average
    elif timeframe.endswith("m"):
        groups_by_day: Dict[int, List[int]] = {}
        for gi, group in enumerate(groups):
            groups_by_day.setdefault(group[0][0], []).append(gi)
        for di, indices in groups_by_day.items():
            for typ in ("CE", "PE"):
                bars = []
                for gi in indices:
                    _, lo, hi = groups[gi][0]
                    key = contracts[di][typ]
                    if key is None:
                        bars = []
                        break
                    values = days[di].options[key]
                    bars.append((float(values[H, lo:hi].max()), float(values[L, lo:hi].min()),
                                 float(values[C, hi - 1]), float(values[V, lo:hi].sum())))
                if bars:
                    values = np.asarray(bars)
                    average = vwap(values[:, 0], values[:, 1], values[:, 2], values[:, 3])
                    readings[typ][indices] = values[:, 2] - average
    else:
        for gi, group in enumerate(groups):
            for typ in ("CE", "PE"):
                parts = []
                for di, lo, hi in group:
                    key = contracts[di][typ]
                    if key is None:
                        parts = []
                        break
                    parts.append(days[di].options[key][:, lo:hi])
                if parts:
                    values = np.concatenate(parts, axis=1)
                    average = vwap(values[H], values[L], values[C], values[V])[-1]
                    readings[typ][gi] = values[C, -1] - average

    output: Dict[date, Dict[str, np.ndarray]] = {}
    for di, day in enumerate(days):
        buckets = bucket_map[di]
        available_indices = buckets - 1
        if timeframe.endswith("m") and np.any(buckets < 0) and np.any(buckets >= 0):
            available_indices[buckets < 0] = int(buckets[buckets >= 0].max())
        valid = (available_indices >= 0) & (available_indices < len(groups))
        output[day.trade_date] = {}
        for typ in ("CE", "PE"):
            values = np.full(N_BARS, np.nan)
            values[valid] = readings[typ][available_indices[valid]]
            output[day.trade_date][typ] = values
    return output


def indicator_backtest_chart_data(days: List[DayData], cfg: SignalStrategyConfig
                                  ) -> Dict[str, object]:
    """Build native-timeframe candles and spot-indicator curves for a backtest."""
    indicator = cfg.indicator
    timeframe = indicator_timeframe(indicator)
    groups, _ = _timeframe_groups(days, timeframe)
    all_chart_groups, all_bucket_map = _timeframe_groups(days, timeframe, include_partial=True)
    chart_group_indices = [
        index for index, group in enumerate(all_chart_groups)
        if (cfg.start_date is None or days[group[-1][0]].trade_date >= cfg.start_date)
        and (cfg.end_date is None or days[group[-1][0]].trade_date <= cfg.end_date)
    ]
    chart_groups = [all_chart_groups[index] for index in chart_group_indices]

    def aggregate(selected_groups):
        highs, lows, closes, bars = [], [], [], []
        for group in selected_groups:
            first_day, first_bar, _ = group[0]
            last_day, _, last_bar = group[-1]
            high = max(float(days[di].spot[H, lo:hi].max()) for di, lo, hi in group)
            low = min(float(days[di].spot[L, lo:hi].min()) for di, lo, hi in group)
            close = float(days[last_day].spot[C, last_bar - 1])
            minute_of_day = 555 + last_bar - 1
            highs.append(high)
            lows.append(low)
            closes.append(close)
            bars.append({
                "date": days[last_day].trade_date.isoformat(),
                "time": f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}",
                "open": float(days[first_day].spot[O, first_bar]),
                "high": high, "low": low, "close": close,
            })
        return np.asarray(highs), np.asarray(lows), np.asarray(closes), bars

    _, _, _, bars = aggregate(chart_groups)
    base_high, base_low, base_close, _ = aggregate(groups)
    base_curves, label = _indicator_group_curves(
        days, indicator, timeframe, groups, base_high, base_low, base_close)
    chart_lookup = {tuple(group): index for index, group in enumerate(chart_groups)}
    base_group_keys = {tuple(group) for group in groups}
    pane = None
    if isinstance(indicator, MACDParams):
        macd, signal = _macd_curves(base_close, indicator)

        def expand_pane(values):
            expanded = [None] * len(chart_groups)
            last_value_by_day = {}
            for group_index, group in enumerate(groups):
                value = float(values[group_index])
                if np.isfinite(value) and timeframe.endswith("m"):
                    last_value_by_day[group[-1][0]] = value
                chart_index = chart_lookup.get(tuple(group))
                if chart_index is not None and np.isfinite(value):
                    expanded[chart_index] = value
            if timeframe.endswith("m"):
                for chart_index, group in enumerate(chart_groups):
                    if tuple(group) in base_group_keys or group[0][2] != N_BARS:
                        continue
                    value = last_value_by_day.get(group[0][0])
                    if value is not None:
                        expanded[chart_index] = value
            return expanded

        pane = {
            "label": label,
            "lines": [
                {"label": "MACD", "values": expand_pane(macd)},
                {"label": "Signal", "values": expand_pane(signal)},
            ],
            "histogram": expand_pane(macd - signal),
        }
    lines = []
    for name, values in base_curves:
        expanded = [None] * len(chart_groups)
        last_value_by_day = {}
        for group_index, group in enumerate(groups):
            value = float(values[group_index])
            if np.isfinite(value) and timeframe.endswith("m"):
                last_value_by_day[group[-1][0]] = value
            chart_index = chart_lookup.get(tuple(group))
            if chart_index is not None and np.isfinite(value):
                expanded[chart_index] = value
        if timeframe.endswith("m"):
            for chart_index, group in enumerate(chart_groups):
                if tuple(group) in base_group_keys or group[0][2] != N_BARS:
                    continue
                value = last_value_by_day.get(group[0][0])
                if value is not None:
                    expanded[chart_index] = value
        lines.append({"label": name, "values": expanded})

    minute_bar_map = []
    chart_index_map = {old: new for new, old in enumerate(chart_group_indices)}
    for day_index, mapping in enumerate(all_bucket_map):
        visible_date = ((cfg.start_date is None or days[day_index].trade_date >= cfg.start_date)
                        and (cfg.end_date is None or days[day_index].trade_date <= cfg.end_date))
        minute_bar_map.append([
            chart_index_map.get(int(group_index), -1) if visible_date and group_index >= 0 else -1
            for group_index in mapping
        ])
    return {
        "timeframe": timeframe, "label": label,
        "source": "option" if (
            isinstance(indicator, EMAParams) and indicator.source is EMASource.OPTION
            or isinstance(indicator, VwapParams) and indicator.source is VwapSource.PREMIUM
        ) else "spot",
        "bars": bars, "lines": lines, "pane": pane,
        "minute_bar_map": minute_bar_map,
    }


def indicator_display_for_day(days: List[DayData], cfg: SignalStrategyConfig,
                              trade_date: date) -> Dict[str, object]:
    """Return closed-candle spot indicator curves expanded to the selected session's minutes."""
    indicator = cfg.indicator
    if isinstance(indicator, EMAParams) and indicator.source is EMASource.OPTION:
        return {"label": "Option-premium EMA", "lines": [], "source": "option"}
    if isinstance(indicator, VwapParams) and indicator.source is VwapSource.PREMIUM:
        return {"label": "Option-premium VWAP", "lines": [], "source": "option"}

    target = next((i for i, day in enumerate(days) if day.trade_date == trade_date), None)
    if target is None:
        return {"label": "Indicator unavailable", "lines": [], "source": "spot"}

    timeframe = indicator_timeframe(indicator)
    groups, bucket_map = _timeframe_groups(days, timeframe)
    highs, lows, closes = [], [], []
    for group in groups:
        last_day, _, last_bar = group[-1]
        highs.append(max(float(days[di].spot[H, lo:hi].max()) for di, lo, hi in group))
        lows.append(min(float(days[di].spot[L, lo:hi].min()) for di, lo, hi in group))
        closes.append(float(days[last_day].spot[C, last_bar - 1]))
    high, low, close = np.asarray(highs), np.asarray(lows), np.asarray(closes)
    curves, label = _indicator_group_curves(days, indicator, timeframe, groups,
                                             high, low, close)
    pane_curves = []
    if isinstance(indicator, MACDParams):
        macd, signal = _macd_curves(close, indicator)
        pane_curves = [("MACD", macd), ("Signal", signal)]

    closed_groups = bucket_map[target] - 1
    lines = []
    for name, values in curves:
        expanded = np.full(N_BARS, np.nan)
        valid = (closed_groups >= 0) & (closed_groups < values.size)
        expanded[valid] = values[closed_groups[valid]]
        lines.append({"label": name, "values": expanded})
    pane_lines = []
    for name, values in pane_curves:
        expanded = np.full(N_BARS, np.nan)
        valid = (closed_groups >= 0) & (closed_groups < values.size)
        expanded[valid] = values[closed_groups[valid]]
        pane_lines.append({"label": name, "values": expanded})
    pane_histogram = []
    if isinstance(indicator, MACDParams):
        macd, signal = _macd_curves(close, indicator)
        histogram = macd - signal
        expanded = np.full(N_BARS, np.nan)
        valid = (closed_groups >= 0) & (closed_groups < histogram.size)
        expanded[valid] = histogram[closed_groups[valid]]
        pane_histogram = expanded
    display = {"label": label, "lines": lines, "pane_lines": pane_lines,
               "pane_histogram": pane_histogram, "source": "spot"}
    if isinstance(indicator, EMAParams) and indicator.source is EMASource.UNDERLYING:
        signal, signal_valid = _ema_bar_signals(close, indicator)
        target_groups = np.unique(bucket_map[target][bucket_map[target] >= 0])
        if target_groups.size:
            target_start, target_end = int(target_groups[0]), int(target_groups[-1])
            chart_start = max(0, target_start - 300)
            chart_groups = range(chart_start, target_end + 1)
            chart_bars = []
            chart_index = {}
            for chart_index_value, group_index in enumerate(chart_groups):
                group = groups[group_index]
                first_day, first_bar, _ = group[0]
                last_day, _, last_bar = group[-1]
                chart_index[group_index] = chart_index_value
                chart_bars.append({
                    "date": days[last_day].trade_date.isoformat(),
                    "time": last_bar - 1,
                    "label": f"{days[last_day].trade_date.isoformat()} "
                             f"{(555 + last_bar - 1) // 60:02d}:{(555 + last_bar - 1) % 60:02d}",
                    "open": float(days[first_day].spot[O, first_bar]),
                    "high": high[group_index],
                    "low": low[group_index],
                    "close": close[group_index],
                    "selected": any(di == target for di, _, _ in group),
                })
            changes = []
            first_relevant = max(1, target_start - 1)
            for group_index in range(first_relevant, target_end + 1):
                previous = group_index - 1
                direction = int(signal[group_index])
                if (not signal_valid[previous] or not signal_valid[group_index]
                        or direction == 0 or direction == int(signal[previous])):
                    continue
                group = groups[group_index]
                last_day, _, last_bar = group[-1]
                changes.append({
                    "index": chart_index[group_index],
                    "direction": direction,
                    "reversal": int(signal[previous]) == -direction,
                    "date": days[last_day].trade_date.isoformat(),
                    "time": last_bar - 1,
                })
            display["verification"] = {
                "timeframe": timeframe,
                "mode": indicator.mode.value,
                "bars": chart_bars,
                "lines": [{"label": name, "values": [float(values[i]) for i in chart_groups]}
                          for name, values in curves],
                "changes": changes,
                "selected_date": trade_date.isoformat(),
            }
    return display


def compute_option_ema_signals_for_days(days: List[DayData], cfg: SignalStrategyConfig,
                                       spec: InstrumentSpec, resolve
                                       ) -> Dict[date, OptionEMASignalData]:
    """Build closed-candle EMA states for each session's selected CE and PE contracts."""
    ind = cfg.indicator
    if not isinstance(ind, EMAParams) or ind.source is not EMASource.OPTION:
        raise ValueError("option EMA signals require EMAParams with OPTION source")
    timeframe = indicator_timeframe(ind)
    groups, bucket_map = _timeframe_groups(days, timeframe)
    contract_by_day: Dict[int, Dict[str, Optional[OptionKey]]] = {}
    errors_by_day: Dict[int, Dict[str, Optional[str]]] = {}
    group_closes = {typ: np.full(len(groups), np.nan) for typ in ("CE", "PE")}

    for di, day in enumerate(days):
        expiry = day.selected_expiry or select_expiry(day, cfg.expiry)
        day_contracts: Dict[str, Optional[OptionKey]] = {"CE": None, "PE": None}
        errors: Dict[str, Optional[str]] = {"CE": None, "PE": None}
        if expiry is None:
            errors = {"CE": f"no {cfg.expiry.value} expiry in data",
                      "PE": f"no {cfg.expiry.value} expiry in data"}
        else:
            for typ, opt_type in (("CE", OptionType.CE), ("PE", OptionType.PE)):
                key = resolve(cfg.strike, opt_type, day, expiry, bar_index(cfg.start_time), spec, O)
                if key is None or key not in day.options:
                    errors[typ] = f"option EMA: {typ} contract unavailable at start_time"
                else:
                    day_contracts[typ] = key
        contract_by_day[di], errors_by_day[di] = day_contracts, errors

    for gi, group in enumerate(groups):
        for di, _, hi in group:
            for typ in ("CE", "PE"):
                key = contract_by_day[di][typ]
                if key is not None:
                    group_closes[typ][gi] = float(days[di].options[key][C, hi - 1])

    signal_arrays = {}
    for typ in ("CE", "PE"):
        close = group_closes[typ]
        if ind.mode is EMASignalMode.EMA_CROSSOVER:
            fast = _moving_average(close, ind.fast_period, MovingAverageType.EMA)
            slow = _moving_average(close, ind.slow_period, MovingAverageType.EMA)
            valid = np.isfinite(fast) & np.isfinite(slow)
            raw = np.where(fast > slow, 1, np.where(fast < slow, -1, 0)).astype(np.int8)
        else:
            reference = _moving_average(close, ind.period, MovingAverageType.EMA)
            valid = np.isfinite(close) & np.isfinite(reference)
            above, below = valid & (close > reference), valid & (close < reference)
            if ind.slope_filter:
                slope = _moving_average(close, ind.slope_period, ind.slope_type)
                rising = np.isfinite(slope) & (slope > np.roll(slope, 1))
                falling = np.isfinite(slope) & (slope < np.roll(slope, 1))
                rising[0] = falling[0] = False
                above &= rising
                below &= falling
            raw = np.where(above, 1, np.where(below, -1, 0)).astype(np.int8)
        signal_arrays[typ] = raw, valid

    output: Dict[date, OptionEMASignalData] = {}
    for di, day in enumerate(days):
        bucket_indices = bucket_map[di] - 1
        has_closed = bucket_indices >= 0
        streams: OptionEMASignalData = {}
        for typ in ("CE", "PE"):
            raw, bar_valid = signal_arrays[typ]
            sig = np.zeros(N_BARS, dtype=np.int8)
            valid = np.zeros(N_BARS, dtype=bool)
            sig[has_closed] = raw[bucket_indices[has_closed]]
            valid[has_closed] = bar_valid[bucket_indices[has_closed]]
            contract_key = contract_by_day[di][typ]
            locked: Optional[Dict[str, OptionKey]] = None
            if contract_key is not None:
                locked = {typ: contract_key}
            streams[typ] = (sig, valid, locked, errors_by_day[di][typ])
        output[day.trade_date] = streams
    return output
