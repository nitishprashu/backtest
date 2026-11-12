"""Instrument specifications and registry.

Everything that differs between underlyings lives here, so the engine never
mentions "NIFTY" directly. Only NIFTY is registered by default, and only with
values that were checked against exchange / broker circulars. Register other
underlyings yourself with values you have verified.

Rules encoded for NIFTY (checked against NSE circular summaries, Oct 2026):
* LOT SIZE is a property of the CONTRACT, so it is keyed by the contract's expiry
  date, not by the trade date: existing contracts keep their old lot until they expire.
    2021-08-01 .. 2024-04-25                  : 50
    2024-04-26 .. 2024-12-19                  : 25
    2024-12-26 and 2025-01-30 (old monthlies) : 25   (exceptions in the circular)
    2024-12-20 .. 2025-12-30 (other expiries) : 75
    expiry >= 2026-01-06                      : 65
* STT on options (sell side, on premium): 0.0625% from 2023-04-01, 0.10% from 2024-10-01,
  0.15% from 2026-04-01.
* NSE transaction charge on options (premium turnover, both sides): 0.05% from 2023-04-01,
  0.0495% from 2024-04-01, 0.03503% from 2024-10-01.
* NSE IPFT: Rs 50 per crore of premium. SEBI fee: Rs 10 per crore. Stamp duty: 0.003% on buys.
  GST 18% on brokerage + transaction charge + SEBI fee + IPFT.
* Brokerage is broker/plan specific and is NOT assumed: it defaults to 0. Set it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, List, Optional, Tuple

Schedule = Tuple[Tuple[date, float], ...]


def _pick(schedule, d: date):
    """Use the dated value, falling back to the latest value before schedule coverage begins."""
    value = None
    for eff, v in schedule:
        if d >= eff:
            value = v
        else:
            break
    return value if value is not None else (schedule[-1][1] if schedule else None)


NIFTY_OPTIONS_STT: Schedule = (
    (date(2023, 4, 1), 0.000625),
    (date(2024, 10, 1), 0.001),
    (date(2026, 4, 1), 0.0015),
)
NSE_OPTIONS_TXN: Schedule = (
    (date(2023, 4, 1), 0.0005),
    (date(2024, 4, 1), 0.000495),
    (date(2024, 10, 1), 0.0003503),
)
NIFTY_LOT_BY_EXPIRY: Tuple[Tuple[date, int], ...] = (
    (date(2021, 8, 1), 50),
    (date(2024, 4, 26), 25),
    (date(2024, 12, 20), 75),
    (date(2026, 1, 6), 65),
)
NIFTY_LOT_EXCEPTIONS: Tuple[Tuple[date, int], ...] = (
    (date(2024, 12, 26), 25),     # December 2024 monthly: existing contract, old lot
    (date(2025, 1, 30), 25),      # January 2025 monthly: last monthly with the old lot
)


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    strike_step: int
    exchange: str = "NFO"
    spot_exchange: str = "NSE"
    lot_size_by_expiry: Tuple[Tuple[date, int], ...] = ()
    lot_size_exceptions: Tuple[Tuple[date, int], ...] = ()
    fixed_lot_size: Optional[int] = None      # use instead of the schedule for a constant lot
    brokerage_per_order: float = 0.0          # NOT assumed - set from your broker plan
    brokerage_cap_pct: Optional[float] = None
    stt_schedule: Schedule = NIFTY_OPTIONS_STT
    exchange_txn_schedule: Schedule = NSE_OPTIONS_TXN
    ipft_rate: float = 50 / 1e7
    sebi_rate: float = 10 / 1e7
    stamp_rate: float = 0.00003
    gst_rate: float = 0.18

    def lot_size_for(self, expiry: date) -> int:
        for d, lot in self.lot_size_exceptions:
            if d == expiry:
                return lot
        if self.fixed_lot_size is not None:
            return self.fixed_lot_size
        lot = _pick(self.lot_size_by_expiry, expiry)
        if lot is None:
            raise ValueError(f"{self.symbol}: no verified lot size for expiry {expiry}")
        return int(lot)

    def stt_rate_on(self, d: date) -> float:
        v = _pick(self.stt_schedule, d)
        if v is None:
            raise ValueError(f"{self.symbol}: no verified STT rate for {d}")
        return v

    def txn_rate_on(self, d: date) -> float:
        v = _pick(self.exchange_txn_schedule, d)
        if v is None:
            raise ValueError(f"{self.symbol}: no verified exchange charge for {d}")
        return v


class InstrumentRegistry:
    def __init__(self, specs: Iterable[InstrumentSpec] = ()):
        self._specs = {}
        for s in specs:
            self.register(s)

    def register(self, spec: InstrumentSpec, overwrite: bool = False) -> None:
        key = spec.symbol.upper()
        if key in self._specs and not overwrite:
            raise ValueError(f"{key} already registered (pass overwrite=True)")
        self._specs[key] = spec

    def get(self, symbol: str) -> InstrumentSpec:
        try:
            return self._specs[symbol.upper()]
        except KeyError:
            raise KeyError(f"{symbol!r} is not registered (registered: {self.symbols()}). "
                           "Register an InstrumentSpec with values you have verified.") from None

    def symbols(self) -> List[str]:
        return sorted(self._specs)

    def __contains__(self, symbol: str) -> bool:
        return symbol.upper() in self._specs


NIFTY = InstrumentSpec(symbol="NIFTY", strike_step=50,
                       lot_size_by_expiry=NIFTY_LOT_BY_EXPIRY, lot_size_exceptions=NIFTY_LOT_EXCEPTIONS)

DEFAULT_REGISTRY = InstrumentRegistry([NIFTY])
