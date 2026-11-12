"""Technical indicators on plain NumPy arrays (no pandas, no per-row DataFrame work)."""
from __future__ import annotations

from typing import Tuple

import numpy as np

from .data import N_BARS


def resample_ohlc(spot4: np.ndarray, tf: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """1-minute (4, N_BARS) o/h/l/c -> complete `tf`-minute bars aligned to 09:15.

    A trailing partial bar (when tf does not divide 375) is dropped, so every
    returned bar is fully closed.
    """
    nb = N_BARS // tf
    m = nb * tf
    o = spot4[0, :m].reshape(nb, tf)[:, 0]
    h = spot4[1, :m].reshape(nb, tf).max(axis=1)
    l = spot4[2, :m].reshape(nb, tf).min(axis=1)
    c = spot4[3, :m].reshape(nb, tf)[:, -1]
    return o, h, l, c


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    tr = np.empty(h.size)
    tr[0] = h[0] - l[0]
    if h.size > 1:
        pc = c[:-1]
        tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])
    return tr


def atr_wilder(tr: np.ndarray, period: int) -> np.ndarray:
    """Wilder/RMA ATR seeded with the SMA of the first `period` true ranges."""
    atr = np.full(tr.size, np.nan)
    if tr.size >= period:
        atr[period - 1] = tr[:period].mean()
        for i in range(period, tr.size):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def supertrend(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 10,
               multiplier: float = 3.0) -> Tuple[np.ndarray, np.ndarray]:
    """Classic Supertrend. Returns (line, direction); direction is +1 up / -1 down / 0 before the
    ATR is seeded (first `period - 1` bars). The recurrence is inherently sequential, but it runs
    over a few dozen bars per day, not over minutes x contracts."""
    n = c.size
    line = np.full(n, np.nan)
    direction = np.zeros(n, dtype=np.int8)
    if n < period:
        return line, direction
    atr = atr_wilder(true_range(h, l, c), period)
    hl2 = (h + l) / 2.0
    ub, lb = hl2 + multiplier * atr, hl2 - multiplier * atr
    fu, fl = np.full(n, np.nan), np.full(n, np.nan)
    s = period - 1
    fu[s], fl[s] = ub[s], lb[s]
    direction[s] = 1 if c[s] >= c[0] else -1          # seed: trend over the ATR window
    line[s] = fl[s] if direction[s] == 1 else fu[s]
    for i in range(s + 1, n):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if direction[i - 1] == -1:
            direction[i] = 1 if c[i] > fu[i] else -1
        else:
            direction[i] = -1 if c[i] < fl[i] else 1
        line[i] = fl[i] if direction[i] == 1 else fu[i]
    return line, direction


def vwap(h: np.ndarray, l: np.ndarray, c: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Session-anchored VWAP of typical price. NaN bars carry zero weight.

    If the instrument has no volume at all (e.g. an index), weights fall back to 1 per bar, i.e.
    the cumulative mean of typical price - not a true VWAP.
    """
    tp = (h + l + c) / 3.0
    fin = np.isfinite(tp)
    w = np.where(fin, np.nan_to_num(v), 0.0)
    if w.sum() <= 0:
        w = fin.astype(float)
    num = np.cumsum(np.where(fin, tp * w, 0.0))
    den = np.cumsum(w)
    out = np.full(tp.size, np.nan)
    np.divide(num, den, out=out, where=den > 0)
    return out
