"""Data ingestion and per-day array construction.

Canonical schemas (what the engine consumes):
    spot    : timestamp, open, high, low, close
    options : timestamp, expiry, strike, option_type (CE/PE), open, high, low, close, volume

Loaders also understand the folder layout written by ICICI_DATA_FETCHER.py
(``<out>/NIFTY_spot_1min.csv`` and ``<out>/<expiry>/NIFTY_yy_mm_dd_<strike>_<call|put>.csv``).

The engine works on one trading day at a time as dense NumPy arrays over the
375 one-minute bars of the session (09:15 .. 15:29). Missing bars are
forward-filled from the previous close (volume 0); bars before an
instrument's first print stay NaN and make that instrument unusable at entry.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from datetime import date, time
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np
import pandas as pd

SESSION_OPEN_MIN = 9 * 60 + 15
N_BARS = 375                      # 09:15 .. 15:29 inclusive
O, H, L, C, V = 0, 1, 2, 3, 4     # row indices in the per-instrument arrays

OptionKey = Tuple[date, int, str]  # (expiry, strike, "CE"/"PE")


def bar_index(t: time) -> int:
    return t.hour * 60 + t.minute - SESSION_OPEN_MIN


@dataclass
class DayData:
    trade_date: date
    spot: np.ndarray                      # shape (4, N_BARS): o,h,l,c
    options: Dict[OptionKey, np.ndarray]  # shape (5, N_BARS): o,h,l,c,v
    expiries: List[date]                  # sorted expiries that have data this day
    prev_spot: Optional[np.ndarray] = None  # previous trading day's spot (4, N_BARS), indicator warm-up
    selected_expiry: Optional[date] = None  # set by the store; the engines trade exactly this expiry
    cache: Dict[object, object] = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def _naive_ist(s: pd.Series) -> pd.Series:
    s = pd.to_datetime(s)
    if getattr(s.dt, "tz", None) is not None:
        s = s.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return s


def normalize_spot(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={c: c.strip().lower() for c in df.columns})
    for alt in ("datetime", "date", "time"):
        if "timestamp" not in df.columns and alt in df.columns:
            df = df.rename(columns={alt: "timestamp"})
    need = ["timestamp", "open", "high", "low", "close"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"spot data missing columns {missing}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    out = df[need + ["volume"]].copy()
    out["timestamp"] = _naive_ist(out["timestamp"])
    for c in need[1:] + ["volume"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp", kind="stable")
    return out.drop_duplicates("timestamp", keep="last").reset_index(drop=True)


_OT_MAP = {"CALL": "CE", "CE": "CE", "C": "CE", "PUT": "PE", "PE": "PE", "P": "PE"}


def normalize_options(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={c: c.strip().lower() for c in df.columns})
    renames = {"datetime": "timestamp", "right": "option_type", "strike_price": "strike",
               "expiry_date": "expiry"}
    df = df.rename(columns={k: v for k, v in renames.items() if k in df.columns and v not in df.columns})
    need = ["timestamp", "expiry", "strike", "option_type", "open", "high", "low", "close"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"options data missing columns {missing}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    if "open_interest" not in df.columns:
        df["open_interest"] = np.nan
    out = df[need + ["volume", "open_interest"]].copy()
    out["timestamp"] = _naive_ist(out["timestamp"])
    exp = pd.to_datetime(out["expiry"])
    if getattr(exp.dt, "tz", None) is not None:
        exp = exp.dt.tz_localize(None)
    out["expiry"] = exp.dt.normalize()
    out["strike"] = pd.to_numeric(out["strike"], errors="coerce").astype("Int64")
    out["option_type"] = out["option_type"].astype(str).str.upper().map(_OT_MAP)
    for c in ("open", "high", "low", "close", "volume", "open_interest"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["timestamp", "strike", "option_type"])
    out["strike"] = out["strike"].astype(int)
    return out.sort_values("timestamp", kind="stable").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# loaders
# --------------------------------------------------------------------------- #
def load_spot(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path) if path.lower().endswith(".parquet") else pd.read_csv(path)
    return normalize_spot(df)


def load_options(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path) if path.lower().endswith(".parquet") else pd.read_csv(path)
    return normalize_options(df)


_BREEZE_FILE = re.compile(
    r"^(?P<sym>[A-Za-z]+)_(?P<yy>\d{2})_(?P<mm>\d{2})_(?P<dd>\d{2})_(?P<strike>\d+)_(?P<right>call|put)\.csv$"
)


def load_breeze_options_dir(output_dir: str, symbol: str = "NIFTY") -> pd.DataFrame:
    """Read every ``<output_dir>/<expiry>/<SYMBOL>_yy_mm_dd_<strike>_<call|put>.csv``."""
    frames = []
    keep = {"datetime", "open", "high", "low", "close", "volume"}
    for fp in glob.glob(os.path.join(output_dir, "*", "*.csv")):
        m = _BREEZE_FILE.match(os.path.basename(fp))
        if not m or m["sym"].upper() != symbol.upper():
            continue
        try:
            df = pd.read_csv(fp, usecols=lambda c: c.strip().lower() in keep)
        except (ValueError, pd.errors.EmptyDataError):
            continue
        if df.empty:
            continue
        df["expiry"] = pd.Timestamp(2000 + int(m["yy"]), int(m["mm"]), int(m["dd"]))
        df["strike"] = int(m["strike"])
        df["option_type"] = "CE" if m["right"] == "call" else "PE"
        frames.append(df)
    if not frames:
        raise FileNotFoundError(f"no option CSVs for {symbol} under {output_dir}")
    return normalize_options(pd.concat(frames, ignore_index=True))


# --------------------------------------------------------------------------- #
# DataStore
# --------------------------------------------------------------------------- #
def _bounds(ts: pd.Series) -> Dict[date, Tuple[int, int]]:
    d64 = ts.values.astype("datetime64[D]")
    uniq, first = np.unique(d64, return_index=True)
    hi = np.append(first[1:], len(d64))
    return {u.astype(object): (int(lo), int(h)) for u, lo, h in zip(uniq, first, hi)}


class DataStore:
    """Holds normalised spot + options frames and hands out one day at a time."""

    def __init__(self, spot: pd.DataFrame, options: pd.DataFrame):
        self.spot = normalize_spot(spot)
        self.options = normalize_options(options)
        self._sb = _bounds(self.spot["timestamp"])
        self._ob = _bounds(self.options["timestamp"])

    @classmethod
    def from_breeze_output(cls, output_dir: str, symbol: str = "NIFTY") -> "DataStore":
        spot = load_spot(os.path.join(output_dir, f"{symbol}_spot_1min.csv"))
        return cls(spot, load_breeze_options_dir(output_dir, symbol))

    @property
    def dates(self) -> List[date]:
        return sorted(set(self._sb) & set(self._ob))

    @property
    def expiry_dates(self) -> List[date]:
        """All distinct listed option expiries represented in the loaded option data."""
        return sorted(pd.to_datetime(self.options["expiry"]).dt.date.unique())

    def day_frames(self, d: date) -> Tuple[pd.DataFrame, pd.DataFrame]:
        s0, s1 = self._sb[d]
        o0, o1 = self._ob[d]
        return self.spot.iloc[s0:s1], self.options.iloc[o0:o1]

    def build_spot_day(self, d: date) -> "DayData":
        """Build a spot-only day without requiring option data for the session."""
        s0, s1 = self._sb[d]
        return DayData(d, spot_to_array(self.spot.iloc[s0:s1]), {}, [])

    def build_day(self, d: date, expiry_mode, max_gap_days: Optional[int], need_prev: bool
                  ) -> Tuple[Optional["DayData"], Optional[str]]:
        """(DayData, None) or (None, skip_reason). Same interface as SqliteDataStore."""
        from .expiry import choose_expiry
        ck = (d, expiry_mode, max_gap_days, need_prev)
        cache = self.__dict__.setdefault("_day_cache", {})
        if ck in cache:
            return cache[ck]
        out = self._build_day_uncached(d, expiry_mode, max_gap_days, need_prev)
        if len(cache) < 400:
            cache[ck] = out
        return out

    def _build_day_uncached(self, d, expiry_mode, max_gap_days, need_prev):
        from .expiry import choose_expiry
        s, o = self.day_frames(d)
        day = build_day_data(d, s, o)
        exp, why = choose_expiry(day.expiries, d, expiry_mode, max_gap_days)
        if exp is None:
            return None, why
        day.selected_expiry = exp
        if need_prev:
            ds = self.dates
            i = ds.index(d)
            if i > 0:
                day.prev_spot = spot_to_array(self.day_frames(ds[i - 1])[0])
        return day, None

    def iter_day_frames(self, start: Optional[date] = None, end: Optional[date] = None,
                        weekdays: Optional[Tuple[int, ...]] = None
                        ) -> Iterator[Tuple[date, pd.DataFrame, pd.DataFrame]]:
        for d in self.dates:
            if (start and d < start) or (end and d > end):
                continue
            if weekdays is not None and d.weekday() not in weekdays:
                continue
            s, o = self.day_frames(d)
            yield d, s, o

    def iter_day_frames_with_prev(self, start: Optional[date] = None, end: Optional[date] = None,
                                  weekdays: Optional[Tuple[int, ...]] = None):
        """Like iter_day_frames, plus the previous trading day's spot frame (or None) for warm-up."""
        ds = self.dates
        for i, d in enumerate(ds):
            if (start and d < start) or (end and d > end):
                continue
            if weekdays is not None and d.weekday() not in weekdays:
                continue
            s, o = self.day_frames(d)
            prev = self.day_frames(ds[i - 1])[0] if i > 0 else None
            yield d, s, o, prev


# --------------------------------------------------------------------------- #
# DataFrame -> dense day arrays
# --------------------------------------------------------------------------- #
def _fill(arr: np.ndarray) -> np.ndarray:
    """Forward-fill missing bars from the previous close; leading gaps stay NaN."""
    c = arr[C]
    idx = np.where(~np.isnan(c), np.arange(c.size), 0)
    np.maximum.accumulate(idx, out=idx)
    fc = c[idx]
    for r in (O, H, L):
        m = np.isnan(arr[r])
        arr[r][m] = fc[m]
    arr[C] = fc
    if arr.shape[0] > V:
        arr[V] = np.nan_to_num(arr[V])
    return arr


def _bar_positions(ts: pd.Series) -> np.ndarray:
    return (ts.dt.hour.values * 60 + ts.dt.minute.values - SESSION_OPEN_MIN).astype(int)


def spot_to_array(spot_df: pd.DataFrame) -> np.ndarray:
    pos = _bar_positions(spot_df["timestamp"])
    ok = (pos >= 0) & (pos < N_BARS)
    spot = np.full((4, N_BARS), np.nan)
    spot[:, pos[ok]] = spot_df[["open", "high", "low", "close"]].values[ok].T
    return _fill(np.vstack([spot, np.zeros((1, N_BARS))]))[:4]


def build_day_data(trade_date: date, spot_df: pd.DataFrame, opt_df: pd.DataFrame) -> DayData:
    spot = spot_to_array(spot_df)

    options: Dict[OptionKey, np.ndarray] = {}
    if len(opt_df):
        opt_pos = _bar_positions(opt_df["timestamp"])
        opt_df = opt_df.assign(_pos=opt_pos)
        opt_df = opt_df[(opt_df["_pos"] >= 0) & (opt_df["_pos"] < N_BARS)]
        for (exp, strike, ot), g in opt_df.groupby(["expiry", "strike", "option_type"], sort=False):
            arr = np.full((5, N_BARS), np.nan)
            arr[:, g["_pos"].values] = g[["open", "high", "low", "close", "volume"]].values.T
            options[(pd.Timestamp(exp).date(), int(strike), str(ot))] = _fill(arr)
    expiries = sorted({k[0] for k in options})
    return DayData(trade_date=trade_date, spot=spot, options=options, expiries=expiries)
