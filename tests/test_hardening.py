"""Regression tests for the review fixes: settings validation, SQLite connection hygiene, malformed HTTP
headers, the day-cache memory cap and the CAGR guard. Run: python -m unittest discover -s tests"""
import gc
import os
import socket
import sys
import tempfile
import unittest
import warnings
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from lab import strategy as strat
from lab.runs import RunStore
from lab.server import create_server, serve_in_thread
from lab.service import Service
from lab_data import DuckDbStore
from optbt import ExpirySelect
from optbt.analytics import compute_metrics, build_daily
from test_lab_data import D1, D2, FakeAccess


def _globals(**over):
    g = strat.default_globals(date(2025, 1, 1), date(2025, 2, 1))
    g.update(over)
    return g


class SettingsValidationTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(strat.validate_globals(_globals()), [])

    def test_bad_values_are_rejected(self):
        bad = [dict(strike_interval=0), dict(strike_interval=-50), dict(lot_mode="manual", lot_size=0),
               dict(lot_mode="bogus"), dict(capital=-1.0), dict(brokerage=-5.0), dict(slip_pct=-0.1),
               dict(slip_stop_mult=0.5), dict(slip_thin_mult=0.0), dict(entry_field="mid"),
               dict(expiry="Third weekly"), dict(dte_min=5, dte_max=2), dict(dte_min=-1)]
        for over in bad:
            self.assertTrue(strat.validate_globals(_globals(**over)), over)

    def test_full_validate_includes_global_checks(self):
        errs = strat.validate(strat.default_params(), _globals(strike_interval=0))
        self.assertTrue(any("Strike interval" in e for e in errs))


class RunStoreTests(unittest.TestCase):
    def test_no_leaked_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(os.path.join(tmp, "runs.db"))
            empty = pd.DataFrame({"date": [], "traded": []})
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                rid = store.save("a", "multi", {}, {}, {"total_net_pnl": 1.0}, {}, empty, empty, [], 0.1)
                store.list(); store.rename(rid, "b"); store.get(rid); store.delete(rid)
                gc.collect()


class HttpHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()

        class P:                              # coverage() is all /api/config needs
            def coverage(self):
                return {"first": date(2025, 1, 1), "last": date(2025, 1, 2), "spot_rows": 0, "option_years": []}
        cls.srv = create_server(Service(P(), RunStore(os.path.join(cls.tmp.name, "r.db"))), port=0)
        serve_in_thread(cls.srv)
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def _raw(self, content_length):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall((f"POST /api/runs HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Lab-Client: 1\r\n"
                   f"Content-Length: {content_length}\r\nConnection: close\r\n\r\n").encode())
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        return data.split(b"\r\n", 1)[0]

    def test_malformed_content_length_gets_a_400_not_a_dropped_connection(self):
        self.assertIn(b"400", self._raw("abc"))
        self.assertIn(b"400", self._raw("-5"))

    def test_oversized_body_gets_413(self):
        self.assertIn(b"413", self._raw(10 * 1024 * 1024))


class CacheCapTests(unittest.TestCase):
    def test_cache_evicts_by_size(self):
        store = DuckDbStore(FakeAccess(), D1, D2, cache_mb=0.0001)       # smaller than one session
        store.set_window(extra_steps=2)
        for d in (D1, D2):
            day, why = store.build_day(d, ExpirySelect.CURRENT_WEEKLY, 7, False)
            self.assertIsNone(why)
        self.assertEqual(len(store._cache), 1)                            # always keeps the newest day
        self.assertEqual(store._cache_bytes, sum(store._cache_sizes.values()))

    def test_default_cap_keeps_both_days(self):
        store = DuckDbStore(FakeAccess(), D1, D2)
        store.set_window(extra_steps=2)
        for d in (D1, D2):
            store.build_day(d, ExpirySelect.CURRENT_WEEKLY, 7, False)
        self.assertEqual(len(store._cache), 2)


class CagrGuardTests(unittest.TestCase):
    def _metrics(self, days):
        d = pd.bdate_range("2025-01-01", periods=days)
        trades = pd.DataFrame({
            "date": [x.date() for x in d], "leg_id": "L1", "gross_pnl": 1000.0, "costs": 0.0, "net_pnl": 1000.0,
            "slippage_cost": 0.0, "exit_reason": "EOD_SQUARE_OFF"})
        daily = build_daily(trades, 100000.0)
        return compute_metrics(trades, daily, 100000.0)

    def test_short_window_has_no_annualised_return(self):
        m = self._metrics(5)
        self.assertTrue(np.isnan(m["cagr_pct"]))
        self.assertTrue(np.isnan(m["calmar"]))

    def test_long_window_still_reports_cagr(self):
        self.assertTrue(np.isfinite(self._metrics(120)["cagr_pct"]))


if __name__ == "__main__":
    unittest.main()
