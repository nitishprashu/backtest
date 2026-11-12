"""python -m lab  [--data-dir ...] [--port 8765]"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

from . import APP_VERSION
from .providers import DuckProvider
from .runs import RunStore
from .server import create_server
from .service import Service

DEFAULT_DATA = os.getenv("NIFTY_BREEZE_DATA_DIR", r"D:\Nitish Personal\Python\breeze_data")
DEFAULT_CRED = os.getenv("BREEZE_CREDENTIAL_FILE", r"D:\Nitish Personal\Python\icici\credential.txt")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lab", description="NIFTY Options Lab (local web app)")
    ap.add_argument("--data-dir", default=DEFAULT_DATA, help="folder with nifty_1min_spot.duckdb and nifty_options_<year>.duckdb")
    ap.add_argument("--credentials", default=DEFAULT_CRED, help="Breeze credential.txt (only for repairs)")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent.parent / "lab_runs.db"),
                    help="SQLite file for run history")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    service = Service(DuckProvider(a.data_dir, a.credentials), RunStore(a.db))
    srv = create_server(service, a.port)
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"NIFTY Options Lab v{APP_VERSION}\n  data : {a.data_dir}\n  runs : {a.db}\n  open : {url}\n  (Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
