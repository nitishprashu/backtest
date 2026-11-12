"""JSON helpers: everything the API returns goes through clean() so NaN/inf/numpy/pandas/dates are safe."""
from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Any

import numpy as np
import pandas as pd


def clean(o: Any) -> Any:
    if o is None or isinstance(o, (str, bool)):
        return o
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(o, int):
        return o
    if isinstance(o, pd.Timestamp):
        return None if pd.isna(o) else o.isoformat()
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, time):
        return o.strftime("%H:%M")
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return [clean(v) for v in o.tolist()]
    if isinstance(o, pd.DataFrame):
        return [clean(r) for r in o.to_dict("records")]
    if isinstance(o, pd.Series):
        return [clean(v) for v in o.tolist()]
    if o is pd.NaT:
        return None
    return str(o)
