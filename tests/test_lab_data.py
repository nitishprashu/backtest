"""DuckDbStore + engine through a fake accessor (no DuckDB needed). Run: python -m unittest discover -s tests"""
import importlib.util
import os
import sys
import unittest
from datetime import date, time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from optbt import (ExpirySelect, LegConfig, OptionType, RiskRule, Side, StrategyConfig, StrikeSelection,
                   run_backtest)
from lab_data import (CATALOGUE_COLUMNS, OPTION_DB_TEMPLATE, SPOT_DB_NAME, DuckDbStore,
                      V3Access)

D1, D2, EXP = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 9)
NEXT_EXP = date(2025, 1, 16)


class _Exists:
    def exists(self):
        return True


class FakeAccess:
    """Contracts 23300..23700 step 50 on 2025-01-09; flat premium; one query counter."""
    def __init__(self):
        self.calls = 0
        self.strikes = list(range(23300, 23750, 50))

    def option_db(self, year):
        return _Exists()

    def spot_frame(self, start, end):
        rows = []
        for d in (D1, D2):
            ts = pd.date_range(f"{d} 09:15", periods=375, freq="min")
            rows.append(pd.DataFrame({"datetime": ts, "open": 23500.0, "high": 23510.0, "low": 23490.0,
                                      "close": 23500.0, "volume": 0.0}))
        return pd.concat(rows, ignore_index=True)

    def catalogue(self, years):
        return pd.DataFrame([(f"T_{k}_{t}", float(k), t, EXP, 2025, "NIFTY")
                             for k in self.strikes for t in ("CE", "PE")], columns=CATALOGUE_COLUMNS)

    def quotes_many(self, year, tables, day):
        self.calls += 1
        out = {}
        ts = pd.date_range(f"{day} 09:15", periods=375, freq="min")
        for t in tables:
            out[t] = pd.DataFrame({"datetime": ts, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                                   "volume": 10.0, "open_interest": 5.0})
        return out


class FakeCarryAccess(FakeAccess):
    def catalogue(self, years):
        return pd.DataFrame(
            [(f"T_{expiry}_{k}_{t}", float(k), t, expiry, 2025, "NIFTY")
             for expiry in (EXP, NEXT_EXP) for k in self.strikes for t in ("CE", "PE")],
            columns=CATALOGUE_COLUMNS,
        )


def straddle(**kw):
    legs = tuple(LegConfig(Side.SELL, ot, StrikeSelection.atm(), stop_loss=RiskRule.percent(25))
                 for ot in (OptionType.CE, OptionType.PE))
    return StrategyConfig(name="t", underlying="NIFTY", entry_time=time(9, 20), exit_time=time(15, 15),
                          legs=legs, apply_costs=False, **kw)


class StoreTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("duckdb"), "duckdb is not installed")
    def test_contract_tables_are_loaded_together_for_a_session(self):
        import tempfile
        import duckdb

        with tempfile.TemporaryDirectory() as directory:
            option_path = os.path.join(directory, OPTION_DB_TEMPLATE.format(year=EXP.year))
            names = ["NIFTY_23500_CE_09012025", "NIFTY_23500_PE_09012025"]
            stamps = [pd.Timestamp(D1) + pd.Timedelta(minutes=555 + i) for i in range(2)]
            with duckdb.connect(option_path) as db:
                for name, price in zip(names, (100.0, 110.0)):
                    db.execute(f"""CREATE TABLE "{name}" (
                        datetime TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE,
                        "close" DOUBLE, volume BIGINT, open_interest BIGINT)""")
                    db.executemany(
                        f"""INSERT INTO "{name}" VALUES (?, ?, ?, ?, ?, 10, 5)""",
                        [(stamp, price, price + 1, price - 1, price) for stamp in stamps])

            access = V3Access(Path(directory))
            with patch("lab_data.OPTION_QUERY_BATCH_SIZE", 1):
                quotes = access.quotes_many(EXP.year, names, D1)
            chart = access.chart_contract_frame(EXP, 23500, "CE", D1, D1)

        self.assertEqual(set(quotes), set(names))
        self.assertEqual(quotes[names[0]]["close"].tolist(), [100.0, 100.0])
        self.assertEqual(quotes[names[1]]["close"].tolist(), [110.0, 110.0])
        self.assertTrue(all(frame["datetime"].is_monotonic_increasing for frame in quotes.values()))
        self.assertEqual(chart["close"].tolist(), [100.0, 100.0])
        self.assertEqual(chart["open_interest"].tolist(), [5.0, 5.0])

    @unittest.skipUnless(importlib.util.find_spec("duckdb"), "duckdb is not installed")
    def test_generic_market_data_options_are_loaded(self):
        import tempfile
        import duckdb

        with tempfile.TemporaryDirectory() as directory:
            data_dir = os.path.abspath(directory)
            spot_path = os.path.join(data_dir, SPOT_DB_NAME)
            option_path = os.path.join(data_dir, OPTION_DB_TEMPLATE.format(year=EXP.year))
            with duckdb.connect(spot_path) as db:
                db.execute("""CREATE TABLE market_data (
                    stock_code VARCHAR, datetime TIMESTAMP, open DOUBLE, high DOUBLE,
                    low DOUBLE, "close" DOUBLE, volume DOUBLE)""")
                db.executemany(
                    "INSERT INTO market_data VALUES ('NIFTY', ?, 23500, 23510, 23490, 23500, 0)",
                    [(pd.Timestamp(D1) + pd.Timedelta(minutes=555 + i),) for i in range(3)])
            with duckdb.connect(option_path) as db:
                db.execute("""CREATE TABLE market_data (
                    stock_code VARCHAR, expiry_date DATE, "right" VARCHAR, strike_price DOUBLE,
                    datetime TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, "close" DOUBLE,
                    volume BIGINT, open_interest BIGINT)""")
                db.executemany(
                    """INSERT INTO market_data VALUES
                       ('NIFTY', ?, ?, 23500, ?, 100, 101, 99, 100, 10, 5)""",
                    [(EXP, right, pd.Timestamp(D1) + pd.Timedelta(minutes=555 + i))
                     for right in ("CE", "put") for i in range(3)])

            store = DuckDbStore(V3Access(Path(data_dir)), D1, D1)
            store.set_window(extra_steps=0)
            day, why = store.build_day(D1, ExpirySelect.CURRENT_WEEKLY, 7, False)

            self.assertIsNone(why)
            self.assertEqual(set(day.options), {(EXP, 23500, "CE"), (EXP, 23500, "PE")})
            self.assertEqual(day.options[(EXP, 23500, "CE")][3, 0], 100)
            chart = store.access.chart_contract_frame(EXP, 23500, "CE", D1, D1)
            self.assertEqual(chart["open_interest"].tolist(), [5.0, 5.0, 5.0])

    def test_end_to_end_two_days_and_cache(self):
        acc = FakeAccess()
        store = DuckDbStore(acc, D1, D2)
        self.assertEqual(store.dates, [D1, D2])
        res = run_backtest(straddle(), store)
        self.assertEqual(res.skipped, [])
        self.assertEqual(len(res.trades), 4)
        self.assertEqual(set(res.trades["qty"]), {75})
        misses = store.stats["cache_misses"]
        run_backtest(straddle(), store)                      # second run: served from the day cache
        self.assertEqual(store.stats["cache_misses"], misses)
        self.assertGreater(store.stats["cache_hits"], 0)

    def test_strike_window_limits_loaded_contracts(self):
        store = DuckDbStore(FakeAccess(), D1, D2)
        store.set_window(extra_steps=2)                       # 23490-100 .. 23510+100
        day, why = store.build_day(D1, ExpirySelect.CURRENT_WEEKLY, 7, False)
        keys = {k[1] for k in day.options}
        self.assertEqual(keys, {23400, 23450, 23500, 23550, 23600})

    def test_fixed_strike_is_always_loaded(self):
        store = DuckDbStore(FakeAccess(), D1, D2)
        store.set_window(extra_steps=1, fixed_strikes=[23700])
        day, _ = store.build_day(D1, ExpirySelect.CURRENT_WEEKLY, 7, False)
        self.assertIn(23700, {k[1] for k in day.options})

    def test_carry_mode_loads_adjacent_active_expiry_contracts(self):
        store = DuckDbStore(FakeCarryAccess(), D1, D2)
        store.set_window(extra_steps=0, carry_forward=True)
        day, why = store.build_day(D1, ExpirySelect.CURRENT_WEEKLY, 7, False)

        self.assertIsNone(why)
        self.assertEqual({k[0] for k in day.options}, {EXP, NEXT_EXP})

    def test_carry_loader_fetches_an_exact_held_contract_outside_the_spot_window(self):
        store = DuckDbStore(FakeCarryAccess(), D1, D2)
        store.set_window(extra_steps=0, carry_forward=True)
        day, why = store.build_day(D1, ExpirySelect.CURRENT_WEEKLY, 7, False)
        held = (EXP, 23700, "CE")

        self.assertIsNone(why)
        self.assertNotIn(held, day.options)
        loaded = store.load_contract_day(D2, held)
        loaded_roll = store.load_contract_day(D2, (NEXT_EXP, 23700, "CE"))
        self.assertIsNotNone(loaded)
        self.assertIsNotNone(loaded_roll)
        self.assertTrue(np.isfinite(loaded[0]).any())

    def test_carry_mode_loads_the_next_monthly_contract_for_rollover(self):
        expiries = [
            date(2025, 1, 9), date(2025, 1, 16), date(2025, 1, 23), date(2025, 1, 30),
            date(2025, 2, 6), date(2025, 2, 13), date(2025, 2, 20), date(2025, 2, 27),
        ]

        class MonthlyCarryAccess(FakeAccess):
            def catalogue(self, years):
                return pd.DataFrame(
                    [(f"T_{expiry}_{k}_{t}", float(k), t, expiry, 2025, "NIFTY")
                     for expiry in expiries for k in self.strikes for t in ("CE", "PE")],
                    columns=CATALOGUE_COLUMNS,
                )

        store = DuckDbStore(MonthlyCarryAccess(), D1, D2)
        store.set_window(extra_steps=0, carry_forward=True)
        day, why = store.build_day(D1, ExpirySelect.MONTHLY, 7, False)

        self.assertIsNone(why)
        self.assertEqual(day.selected_expiry, date(2025, 1, 30))
        self.assertIn(date(2025, 2, 27), {key[0] for key in day.options})

    def test_prev_day_spot_for_warmup(self):
        store = DuckDbStore(FakeAccess(), D2, D2)
        day, _ = store.build_day(D2, ExpirySelect.CURRENT_WEEKLY, 7, True)
        self.assertIsNotNone(day.prev_spot)

    def test_missing_expiry_guard_via_store(self):
        store = DuckDbStore(FakeAccess(), D1, D2)
        day, why = store.build_day(D1, ExpirySelect.NEXT_WEEKLY, 7, False)
        self.assertIsNone(day)
        self.assertIn("second expiry", why)


if __name__ == "__main__":
    unittest.main()


class SessionCheckTest(unittest.TestCase):
    def test_session_check(self):
        c = DuckDbStore(FakeAccess(), D1, D2).session_check()
        self.assertEqual((c["first_bar"], c["last_bar"], c["days_with_375_bars"]), ("09:15", "15:29", 2))
