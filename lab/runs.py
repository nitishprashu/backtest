"""Run history in SQLite (standard library). One row per backtest; trades/daily stored compressed."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import threading
import uuid
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .serial import clean

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, created TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
    strategy TEXT NOT NULL, settings TEXT NOT NULL, metrics TEXT NOT NULL, summary TEXT NOT NULL,
    daily BLOB NOT NULL, trades BLOB NOT NULL, skipped TEXT NOT NULL, elapsed REAL NOT NULL
)"""


def _pack(df: pd.DataFrame) -> bytes:
    return zlib.compress(df.to_json(orient="split", date_format="iso", date_unit="s").encode("utf-8"), 6)


def _unpack(blob: bytes) -> pd.DataFrame:
    return pd.read_json(io.StringIO(zlib.decompress(blob).decode("utf-8")), orient="split", convert_dates=False)


def trades_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Restore python dates / timestamps after the JSON round trip."""
    if df.empty:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    for c in ("entry_time", "exit_time"):
        df[c] = pd.to_datetime(df[c])
    return df


def daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["traded"] = df["traded"].astype(bool)
    return df


class RunStore:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as con:
            con.execute(SCHEMA)

    @contextlib.contextmanager
    def _conn(self):
        """Commit on success, roll back on error, and ALWAYS close (sqlite3's own context manager
        only commits, so every call used to leak a connection until garbage collection)."""
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def save(self, name: str, kind: str, strategy: Dict[str, Any], settings: Dict[str, Any],
             metrics: Dict[str, Any], summary: Dict[str, Any], daily: pd.DataFrame, trades: pd.DataFrame,
             skipped: List[Any], elapsed: float) -> str:
        rid = uuid.uuid4().hex[:10]
        with self._lock, self._conn() as con:
            con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                rid, datetime.now().isoformat(timespec="seconds"), name, kind,
                json.dumps(clean(strategy)), json.dumps(clean(settings)), json.dumps(clean(metrics)),
                json.dumps(clean(summary)), _pack(daily), _pack(trades), json.dumps(clean(skipped)), float(elapsed)))
        return rid

    def list(self) -> List[Dict[str, Any]]:
        with self._conn() as con:
            rows = con.execute("SELECT id, created, name, kind, metrics, summary, elapsed FROM runs "
                               "ORDER BY created DESC").fetchall()
        return [{"id": r["id"], "created": r["created"], "name": r["name"], "kind": r["kind"],
                 "metrics": json.loads(r["metrics"]), "summary": json.loads(r["summary"]),
                 "elapsed": r["elapsed"]} for r in rows]

    def get(self, rid: str) -> Optional[Dict[str, Any]]:
        with self._conn() as con:
            r = con.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        if r is None:
            return None
        return {"id": r["id"], "created": r["created"], "name": r["name"], "kind": r["kind"],
                "strategy": json.loads(r["strategy"]), "settings": json.loads(r["settings"]),
                "metrics": json.loads(r["metrics"]), "summary": json.loads(r["summary"]),
                "daily": daily_frame(_unpack(r["daily"])), "trades": trades_frame(_unpack(r["trades"])),
                "skipped": json.loads(r["skipped"]), "elapsed": r["elapsed"]}

    def rename(self, rid: str, name: str) -> bool:
        with self._lock, self._conn() as con:
            return con.execute("UPDATE runs SET name=? WHERE id=?", (name[:120], rid)).rowcount > 0

    def delete(self, rid: str) -> bool:
        with self._lock, self._conn() as con:
            return con.execute("DELETE FROM runs WHERE id=?", (rid,)).rowcount > 0
