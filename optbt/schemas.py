"""Typed schemas: strategy configuration, trade columns and result container."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .greeks import GreekSettings

MAX_LEGS = 8
GREEK_METRICS = ("delta", "iv", "theta", "vega")
CONDITION_KINDS = ("atm_iv", "net_delta", "net_theta", "net_vega", "spot_vs_vwap", "spot_move", "supertrend")


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class ExpirySelect(str, Enum):
    CURRENT_WEEKLY = "CURRENT_WEEKLY"
    NEXT_WEEKLY = "NEXT_WEEKLY"
    MONTHLY = "MONTHLY"


class StrikeMode(str, Enum):
    ATM_OFFSET = "ATM_OFFSET"
    PREMIUM = "PREMIUM"
    FIXED = "FIXED"
    GREEK = "GREEK"


class ATMReference(str, Enum):
    SPOT = "SPOT"
    SYNTHETIC_FUTURE = "SYNTHETIC_FUTURE"


class ValueType(str, Enum):
    PERCENT = "PERCENT"
    POINTS = "POINTS"


class ReEntryMode(str, Enum):
    NONE = "NONE"
    RE_EXECUTE = "RE_EXECUTE"   # fresh execution at next bar open, strike rule re-evaluated
    RE_COST = "RE_COST"         # same strike, re-enter when premium returns to original cost


class ExitReason(str, Enum):
    SL = "SL"
    TP = "TP"
    TRAILING_SL = "TRAILING_SL"
    PORTFOLIO_SL = "PORTFOLIO_SL"
    PORTFOLIO_TP = "PORTFOLIO_TP"
    PORTFOLIO_TRAILING_SL = "PORTFOLIO_TRAILING_SL"
    EOD_SQUARE_OFF = "EOD_SQUARE_OFF"
    DATA_GAP = "DATA_GAP"                    # required carried-contract data is unavailable
    SIGNAL_EXIT = "SIGNAL_EXIT"          # closed by an opposite indicator signal
    ROLLED = "ROLLED"                    # contract closed and replaced by a later expiry
    EXPIRY = "EXPIRY"                    # carried position closed at contract expiry
    BACKTEST_END = "BACKTEST_END"        # carried position closed at the data/backtest boundary


class HoldingMode(str, Enum):
    INTRADAY = "INTRADAY"
    CARRY_SINGLE = "CARRY_SINGLE"
    CARRY_FRESH_ENTRIES = "CARRY_FRESH_ENTRIES"


class RolloverStrikeMode(str, Enum):
    ORIGINAL = "ORIGINAL"
    RESELECT = "RESELECT"


@dataclass(frozen=True)
class RiskRule:
    """Distance from entry premium, as % of entry or absolute points."""
    value: float
    kind: ValueType = ValueType.PERCENT

    def __post_init__(self):
        if self.value <= 0:
            raise ValueError("RiskRule value must be > 0")

    @classmethod
    def percent(cls, v: float) -> "RiskRule":
        return cls(v, ValueType.PERCENT)

    @classmethod
    def points(cls, v: float) -> "RiskRule":
        return cls(v, ValueType.POINTS)

    def distance(self, entry: float) -> float:
        return entry * self.value / 100.0 if self.kind is ValueType.PERCENT else float(self.value)


@dataclass(frozen=True)
class TrailRule:
    """Lock & trail: for every `trigger` of favourable move, move SL by `step`."""
    trigger: float
    step: float
    kind: ValueType = ValueType.PERCENT

    def __post_init__(self):
        if self.trigger <= 0 or self.step <= 0:
            raise ValueError("TrailRule trigger and step must be > 0")

    def _d(self, v: float, entry: float) -> float:
        return entry * v / 100.0 if self.kind is ValueType.PERCENT else float(v)

    def trigger_distance(self, entry: float) -> float:
        return self._d(self.trigger, entry)

    def step_distance(self, entry: float) -> float:
        return self._d(self.step, entry)


@dataclass(frozen=True)
class ReEntryRule:
    mode: ReEntryMode = ReEntryMode.NONE
    max_count: int = 0

    def __post_init__(self):
        if self.mode is not ReEntryMode.NONE and self.max_count < 1:
            raise ValueError("ReEntryRule needs max_count >= 1 when mode is not NONE")


@dataclass(frozen=True)
class StrikeSelection:
    mode: StrikeMode = StrikeMode.ATM_OFFSET
    offset: int = 0                      # signed strike steps from ATM: strike = ATM + offset*step
    atm_reference: ATMReference = ATMReference.SPOT
    target_premium: Optional[float] = None
    premium_min: Optional[float] = None
    premium_max: Optional[float] = None
    fixed_strike: Optional[float] = None
    greek_metric: str = "delta"
    greek_target: Optional[float] = None
    greek_min: Optional[float] = None
    greek_max: Optional[float] = None

    def __post_init__(self):
        if self.mode is StrikeMode.GREEK:
            if self.greek_metric not in GREEK_METRICS or self.greek_target is None:
                raise ValueError(f"GREEK mode needs greek_metric in {GREEK_METRICS} and greek_target")
        if self.mode is StrikeMode.FIXED and self.fixed_strike is None:
            raise ValueError("FIXED mode needs fixed_strike")
        if self.mode is StrikeMode.PREMIUM and self.target_premium is None \
                and (self.premium_min is None or self.premium_max is None):
            raise ValueError("PREMIUM mode needs target_premium or both premium_min and premium_max")

    @classmethod
    def atm(cls, reference: ATMReference = ATMReference.SPOT) -> "StrikeSelection":
        return cls(atm_reference=reference)

    @classmethod
    def fixed(cls, strike: float) -> "StrikeSelection":
        return cls(mode=StrikeMode.FIXED, fixed_strike=float(strike))

    @classmethod
    def steps(cls, n: int, reference: ATMReference = ATMReference.SPOT) -> "StrikeSelection":
        """ATM+n (n may be negative): raw strike steps, e.g. steps(1) = ATM+1."""
        return cls(offset=n, atm_reference=reference)

    @classmethod
    def otm(cls, option_type: OptionType, n: int) -> "StrikeSelection":
        return cls(offset=n if option_type is OptionType.CE else -n)

    @classmethod
    def itm(cls, option_type: OptionType, n: int) -> "StrikeSelection":
        return cls(offset=-n if option_type is OptionType.CE else n)

    @classmethod
    def premium(cls, target: Optional[float] = None, lo: Optional[float] = None,
                hi: Optional[float] = None) -> "StrikeSelection":
        return cls(mode=StrikeMode.PREMIUM, target_premium=target, premium_min=lo, premium_max=hi)

    @classmethod
    def greek(cls, metric: str, target: float, lo: Optional[float] = None,
              hi: Optional[float] = None) -> "StrikeSelection":
        return cls(mode=StrikeMode.GREEK, greek_metric=metric, greek_target=float(target),
                   greek_min=lo, greek_max=hi)


@dataclass(frozen=True)
class LegConfig:
    side: Side
    option_type: OptionType
    strike: StrikeSelection = field(default_factory=StrikeSelection.atm)
    lots: int = 1
    stop_loss: Optional[RiskRule] = None
    target: Optional[RiskRule] = None
    trail_sl: Optional[TrailRule] = None
    reentry: ReEntryRule = field(default_factory=ReEntryRule)
    leg_id: str = ""
    expiry_offset: int = 0  # 0 = strategy expiry; 1 = next listed expiry

    def __post_init__(self):
        if self.expiry_offset < 0:
            raise ValueError("expiry_offset must be >= 0")
        if self.lots < 1:
            raise ValueError("lots must be >= 1")
        if self.trail_sl is not None and self.stop_loss is None:
            raise ValueError("trail_sl requires stop_loss")
        if self.reentry.mode is not ReEntryMode.NONE and self.stop_loss is None:
            raise ValueError("re-entry is triggered by SL; set stop_loss")


@dataclass(frozen=True)
class PortfolioTrail:
    """Aggregate lock & trail in Rs: for every `trigger` of peak profit, raise the SL by `step`."""
    trigger: float
    step: float

    def __post_init__(self):
        if self.trigger <= 0 or self.step <= 0:
            raise ValueError("PortfolioTrail trigger and step must be > 0")


@dataclass(frozen=True)
class EntryCondition:
    """A condition that must hold before opening the multi-leg strategy."""
    kind: str
    op: str = ">="
    value: object = 0.0
    value2: Optional[float] = None
    st_period: int = 10
    st_mult: float = 3.0
    st_tf: int = 5

    def __post_init__(self):
        if self.kind not in CONDITION_KINDS:
            raise ValueError(f"unknown condition kind {self.kind!r}")
        if self.kind in ("spot_vs_vwap", "supertrend"):
            valid = ("above", "below") if self.kind == "spot_vs_vwap" else ("up", "down")
            if self.value not in valid:
                raise ValueError(f"{self.kind} needs value in {valid}")
            return
        if self.op not in (">=", "<=", "between", "abs<="):
            raise ValueError(f"unknown operator {self.op!r}")
        if self.op == "between" and (self.value2 is None or float(self.value2) < float(self.value)):
            raise ValueError("between needs value2 >= value")
        float(self.value)


@dataclass(frozen=True)
class SlippageModel:
    """Realistic fill slippage without a bid/ask: an estimated half-spread paid on every fill.

    per-unit slippage = max(price * pct / 100, min_ticks * tick)
        x stop_mult   for stop-type exits (SL, trailing SL, portfolio SL): stop-market orders pay more
        x thin_mult   when the fill candle's volume is below `thin_volume` (0 disables): illiquid minute
    Buys fill higher, sells fill lower. Calibrate against your own contract notes.
    """
    pct: float = 0.0
    min_ticks: float = 0.0
    stop_mult: float = 1.0
    thin_volume: float = 0.0
    thin_mult: float = 1.0
    tick: float = 0.05

    def __post_init__(self):
        if min(self.pct, self.min_ticks, self.thin_volume) < 0 or self.stop_mult < 1 or self.thin_mult < 1:
            raise ValueError("invalid SlippageModel (multipliers must be >= 1, others >= 0)")

    def per_unit(self, price: float, volume: float, stop: bool = False) -> float:
        s = max(price * self.pct / 100.0, self.min_ticks * self.tick)
        if stop:
            s *= self.stop_mult
        if self.thin_volume > 0 and not (volume >= self.thin_volume):
            s *= self.thin_mult
        return s

    def fill(self, price: float, is_buy: bool, volume: float, stop: bool = False) -> float:
        s = self.per_unit(price, volume, stop)
        return price + s if is_buy else max(price - s, 0.0)


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    underlying: str
    entry_time: time
    exit_time: time
    legs: Tuple[LegConfig, ...]
    expiry: ExpirySelect = ExpirySelect.CURRENT_WEEKLY
    portfolio_sl: Optional[float] = None       # max loss in Rs (positive number)
    portfolio_tp: Optional[float] = None       # target in Rs (positive number)
    portfolio_trail: Optional[PortfolioTrail] = None
    portfolio_sl_pct: Optional[float] = None   # % of the day's entry premium notional (alternative to Rs)
    portfolio_tp_pct: Optional[float] = None
    slippage_pct: float = 0.0                  # percent of price, applied adversely to every fill
    slippage_model: Optional[SlippageModel] = None   # overrides slippage_pct when given
    dte_min: Optional[int] = None              # trade only when calendar days to expiry >= this
    dte_max: Optional[int] = None              # ... and <= this
    apply_costs: bool = True
    entry_price_field: str = "open"            # "open" (default) or "close" of the entry candle
    exit_price_field: str = "close"            # EOD square-off price field
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    weekdays: Optional[Tuple[int, ...]] = None  # Mon=0..Fri=4; None = all
    capital: Optional[float] = None            # None: no % / ratio metrics (they need a capital base)
    max_expiry_gap_days: Optional[int] = 7     # skip a day if an expiry looks missing; None disables
    portfolio_sl_points: Optional[float] = None  # adverse combined premium move, per strategy lot
    holding_mode: HoldingMode = HoldingMode.INTRADAY
    rollover_strike_mode: RolloverStrikeMode = RolloverStrikeMode.ORIGINAL
    entry_conditions: Tuple[EntryCondition, ...] = ()
    entry_window_end: Optional[time] = None
    greek: GreekSettings = GreekSettings()
    entry_greeks: bool = True
    expiry_day_only: bool = False

    def __post_init__(self):
        if not 1 <= len(self.legs) <= MAX_LEGS:
            raise ValueError(f"legs must be between 1 and {MAX_LEGS}")
        if self.entry_time >= self.exit_time:
            raise ValueError("entry_time must be before exit_time")
        if self.entry_price_field not in ("open", "close") or self.exit_price_field not in ("open", "close"):
            raise ValueError("price fields must be 'open' or 'close'")
        if self.entry_window_end is not None and not self.entry_time <= self.entry_window_end < self.exit_time:
            raise ValueError("entry_window_end must be between entry_time and exit_time")
        sl_rules = (self.portfolio_sl, self.portfolio_sl_pct, self.portfolio_sl_points)
        if self.portfolio_trail is not None and not any(v is not None for v in sl_rules):
            raise ValueError("portfolio_trail requires a strategy stop-loss")
        if sum(v is not None for v in sl_rules) > 1:
            raise ValueError("use only one strategy stop-loss unit")
        for v in (self.portfolio_sl_pct, self.portfolio_tp_pct, self.portfolio_sl_points):
            if v is not None and v <= 0:
                raise ValueError("portfolio risk rules must be > 0")
        if self.portfolio_tp is not None and self.portfolio_tp_pct is not None:
            raise ValueError("use portfolio_tp (Rs) or portfolio_tp_pct, not both")
        if self.slippage_pct < 0:
            raise ValueError("slippage_pct must be >= 0")
        for v in (self.portfolio_sl, self.portfolio_tp):
            if v is not None and v <= 0:
                raise ValueError("portfolio_sl / portfolio_tp are positive Rs amounts")


TRADE_COLUMNS: List[str] = [
    "date", "strategy", "leg_id", "signal_direction", "segment", "expiry", "strike", "option_type", "side",
    "lots", "qty", "entry_time", "entry_price", "signal_value_entry", "signal_ema_entry",
    "signal_fast_ema_entry", "signal_slow_ema_entry", "signal_spot_entry", "spot_entry",
    "exit_time", "exit_price", "signal_value_exit", "signal_ema_exit", "signal_fast_ema_exit",
    "signal_slow_ema_exit", "signal_spot_exit", "spot_exit", "points",
    "gross_pnl", "slippage_cost", "costs", "net_pnl", "exit_reason", "day_mae", "day_mfe",
    "entry_iv", "entry_delta", "entry_gamma", "entry_theta", "entry_vega",
]


def _jsonable(o: Any):
    import numpy as np
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if f != f or f in (float("inf"), float("-inf")) else f
    if isinstance(o, (date, pd.Timestamp)):
        return str(o)
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return o


@dataclass
class BacktestResult:
    config: Any                   # StrategyConfig or SignalStrategyConfig
    trades: pd.DataFrame          # trade audit log, one row per leg segment
    daily: pd.DataFrame           # daily net P&L, equity curve, drawdown
    metrics: Dict[str, Any]
    monthly: pd.DataFrame         # year x month net P&L matrix
    weekday: pd.DataFrame         # day-of-week breakdown
    skipped: List[Tuple[date, str]] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        """Execution payload: everything the spec asks for, as plain Python objects."""
        return {
            "trade_log": self.trades,
            "daily_series": self.daily,
            "metrics": self.metrics,
            "monthly_pnl": self.monthly,
            "weekday_pnl": self.weekday,
            "skipped_days": self.skipped,
        }

    def save(self, out_dir: str) -> None:
        os.makedirs(out_dir, exist_ok=True)
        self.trades.to_csv(os.path.join(out_dir, "trades.csv"), index=False)
        self.daily.to_csv(os.path.join(out_dir, "daily.csv"), index=False)
        self.monthly.to_csv(os.path.join(out_dir, "monthly_pnl.csv"))
        self.weekday.to_csv(os.path.join(out_dir, "weekday_pnl.csv"))
        with open(os.path.join(out_dir, "metrics.json"), "w", encoding="utf-8") as f:
            json.dump(_jsonable(self.metrics), f, indent=2)
        with open(os.path.join(out_dir, "skipped_days.txt"), "w", encoding="utf-8") as f:
            for d, why in self.skipped:
                f.write(f"{d}\t{why}\n")
