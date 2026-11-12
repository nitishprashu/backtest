"""Data layer for NIFTY Options Lab V4: your V3 DuckDB layout -> the optbt engine.

Layout (unchanged from V3):
    <DATA_DIR>/nifty_1min_spot.duckdb          table market_data (stock_code='NIFTY') or NIFTY_NSE_SPOT
    <DATA_DIR>/nifty_options_<year>.duckdb     one table per contract:  NIFTY_<strike>_<CE|PE>_<ddmmyyyy>
                                               (columns datetime, open, high, low, close, volume, open_interest)
    <year> in the file name is the contract's EXPIRY year.

Backtests only READ. Repairs/downloads (Breeze) are explicit actions from the Data tab.
No streamlit import here, so this module is testable without the UI.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time as time_module
import uuid
from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from optbt.data import DayData, build_day_data, spot_to_array
from optbt.expiry import choose_expiry
from optbt.schemas import ExpirySelect

BREEZE_MIN_DATE = date(2016, 9, 1)
SPOT_DB_NAME = "nifty_1min_spot.duckdb"
OPTION_DB_TEMPLATE = "nifty_options_{year}.duckdb"
LEGACY_SPOT_TABLE = "NIFTY_NSE_SPOT"
OPTION_TABLE_RE = re.compile(r"^NIFTY_(\d+(?:\.\d+)?)_(CE|PE)_(\d{8})$", re.I)
CATALOGUE_COLUMNS = ["table", "strike", "type", "expiry", "year", "stock_code"]
# Day arrays are ~15 KB per contract; a wide Greek/premium strike window plus carry expiries can make one
# session several MB, so a pure "300 days" cap could reach multiple GB. Evict by estimated size too.
DEFAULT_CACHE_MB = float(os.getenv("NIFTY_LAB_CACHE_MB", "1200"))
API_MIN_INTERVAL = 0.35
MAX_API_RETRIES = 3
OPTION_QUERY_BATCH_SIZE = 32


def quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _duck():
    import duckdb  # imported lazily so the engine/tests run without DuckDB installed
    return duckdb


# =============================================================================
# DuckDB access (SQL ported 1:1 from V3)
# =============================================================================
class V3Access:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.spot_db = self.data_dir / SPOT_DB_NAME

    def option_db(self, year: int) -> Path:
        return self.data_dir / OPTION_DB_TEMPLATE.format(year=year)

    def option_years(self) -> List[int]:
        years = []
        if self.data_dir.exists():
            for p in self.data_dir.glob("nifty_options_*.duckdb"):
                m = re.fullmatch(r"nifty_options_(\d{4})", p.stem, re.I)
                if m:
                    years.append(int(m.group(1)))
        return sorted(set(years))

    @staticmethod
    def _tables(db) -> set:
        return {r[0] for r in db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}

    def spot_coverage(self) -> Tuple[Optional[date], Optional[date], int]:
        if not self.spot_db.exists():
            return None, None, 0
        with _duck().connect(str(self.spot_db), read_only=True) as db:
            names = self._tables(db)
            if "market_data" in names:
                row = db.execute("""SELECT MIN(CAST(datetime AS DATE)), MAX(CAST(datetime AS DATE)), COUNT(*)
                                    FROM market_data WHERE upper(stock_code)='NIFTY'""").fetchone()
            elif LEGACY_SPOT_TABLE in names:
                row = db.execute(f"SELECT MIN(CAST(datetime AS DATE)), MAX(CAST(datetime AS DATE)), COUNT(*) "
                                 f"FROM {quote_identifier(LEGACY_SPOT_TABLE)}").fetchone()
            else:
                raise ValueError(f"No supported NIFTY spot table found in {self.spot_db}")
        return row[0], row[1], int(row[2] or 0)

    def spot_frame(self, start: date, end: date) -> pd.DataFrame:
        if not self.spot_db.exists():
            raise FileNotFoundError(f"Spot database not found: {self.spot_db}")
        with _duck().connect(str(self.spot_db), read_only=True) as db:
            names = self._tables(db)
            params = [datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min)]
            if "market_data" in names:
                q = """SELECT CAST(datetime AS TIMESTAMP) datetime, CAST(open AS DOUBLE) open,
                              CAST(high AS DOUBLE) high, CAST(low AS DOUBLE) low, CAST("close" AS DOUBLE) AS "close",
                              TRY_CAST(volume AS DOUBLE) volume
                       FROM market_data
                       WHERE upper(stock_code)='NIFTY' AND CAST(datetime AS TIMESTAMP)>=? AND CAST(datetime AS TIMESTAMP)<?
                       ORDER BY datetime"""
            elif LEGACY_SPOT_TABLE in names:
                q = f"""SELECT CAST(datetime AS TIMESTAMP) datetime, CAST(open AS DOUBLE) open,
                               CAST(high AS DOUBLE) high, CAST(low AS DOUBLE) low, CAST("close" AS DOUBLE) AS "close",
                               TRY_CAST(volume AS DOUBLE) volume
                        FROM {quote_identifier(LEGACY_SPOT_TABLE)}
                        WHERE CAST(datetime AS TIMESTAMP)>=? AND CAST(datetime AS TIMESTAMP)<?
                        ORDER BY datetime"""
            else:
                raise ValueError(f"No supported NIFTY spot table found in {self.spot_db}")
            df = db.execute(q, params).df()
        if not df.empty:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            for c in ("open", "high", "low", "close"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).drop_duplicates("datetime")
            df = df.sort_values("datetime")
        return df.reset_index(drop=True)

    def catalogue_year(self, year: int) -> pd.DataFrame:
        path = self.option_db(year)
        if not path.exists():
            return pd.DataFrame(columns=CATALOGUE_COLUMNS)
        rows: list = []
        with _duck().connect(str(path), read_only=True) as db:
            names = self._tables(db)
            if "market_data" in names:
                rows = db.execute("""
                    SELECT 'market_data', CAST(strike_price AS DOUBLE),
                           CASE WHEN lower("right") IN ('call','ce') THEN 'CE'
                                WHEN lower("right") IN ('put','pe') THEN 'PE' END,
                           CAST(expiry_date AS DATE), ?, stock_code
                    FROM market_data
                    WHERE expiry_date IS NOT NULL AND strike_price IS NOT NULL
                      AND lower("right") IN ('call','ce','put','pe') AND year(expiry_date)=?
                    GROUP BY 1,2,3,4,6 ORDER BY 4,2,3""", [year, year]).fetchall()
            else:
                for table in names:
                    m = OPTION_TABLE_RE.fullmatch(table)
                    if not m:
                        continue
                    try:
                        exp = datetime.strptime(m.group(3), "%d%m%Y").date()
                    except ValueError:
                        continue
                    if exp.year == year:
                        rows.append((table, float(m.group(1)), m.group(2).upper(), exp, year, "NIFTY"))
        return pd.DataFrame(rows, columns=CATALOGUE_COLUMNS)

    def catalogue(self, years: Iterable[int]) -> pd.DataFrame:
        frames = [self.catalogue_year(y) for y in years if self.option_db(y).exists()]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame(columns=CATALOGUE_COLUMNS)
        out = pd.concat(frames, ignore_index=True)
        return out.drop_duplicates(subset=["strike", "type", "expiry", "year", "stock_code"]).reset_index(drop=True)

    def chart_contract_frame(self, expiry: date, strike: int, option_type: str,
                             start: date, end: date) -> pd.DataFrame:
        """Read one contract's full OHLCV/OI chart range without loading whole sessions."""
        columns = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
        rows = self.catalogue([expiry.year])
        rows = rows[(rows["expiry"] == expiry)
                    & np.isclose(rows["strike"].astype(float), float(strike))
                    & (rows["type"].str.upper() == option_type.upper())]
        frames = []
        lower = datetime.combine(start, time.min)
        upper = datetime.combine(end + timedelta(days=1), time.min)
        for year, contracts in rows.groupby("year"):
            path = self.option_db(int(year))
            with _duck().connect(str(path), read_only=True) as db:
                generic = contracts[contracts["table"] == "market_data"]
                if not generic.empty:
                    stock = str(generic.iloc[0]["stock_code"])
                    rights = ("call", "ce") if option_type.upper() == "CE" else ("put", "pe")
                    frame = db.execute("""
                        SELECT CAST(datetime AS TIMESTAMP) AS chart_timestamp, CAST(open AS DOUBLE) open,
                               CAST(high AS DOUBLE) high, CAST(low AS DOUBLE) low,
                               CAST("close" AS DOUBLE) AS "close", TRY_CAST(volume AS DOUBLE) volume,
                               TRY_CAST(open_interest AS DOUBLE) open_interest
                        FROM market_data
                        WHERE upper(stock_code)=upper(?) AND CAST(expiry_date AS DATE)=?
                          AND CAST(strike_price AS DOUBLE)=? AND lower("right") IN (?,?)
                          AND CAST(datetime AS TIMESTAMP)>=? AND CAST(datetime AS TIMESTAMP)<?
                        ORDER BY datetime
                    """, [stock, expiry, float(strike), *rights, lower, upper]).df()
                    if not frame.empty:
                        frames.append(frame)
                tables = contracts[contracts["table"] != "market_data"]
                if not tables.empty:
                    queries, params = [], []
                    for table in tables["table"]:
                        queries.append(f"""
                            SELECT CAST(datetime AS TIMESTAMP) AS chart_timestamp, CAST(open AS DOUBLE) open,
                                   CAST(high AS DOUBLE) high, CAST(low AS DOUBLE) low,
                                   CAST("close" AS DOUBLE) AS "close", TRY_CAST(volume AS DOUBLE) volume,
                                   TRY_CAST(open_interest AS DOUBLE) open_interest
                            FROM {quote_identifier(table)}
                            WHERE CAST(datetime AS TIMESTAMP)>=? AND CAST(datetime AS TIMESTAMP)<?
                        """)
                        params.extend([lower, upper])
                    frame = db.execute(" UNION ALL ".join(queries) + " ORDER BY chart_timestamp", params).df()
                    if not frame.empty:
                        frames.append(frame)
        if not frames:
            return pd.DataFrame(columns=columns)
        out = pd.concat(frames, ignore_index=True)
        out = out.rename(columns={"chart_timestamp": "timestamp"})
        out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
        for column in columns[1:]:
            out[column] = pd.to_numeric(out[column], errors="coerce")
        return (out.dropna(subset=["timestamp", "open", "high", "low", "close"])
                .drop_duplicates("timestamp", keep="last").sort_values("timestamp")
                .reset_index(drop=True))

    def quotes_many(self, year: int, tables: List[str], day: date) -> Dict[str, pd.DataFrame]:
        """Read requested contract tables in bounded batches for one day."""
        out: Dict[str, pd.DataFrame] = {}
        tables = list(dict.fromkeys(tables))
        if "market_data" in tables:
            raise ValueError("Generic market_data option storage is not supported by the V4 reader")
        if not tables:
            return out
        path = self.option_db(year)
        if not path.exists():
            raise FileNotFoundError(str(path))
        start = datetime.combine(day, time.min)
        end = datetime.combine(day + timedelta(days=1), time.min)
        with _duck().connect(str(path), read_only=True) as db:
            for offset in range(0, len(tables), OPTION_QUERY_BATCH_SIZE):
                batch = tables[offset:offset + OPTION_QUERY_BATCH_SIZE]
                queries = []
                params: List[Any] = []
                for table in batch:
                    queries.append(f"""
                        SELECT ? AS contract_table, CAST(datetime AS TIMESTAMP) datetime,
                               CAST(open AS DOUBLE) open, CAST(high AS DOUBLE) high,
                               CAST(low AS DOUBLE) low, CAST("close" AS DOUBLE) AS "close",
                               TRY_CAST(volume AS DOUBLE) volume,
                               TRY_CAST(open_interest AS DOUBLE) open_interest
                        FROM {quote_identifier(table)}
                        WHERE CAST(datetime AS TIMESTAMP)>=? AND CAST(datetime AS TIMESTAMP)<?
                    """)
                    params.extend([table, start, end])
                df = db.execute(" UNION ALL ".join(queries), params).df()
                if df.empty:
                    continue

                df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
                for column in ("open", "high", "low", "close", "volume", "open_interest"):
                    df[column] = pd.to_numeric(df[column], errors="coerce")
                df = df.dropna(subset=["datetime", "open", "high", "low", "close"])
                for table, quotes in df.groupby("contract_table", sort=False):
                    out[table] = (quotes.drop(columns="contract_table")
                                  .drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True))
                del df
        for table in tables:
            out.setdefault(table, pd.DataFrame(columns=[
                "datetime", "open", "high", "low", "close", "volume", "open_interest"]))
        return out

    def quotes_generic(self, year: int, contracts: List[Tuple[str, date, float, str]], day: date
                       ) -> Dict[Tuple[str, date, float, str], pd.DataFrame]:
        """Read selected contracts from a generic market_data table in one query."""
        out: Dict[Tuple[str, date, float, str], pd.DataFrame] = {}
        if not contracts:
            return out
        path = self.option_db(year)
        if not path.exists():
            raise FileNotFoundError(str(path))
        contract_filters = []
        contract_params: List[Any] = []
        for stock_code, expiry, strike, option_type in contracts:
            rights = ("call", "ce") if option_type.upper() == "CE" else ("put", "pe")
            contract_filters.append(
                '(upper(stock_code)=upper(?) AND CAST(expiry_date AS DATE)=? '
                'AND CAST(strike_price AS DOUBLE)=? AND lower("right") IN (?,?))')
            contract_params.extend([stock_code, expiry, float(strike), *rights])
        params = contract_params + [
            datetime.combine(day, time.min), datetime.combine(day + timedelta(days=1), time.min)]
        with _duck().connect(str(path), read_only=True) as db:
            df = db.execute(f"""
                SELECT CAST(datetime AS TIMESTAMP) datetime, CAST(open AS DOUBLE) open,
                       CAST(high AS DOUBLE) high, CAST(low AS DOUBLE) low,
                       CAST("close" AS DOUBLE) AS "close", TRY_CAST(volume AS DOUBLE) volume,
                       TRY_CAST(open_interest AS DOUBLE) open_interest, upper(stock_code) stock_code,
                       CAST(expiry_date AS DATE) expiry, CAST(strike_price AS DOUBLE) strike,
                       CASE WHEN lower("right") IN ('call','ce') THEN 'CE' ELSE 'PE' END option_type
                FROM market_data
                WHERE ({' OR '.join(contract_filters)})
                  AND CAST(datetime AS TIMESTAMP)>=? AND CAST(datetime AS TIMESTAMP)<?
                ORDER BY datetime""", params).df()
        if df.empty:
            return out
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        for column in ("open", "high", "low", "close", "volume", "open_interest", "strike"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df = df.dropna(subset=["datetime", "open", "high", "low", "close"])
        for key, quotes in df.groupby(["stock_code", "expiry", "strike", "option_type"], sort=False):
            stock_code, expiry, strike, option_type = key
            out[(str(stock_code), pd.Timestamp(expiry).date(), float(strike), str(option_type))] = (
                quotes.drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True))
        return out


# =============================================================================
# Store: the object the engine talks to (dates / build_day)
# =============================================================================
class DuckDbStore:
    """Implements the optbt store interface on top of any accessor with the V3Access methods.

    Strike window: only contracts whose strike lies within the day's spot range +- `extra_steps`
    strike steps (plus any fixed strikes) are loaded. The UI sets this from the strategy so that every
    strike a leg can resolve to (including re-executions) is present.
    """

    def __init__(self, access, start: date, end: date, strike_step: int = 50, cache_days: int = 300,
                 warmup_days: int = 10, cache_mb: Optional[float] = None):
        self.access = access
        self.start, self.end = start, end
        self.strike_step = strike_step
        warm_start = start - timedelta(days=warmup_days)
        self._spot = access.spot_frame(warm_start, end)
        if self._spot.empty:
            raise ValueError("No NIFTY spot candles in the selected period")
        self._spot_by_day: Dict[date, pd.DataFrame] = {
            d: g for d, g in self._spot.groupby(self._spot["datetime"].dt.date)}
        self._all_dates = sorted(self._spot_by_day)
        first_option_year = (start - timedelta(days=warmup_days)).year
        years = [y for y in range(first_option_year, end.year + 2) if access.option_db(y).exists()] \
            if hasattr(access, "option_db") else list(range(first_option_year, end.year + 2))
        self.catalogue = access.catalogue(years)
        if self.catalogue.empty:
            raise ValueError("No option contracts found for the selected period")
        self._expiries = sorted(set(self.catalogue["expiry"]))
        self._extra_steps = 6
        self._fixed: Tuple[float, ...] = ()
        self._carry_forward = False
        self._cache: "OrderedDict[tuple, DayData]" = OrderedDict()
        self._cache_days = cache_days
        self._cache_bytes_cap = int((DEFAULT_CACHE_MB if cache_mb is None else cache_mb) * 1024 * 1024)
        self._cache_bytes = 0
        self._cache_sizes: Dict[tuple, int] = {}
        self.stats = {"option_rows_outside_session": 0, "spot_rows_outside_session": 0,
                      "contracts_loaded": 0, "cache_hits": 0, "cache_misses": 0}

    def session_check(self) -> Dict[str, Any]:
        """First / last minute-of-day seen in spot and bars per day: confirms the 09:15-15:29 convention."""
        t = self._spot[(self._spot["datetime"].dt.date >= self.start) & (self._spot["datetime"].dt.date <= self.end)]
        if t.empty:
            return {}
        hm = t["datetime"].dt.strftime("%H:%M")
        per_day = t.groupby(t["datetime"].dt.date).size()
        return {"first_bar": hm.min(), "last_bar": hm.max(), "median_bars_per_day": int(per_day.median()),
                "days_with_375_bars": int((per_day == 375).sum()), "days": int(per_day.size)}

    # ---- configuration ---------------------------------------------------------------
    def set_window(self, extra_steps: int, fixed_strikes: Iterable[float] = (),
                   carry_forward: bool = False) -> None:
        self._extra_steps = int(extra_steps)
        self._fixed = tuple(sorted(set(float(x) for x in fixed_strikes)))
        self._carry_forward = bool(carry_forward)

    @property
    def dates(self) -> List[date]:
        return [d for d in self._all_dates if self.start <= d <= self.end]

    @property
    def history_dates(self) -> List[date]:
        return [d for d in self._all_dates if d <= self.end]

    @property
    def expiry_dates(self) -> List[date]:
        return list(self._expiries)

    def build_spot_day(self, d: date) -> DayData:
        sdf = self._spot_by_day[d].rename(columns={"datetime": "timestamp"})
        spot = spot_to_array(sdf[["timestamp", "open", "high", "low", "close"]])
        return DayData(d, spot, {}, [])

    # ---- the engine-facing call ------------------------------------------------------
    def build_day(self, d: date, expiry_mode, max_gap_days: Optional[int], need_prev: bool
                  ) -> Tuple[Optional[DayData], Optional[str]]:
        exp, why = choose_expiry(self._expiries, d, expiry_mode, max_gap_days)
        if exp is None:
            return None, why
        key = (d, exp, self._extra_steps, self._fixed, need_prev, self._carry_forward)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            self.stats["cache_hits"] += 1
            return hit, None
        self.stats["cache_misses"] += 1

        sdf = self._spot_by_day[d]
        spot_in = sdf.rename(columns={"datetime": "timestamp"})
        mins = spot_in["timestamp"].dt.hour * 60 + spot_in["timestamp"].dt.minute
        self.stats["spot_rows_outside_session"] += int(((mins < 555) | (mins > 929)).sum())
        lo = float(sdf["low"].min()) - self._extra_steps * self.strike_step
        hi = float(sdf["high"].max()) + self._extra_steps * self.strike_step
        selected_expiries = {exp}
        if self._carry_forward:
            future_expiries = sorted(e for e in self._expiries if e >= d)
            selected_expiries.update(future_expiries[:3])
            monthly, _ = choose_expiry(self._expiries, d, ExpirySelect.MONTHLY, None)
            if monthly is not None:
                selected_expiries.add(monthly)
                later_months = sorted({(e.year, e.month) for e in self._expiries
                                       if (e.year, e.month) > (monthly.year, monthly.month)})
                if later_months:
                    next_month = later_months[0]
                    selected_expiries.add(max(
                        e for e in self._expiries if (e.year, e.month) == next_month))
        cat = self.catalogue[self.catalogue["expiry"].isin(selected_expiries)]
        fixed = set(self._fixed)
        want = cat[((cat["strike"] >= lo) & (cat["strike"] <= hi)) | (cat["strike"].isin(fixed))]
        if want.empty:
            return None, f"expiry {exp}: no contracts within the day's strike window"

        frames = []
        for year, g in want.groupby("year"):
            generic = g[g["table"] == "market_data"]
            generic_quotes = {}
            if not generic.empty:
                if not hasattr(self.access, "quotes_generic"):
                    raise TypeError("Accessor does not support generic market_data option storage")
                contracts = [
                    (str(row.stock_code), pd.Timestamp(row.expiry).date(), float(row.strike), str(row.type))
                    for row in generic.itertuples(index=False)
                ]
                generic_quotes = self.access.quotes_generic(int(year), contracts, d)
            tables = g[g["table"] != "market_data"]
            quotes = self.access.quotes_many(int(year), list(tables["table"]), d) if not tables.empty else {}
            for row in g.itertuples(index=False):
                if row.table == "market_data":
                    qkey = (str(row.stock_code).upper(), pd.Timestamp(row.expiry).date(),
                            float(row.strike), str(row.type).upper())
                    q = generic_quotes.get(qkey)
                else:
                    q = quotes.get(row.table)
                if q is None or q.empty:
                    continue
                m = q["datetime"].dt.hour * 60 + q["datetime"].dt.minute
                self.stats["option_rows_outside_session"] += int(((m < 555) | (m > 929)).sum())
                self.stats["contracts_loaded"] += 1
                frames.append(pd.DataFrame({
                    "timestamp": q["datetime"], "expiry": pd.Timestamp(row.expiry), "strike": int(round(row.strike)),
                    "option_type": row.type, "open": q["open"], "high": q["high"], "low": q["low"],
                    "close": q["close"], "volume": q["volume"]}))
        if not frames:
            return None, f"expiry {exp}: no option candles on {d}"
        day = build_day_data(d, spot_in[["timestamp", "open", "high", "low", "close"]],
                             pd.concat(frames, ignore_index=True))
        day.selected_expiry = exp
        if need_prev:
            i = self._all_dates.index(d)
            if i > 0:
                day.prev_spot = spot_to_array(
                    self._spot_by_day[self._all_dates[i - 1]].rename(columns={"datetime": "timestamp"})
                    [["timestamp", "open", "high", "low", "close"]])
        size = int(day.spot.nbytes + sum(a.nbytes for a in day.options.values()))
        self._cache[key] = day
        self._cache_sizes[key] = size
        self._cache_bytes += size
        while len(self._cache) > 1 and (len(self._cache) > self._cache_days
                                        or self._cache_bytes > self._cache_bytes_cap):
            old_key, _ = self._cache.popitem(last=False)
            self._cache_bytes -= self._cache_sizes.pop(old_key, 0)
        return day, None

    def load_contract_day(self, d: date, contract: tuple) -> Optional[np.ndarray]:
        """Load one exact contract/session even when its strike is outside the spot window."""
        expiry, strike, option_type = contract
        contract_rows = self.catalogue[
            (self.catalogue["expiry"] == expiry)
            & np.isclose(self.catalogue["strike"].astype(float), float(strike))
            & (self.catalogue["type"].str.upper() == str(option_type).upper())
        ]
        if contract_rows.empty or d not in self._spot_by_day:
            return None

        frames = []
        for year, rows in contract_rows.groupby("year"):
            generic = rows[rows["table"] == "market_data"]
            generic_quotes = {}
            if not generic.empty:
                if not hasattr(self.access, "quotes_generic"):
                    raise TypeError("Accessor does not support generic market_data option storage")
                contracts = [
                    (str(row.stock_code), pd.Timestamp(row.expiry).date(), float(row.strike), str(row.type))
                    for row in generic.itertuples(index=False)
                ]
                generic_quotes = self.access.quotes_generic(int(year), contracts, d)
            tables = rows[rows["table"] != "market_data"]
            quotes = self.access.quotes_many(int(year), list(tables["table"]), d) if not tables.empty else {}
            for row in rows.itertuples(index=False):
                if row.table == "market_data":
                    qkey = (str(row.stock_code).upper(), pd.Timestamp(row.expiry).date(),
                            float(row.strike), str(row.type).upper())
                    q = generic_quotes.get(qkey)
                else:
                    q = quotes.get(row.table)
                if q is None or q.empty:
                    continue
                frames.append(pd.DataFrame({
                    "timestamp": q["datetime"], "expiry": pd.Timestamp(row.expiry),
                    "strike": int(round(row.strike)), "option_type": str(row.type).upper(),
                    "open": q["open"], "high": q["high"], "low": q["low"],
                    "close": q["close"], "volume": q["volume"],
                }))
        if not frames:
            return None
        spot = self._spot_by_day[d].rename(columns={"datetime": "timestamp"})
        day = build_day_data(d, spot[["timestamp", "open", "high", "low", "close"]],
                             pd.concat(frames, ignore_index=True))
        loaded_key = (expiry, int(round(strike)), str(option_type).upper())
        result = day.options.get(loaded_key)
        if result is not None:
            self.stats["contracts_loaded"] += 1
        return result


# =============================================================================
# Breeze repair (explicit action; V3 logic, minus Streamlit caches)
# =============================================================================
_rate_lock = threading.Lock()
_last_api_start = 0.0
_breeze_cache: Dict[str, Any] = {}


def read_credentials(path: Path) -> Dict[str, str]:
    if not Path(path).exists():
        raise FileNotFoundError(f"Breeze credential file not found: {path}")
    aliases = {"api_key": "api_key", "apikey": "api_key", "app_key": "api_key",
               "secret_key": "secret_key", "api_secret": "secret_key", "secret": "secret_key",
               "session_token": "session_token", "sessionkey": "session_token", "session": "session_token"}
    result: Dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        key = re.sub(r"[^a-z0-9_]", "", key.lower().strip())
        value = value.strip().strip("\"'")
        if key in aliases and value:
            result[aliases[key]] = value
    missing = [x for x in ("api_key", "secret_key", "session_token") if not result.get(x)]
    if missing:
        raise ValueError("Missing in credential.txt: " + ", ".join(missing))
    return result


def breeze_session(credentials_path: str, nonce: int = 0):
    key = f"{credentials_path}|{nonce}"
    if key in _breeze_cache:
        return _breeze_cache[key]
    try:
        from breeze_connect import BreezeConnect
    except ImportError:
        raise ImportError("breeze-connect is not installed. Run: pip install breeze-connect") from None
    c = read_credentials(Path(credentials_path))
    breeze = BreezeConnect(api_key=c["api_key"])
    breeze.generate_session(api_secret=c["secret_key"], session_token=c["session_token"])
    _breeze_cache[key] = breeze
    return breeze


def _pace_api() -> None:
    global _last_api_start
    with _rate_lock:
        wait = API_MIN_INTERVAL - (time_module.monotonic() - _last_api_start)
        if wait > 0:
            time_module.sleep(wait)
        _last_api_start = time_module.monotonic()


def _breeze_rows(response: Any) -> list:
    if not isinstance(response, dict):
        return []
    s = response.get("Success")
    return s if isinstance(s, list) else ([s] if isinstance(s, dict) else [])


def _breeze_error(response: Any) -> str:
    if not isinstance(response, dict):
        return str(response)
    if response.get("Error") or response.get("Status") not in (None, 200, "200"):
        return f"Status={response.get('Status')}; Error={response.get('Error')}"
    return ""


def breeze_option_request(breeze, expiry_date: date, option_type: str, strike: float,
                          start: datetime, end: datetime, interval: str = "1minute") -> pd.DataFrame:
    if start.date() < BREEZE_MIN_DATE:
        raise ValueError(f"Breeze historical API starts at {BREEZE_MIN_DATE:%d-%b-%Y}")
    params = {
        "interval": interval,
        "from_date": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "to_date": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "stock_code": "NIFTY", "exchange_code": "NFO", "product_type": "options",
        "expiry_date": datetime.combine(expiry_date, time.min).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "right": "call" if option_type.upper() == "CE" else "put", "strike_price": str(strike),
    }
    last_error = ""
    for attempt in range(MAX_API_RETRIES):
        try:
            _pace_api()
            response = breeze.get_historical_data_v2(**params)
            error, rows = _breeze_error(response), _breeze_rows(response)
            if error and not rows:
                last_error = error
                time_module.sleep(0.75 * (attempt + 1))
                continue
            if not rows:
                return pd.DataFrame()
            frame = pd.DataFrame(rows).rename(columns={"openInterest": "open_interest"})
            for c in ("open", "high", "low", "close", "volume", "open_interest"):
                if c not in frame:
                    frame[c] = np.nan
                frame[c] = pd.to_numeric(frame[c], errors="coerce")
            frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
            frame = frame.dropna(subset=["datetime", "close"])
            return (frame[["datetime", "open", "high", "low", "close", "volume", "open_interest"]]
                    .sort_values("datetime").drop_duplicates("datetime"))
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < MAX_API_RETRIES:
                time_module.sleep(0.75 * (attempt + 1))
    raise RuntimeError(last_error or "Breeze request failed")


def make_contract_table(expiry_date: date, option_type: str, strike: float) -> str:
    return f"NIFTY_{strike:g}_{option_type.upper()}_{expiry_date:%d%m%Y}"


def repair_contract(access: V3Access, breeze, expiry_date: date, option_type: str, strike: float,
                    start: date, end: date) -> Dict[str, Any]:
    """Download [start, end] for one contract and upsert it. Returns a small report dict."""
    table = make_contract_table(expiry_date, option_type, strike)
    if end < start:
        return {"ok": False, "table": table, "message": "Invalid date range", "rows": 0}
    frame = breeze_option_request(breeze, expiry_date, option_type, strike,
                                  datetime.combine(start, time.min), datetime.combine(end, time.max))
    if frame.empty:
        return {"ok": True, "table": table, "message": "Breeze returned no rows", "rows": 0}
    access.data_dir.mkdir(parents=True, exist_ok=True)
    with _duck().connect(str(access.option_db(expiry_date.year))) as db:
        qt = quote_identifier(table)
        db.execute(f"""CREATE TABLE IF NOT EXISTS {qt} (datetime TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE,
                       close DOUBLE, volume BIGINT, open_interest BIGINT)""")
        db.execute(f"CREATE INDEX IF NOT EXISTS {quote_identifier('idx_' + re.sub('[^A-Za-z0-9_]', '_', table))} "
                   f"ON {qt}(datetime)")
        tmp = f"_v4_stage_{uuid.uuid4().hex[:12]}"
        db.register(tmp, frame)
        db.execute(f"DELETE FROM {qt} WHERE datetime IN (SELECT datetime FROM {quote_identifier(tmp)})")
        db.execute(f"INSERT INTO {qt} SELECT datetime, open, high, low, close, CAST(volume AS BIGINT), "
                   f"CAST(open_interest AS BIGINT) FROM {quote_identifier(tmp)}")
        db.unregister(tmp)
        db.commit()
    return {"ok": True, "table": table, "message": "Repair completed", "rows": len(frame)}
