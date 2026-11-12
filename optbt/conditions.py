"""Entry-condition evaluation (all conditions are ANDed). Spot-based arrays are built once per session;
Greek-based conditions are evaluated lazily and only for the minutes that survive the cheap checks."""
from __future__ import annotations

from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .data import C, N_BARS, O, DayData
from .greeks import GreekSettings, greeks_at
from .indicators import vwap
from .instruments import InstrumentSpec
from .schemas import EntryCondition, Side


def _cmp(op: str, x: float, a: float, b: Optional[float]) -> bool:
    if not np.isfinite(x):
        return False
    if op == ">=":
        return x >= a
    if op == "<=":
        return x <= a
    if op == "between":
        return a <= x <= float(b)
    if op == "abs<=":
        return abs(x) <= a
    raise ValueError(op)


class CondEval:
    def __init__(self, day: DayData, cfg, spec: InstrumentSpec, expiry: date, fidx: int, resolve: Callable):
        self.day, self.cfg, self.spec, self.expiry, self.fidx, self.resolve = day, cfg, spec, expiry, fidx, resolve
        self.gs: GreekSettings = cfg.greek
        self._vwap: Optional[np.ndarray] = None
        self._st: Dict[Tuple, Tuple[np.ndarray, np.ndarray]] = {}
        self.qty_lot = spec.lot_size_for(expiry)

    # ---- spot based
    def _vwap_arr(self) -> np.ndarray:
        if self._vwap is None:
            sp = self.day.spot
            self._vwap = vwap(sp[1], sp[2], sp[3], np.zeros(N_BARS))
        return self._vwap

    def _supertrend(self, c: EntryCondition):
        from .signals import SupertrendParams, _supertrend_signals

        k = (c.st_period, c.st_mult, c.st_tf)
        if k not in self._st:
            self._st[k] = _supertrend_signals(self.day, SupertrendParams(int(c.st_period), float(c.st_mult), int(c.st_tf), True))
        return self._st[k]

    # ---- Greek based
    def _net(self, j: int, what: str) -> float:
        total = 0.0
        for leg in self.cfg.legs:
            key = self.resolve(leg.strike, leg.option_type, self.day, self.expiry, j, self.spec, self.fidx, self.gs)
            if key is None:
                return float("nan")
            g = greeks_at(self.day, key, j, self.fidx, self.gs)
            s = 1 if leg.side is Side.BUY else -1
            if what == "net_delta":
                total += s * leg.lots * g["delta"]
            elif what == "net_theta":
                total += s * leg.lots * self.qty_lot * g["theta"]
            else:
                total += s * leg.lots * self.qty_lot * g["vega"]
        return total

    def _atm_iv(self, j: int) -> float:
        spot = self.day.spot[self.fidx, j]
        best, bd = None, 1e18
        for k, arr in self.day.options.items():
            if k[0] == self.expiry and k[2] == "CE" and (self.expiry, k[1], "PE") in self.day.options:
                d = abs(k[1] - spot)
                if d < bd and np.isfinite(arr[self.fidx, j]) and arr[self.fidx, j] > 0 \
                        and np.isfinite(self.day.options[(self.expiry, k[1], "PE")][self.fidx, j]):
                    best, bd = k[1], d
        if best is None:
            return float("nan")
        ce = greeks_at(self.day, (self.expiry, best, "CE"), j, self.fidx, self.gs)["iv"]
        pe = greeks_at(self.day, (self.expiry, best, "PE"), j, self.fidx, self.gs)["iv"]
        return float(np.nanmean([ce, pe])) if np.isfinite(ce) or np.isfinite(pe) else float("nan")

    def check(self, c: EntryCondition, j: int) -> bool:
        k = c.kind
        if k == "spot_vs_vwap":
            if j < 1:
                return False
            v, s = self._vwap_arr()[j - 1], self.day.spot[self.fidx, j]
            return bool(np.isfinite(v) and np.isfinite(s) and (s > v if c.value == "above" else s < v))
        if k == "spot_move":
            o = self.day.spot[O, 0]
            return _cmp(c.op, (self.day.spot[self.fidx, j] - o) / o * 100.0, float(c.value), c.value2)
        if k == "supertrend":
            sig, valid = self._supertrend(c)
            return bool(valid[j] and sig[j] == (1 if c.value == "up" else -1))
        if k == "atm_iv":
            return _cmp(c.op, self._atm_iv(j), float(c.value), c.value2)
        return _cmp(c.op, self._net(j, k), float(c.value), c.value2)     # net_delta / net_theta / net_vega


def find_entry_bar(day: DayData, cfg, spec: InstrumentSpec, expiry: date, ei0: int, xi: int, fidx: int,
                   resolve: Callable) -> Tuple[Optional[int], Optional[str]]:
    """First minute in [entry_time, entry_window_end] where every entry condition holds."""
    if not cfg.entry_conditions:
        return ei0, None
    from .data import bar_index
    last = bar_index(cfg.entry_window_end) if cfg.entry_window_end else ei0
    last = min(max(last, ei0), xi - 1)
    ev = CondEval(day, cfg, spec, expiry, fidx, resolve)
    # cheap spot conditions first, Greek conditions only on the minutes that pass them
    order = sorted(cfg.entry_conditions, key=lambda c: 1 if c.kind in ("atm_iv", "net_delta", "net_theta", "net_vega") else 0)
    for j in range(ei0, last + 1):
        if all(ev.check(c, j) for c in order):
            return j, None
    return None, f"filter: entry conditions not met between {cfg.entry_time:%H:%M} and {(cfg.entry_window_end or cfg.entry_time):%H:%M}"
