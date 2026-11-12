"""Option Greeks from the data itself (no external vol surface).

Model: European options priced with Black-76 on a forward F = S * exp(c * T).
* The carry c is ESTIMATED FROM YOUR DATA each session by put-call parity on the at-the-money strike
  (F = K + (C - P) / DF), so dividends and borrowing costs are already inside it. If parity cannot be
  estimated (missing prices) the code falls back to c = r - q from GreekSettings and says so.
* r (risk-free rate) only enters through the discount factor DF = exp(-r T) - a tiny effect for weeklies.
* Time to expiry uses calendar time to 15:30 IST on the expiry date, in years of 365 days.
* Implied vol is solved per price (Newton with a bisection safeguard). Prices below intrinsic or at/above the
  no-arbitrage ceiling give NaN, never a made-up number.

Units reported:
    iv     percent (14.2)                         delta  per unit of underlying, spot delta (call +, put -)
    gamma  change of delta per 1 NIFTY point      theta  rupees per calendar day per unit (negative for longs)
    vega   rupees per 1 volatility point per unit
Theta is a central-difference time-derivative at constant spot (no closed-form slip-ups); delta/gamma/vega are analytic and
cross-checked by finite differences in the tests.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional, Tuple

import numpy as np

from .data import C, N_BARS, O, V, DayData, OptionKey, SESSION_OPEN_MIN

_ERF = np.frompyfunc(math.erf, 1, 1)
SQRT2 = math.sqrt(2.0)
SQRT2PI = math.sqrt(2.0 * math.pi)
EXPIRY_CLOSE_MIN = 15 * 60 + 30       # expiry settles at 15:30
MIN_PER_YEAR = 365.0 * 24 * 60


@dataclass(frozen=True)
class GreekSettings:
    rate: float = 0.065              # annual risk-free rate (only used for discounting and the fallback carry)
    forward: str = "parity"          # "parity" (carry from your data) or "manual" (carry = rate - div_yield)
    div_yield: float = 0.0           # annual, used only by forward="manual" / the fallback
    parity_minutes: int = 20         # minutes after 09:20 used to estimate the carry

    def __post_init__(self):
        if self.forward not in ("parity", "manual"):
            raise ValueError("forward must be 'parity' or 'manual'")
        if not -0.2 <= self.rate <= 0.5 or not 0 <= self.div_yield <= 0.2:
            raise ValueError("rate / div_yield out of range")


def _ncdf(x):
    return 0.5 * (1.0 + np.asarray(_ERF(np.asarray(x, dtype=float) / SQRT2), dtype=float))


def _npdf(x):
    x = np.asarray(x, dtype=float)
    return np.exp(-0.5 * x * x) / SQRT2PI


def tte_years(expiry: date, trade_date: date, bar, at_close: bool = False):
    """Years from the bar's start (or end) to expiry 15:30. `bar` may be an int or an array of bar indexes."""
    days = (expiry - trade_date).days
    clock = SESSION_OPEN_MIN + np.asarray(bar, dtype=float) + (1.0 if at_close else 0.0)
    return np.maximum((days * 1440.0 + (EXPIRY_CLOSE_MIN - clock)) / MIN_PER_YEAR, 1e-9)


def b76_price(F, K, T, sigma, is_call, df):
    F, K, T, sigma, df = (np.asarray(a, dtype=float) for a in (F, K, T, sigma, df))
    sq = sigma * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sigma * sigma * T) / sq
    d2 = d1 - sq
    call = df * (F * _ncdf(d1) - K * _ncdf(d2))
    put = df * (K * _ncdf(-d2) - F * _ncdf(-d1))
    return np.where(is_call, call, put)


def implied_vol(price, F, K, T, is_call, df, tol: float = 1e-8, maxit: int = 80):
    """Vectorised implied volatility (decimal). NaN where no valid solution exists."""
    price, F, K, T, df = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (price, F, K, T, df)))
    shape0 = price.shape
    price, F, K, T, df = (np.atleast_1d(a) for a in (price, F, K, T, df))
    is_call = np.broadcast_to(np.atleast_1d(np.asarray(is_call, dtype=bool)), price.shape)
    intrinsic = df * np.where(is_call, np.maximum(F - K, 0.0), np.maximum(K - F, 0.0))
    ceiling = df * np.where(is_call, F, K)
    valid = np.isfinite(price) & np.isfinite(F) & (price > intrinsic + 1e-9) & (price < ceiling - 1e-9) & (T > 0)
    out = np.full(price.shape, np.nan)
    if not valid.any():
        return out.reshape(shape0)
    idx = np.nonzero(valid)
    p, f, k, t, d, c = price[idx], F[idx], K[idx], T[idx], df[idx], is_call[idx]
    lo, hi = np.full(p.shape, 1e-4), np.full(p.shape, 8.0)
    sig = np.full(p.shape, 0.25)
    for _ in range(maxit):
        px = b76_price(f, k, t, sig, c, d)
        diff = px - p
        if np.all(np.abs(diff) < tol):
            break
        over = diff > 0
        hi = np.where(over, sig, hi)
        lo = np.where(over, lo, sig)
        sq = sig * np.sqrt(t)
        d1 = (np.log(f / k) + 0.5 * sig * sig * t) / sq
        vega = d * f * _npdf(d1) * np.sqrt(t)
        with np.errstate(divide="ignore", invalid="ignore"):
            newt = sig - diff / vega
        ok = np.isfinite(newt) & (newt > lo) & (newt < hi)
        sig = np.where(ok, newt, 0.5 * (lo + hi))
    px = b76_price(f, k, t, sig, c, d)
    good = np.abs(px - p) < max(tol * 100, 1e-6)
    res = np.where(good, sig, np.nan)
    out[idx] = res
    return out.reshape(shape0)


def greeks_from_iv(S, K, T, sigma, is_call, rate: float, carry: float) -> Dict[str, np.ndarray]:
    """delta, gamma, theta, vega for given inputs (all array-broadcastable). sigma is decimal."""
    S, K, T, sigma = (np.asarray(a, dtype=float) for a in (S, K, T, sigma))
    is_call = np.asarray(is_call, dtype=bool)
    F = S * np.exp(carry * T)
    df = np.exp(-rate * T)
    sq = sigma * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sigma * sigma * T) / sq
    fwd_disc = df * np.exp(carry * T)                  # d(price)/dS multiplier
    call_delta = fwd_disc * _ncdf(d1)
    delta = np.where(is_call, call_delta, call_delta - fwd_disc)
    gamma = fwd_disc * _npdf(d1) / (S * sq)
    vega = df * F * _npdf(d1) * np.sqrt(T) / 100.0     # per 1 vol point
    h = np.minimum(5e-4, T / 2.0)                      # about 4 hours, never past expiry
    Tm, Tp = T - h, T + h
    pm = b76_price(S * np.exp(carry * Tm), K, Tm, sigma, is_call, np.exp(-rate * Tm))
    pp = b76_price(S * np.exp(carry * Tp), K, Tp, sigma, is_call, np.exp(-rate * Tp))
    theta = -(pp - pm) / (2.0 * h) / 365.0             # rupees per calendar day at constant spot (central difference)
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


# --------------------------------------------------------------------------- session-level helpers
def estimate_carry(day: DayData, expiry: date, gs: GreekSettings) -> Tuple[float, str]:
    """Annual carry c with F = S*exp(c*T). Returns (c, source) where source is 'parity' or 'manual'/'fallback'."""
    key = ("carry", expiry, gs)
    if key in day.cache:
        return day.cache[key]
    manual = gs.rate - gs.div_yield
    res: Tuple[float, str] = (manual, "manual" if gs.forward == "manual" else "fallback")
    if gs.forward == "parity":
        j0, j1 = 5, min(N_BARS - 1, 5 + gs.parity_minutes)
        spot = day.spot[C, j0:j1 + 1]
        if np.isfinite(spot).all():
            s_mid = float(np.median(spot))
            strikes = sorted({k[1] for k in day.options if k[0] == expiry and (expiry, k[1], "CE") in day.options
                              and (expiry, k[1], "PE") in day.options})
            if strikes:
                k0 = min(strikes, key=lambda x: abs(x - s_mid))
                ce, pe = day.options[(expiry, k0, "CE")][C, j0:j1 + 1], day.options[(expiry, k0, "PE")][C, j0:j1 + 1]
                bars = np.arange(j0, j1 + 1)
                T = tte_years(expiry, day.trade_date, bars, at_close=True)
                df = np.exp(-gs.rate * T)
                ok = np.isfinite(ce) & np.isfinite(pe) & (ce > 0) & (pe > 0) & np.isfinite(spot)
                if ok.sum() >= 5:
                    F = k0 + (ce[ok] - pe[ok]) / df[ok]
                    c = np.log(F / spot[ok]) / T[ok]
                    res = (float(np.median(c)), "parity")
    day.cache[key] = res
    return res


def _tkind(fidx: int) -> bool:
    return fidx == C            # price field "close" -> time at the end of the bar


def chain_greeks_at(day: DayData, expiry: date, otype: str, bar: int, fidx: int, gs: GreekSettings):
    """Greeks for every strike of `otype` with a usable price at `bar`. Returns dict of arrays plus 'strike'."""
    keys, prices = [], []
    for k, arr in day.options.items():
        if k[0] == expiry and k[2] == otype:
            p = arr[fidx, bar]
            if np.isfinite(p) and p > 0:
                keys.append(k)
                prices.append(float(p))
    if not keys:
        return {"strike": np.array([]), "iv": np.array([]), "delta": np.array([]), "gamma": np.array([]),
                "theta": np.array([]), "vega": np.array([])}
    strikes = np.array([k[1] for k in keys], dtype=float)
    spot = float(day.spot[fidx, bar])
    return _compute(day, expiry, strikes, np.array(prices), otype == "CE", spot, bar, fidx, gs)


def _compute(day, expiry, strikes, prices, is_call, spot, bar, fidx, gs):
    c, _src = estimate_carry(day, expiry, gs)
    T = float(tte_years(expiry, day.trade_date, bar, at_close=_tkind(fidx)))
    F = spot * math.exp(c * T)
    df = math.exp(-gs.rate * T)
    iv = implied_vol(prices, F, strikes, T, is_call, df)
    g = greeks_from_iv(spot, strikes, T, iv, is_call, gs.rate, c)
    return {"strike": strikes, "iv": iv * 100.0, **g}


def greeks_at(day: DayData, key: OptionKey, bar: int, fidx: int, gs: GreekSettings) -> Dict[str, float]:
    """Scalar Greeks of one contract at one bar (price = open or close of that bar)."""
    arr = day.options[key]
    p = arr[fidx, bar]
    spot = day.spot[fidx, bar]
    nan = {"iv": np.nan, "delta": np.nan, "gamma": np.nan, "theta": np.nan, "vega": np.nan}
    if not (np.isfinite(p) and p > 0 and np.isfinite(spot)):
        return nan
    r = _compute(day, key[0], np.array([float(key[1])]), np.array([float(p)]), key[2] == "CE", float(spot), bar, fidx, gs)
    return {k: float(r[k][0]) for k in ("iv", "delta", "gamma", "theta", "vega")}


def greeks_series(day: DayData, key: OptionKey, gs: GreekSettings) -> Dict[str, np.ndarray]:
    """Close-based Greeks for every bar of the session (NaN where the price is unusable)."""
    ck = ("series", key, gs)
    if ck in day.cache:
        return day.cache[ck]
    arr = day.options[key]
    bars = np.arange(N_BARS)
    spot = day.spot[C]
    c, _ = estimate_carry(day, key[0], gs)
    T = tte_years(key[0], day.trade_date, bars, at_close=True)
    F = spot * np.exp(c * T)
    df = np.exp(-gs.rate * T)
    iv = implied_vol(arr[C], F, float(key[1]), T, key[2] == "CE", df)
    g = greeks_from_iv(spot, float(key[1]), T, iv, key[2] == "CE", gs.rate, c)
    out = {"iv": iv * 100.0, **g}
    day.cache[ck] = out
    return out
