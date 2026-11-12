"""Shared day-by-day runner (serial or multi-process) used by both engines.

Each worker process receives the store once (initializer), then handles dates one at a time:
build the day's arrays (strict loading), simulate, return rows or an explicit skip reason.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from datetime import date
from typing import Callable, List, Optional, Tuple

_G: dict = {}


def select_dates(store, cfg) -> List[date]:
    expiry_dates = getattr(store, "expiry_dates", None)
    if cfg.expiry_day_only and expiry_dates is None:
        raise ValueError("data store must expose expiry_dates when expiry-day-only filtering is enabled")
    expiry_dates = set(expiry_dates) if expiry_dates is not None else set()
    out = []
    for d in store.dates:
        if cfg.start_date and d < cfg.start_date:
            continue
        if cfg.end_date and d > cfg.end_date:
            continue
        if cfg.weekdays is not None and d.weekday() not in cfg.weekdays:
            continue
        if cfg.expiry_day_only and d not in expiry_dates:
            continue
        out.append(d)
    return out


def _init(store, simulate, cfg, spec, need_prev):
    _G.update(store=store, simulate=simulate, cfg=cfg, spec=spec, need_prev=need_prev)


def _one(d: date) -> Tuple[date, list, str]:
    cfg = _G["cfg"]
    try:
        day, why = _G["store"].build_day(d, cfg.expiry, cfg.max_expiry_gap_days, _G["need_prev"])
        if day is None:
            return d, [], why
        rows, why = _G["simulate"](day, cfg, _G["spec"])
        return d, rows, why
    except Exception as e:      # one bad day is reported, never silently traded or ignored
        return d, [], f"error: {type(e).__name__}: {e}"


def run_days(store, dates: List[date], simulate: Callable, cfg, spec, need_prev: bool,
             workers: int = 1, progress: Optional[Callable[[int, int], None]] = None
             ) -> List[Tuple[date, list, str]]:
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init,
                                 initargs=(store, simulate, cfg, spec, need_prev)) as ex:
            return list(ex.map(_one, dates, chunksize=4))
    _init(store, simulate, cfg, spec, need_prev)
    out = []
    n = len(dates)
    for i, d in enumerate(dates, 1):
        out.append(_one(d))
        if progress is not None and (i % 5 == 0 or i == n):
            progress(i, n)
    return out
