"""Intraday path of a simulated day (for the trade explorer): spot, per-leg and combined MTM."""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

from .data import C, N_BARS, DayData, SESSION_OPEN_MIN, bar_index


def intraday_path(day: DayData, trade_rows: pd.DataFrame) -> pd.DataFrame:
    """Minute-by-minute gross MTM (bar close) for positions active during this session.

    Columns: time, spot, mtm (sum), one column per leg segment ('<leg_id>#<segment>'), and in_trade.
    Cross-session positions are marked from their entry price through the current session; costs are
    excluded (they are in the ledger).
    """
    minutes = pd.to_datetime(
        [pd.Timestamp(day.trade_date) + pd.Timedelta(minutes=SESSION_OPEN_MIN + i) for i in range(N_BARS)])
    out = pd.DataFrame({"time": minutes, "spot": day.spot[C]})
    total = np.zeros(N_BARS)
    first = N_BARS
    last = 0
    for r in trade_rows.itertuples(index=False):
        arr = day.options[(r.expiry, int(r.strike), r.option_type)]
        s = 1 if r.side == "BUY" else -1
        entry_date = r.entry_time.date()
        exit_date = r.exit_time.date()
        if entry_date > day.trade_date or exit_date < day.trade_date:
            continue
        ei = 0 if entry_date < day.trade_date else bar_index(r.entry_time.time())
        exited_today = exit_date == day.trade_date
        xi = bar_index(r.exit_time.time()) if exited_today else N_BARS - 1
        seg = np.zeros(N_BARS)
        if exited_today:
            seg[ei:xi] = s * (arr[C, ei:xi] - r.entry_price) * r.qty
            seg[xi:] = r.gross_pnl
        else:
            seg[ei:] = s * (arr[C, ei:] - r.entry_price) * r.qty
        out[f"{r.leg_id}#{r.segment}"] = seg
        total += seg
        first, last = min(first, ei), max(last, xi)
    out["mtm"] = total
    out["in_trade"] = False
    out.loc[first:last, "in_trade"] = True
    return out
