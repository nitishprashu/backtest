"""Data providers. DuckProvider reads your V3 DuckDB layout (see lab_data.py); tests inject their own."""
from __future__ import annotations

import threading
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

import lab_data


class DuckProvider:
    label = "DuckDB"

    def __init__(self, data_dir: str, credentials_file: str = ""):
        self.access = lab_data.V3Access(Path(data_dir))
        self.credentials_file = credentials_file
        self._lock = threading.Lock()
        self._key = None
        self._store = None

    def coverage(self) -> Dict[str, Any]:
        first, last, rows = self.access.spot_coverage()
        return {"first": first, "last": last, "spot_rows": rows, "option_years": self.access.option_years(),
                "source": str(self.access.data_dir)}

    def store(self, start: date, end: date, step: int, warmup_days: int = 10):
        with self._lock:
            key = (start, end, step, warmup_days)
            if key != self._key:
                self._store = lab_data.DuckDbStore(self.access, start, end, step, warmup_days=warmup_days)
                self._key = key
            return self._store

    def repair(self, expiry: date, typ: str, strike: float, d1: date, d2: date) -> Dict[str, Any]:
        breeze = lab_data.breeze_session(self.credentials_file)
        rep = lab_data.repair_contract(self.access, breeze, expiry, typ, strike, d1, d2)
        with self._lock:
            self._key = None                         # force a fresh catalogue next time
        return rep
