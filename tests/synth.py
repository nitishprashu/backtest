"""Synthetic 1-minute spot + options data (Black-Scholes on a random-walk spot).

For demos and smoke tests only - it has no real-market microstructure, so never
read performance numbers from it as evidence about a strategy.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Tuple

import numpy as np
import pandas as pd

from optbt.data import N_BARS, SESSION_OPEN_MIN


def _ncdf(x: np.ndarray) -> np.ndarray:
    # Abramowitz-Stegun 7.1.26 erf approximation (|err| < 1.5e-7)
    z = np.abs(x) / np.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * z)
    poly = ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t
    erf = 1.0 - poly * np.exp(-z * z)
    return 0.5 * (1.0 + np.where(x >= 0, erf, -erf))


def _bs(S, K, T, sigma, is_call):
    T = np.maximum(T, 1e-7)
    sq = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + 0.5 * sigma ** 2 * T) / sq
    d2 = d1 - sq
    if is_call:
        return S * _ncdf(d1) - K * _ncdf(d2)
    return K * _ncdf(-d2) - S * _ncdf(-d1)


def make_synthetic_market(start: str = "2025-01-01", end: str = "2025-03-31", seed: int = 7,
                          start_spot: float = 23500.0, annual_vol: float = 0.14,
                          strike_step: int = 50, strikes_each_side: int = 25,
                          expiry_weekday: int = 1) -> Tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, end)
    horizon = days[-1] + pd.Timedelta(days=14)
    expiries = [d.date() for d in pd.date_range(days[0], horizon) if d.weekday() == expiry_weekday]
    sig_min = annual_vol / np.sqrt(252 * N_BARS)
    minutes = np.arange(N_BARS)
    hour_frac = (SESSION_OPEN_MIN + minutes) / 60.0

    spot_rows, opt_frames = [], []
    spot = start_spot
    for day in days:
        spot *= np.exp(rng.normal(0, 0.004))                         # overnight gap
        r = rng.normal(0, sig_min, N_BARS)
        close = spot * np.exp(np.cumsum(r))
        open_ = np.concatenate(([spot], close[:-1]))
        wig = np.abs(rng.normal(0, sig_min * 0.6, N_BARS))
        high = np.maximum(open_, close) * (1 + wig)
        low = np.minimum(open_, close) * (1 - wig)
        spot = float(close[-1])
        ts = pd.Timestamp(day) + pd.to_timedelta(SESSION_OPEN_MIN + minutes, unit="m")
        spot_rows.append(pd.DataFrame({"timestamp": ts, "open": open_, "high": high,
                                       "low": low, "close": close}))

        atm = int(round(open_[0] / strike_step) * strike_step)
        strikes = [atm + k * strike_step for k in range(-strikes_each_side, strikes_each_side + 1)]
        for exp in [e for e in expiries if e >= day.date()][:2]:
            # trading-time decay: full sessions after today + the unexpired fraction of today
            after = int(np.busday_count(day.date() + timedelta(days=1), exp + timedelta(days=1)))
            T = (after + (N_BARS - minutes) / N_BARS) / 252.0          # at the bar's open
            Tc = (after + (N_BARS - minutes - 1) / N_BARS) / 252.0     # at the bar's close
            for K in strikes:
                for ot in ("CE", "PE"):
                    iv = annual_vol          # flat vol: a spot-dependent smile would add a fake convexity edge
                    is_call = ot == "CE"
                    po, pc = _bs(open_, K, T, iv, is_call), _bs(close, K, Tc, iv, is_call)
                    p_hi_spot, p_lo_spot = _bs(high, K, Tc, iv, is_call), _bs(low, K, Tc, iv, is_call)
                    hi = np.maximum.reduce([po, pc, p_hi_spot, p_lo_spot])
                    lo = np.minimum.reduce([po, pc, p_hi_spot, p_lo_spot])
                    tick = lambda x: np.maximum(np.round(x / 0.05) * 0.05, 0.05)
                    opt_frames.append(pd.DataFrame({
                        "timestamp": ts, "expiry": pd.Timestamp(exp), "strike": K, "option_type": ot,
                        "open": tick(po), "high": tick(hi), "low": tick(lo), "close": tick(pc),
                        "volume": rng.integers(50, 5000, N_BARS) * 65,
                    }))
    return pd.concat(spot_rows, ignore_index=True), pd.concat(opt_frames, ignore_index=True)
