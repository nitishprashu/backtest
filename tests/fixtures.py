"""Test-only market: Black-Scholes option paths on a random-walk spot, behind the provider interface.
(The product never simulates data; this exists so the API and UI can be exercised without your database.)"""
import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from optbt import DataStore
from synth import make_synthetic_market


class FrameStore(DataStore):
    def __init__(self, spot, options):
        super().__init__(spot, options)
        o = self.options
        self.catalogue = (o.assign(expiry=o["expiry"].dt.date, table="t")[["expiry", "strike", "option_type", "table"]]
                          .drop_duplicates(["expiry", "strike", "option_type"]).reset_index(drop=True))
        self.stats = {"cache_hits": 0, "cache_misses": 0, "contracts_loaded": 0, "option_rows_outside_session": 0,
                      "spot_rows_outside_session": 0}

    def set_window(self, extra_steps, fixed_strikes=(), carry_forward=False):
        pass

    def session_check(self):
        t = self.spot
        hm = t["timestamp"].dt.strftime("%H:%M")
        per = t.groupby(t["timestamp"].dt.date).size()
        return {"first_bar": hm.min(), "last_bar": hm.max(), "median_bars_per_day": int(per.median()),
                "days_with_375_bars": int((per == 375).sum()), "days": int(per.size)}


class MemoryProvider:
    label = "memory"

    def __init__(self, start="2025-01-01", end="2025-03-14", seed=11, strikes_each_side=12):
        import pickle, tempfile
        cache = os.path.join(tempfile.gettempdir(), f"nlab_market_{start}_{end}_{seed}_{strikes_each_side}.pkl")
        if os.path.exists(cache):
            with open(cache, "rb") as f:
                spot, opts = pickle.load(f)
        else:
            spot, opts = make_synthetic_market(start, end, seed=seed, strikes_each_side=strikes_each_side)
            with open(cache, "wb") as f:
                pickle.dump((spot, opts), f)
        self._store = FrameStore(spot, opts)
        self.repairs = []

    def coverage(self):
        d = self._store.dates
        return {"first": d[0], "last": d[-1], "spot_rows": len(self._store.spot), "option_years": [d[0].year],
                "source": "memory"}

    def store(self, start, end, step, warmup_days=10):
        return self._store

    def repair(self, expiry, typ, strike, d1, d2):
        self.repairs.append((expiry, typ, strike, d1, d2))
        return {"ok": True, "table": "x", "message": "Repair completed", "rows": 1}
