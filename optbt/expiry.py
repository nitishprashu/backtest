"""Expiry selection with a data-completeness guard.

A silently missing weekly expiry would make "CURRENT_WEEKLY" quietly mean "the one after it".
`max_gap_days` (default 7 in the configs) turns that into an explicit skipped day instead:
consecutive weekly expiries must be no more than that many days apart. Set it to None to
disable the guard (e.g. for an instrument without weekly expiries).
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional, Tuple

from .schemas import ExpirySelect


def choose_expiry(expiries: Iterable[date], trade_date: date, mode: ExpirySelect,
                  max_gap_days: Optional[int]) -> Tuple[Optional[date], Optional[str]]:
    exps = sorted(e for e in set(expiries) if e >= trade_date)
    if not exps:
        return None, "no expiry on/after the trade date"
    g = max_gap_days
    if mode is ExpirySelect.CURRENT_WEEKLY:
        chosen = exps[0]
        if g is not None and (chosen - trade_date).days > g:
            return None, (f"nearest expiry {chosen} is {(chosen - trade_date).days} days ahead "
                          f"(> {g}); an expiry may be missing from the data")
        return chosen, None
    if mode is ExpirySelect.NEXT_WEEKLY:
        if len(exps) < 2:
            return None, "no second expiry on/after the trade date"
        if g is not None and ((exps[0] - trade_date).days > g or (exps[1] - exps[0]).days > g):
            return None, (f"expiries {exps[0]}, {exps[1]} are more than {g} days apart; "
                          "an expiry may be missing from the data")
        return exps[1], None
    # MONTHLY: last available expiry of the month that contains the nearest expiry
    month = (exps[0].year, exps[0].month)
    chosen = max(e for e in exps if (e.year, e.month) == month)
    if g is not None and (chosen - trade_date).days > 5 * g:
        return None, f"monthly expiry {chosen} is more than {5 * g} days ahead"
    return chosen, None
